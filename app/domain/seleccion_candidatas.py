"""Selección de las estaciones que el DP va a considerar.

Aquí está el cuello de botella del proyecto, no en el DP (ARQUITECTURA.md §8
punto 1): el optimizador resuelve 250 candidatas en decenas de milisegundos,
pero cada candidata cuesta peticiones a OSRM, y el servidor público va a ~1 req/s.

El procedimiento es el de §8 punto 2, en dos fases:

1. **Filtro grueso y barato**: se trocea la polilínea en tramos y de cada uno se
   saca un rectángulo estrecho. El R*Tree resuelve eso en nada. Se hace por
   tramos y no con un rectángulo único porque el bbox de Madrid-Barcelona mete
   dentro media península.
2. **Filtro caro y exacto**: el desvío real por carretera, con ``/table``. Eso
   ya no es de este módulo, sino del ``RoutingProvider``.

Entre las dos fases se recorta por precio: de cada tramo solo pasan las más
baratas. Es lo que permite bajar de miles de estaciones a unas decenas sin que
el plan empeore, porque una estación cara rodeada de baratas no entra en el
óptimo por muy bien situada que esté.

El módulo vive en ``domain/`` porque decide *qué* se optimiza, que es negocio,
no fontanería: solo habla con los puertos y no conoce SQLite ni OSRM.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.models import Desvio, Estacion, EstacionCandidata, Precio, Vehiculo
from app.domain.ports.price_store import PriceStore
from app.domain.ports.routing_provider import (
    Coordenada,
    MatrizRuta,
    Ruta,
    bbox_de,
    distancia_haversine_km,
)
from app.domain.precio_efectivo import PerfilDescuento, precio_efectivo


@dataclass(frozen=True)
class ParametrosSeleccion:
    """Ajustes del corredor de búsqueda.

    ``max_candidatas`` es el mando que gobierna el tiempo de respuesta: con el
    OSRM público, ``n`` candidatas cuestan ``ceil((n+2)/50)^2`` peticiones a
    ``/table`` a un segundo cada una. 50 candidatas es una petición y unos
    segundos; 250 son 25 peticiones y casi medio minuto. Con OSRM propio (§9)
    este número puede subir sin miedo.

    ``max_desvio_km`` no filtra aquí —el filtro fino es de ``depurar_matriz``,
    con distancias reales—, pero sí ensancha el corredor: sería absurdo que el
    rectángulo grueso tirase estaciones que el filtro exacto habría aceptado.
    """

    margen_km: float = 5.0
    tramo_km: float = 50.0
    max_candidatas: int = 50
    solo_vigentes: bool = True
    max_desvio_km: float = 6.0

    @property
    def margen_efectivo_km(self) -> float:
        """Ancho real del corredor.

        Un desvío de ``d`` km ida y vuelta pone la estación a ~``d/2`` km de la
        ruta, así que el rectángulo tiene que llegar al menos hasta ahí.
        """
        return max(self.margen_km, self.max_desvio_km / 2.0)


@dataclass(frozen=True)
class EstacionCercana:
    """Una estación con su distancia en línea recta al punto de referencia."""

    estacion: Estacion
    precio: Precio
    distancia_km: float


class ExtremosSinRuta(Exception):
    """OSRM no sabe llegar al origen o al destino: no hay nada que optimizar."""


# ---------------------------------------------------------------------------
# Fase 1: corredor + recorte por precio
# ---------------------------------------------------------------------------


def trocear(geometria: Sequence[Coordenada], tramo_km: float) -> list[list[Coordenada]]:
    """Parte la polilínea en tramos de aproximadamente ``tramo_km``.

    Los tramos comparten el punto de unión, así que los rectángulos se solapan
    un poco y ninguna estación se cuela por la juntura.
    """
    if len(geometria) < 2:
        raise ValueError("La polilínea necesita al menos dos puntos.")

    tramos: list[list[Coordenada]] = []
    actual = [geometria[0]]
    acumulado = 0.0
    for anterior, punto in zip(geometria, geometria[1:], strict=False):
        acumulado += distancia_haversine_km(anterior, punto)
        actual.append(punto)
        if acumulado >= tramo_km:
            tramos.append(actual)
            actual = [punto]
            acumulado = 0.0
    if len(actual) > 1:
        tramos.append(actual)
    return tramos or [list(geometria)]


def seleccionar(
    store: PriceStore,
    ruta: Ruta,
    vehiculo: Vehiculo,
    rotulos: Sequence[str] | None = None,
    parametros: ParametrosSeleccion | None = None,
    usuario: PerfilDescuento | None = None,
    ahora: object = None,
) -> list[EstacionCandidata]:
    """Candidatas del corredor, **ordenadas por avance en la ruta**.

    El orden es el que exige el DP. Aquí es el del tramo al que pertenece cada
    una, que es una aproximación buena; ``depurar_matriz`` lo afina después con
    las distancias reales, cuando ya se conocen.
    """
    parametros = parametros or ParametrosSeleccion()
    tramos = trocear(ruta.geometria, parametros.tramo_km)
    por_tramo = max(1, parametros.max_candidatas // len(tramos))

    # Repartir el cupo por tramos y no coger las N más baratas de toda la ruta
    # es deliberado: las N más baratas pueden estar todas en la misma provincia
    # y dejar 300 km sin una sola parada posible.
    vistas: dict[int, tuple[int, EstacionCandidata]] = {}
    for orden, tramo in enumerate(tramos):
        min_lat, min_lon, max_lat, max_lon = bbox_de(tramo, parametros.margen_efectivo_km)
        encontradas = store.estaciones_en_bbox(
            min_lat,
            min_lon,
            max_lat,
            max_lon,
            vehiculo.tipo_combustible,
            rotulos,
            solo_vigentes=parametros.solo_vigentes,
            ahora=ahora,
        )
        mejores = sorted(
            encontradas,
            key=lambda par: precio_efectivo(par[0], par[1].precio_milesimas, usuario),
        )[:por_tramo]
        for estacion, precio in mejores:
            vistas.setdefault(estacion.id, (orden, EstacionCandidata(estacion, precio)))

    return [candidata for _, candidata in sorted(vistas.values(), key=lambda par: par[0])]


# ---------------------------------------------------------------------------
# Fase 2: limpiar lo que OSRM no supo resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatrizDepurada:
    candidatas: list[EstacionCandidata]
    distancias_km: list[list[float]]
    duraciones_s: list[list[float]]
    desvios: list[Desvio]
    descartadas: list[EstacionCandidata]
    descartadas_por_desvio: list[EstacionCandidata]


def linea_base(matriz: MatrizRuta, n: int, destino: int) -> Desvio:
    """Lo que cuesta el viaje **sin parar**, y contra lo que se mide todo desvío.

    Lo obvio sería la celda ``[0][destino]`` de la matriz, y es lo que se hacía.
    Está mal, y de una forma que engaña hacia el lado peligroso.

    ``/table`` de OSRM devuelve la distancia **del camino más rápido**, no la del
    más corto. El más rápido puede dar un rodeo por autovía, así que la celda
    directa a veces es más larga que un camino que pase por una estación. Medido
    en Madrid-Barcelona: la celda directa daba 642,4 km cuando por una gasolinera
    pegada a la ruta se llegaba en 636,1. Usar 642,4 de referencia restaba 6,2 km
    a **todos** los desvíos, y colaba como "a menos de 10 km" gasolineras que
    estaban a dieciséis.

    Así que la referencia es el camino más corto que la matriz conoce: la celda
    directa o, si alguna estación lo mejora, esa. Nunca infravalora un desvío, que
    es el único error que aquí importa: de los dos posibles, el que manda al
    conductor lejos sin avisar.
    """
    mejor_km = matriz.distancias_km[0][destino]
    mejor_s = matriz.duraciones_s[0][destino]
    for i in range(1, n + 1):
        ida, vuelta = matriz.distancias_km[0][i], matriz.distancias_km[i][destino]
        ida_s, vuelta_s = matriz.duraciones_s[0][i], matriz.duraciones_s[i][destino]
        if math.isfinite(ida) and math.isfinite(vuelta):
            mejor_km = min(mejor_km, ida + vuelta)
        if math.isfinite(ida_s) and math.isfinite(vuelta_s):
            mejor_s = min(mejor_s, ida_s + vuelta_s)
    return Desvio(km=mejor_km, segundos=mejor_s)


def desvio_de(
    matriz: MatrizRuta, indice: int, destino: int, base: Desvio
) -> Desvio | None:
    """Lo que cuesta pasar por ``indice`` en vez de seguir de largo.

    ``base`` es lo que cuesta el viaje sin parar (ver ``linea_base``); no se
    saca de la celda directa por lo que allí se explica.

    ``None`` si OSRM no sabe ir hasta ahí o volver: eso no es un desvío grande,
    es que no hay camino (§8.4).
    """
    tramos = (
        matriz.distancias_km[0][indice],
        matriz.distancias_km[indice][destino],
        matriz.duraciones_s[0][indice],
        matriz.duraciones_s[indice][destino],
    )
    if not all(math.isfinite(valor) for valor in tramos):
        return None
    ida_km, vuelta_km, ida_s, vuelta_s = tramos
    # El máximo con cero es por prudencia numérica: por construcción la base es
    # menor o igual que cualquier suma, pero un empate puede salir en -0.0001 y
    # un desvío negativo no significaría nada.
    return Desvio(
        km=max(0.0, ida_km + vuelta_km - base.km),
        segundos=max(0.0, ida_s + vuelta_s - base.segundos),
    )


def depurar_matriz(
    candidatas: Sequence[EstacionCandidata],
    matriz: MatrizRuta,
    max_desvio_km: float = 6.0,
    max_desvio_min: float = 10.0,
) -> MatrizDepurada:
    """Deja solo las candidatas utilizables, con su desvío, en orden de avance.

    Tres cosas, todas antes de llamar al DP:

    1. **Fuera las que OSRM no resolvió** (§8.4): tienen sus celdas a ``inf`` y
       no pueden entrar en la matriz.
    2. **Fuera las que no son viables**: el desvío real por carretera no cabe en
       el límite. Es aquí donde vive la restricción que sustituye al precio del
       tiempo (§8.2); el DP ya solo ve estaciones a las que merece la pena ir, y
       dentro de ellas puede mandar el dinero sin producir disparates.
    3. **Reordena por kilometraje real** desde el origen. El DP da por hecho que
       el orden de las candidatas es el de avance por la ruta; aquí ese orden
       deja de ser la estimación por tramos y pasa a ser la distancia medida.

    ``max_desvio_min`` es una red de seguridad, no el mando principal: cubre la
    gasolinera que está a un kilómetro de la vía pero a diez minutos por dentro
    del pueblo, que los kilómetros por sí solos no distinguen.

    Raises:
        ExtremosSinRuta: si el que no se resuelve es el origen o el destino.
    """
    n = len(candidatas)
    destino = n + 1
    sin_respuesta = set(matriz.indices_sin_respuesta)
    if 0 in sin_respuesta or destino in sin_respuesta:
        cual = "el origen" if 0 in sin_respuesta else "el destino"
        raise ExtremosSinRuta(
            f"OSRM no sabe llegar a {cual} del trayecto. Comprueba que las coordenadas "
            "caen en una carretera y no en mitad del campo o del mar."
        )

    max_desvio_s = max_desvio_min * 60.0
    base = linea_base(matriz, n, destino)
    validas: list[int] = []
    desvios: dict[int, Desvio] = {}
    fuera_de_limite: list[int] = []

    for i in range(1, n + 1):
        if i in sin_respuesta:
            continue
        desvio = desvio_de(matriz, i, destino, base)
        if desvio is None:
            sin_respuesta.add(i)
            continue
        if desvio.km > max_desvio_km or desvio.segundos > max_desvio_s:
            fuera_de_limite.append(i)
            continue
        validas.append(i)
        desvios[i] = desvio

    validas.sort(key=lambda i: matriz.distancias_km[0][i])
    orden = [0, *validas, destino]

    return MatrizDepurada(
        candidatas=[candidatas[i - 1] for i in validas],
        distancias_km=[[matriz.distancias_km[a][b] for b in orden] for a in orden],
        duraciones_s=[[matriz.duraciones_s[a][b] for b in orden] for a in orden],
        desvios=[desvios[i] for i in validas],
        descartadas=[candidatas[i - 1] for i in sorted(sin_respuesta) if 1 <= i <= n],
        descartadas_por_desvio=[candidatas[i - 1] for i in fuera_de_limite],
    )


# ---------------------------------------------------------------------------
# Caso "voy por debajo de la reserva" (§7)
# ---------------------------------------------------------------------------

# Radios de búsqueda en grados, de menos a más. Se para en el primero que
# devuelve algo: lo normal es acertar con el más pequeño y no barrer de más.
_RADIOS_GRADOS = (0.05, 0.15, 0.4, 1.0)


def estaciones_cercanas(
    store: PriceStore,
    punto: Coordenada,
    producto: str,
    limite: int = 5,
    solo_vigentes: bool = True,
    ahora: object = None,
) -> list[EstacionCercana]:
    """Las estaciones más próximas a ``punto``, por distancia en línea recta.

    Es la respuesta al conductor que ya va por debajo de la reserva: ahí la
    pregunta no es "cuál es la ruta óptima" sino "dónde reposto ya" (§7). El
    punto de referencia es el **origen del trayecto**, que es la única posición
    que la API conoce mientras no haya geolocalización (§10).

    La línea recta basta aquí: se trata de ordenar un puñado de gasolineras que
    están a pocos kilómetros, no de valorar un desvío.
    """
    for radio in _RADIOS_GRADOS:
        encontradas = store.estaciones_en_bbox(
            punto.lat - radio,
            punto.lon - radio,
            punto.lat + radio,
            punto.lon + radio,
            producto,
            None,
            solo_vigentes=solo_vigentes,
            ahora=ahora,
        )
        if encontradas:
            cercanas = [
                EstacionCercana(
                    estacion=estacion,
                    precio=precio,
                    distancia_km=distancia_haversine_km(
                        punto, Coordenada(estacion.lat, estacion.lon)
                    ),
                )
                for estacion, precio in encontradas
            ]
            cercanas.sort(key=lambda c: c.distancia_km)
            return cercanas[:limite]
    return []


def puntos_para_la_matriz(
    origen: Coordenada, candidatas: Sequence[EstacionCandidata], destino: Coordenada
) -> list[Coordenada]:
    """Monta la lista de puntos con el convenio de índices del DP.

    0 = origen, 1..n = candidatas en orden, n+1 = destino.
    """
    return [
        origen,
        *(Coordenada(c.estacion.lat, c.estacion.lon) for c in candidatas),
        destino,
    ]


def peticiones_estimadas(n_puntos: int, max_puntos_por_peticion: int = 100) -> int:
    """Cuántas llamadas a ``/table`` costará una matriz de ``n_puntos``.

    Sirve para avisar antes de tener al usuario esperando medio minuto.
    """
    bloque = max(1, max_puntos_por_peticion // 2)
    return math.ceil(n_puntos / bloque) ** 2
