"""Gas station problem con desvío acotado, resuelto con programación dinámica.

Variante de Khuller, Malekian y Mestre adaptada a lo que pide ARQUITECTURA.md §7
y §8: depósito con capacidad y reserva mínima, y desvío limitado por una
restricción dura en vez de por un precio del tiempo.

Este módulo **no importa nada de ``adapters/``, ni SQLite, ni OSRM, ni httpx**.
Recibe objetos ya construidos y matrices ya calculadas. Es deliberado: si el DP
dependiera de la red para poder probarse, la arquitectura hexagonal no habría
servido de nada (§12).

Convenio de índices de las matrices
-----------------------------------
Con ``n`` candidatas, las matrices son ``(n + 2) x (n + 2)``:

    0          -> origen
    1 .. n     -> ``candidatas[0] .. candidatas[n - 1]``, **en orden de avance
                  por la ruta**
    n + 1      -> destino

``distancias_km[i][j]`` es la distancia real conduciendo de ``i`` a ``j``, puerta
a puerta. Eso es justo lo que devuelve ``/table`` de OSRM, y por eso el coste en
combustible del desvío sale solo: ir por la estación ``j`` cuesta más kilómetros
que no ir. Solo se usan las celdas con ``j > i``.

Modelo de coste (§8.2)
----------------------
    minimizar   euros de combustible comprado
    sujeto a    el desvío de cada estación cabe en el límite admitido
                el nivel nunca baja de la reserva
    desempate   menos tiempo total

**El tiempo del conductor no se convierte a euros.** Una hora no cuesta dinero, y
fijarle un precio era una decisión que nadie había tomado y que contaminaba la
única cifra real del viaje: lo que se paga en el surtidor. Lo que impedía mandar
a nadie 4 km fuera de la A-2 por 60 céntimos ya no es un precio de la hora sino
el límite de desvío, que se aplica **antes** del DP al elegir qué candidatas
entran (``seleccion_candidatas.depurar_matriz``). Dentro de lo admisible manda el
dinero, y si al conductor no le compensa ese desvío, la lista de opciones (§8.6)
le ofrece la alternativa diciéndole exactamente cuánto le cuesta.

``tiempo_parada_s`` sobrevive, pero en la componente de **tiempo**, nunca en la de
dinero: sigue evitando paradas gratuitas (dos repostajes al mismo precio pierden
contra uno) sin ponerle precio a la hora.

El "combustible comprado" se valora con ``precio_efectivo`` (§6.1), nunca con el
precio nominal del Geoportal: los descuentos de fidelización pueden cambiar cuál
es la estación óptima, no solo cuál se ve más barata.

Aritmética
----------
Todo el DP trabaja en **enteros**: micro-euros y milisegundos. Ni un ``float`` en
la función objetivo: con float, dos estaciones que difieren en una milésima
empatan o se ordenan al azar según por dónde haya pasado la acumulación (§4).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

from app.domain.models import (
    EstacionCandidata,
    Parada,
    Recomendacion,
    TramoRuta,
    Vehiculo,
)
from app.domain.precio_efectivo import PerfilDescuento, precio_efectivo

# Estado del DP = (estación, litros al llegar). Como el nivel es continuo, hay que
# discretizarlo para tener un número finito de estados que tabular (§8.1).
#
# Constante de ajuste fino, no decisión de diseño, pero el valor importa más de lo
# que parece: el consumo de cada tramo se redondea **hacia arriba** por seguridad,
# así que el paso es también el error del plan. Con 1 L medido en Madrid-Barcelona
# eso eran 1,3 € de más en el viaje y hasta 1,7 € de ruido entre dos opciones, más
# que las diferencias de precio que las opciones (§8.6) existen para enseñar: dos
# gasolineras a una milésima de distancia salían separadas por un euro y medio.
# Con 0,25 L el ruido baja a ~0,4 € y el DP entero sigue costando ~300 ms, que en
# una petición donde esperar a OSRM son dos segundos y medio no se nota.
PASO_DISCRETIZACION_L = 0.25

_MICRO = 1_000_000  # micro-euros por euro
_EPS = 1e-9


class Coste(NamedTuple):
    """Valor de la función objetivo. **El dinero manda; el tiempo desempata.**

    Al ser una tupla, Python la compara lexicográficamente sin que haya que
    escribir nada: primero los micro-euros, y solo si empatan, los milisegundos.
    Eso es exactamente el criterio de §8.2, y sale gratis y sin floats.

    Que el tiempo esté en una componente aparte y no sumado al dinero es la
    diferencia entre "tu hora vale 15 €" y "entre dos planes que cuestan lo
    mismo, prefiero el más rápido". Lo segundo no le pone precio a nada.
    """

    combustible_micro_eur: int
    tiempo_ms: int

    def __add__(self, otro: Coste) -> Coste:  # type: ignore[override]
        return Coste(self[0] + otro[0], self[1] + otro[1])


_CERO = Coste(0, 0)
# "Inalcanzable", no "carísimo". Se compara siempre con `< _INF` antes de sumar,
# así que nunca se acumula sobre él.
_INF = Coste(1 << 62, 0)


@dataclass(frozen=True)
class ParametrosOptimizacion:
    """Ajustes del optimizador.

    ``tiempo_parada_s`` es el rato que se pierde en cada repostaje al margen del
    desvío: entrar, repostar, pagar, salir. **No se convierte a euros** (§8.2):
    entra en la componente de tiempo de ``Coste``, que solo sirve para desempatar,
    y en el tiempo que se le informa al usuario en minutos.
    """

    paso_discretizacion_l: float = PASO_DISCRETIZACION_L
    tiempo_parada_s: float = 300.0


# ---------------------------------------------------------------------------
# Errores del dominio
# ---------------------------------------------------------------------------


class ErrorOptimizacion(Exception):
    """Base de los errores del optimizador."""


class VehiculoInvalido(ErrorOptimizacion):
    """Los datos del vehículo no son coherentes."""


@dataclass(frozen=True)
class DiagnosticoInviable:
    """Por qué no hay plan posible, con datos concretos.

    "Sin solución" a secas no le sirve a nadie: hay que poder decir *dónde* se
    rompe el trayecto y por cuántos kilómetros (§7).
    """

    desde: str
    hasta: str
    distancia_km: float
    autonomia_km: float
    candidatas_totales: int
    candidatas_alcanzables: int

    @property
    def faltan_km(self) -> float:
        return self.distancia_km - self.autonomia_km

    def __str__(self) -> str:
        base = (
            f"El trayecto no es viable: de {self.desde} a {self.hasta} hay "
            f"{self.distancia_km:.1f} km y la autonomía disponible es de "
            f"{self.autonomia_km:.1f} km (faltan {self.faltan_km:.1f} km)."
        )
        if self.candidatas_totales == 0:
            return base + " No se ha pasado ninguna estación candidata al optimizador."
        if self.candidatas_alcanzables == 0:
            return base + (
                f" Ninguna de las {self.candidatas_totales} estaciones candidatas está"
                " al alcance; habría que ampliar el radio de búsqueda."
            )
        return base + (
            f" Se alcanzan {self.candidatas_alcanzables} de {self.candidatas_totales}"
            " estaciones candidatas, pero desde ninguna se llega más lejos."
        )


class TrayectoInviable(ErrorOptimizacion):
    """No existe ningún plan que respete la reserva mínima."""

    def __init__(self, diagnostico: DiagnosticoInviable) -> None:
        super().__init__(str(diagnostico))
        self.diagnostico = diagnostico


# ---------------------------------------------------------------------------
# Modelo compartido por las dos pasadas del DP
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Modelo:
    """Todo lo que las pasadas de ida y de vuelta necesitan saber.

    Se construye una vez y lo comparten las dos, para que no puedan discrepar en
    la discretización, los precios o los redondeos.
    """

    vehiculo: Vehiculo
    distancias_km: Sequence[Sequence[float]]
    duraciones_s: Sequence[Sequence[float]] | None
    precios_milesimas: Sequence[int]  # precio efectivo (§6.1), para presentar
    precios_u: Sequence[int]  # el mismo precio, en micro-euros por unidad del DP
    cap_u: int
    reserva_u: int
    inicial_u: int
    paso: float
    parada_ms: int

    @property
    def n_nodos(self) -> int:
        return len(self.precios_u) + 2

    @property
    def destino(self) -> int:
        return self.n_nodos - 1

    def precio_unidad(self, i: int) -> int | None:
        """``None`` en origen y destino: no son estaciones, ahí no se reposta."""
        if 1 <= i <= len(self.precios_u):
            return self.precios_u[i - 1]
        return None

    def hay_arista(self, i: int, j: int) -> bool:
        # `inf` es como el RoutingProvider dice "no sé ir de aquí a allí" (§8.4).
        # No es una arista cara: es que no existe.
        return math.isfinite(self.distancias_km[i][j])

    def consumo_unidades(self, i: int, j: int) -> int:
        litros = self.distancias_km[i][j] * self.vehiculo.consumo_l_100km / 100.0
        return math.ceil(litros / self.paso - _EPS)

    def tiempo_arista(self, i: int, j: int) -> Coste:
        if self.duraciones_s is None or not math.isfinite(self.duraciones_s[i][j]):
            return _CERO
        return Coste(0, round(self.duraciones_s[i][j] * 1000))


def _construir_modelo(
    vehiculo: Vehiculo,
    candidatas: Sequence[EstacionCandidata],
    distancias_km: Sequence[Sequence[float]],
    duraciones_s: Sequence[Sequence[float]] | None,
    parametros: ParametrosOptimizacion,
    usuario: PerfilDescuento | None,
) -> _Modelo:
    _validar_vehiculo(vehiculo)
    _validar_matrices(len(candidatas), distancias_km, duraciones_s)

    # El precio con el que optimiza el DP nunca es el nominal (§6.1). Se resuelve
    # una sola vez, aquí, y de aquí sale la única fuente de precios del resto del
    # módulo: así la cuenta que hace el DP y la que se le enseña al usuario no
    # pueden divergir.
    paso = parametros.paso_discretizacion_l
    precios_milesimas = [
        precio_efectivo(c.estacion, c.precio_milesimas, usuario) for c in candidatas
    ]
    precios_u = [_micro_eur_por_unidad(p, paso) for p in precios_milesimas]

    # Redondeos siempre del lado seguro: nunca prometer un plan que en la
    # realidad se quede corto de combustible.
    cap_u = int(vehiculo.capacidad_deposito_l / paso + _EPS)  # hacia abajo
    reserva_u = math.ceil(vehiculo.reserva_minima_l / paso - _EPS)  # hacia arriba
    inicial_u = min(int(vehiculo.nivel_actual_l / paso + _EPS), cap_u)

    if inicial_u < reserva_u:
        raise VehiculoInvalido(
            f"El nivel actual ({vehiculo.nivel_actual_l:g} L) ya está por debajo de la "
            f"reserva mínima ({vehiculo.reserva_minima_l:g} L). El optimizador de ruta "
            "no es la herramienta para esto: busca la estación más cercana y repón antes."
        )

    return _Modelo(
        vehiculo=vehiculo,
        distancias_km=distancias_km,
        duraciones_s=duraciones_s,
        precios_milesimas=precios_milesimas,
        precios_u=precios_u,
        cap_u=cap_u,
        reserva_u=reserva_u,
        inicial_u=inicial_u,
        paso=paso,
        parada_ms=round(parametros.tiempo_parada_s * 1000),
    )


# ---------------------------------------------------------------------------
# Optimizador
# ---------------------------------------------------------------------------


def optimizar_repostaje(
    vehiculo: Vehiculo,
    candidatas: Sequence[EstacionCandidata],
    distancias_km: Sequence[Sequence[float]],
    duraciones_s: Sequence[Sequence[float]] | None = None,
    parametros: ParametrosOptimizacion | None = None,
    usuario: PerfilDescuento | None = None,
    primera_parada_en: int | None = None,
    parada_unica: bool = False,
) -> Recomendacion:
    """Devuelve el plan de repostaje más barato para el trayecto.

    Args:
        vehiculo: consumo, depósito, nivel actual y reserva mínima.
        candidatas: estaciones con precio, **ordenadas por avance en la ruta**.
            Se dan por ya filtradas por desvío: el límite de viabilidad se aplica
            fuera, al construir la matriz (§8.2).
        distancias_km: matriz ``(n+2)x(n+2)`` de distancias reales, en km.
        duraciones_s: matriz ``(n+2)x(n+2)`` de duraciones, en segundos. Si se
            omite, el desempate por tiempo se reduce al número de paradas. Para
            producción conviene pasarla; para tests con datos inventados suele
            sobrar.
        parametros: ajustes finos del optimizador.
        usuario: perfil de descuentos de fidelización. Fase 1 siempre ``None``
            (§6.1); el DP no lo interpreta, solo se lo pasa a
            ``precio_efectivo``.
        primera_parada_en: índice de candidata (base 1, el del convenio de las
            matrices) que será la **próxima parada** del plan: no se reposta antes
            de ella, sí en ella, y a partir de ahí el plan vuelve a ser libre. Es
            lo que contesta "¿y si prefiero parar a las dos horas?" (§8.6).
            ``None`` para el plan óptimo sin restricciones.
        parada_unica: con ``primera_parada_en``, prohíbe repostar en ningún otro
            sitio: "paro ahí, lleno lo que haga falta y llego". Lanza
            ``TrayectoInviable`` si el depósito no da para tanto, y entonces toca
            volver a preguntar sin esta restricción.

    Raises:
        VehiculoInvalido: los datos del vehículo no son coherentes.
        TrayectoInviable: no hay plan que respete la reserva mínima. La excepción
            lleva un ``DiagnosticoInviable`` con dónde y por cuánto se rompe.
    """
    parametros = parametros or ParametrosOptimizacion()
    modelo = _construir_modelo(
        vehiculo, candidatas, distancias_km, duraciones_s, parametros, usuario
    )
    ida = _tabla_adelante(modelo, primera_parada=primera_parada_en, parada_unica=parada_unica)

    mejor_nivel = _argmin_finito(ida.coste[modelo.destino])
    if mejor_nivel is None:
        raise TrayectoInviable(_diagnosticar(modelo, candidatas, ida.coste, primera_parada_en))

    return _reconstruir(
        modelo=modelo,
        candidatas=candidatas,
        parametros=parametros,
        padre=ida.padre,
        nivel_destino=mejor_nivel,
    )


def sobrecoste_por_candidata(
    vehiculo: Vehiculo,
    candidatas: Sequence[EstacionCandidata],
    distancias_km: Sequence[Sequence[float]],
    duraciones_s: Sequence[Sequence[float]] | None = None,
    parametros: ParametrosOptimizacion | None = None,
    usuario: PerfilDescuento | None = None,
) -> list[Decimal | None]:
    """Cuánto encarece el viaje que la próxima parada sea cada candidata.

        Devuelve una lista paralela a ``candidatas``: euros de más respecto al plan
        óptimo, o ``None`` si esa estación no puede ser la próxima parada (no se llega
        hasta ella con la reserva puesta, o desde ahí no se alcanza el destino).

        Es la cifra que convierte una lista de gasolineras en una lista de
        *decisiones*: "parar a las dos horas en vez de a las tres te cuesta 0,71 €"
        (§8.6). Cero exacto en la estación de la óptima, y nunca negativa.

    Cuesta tres pasadas del DP para todas las candidatas a la vez, no una
        pasada por candidata: el plan óptimo de referencia, con cuánto se llega a cada
        estación sin repostar antes, y qué cuesta seguir desde cada estación hasta el
        destino. Pegar las dos últimas por el nivel del depósito da el mínimo buscado
        en O(niveles) por candidata.

        Raises:
            VehiculoInvalido: los datos del vehículo no son coherentes.
            TrayectoInviable: no hay plan óptimo contra el que comparar.
    """
    parametros = parametros or ParametrosOptimizacion()
    modelo = _construir_modelo(
        vehiculo, candidatas, distancias_km, duraciones_s, parametros, usuario
    )

    ida = _tabla_adelante(modelo)
    mejor_nivel = _argmin_finito(ida.coste[modelo.destino])
    if mejor_nivel is None:
        raise TrayectoInviable(_diagnosticar(modelo, candidatas, ida.coste, None))
    optimo = ida.coste[modelo.destino][mejor_nivel]

    # Con cuánto se llega a cada estación sin haber repostado antes (la de ida es
    # la mitad del cálculo) y qué cuesta seguir desde ella hasta el destino (la de
    # vuelta). Pegar las dos por el nivel del depósito da, para cada estación, el
    # mejor plan cuya próxima parada sea esa.
    ayunas = _tabla_adelante(modelo, en_ayunas=True)
    atras = _tabla_atras(modelo)

    sobrecostes: list[Decimal | None] = []
    for indice in range(1, len(candidatas) + 1):
        parar = _repostar_en(
            ayunas.coste[indice],
            modelo.cap_u,
            precio_unidad=modelo.precio_unidad(indice),
            parada_ms=modelo.parada_ms,
            obligatoria=True,
        )
        mejor: Coste | None = None
        for nivel in range(modelo.cap_u + 1):
            salida = parar.tras[nivel]
            resto = atras[indice][nivel]
            if salida >= _INF or resto >= _INF:
                continue
            total = salida + resto
            if mejor is None or total < mejor:
                mejor = total
        if mejor is None:
            sobrecostes.append(None)
        else:
            diferencia = mejor.combustible_micro_eur - optimo.combustible_micro_eur
            sobrecostes.append(Decimal(max(0, diferencia)) / _MICRO)
    return sobrecostes


# ---------------------------------------------------------------------------
# Núcleo del DP: pasada de ida
# ---------------------------------------------------------------------------


class _Ida(NamedTuple):
    coste: list[list[Coste]]  # coste[i][k] = llegar a i con k unidades
    padre: list[list[tuple[int, int, int] | None]]
    repostando: list[list[Coste]]  # estar en i con k unidades habiendo repostado ahí


class _TrasParar(NamedTuple):
    tras: list[Coste]  # salir del nodo con k unidades, repostando o no
    procedencia: list[int]  # con cuántas unidades se había llegado
    repostando: list[Coste]  # salir con k habiendo echado al menos una unidad


def _tabla_adelante(
    modelo: _Modelo,
    primera_parada: int | None = None,
    en_ayunas: bool = False,
    parada_unica: bool = False,
) -> _Ida:
    """Coste mínimo de llegar del origen a cada nodo con cada nivel de depósito.

    ``primera_parada`` responde a "¿y si mi próxima parada fuese esta?": antes de
    ese nodo no se reposta, en él se reposta a la fuerza, y a partir de ahí el
    plan sigue siendo libre.

    ``parada_unica`` además prohíbe repostar *después*: es "paro aquí, lleno y
    llego". Cuando el depósito da para eso, es lo que el conductor quiere decir al
    elegir una parada; cuando no da, hay que dejarle repostar más adelante.

    ``en_ayunas`` prohíbe repostar en todo el trayecto. No es un plan que se le
    proponga a nadie: es la mitad de ida del cálculo de sobrecostes, que necesita
    saber con cuánto se llega a cada estación sin haber repostado antes.
    """
    n_nodos = modelo.n_nodos
    cap_u = modelo.cap_u
    reserva_u = modelo.reserva_u

    coste: list[list[Coste]] = [[_INF] * (cap_u + 1) for _ in range(n_nodos)]
    # padre[j][k] = (nodo anterior, nivel al salir de él, nivel al llegar a él)
    padre: list[list[tuple[int, int, int] | None]] = [[None] * (cap_u + 1) for _ in range(n_nodos)]
    repostando: list[list[Coste]] = [[_INF] * (cap_u + 1) for _ in range(n_nodos)]
    coste[0][modelo.inicial_u] = _CERO

    def precio_en(i: int) -> int | None:
        """``None`` = aquí no se reposta, ni pudiendo."""
        if en_ayunas:
            return None
        if primera_parada is not None and i < primera_parada:
            return None
        if parada_unica and i != primera_parada:
            return None
        return modelo.precio_unidad(i)

    for i in range(n_nodos - 1):
        parar = _repostar_en(
            coste[i],
            cap_u,
            precio_unidad=precio_en(i),
            parada_ms=modelo.parada_ms,
            obligatoria=(i == primera_parada),
        )
        repostando[i] = parar.repostando

        for j in range(i + 1, n_nodos):
            if not modelo.hay_arista(i, j):
                continue
            # Obligar a repostar en un nodo no basta para que el plan pase por él:
            # los nodos van en orden de avance, así que un salto de i a j con
            # `i < f < j` se lo saltaría de largo. Esas aristas se prohíben.
            if primera_parada is not None and i < primera_parada < j:
                continue
            consumo_u = modelo.consumo_unidades(i, j)
            if consumo_u > cap_u - reserva_u:
                continue  # ese salto no cabe ni con el depósito lleno
            tiempo = modelo.tiempo_arista(i, j)
            for salida in range(reserva_u + consumo_u, cap_u + 1):
                if parar.tras[salida] >= _INF:
                    continue
                llegada = salida - consumo_u
                candidato = parar.tras[salida] + tiempo
                if candidato < coste[j][llegada]:
                    coste[j][llegada] = candidato
                    padre[j][llegada] = (i, salida, parar.procedencia[salida])

    return _Ida(coste=coste, padre=padre, repostando=repostando)


def _repostar_en(
    llegada: Sequence[Coste],
    cap_u: int,
    precio_unidad: int | None,
    parada_ms: int,
    obligatoria: bool = False,
) -> _TrasParar:
    """Aplica la decisión "cuánto repostar aquí" a todos los niveles de golpe.

    La clave para que esto sea O(niveles) y no O(niveles²): comprar es lineal en
    la cantidad, así que llegar al nivel ``m`` comprando siempre se puede hacer
    "estando en ``m-1`` y echando una unidad más". Se barre de abajo arriba y se
    arrastra el mínimo. El tiempo fijo de parada se paga solo en la primera
    unidad, por eso hace falta llevar aparte la rama "ya he empezado a repostar".

    Esa rama, ``repostando``, no es un detalle de implementación: es justo lo que
    necesita ``sobrecoste_por_candidata`` para saber cuánto cuesta un plan que se
    obliga a repostar aquí, y por eso se devuelve en vez de descartarse.

    Con ``obligatoria``, no repostar deja de ser una opción: es como se responde
    "¿y si paro en esta?" sin escribir un segundo DP.
    """
    unidad = Coste(precio_unidad or 0, 0)
    primera = Coste(precio_unidad or 0, parada_ms)

    if precio_unidad is None:  # origen y destino no son estaciones
        vacio = [_INF] * (cap_u + 1)
        return _TrasParar(list(llegada), list(range(cap_u + 1)), vacio)

    repostando = [_INF] * (cap_u + 1)
    origen_reposte = [0] * (cap_u + 1)

    for nivel in range(1, cap_u + 1):
        empezar = llegada[nivel - 1] + primera if llegada[nivel - 1] < _INF else _INF
        seguir = repostando[nivel - 1] + unidad if repostando[nivel - 1] < _INF else _INF

        if empezar <= seguir:
            repostando[nivel] = empezar
            origen_reposte[nivel] = nivel - 1
        else:
            repostando[nivel] = seguir
            origen_reposte[nivel] = origen_reposte[nivel - 1]

    if obligatoria:
        return _TrasParar(list(repostando), list(origen_reposte), repostando)

    tras = list(llegada)
    procedencia = list(range(cap_u + 1))  # por defecto: no se ha repostado
    for nivel in range(1, cap_u + 1):
        if repostando[nivel] < tras[nivel]:
            tras[nivel] = repostando[nivel]
            procedencia[nivel] = origen_reposte[nivel]

    return _TrasParar(tras, procedencia, repostando)


# ---------------------------------------------------------------------------
# Núcleo del DP: pasada de vuelta
# ---------------------------------------------------------------------------


def _tabla_atras(modelo: _Modelo) -> list[list[Coste]]:
    """``atras[i][k]``: coste mínimo de ir del nodo ``i`` al destino saliendo con ``k``.

    Es la de ida vista del revés, y con ella el coste de cualquier plan que pase
    repostando por ``i`` es ``ida.repostando[i][k] + atras[i][k]``, minimizado
    sobre el nivel ``k`` con el que se sale de ``i``. Sin esta tabla habría que
    resolver un DP entero por candidata.
    """
    n_nodos = modelo.n_nodos
    cap_u = modelo.cap_u
    reserva_u = modelo.reserva_u
    destino = modelo.destino

    atras: list[list[Coste]] = [[_INF] * (cap_u + 1) for _ in range(n_nodos)]
    atras[destino] = [_CERO] * (cap_u + 1)

    # `entrar[j][k]`: coste desde j hasta el destino habiendo *llegado* a j con k
    # unidades, es decir contando lo que convenga repostar en j. Se calcula la
    # primera vez que hace falta, cuando `atras[j]` ya es definitivo.
    entrar: list[list[Coste] | None] = [None] * n_nodos
    entrar[destino] = atras[destino]  # en el destino no se reposta

    for i in range(n_nodos - 2, -1, -1):
        for j in range(i + 1, n_nodos):
            if not modelo.hay_arista(i, j):
                continue
            consumo_u = modelo.consumo_unidades(i, j)
            if consumo_u > cap_u - reserva_u:
                continue
            if entrar[j] is None:
                entrar[j] = _entrar_y_repostar(
                    atras[j], cap_u, modelo.precio_unidad(j), modelo.parada_ms
                )
            desde_j = entrar[j]
            assert desde_j is not None
            tiempo = modelo.tiempo_arista(i, j)
            for salida in range(reserva_u + consumo_u, cap_u + 1):
                llegada = salida - consumo_u
                if desde_j[llegada] >= _INF:
                    continue
                candidato = desde_j[llegada] + tiempo
                if candidato < atras[i][salida]:
                    atras[i][salida] = candidato

    return atras


def _entrar_y_repostar(
    atras_j: Sequence[Coste], cap_u: int, precio_unidad: int | None, parada_ms: int
) -> list[Coste]:
    """Coste desde ``j`` al destino habiendo llegado con cada nivel, repostando o no.

    Simétrico de ``_repostar_en`` y, como aquel, O(niveles): repostar hasta ``m``
    cuesta ``(m - llegada) * precio``, que separa en un término que solo depende
    de ``m`` y otro que solo depende de ``llegada``. Barriendo ``m`` de arriba
    abajo y arrastrando el mínimo de ``m * precio + atras[m]`` sale el óptimo de
    todos los niveles de llegada de una vez.
    """
    if precio_unidad is None:
        return list(atras_j)

    # sufijo[m] = min sobre m' >= m de (m' * precio + atras_j[m'])
    sufijo: list[Coste] = [_INF] * (cap_u + 2)
    for m in range(cap_u, -1, -1):
        actual = Coste(m * precio_unidad, 0) + atras_j[m] if atras_j[m] < _INF else _INF
        sufijo[m] = actual if actual < sufijo[m + 1] else sufijo[m + 1]

    resultado: list[Coste] = []
    for llegada in range(cap_u + 1):
        mejor = atras_j[llegada]  # seguir sin repostar
        candidato = sufijo[llegada + 1]
        if candidato < _INF:
            # Se le quita lo que ya tenía en el depósito y se le suma el tiempo de
            # la parada, que se paga una sola vez por mucho que se eche.
            con_reposte = Coste(
                candidato.combustible_micro_eur - llegada * precio_unidad,
                candidato.tiempo_ms + parada_ms,
            )
            if con_reposte < mejor:
                mejor = con_reposte
        resultado.append(mejor)
    return resultado


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def _micro_eur_por_unidad(precio_milesimas: int, paso: float) -> int:
    """Coste en micro-euros de una unidad de discretización de combustible."""
    return int(
        (Decimal(precio_milesimas) * Decimal(str(paso)) * 1000).to_integral_value(ROUND_HALF_UP)
    )


def _argmin_finito(costes: Sequence[Coste]) -> int | None:
    mejor: int | None = None
    for nivel, valor in enumerate(costes):
        if valor < _INF and (mejor is None or valor < costes[mejor]):
            mejor = nivel
    return mejor


# ---------------------------------------------------------------------------
# Validación
# ---------------------------------------------------------------------------


def _validar_vehiculo(vehiculo: Vehiculo) -> None:
    if vehiculo.consumo_l_100km <= 0:
        raise VehiculoInvalido("El consumo debe ser mayor que cero (en L/100km).")
    if vehiculo.capacidad_deposito_l <= 0:
        raise VehiculoInvalido("La capacidad del depósito debe ser mayor que cero.")
    if vehiculo.nivel_actual_l > vehiculo.capacidad_deposito_l:
        raise VehiculoInvalido(
            f"El nivel actual ({vehiculo.nivel_actual_l:g} L) no puede superar la "
            f"capacidad del depósito ({vehiculo.capacidad_deposito_l:g} L)."
        )
    if vehiculo.nivel_actual_l < 0 or vehiculo.reserva_minima_l < 0:
        raise VehiculoInvalido("Ni el nivel actual ni la reserva mínima pueden ser negativos.")
    if vehiculo.reserva_minima_l >= vehiculo.capacidad_deposito_l:
        raise VehiculoInvalido(
            f"La reserva mínima ({vehiculo.reserva_minima_l:g} L) deja el depósito "
            f"({vehiculo.capacidad_deposito_l:g} L) sin combustible utilizable."
        )


def _validar_matrices(
    n: int,
    distancias_km: Sequence[Sequence[float]],
    duraciones_s: Sequence[Sequence[float]] | None,
) -> None:
    esperado = n + 2
    for nombre, matriz in (("distancias_km", distancias_km), ("duraciones_s", duraciones_s)):
        if matriz is None:
            continue
        if len(matriz) != esperado or any(len(fila) != esperado for fila in matriz):
            raise ValueError(
                f"{nombre} debe ser {esperado}x{esperado} para {n} candidatas "
                "(origen + candidatas + destino)."
            )
    for i in range(esperado):
        for j in range(i + 1, esperado):
            valor = distancias_km[i][j]
            if math.isnan(valor):
                raise ValueError(
                    f"distancias_km[{i}][{j}] es NaN. Para decir 'no hay ruta' se usa "
                    "`inf`, que el DP sí sabe interpretar."
                )
            if valor < 0:
                raise ValueError(f"distancias_km[{i}][{j}] es negativa.")


# ---------------------------------------------------------------------------
# Reconstrucción del plan
# ---------------------------------------------------------------------------


def _reconstruir(
    modelo: _Modelo,
    candidatas: Sequence[EstacionCandidata],
    parametros: ParametrosOptimizacion,
    padre: Sequence[Sequence[tuple[int, int, int] | None]],
    nivel_destino: int,
) -> Recomendacion:
    destino = modelo.destino
    paso = modelo.paso
    distancias_km = modelo.distancias_km
    duraciones_s = modelo.duraciones_s

    # Camino hacia atrás: (nodo, nivel al llegar, nivel al salir)
    camino: list[tuple[int, int, int]] = [(destino, nivel_destino, nivel_destino)]
    nodo, nivel = destino, nivel_destino
    while nodo != 0:
        anterior = padre[nodo][nivel]
        assert anterior is not None, "camino roto en la reconstrucción del DP"
        nodo_previo, nivel_salida, nivel_llegada = anterior
        camino.append((nodo_previo, nivel_llegada, nivel_salida))
        nodo, nivel = nodo_previo, nivel_llegada
    camino.reverse()

    paradas: list[Parada] = []
    tramos: list[TramoRuta] = []
    coste_combustible = Decimal(0)
    litros_repostados = 0.0
    distancia_total = 0.0
    conduccion_total = 0.0
    km_acumulados = 0.0

    for indice, (nodo, nivel_llegada, nivel_salida) in enumerate(camino):
        litros_comprados = (nivel_salida - nivel_llegada) * paso
        if litros_comprados > 0:
            candidata = candidatas[nodo - 1]
            precio_pagado = modelo.precios_milesimas[nodo - 1]
            coste_parada = Decimal(precio_pagado) / Decimal(1000) * Decimal(str(litros_comprados))
            coste_combustible += coste_parada
            litros_repostados += litros_comprados
            paradas.append(
                Parada(
                    candidata=candidata,
                    precio_efectivo_milesimas=precio_pagado,
                    litros=litros_comprados,
                    nivel_llegada_l=nivel_llegada * paso,
                    nivel_salida_l=nivel_salida * paso,
                    coste_eur=coste_parada,
                    km_desde_origen=km_acumulados,
                )
            )

        if indice + 1 < len(camino):
            siguiente = camino[indice + 1][0]
            distancia = distancias_km[nodo][siguiente]
            duracion = duraciones_s[nodo][siguiente] if duraciones_s is not None else 0.0
            distancia_total += distancia
            conduccion_total += duracion
            km_acumulados += distancia
            tramos.append(
                TramoRuta(
                    desde=_nombre_nodo(nodo, candidatas, destino),
                    hasta=_nombre_nodo(siguiente, candidatas, destino),
                    distancia_km=distancia,
                    duracion_s=duracion,
                    combustible_l=modelo.vehiculo.litros_para(distancia),
                    nivel_llegada_l=camino[indice + 1][1] * paso,
                )
            )

    # El tiempo que le cuesta el plan al conductor es conducir *más* repostar. Los
    # dos van juntos en el total y el exceso; el usuario lo lee en minutos, nunca
    # en euros (§8.2).
    tiempo_paradas = len(paradas) * parametros.tiempo_parada_s
    duracion_total = conduccion_total + tiempo_paradas
    duracion_directa = duraciones_s[0][destino] if duraciones_s is not None else 0.0

    return Recomendacion(
        paradas=paradas,
        tramos=tramos,
        coste_combustible_eur=coste_combustible,
        litros_repostados=litros_repostados,
        distancia_total_km=distancia_total,
        duracion_total_s=duracion_total,
        desvio_km=max(0.0, distancia_total - distancias_km[0][destino]),
        desvio_s=max(0.0, duracion_total - duracion_directa),
        nivel_llegada_destino_l=nivel_destino * paso,
    )


def _nombre_nodo(indice: int, candidatas: Sequence[EstacionCandidata], destino: int) -> str:
    if indice == 0:
        return "Origen"
    if indice == destino:
        return "Destino"
    estacion = candidatas[indice - 1].estacion
    partes = [estacion.rotulo]
    if estacion.municipio:
        partes.append(f"({estacion.municipio})")
    return " ".join(partes)


# ---------------------------------------------------------------------------
# Diagnóstico de inviabilidad
# ---------------------------------------------------------------------------


def _diagnosticar(
    modelo: _Modelo,
    candidatas: Sequence[EstacionCandidata],
    coste: Sequence[Sequence[Coste]],
    primera_parada_en: int | None,
) -> DiagnosticoInviable:
    """Localiza dónde se rompe el trayecto y por cuántos kilómetros."""
    vehiculo = modelo.vehiculo
    distancias_km = modelo.distancias_km
    cap_u = modelo.cap_u
    reserva_u = modelo.reserva_u
    paso = modelo.paso
    n_nodos = modelo.n_nodos
    destino = modelo.destino

    alcanzables = [i for i in range(n_nodos) if any(c < _INF for c in coste[i])]
    sin_alcanzar = sorted(set(range(n_nodos)) - set(alcanzables))

    def autonomia_desde(i: int) -> float:
        # Del origen se sale con lo que haya en el depósito; de una estación, con
        # el depósito lleno, que es el mejor caso posible.
        disponible = max(_niveles_alcanzados(coste[0])) if i == 0 else cap_u
        return max(0.0, (disponible - reserva_u) * paso) * 100.0 / vehiculo.consumo_l_100km

    mejor: tuple[float, int, int] | None = None
    for i in alcanzables:
        alcance = autonomia_desde(i)
        for j in sin_alcanzar:
            if j <= i or not math.isfinite(distancias_km[i][j]):
                continue
            deficit = distancias_km[i][j] - alcance
            if mejor is None or deficit < mejor[0]:
                mejor = (deficit, i, j)

    if mejor is None:  # no debería pasar, pero el mensaje no puede quedarse mudo
        return DiagnosticoInviable(
            desde="Origen"
            if primera_parada_en is None
            else _nombre_nodo(primera_parada_en, candidatas, destino),
            hasta="Destino",
            distancia_km=distancias_km[0][destino],
            autonomia_km=vehiculo.autonomia_actual_km,
            candidatas_totales=len(candidatas),
            candidatas_alcanzables=0,
        )

    _, desde, hasta = mejor
    return DiagnosticoInviable(
        desde=_nombre_nodo(desde, candidatas, destino),
        hasta=_nombre_nodo(hasta, candidatas, destino),
        distancia_km=distancias_km[desde][hasta],
        autonomia_km=autonomia_desde(desde),
        candidatas_totales=len(candidatas),
        candidatas_alcanzables=sum(1 for i in alcanzables if 1 <= i <= len(candidatas)),
    )


def _niveles_alcanzados(costes: Sequence[Coste]) -> list[int]:
    return [nivel for nivel, valor in enumerate(costes) if valor < _INF] or [0]
