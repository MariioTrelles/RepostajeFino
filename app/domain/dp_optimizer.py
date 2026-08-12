"""Gas station problem con coste de desvío, resuelto con programación dinámica.

Variante de Khuller, Malekian y Mestre adaptada a lo que pide ARQUITECTURA.md §7
y §8: depósito con capacidad y reserva mínima, y coste de desvío que incluye
tiempo además de combustible.

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

Modelo de coste
---------------
    coste = combustible comprado
          + valor_tiempo_eur_h * (exceso de tiempo sobre la ruta directa
                                  + tiempo_parada_s por cada repostaje)

Sin el término de tiempo, el algoritmo manda al usuario 4 km fuera de la A-2 para
ahorrar 60 céntimos (§8, punto 3).

Aritmética
----------
Todo el DP trabaja en **micro-euros enteros**. Ni un ``float`` en la función
objetivo: con float, dos estaciones que difieren en una milésima empatan o se
ordenan al azar según por dónde haya pasado la acumulación (§4).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.domain.models import (
    EstacionCandidata,
    Parada,
    Recomendacion,
    TramoRuta,
    Vehiculo,
)

# Estado del DP = (estación, litros al llegar). Como el nivel es continuo, hay que
# discretizarlo para tener un número finito de estados que tabular (§8.1).
# Constante de ajuste fino, no decisión de diseño: con depósitos de 40-70 L da
# 40-70 estados por candidata, trivial. Si alguna ruta larga se hace lenta, subir
# a 2-5 L no toca la lógica del algoritmo.
PASO_DISCRETIZACION_L = 1.0

_MICRO = 1_000_000  # micro-euros por euro
_INF = 1 << 62
_EPS = 1e-9


@dataclass(frozen=True)
class ParametrosOptimizacion:
    """Ajustes del optimizador.

    ``valor_tiempo_eur_h`` es cuánto vale una hora del conductor. Es lo que
    impide que el plan mande a nadie 6 km fuera de la autovía por unos céntimos.
    15 €/h es un valor de partida razonable para uso particular; súbelo si el
    usuario prefiere no parar, bájalo si va con todo el tiempo del mundo.

    ``tiempo_parada_s`` es el rato que se pierde en cada repostaje al margen del
    desvío: entrar, repostar, pagar, salir.
    """

    paso_discretizacion_l: float = PASO_DISCRETIZACION_L
    valor_tiempo_eur_h: float = 15.0
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
# Optimizador
# ---------------------------------------------------------------------------


def optimizar_repostaje(
    vehiculo: Vehiculo,
    candidatas: Sequence[EstacionCandidata],
    distancias_km: Sequence[Sequence[float]],
    duraciones_s: Sequence[Sequence[float]] | None = None,
    parametros: ParametrosOptimizacion | None = None,
) -> Recomendacion:
    """Devuelve el plan de repostaje más barato para el trayecto.

    Args:
        vehiculo: consumo, depósito, nivel actual y reserva mínima.
        candidatas: estaciones con precio, **ordenadas por avance en la ruta**.
        distancias_km: matriz ``(n+2)x(n+2)`` de distancias reales, en km.
        duraciones_s: matriz ``(n+2)x(n+2)`` de duraciones, en segundos. Si se
            omite, el coste de tiempo se reduce al fijo por parada: el desvío
            solo se penaliza por los kilómetros de más. Para producción conviene
            pasarla; para tests con datos inventados suele sobrar.
        parametros: ajustes finos del optimizador.

    Raises:
        VehiculoInvalido: los datos del vehículo no son coherentes.
        TrayectoInviable: no hay plan que respete la reserva mínima. La excepción
            lleva un ``DiagnosticoInviable`` con dónde y por cuánto se rompe.
    """
    parametros = parametros or ParametrosOptimizacion()
    _validar_vehiculo(vehiculo)
    _validar_matrices(len(candidatas), distancias_km, duraciones_s)

    n = len(candidatas)
    n_nodos = n + 2
    destino = n + 1
    paso = parametros.paso_discretizacion_l

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

    micro_por_segundo = Decimal(str(parametros.valor_tiempo_eur_h)) * _MICRO / Decimal(3600)
    parada_u = int((micro_por_segundo * Decimal(str(parametros.tiempo_parada_s))).to_integral_value(
        ROUND_HALF_UP
    ))

    def consumo_unidades(i: int, j: int) -> int:
        litros = distancias_km[i][j] * vehiculo.consumo_l_100km / 100.0
        return math.ceil(litros / paso - _EPS)

    def coste_tiempo_arista(i: int, j: int) -> int:
        if duraciones_s is None:
            return 0
        return int(
            (micro_por_segundo * Decimal(str(duraciones_s[i][j]))).to_integral_value(ROUND_HALF_UP)
        )

    # coste[i][k] = mínimo coste (micro-euros) de llegar al nodo i con k unidades.
    coste: list[list[int]] = [[_INF] * (cap_u + 1) for _ in range(n_nodos)]
    # padre[j][k] = (nodo anterior, nivel al salir de él, nivel al llegar a él)
    padre: list[list[tuple[int, int, int] | None]] = [
        [None] * (cap_u + 1) for _ in range(n_nodos)
    ]
    coste[0][inicial_u] = 0

    for i in range(n_nodos - 1):
        despues, procedencia = _repostar_en(
            coste[i],
            cap_u,
            precio_unidad=(
                _micro_eur_por_unidad(candidatas[i - 1].precio_milesimas, paso)
                if 1 <= i <= n
                else None
            ),
            coste_parada=parada_u,
        )

        for j in range(i + 1, n_nodos):
            consumo_u = consumo_unidades(i, j)
            if consumo_u > cap_u - reserva_u:
                continue  # ese salto no cabe ni con el depósito lleno
            tiempo_u = coste_tiempo_arista(i, j)
            for salida in range(reserva_u + consumo_u, cap_u + 1):
                if despues[salida] >= _INF:
                    continue
                llegada = salida - consumo_u
                candidato = despues[salida] + tiempo_u
                if candidato < coste[j][llegada]:
                    coste[j][llegada] = candidato
                    padre[j][llegada] = (i, salida, procedencia[salida])

    mejor_nivel = _argmin_finito(coste[destino])
    if mejor_nivel is None:
        raise TrayectoInviable(
            _diagnosticar(vehiculo, candidatas, distancias_km, coste, cap_u, reserva_u, paso)
        )

    return _reconstruir(
        vehiculo=vehiculo,
        candidatas=candidatas,
        distancias_km=distancias_km,
        duraciones_s=duraciones_s,
        parametros=parametros,
        padre=padre,
        destino=destino,
        nivel_destino=mejor_nivel,
        paso=paso,
    )


# ---------------------------------------------------------------------------
# Núcleo del DP
# ---------------------------------------------------------------------------


def _repostar_en(
    llegada: Sequence[int],
    cap_u: int,
    precio_unidad: int | None,
    coste_parada: int,
) -> tuple[list[int], list[int]]:
    """Aplica la decisión "cuánto repostar aquí" a todos los niveles de golpe.

    Devuelve ``(coste_tras_repostar, nivel_de_llegada_del_que_viene)``.

    La clave para que esto sea O(niveles) y no O(niveles²): comprar es lineal en
    la cantidad, así que llegar al nivel ``m`` comprando siempre se puede hacer
    "estando en ``m-1`` y echando una unidad más". Se barre de abajo arriba y se
    arrastra el mínimo. El coste fijo de parada se paga solo en la primera unidad,
    por eso hace falta llevar aparte la rama "ya he empezado a repostar".
    """
    tras = list(llegada)
    procedencia = list(range(cap_u + 1))  # por defecto: no se ha repostado

    if precio_unidad is None:  # origen y destino no son estaciones
        return tras, procedencia

    repostando = [_INF] * (cap_u + 1)
    origen_reposte = [0] * (cap_u + 1)

    for nivel in range(1, cap_u + 1):
        empezar = (
            llegada[nivel - 1] + coste_parada + precio_unidad
            if llegada[nivel - 1] < _INF
            else _INF
        )
        seguir = repostando[nivel - 1] + precio_unidad if repostando[nivel - 1] < _INF else _INF

        if empezar <= seguir:
            repostando[nivel] = empezar
            origen_reposte[nivel] = nivel - 1
        else:
            repostando[nivel] = seguir
            origen_reposte[nivel] = origen_reposte[nivel - 1]

        if repostando[nivel] < tras[nivel]:
            tras[nivel] = repostando[nivel]
            procedencia[nivel] = origen_reposte[nivel]

    return tras, procedencia


def _micro_eur_por_unidad(precio_milesimas: int, paso: float) -> int:
    """Coste en micro-euros de una unidad de discretización de combustible."""
    return int(
        (Decimal(precio_milesimas) * Decimal(str(paso)) * 1000).to_integral_value(ROUND_HALF_UP)
    )


def _argmin_finito(costes: Sequence[int]) -> int | None:
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
            if distancias_km[i][j] < 0:
                raise ValueError(f"distancias_km[{i}][{j}] es negativa.")


# ---------------------------------------------------------------------------
# Reconstrucción del plan
# ---------------------------------------------------------------------------


def _reconstruir(
    vehiculo: Vehiculo,
    candidatas: Sequence[EstacionCandidata],
    distancias_km: Sequence[Sequence[float]],
    duraciones_s: Sequence[Sequence[float]] | None,
    parametros: ParametrosOptimizacion,
    padre: Sequence[Sequence[tuple[int, int, int] | None]],
    destino: int,
    nivel_destino: int,
    paso: float,
) -> Recomendacion:
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
    duracion_total = 0.0
    km_acumulados = 0.0

    for indice, (nodo, nivel_llegada, nivel_salida) in enumerate(camino):
        litros_comprados = (nivel_salida - nivel_llegada) * paso
        if litros_comprados > 0:
            candidata = candidatas[nodo - 1]
            coste_parada = candidata.euros_por_litro * Decimal(str(litros_comprados))
            coste_combustible += coste_parada
            litros_repostados += litros_comprados
            paradas.append(
                Parada(
                    candidata=candidata,
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
            duracion_total += duracion
            km_acumulados += distancia
            tramos.append(
                TramoRuta(
                    desde=_nombre_nodo(nodo, candidatas, destino),
                    hasta=_nombre_nodo(siguiente, candidatas, destino),
                    distancia_km=distancia,
                    duracion_s=duracion,
                    combustible_l=vehiculo.litros_para(distancia),
                    nivel_llegada_l=camino[indice + 1][1] * paso,
                )
            )

    duracion_directa = duraciones_s[0][destino] if duraciones_s is not None else 0.0
    desvio_s = max(0.0, duracion_total - duracion_directa)
    tiempo_penalizado_s = desvio_s + len(paradas) * parametros.tiempo_parada_s
    coste_tiempo = (
        Decimal(str(parametros.valor_tiempo_eur_h))
        * Decimal(str(tiempo_penalizado_s))
        / Decimal(3600)
    )

    return Recomendacion(
        paradas=paradas,
        tramos=tramos,
        coste_combustible_eur=coste_combustible,
        coste_tiempo_eur=coste_tiempo,
        litros_repostados=litros_repostados,
        distancia_total_km=distancia_total,
        duracion_total_s=duracion_total,
        desvio_km=max(0.0, distancia_total - distancias_km[0][destino]),
        desvio_s=desvio_s,
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
    vehiculo: Vehiculo,
    candidatas: Sequence[EstacionCandidata],
    distancias_km: Sequence[Sequence[float]],
    coste: Sequence[Sequence[int]],
    cap_u: int,
    reserva_u: int,
    paso: float,
) -> DiagnosticoInviable:
    """Localiza dónde se rompe el trayecto y por cuántos kilómetros."""
    n_nodos = len(coste)
    destino = n_nodos - 1
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
            if j <= i:
                continue
            deficit = distancias_km[i][j] - alcance
            if mejor is None or deficit < mejor[0]:
                mejor = (deficit, i, j)

    if mejor is None:  # no debería pasar, pero el mensaje no puede quedarse mudo
        return DiagnosticoInviable(
            desde="Origen",
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


def _niveles_alcanzados(costes: Sequence[int]) -> list[int]:
    return [nivel for nivel, valor in enumerate(costes) if valor < _INF] or [0]
