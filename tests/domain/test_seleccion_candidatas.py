"""Selección de candidatas: el cuello de botella real del proyecto (§8 punto 1).

Con un ``PriceStore`` de mentira, sin SQLite ni red. Lo que se comprueba no es
que "devuelva algo", sino las tres propiedades de las que depende que el plan
salga bien: que el corredor no se coma media península, que el cupo se reparta
por tramos y que lo que OSRM no resuelva no llegue al DP.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime

import pytest

from app.domain.models import Estacion, EstacionCandidata, Precio, Vehiculo
from app.domain.ports.routing_provider import Coordenada, MatrizRuta, Ruta
from app.domain.seleccion_candidatas import (
    ExtremosSinRuta,
    ParametrosSeleccion,
    depurar_matriz,
    estaciones_cercanas,
    peticiones_estimadas,
    puntos_para_la_matriz,
    seleccionar,
    trocear,
)

T0 = datetime(2026, 8, 12, 7, 0, 0)


def estacion(id_: int, lat: float, lon: float, rotulo: str = "REPSOL") -> Estacion:
    return Estacion(
        id=id_,
        rotulo=rotulo,
        rotulo_raw=rotulo,
        lat=lat,
        lon=lon,
        direccion=f"CALLE {id_}",
        municipio=f"Pueblo {id_}",
    )


def precio(id_: int, milesimas: int) -> Precio:
    return Precio(estacion_id=id_, producto="diesel", precio_milesimas=milesimas, valid_from=T0)


class StoreFalso:
    """Devuelve las estaciones que caen dentro del rectángulo. Nada más."""

    def __init__(self, estaciones: Sequence[tuple[Estacion, Precio]]) -> None:
        self.estaciones = list(estaciones)
        self.consultas: list[tuple[float, float, float, float]] = []

    def estaciones_en_bbox(
        self,
        min_lat,
        min_lon,
        max_lat,
        max_lon,
        producto,
        rotulos=None,
        solo_vigentes=False,
        ahora=None,
    ):
        self.consultas.append((min_lat, min_lon, max_lat, max_lon))
        return [
            (e, p)
            for e, p in self.estaciones
            if min_lat <= e.lat <= max_lat
            and min_lon <= e.lon <= max_lon
            and p.producto == producto
            and (not rotulos or e.rotulo in rotulos)
        ]


def ruta_recta(km: float, puntos: int = 200) -> Ruta:
    """Ruta artificial hacia el este por el paralelo 40, de largo conocido."""
    grados = km / 85.0  # ~85 km por grado de longitud a 40° de latitud
    geometria = tuple(
        Coordenada(lat=40.0, lon=-3.0 + grados * i / (puntos - 1)) for i in range(puntos)
    )
    return Ruta(distancia_km=km, duracion_s=km / 100 * 3600, geometria=geometria)


COCHE = Vehiculo(
    consumo_l_100km=6.5,
    tipo_combustible="diesel",
    capacidad_deposito_l=55.0,
    nivel_actual_l=40.0,
)


# ---------------------------------------------------------------------------
# El corredor
# ---------------------------------------------------------------------------


def test_trocear_parte_la_ruta_en_tramos_del_tamano_pedido() -> None:
    tramos = trocear(ruta_recta(500).geometria, tramo_km=50.0)
    assert 8 <= len(tramos) <= 12  # ~10 tramos de 50 km
    # Los tramos comparten el punto de unión: nada se cuela por la juntura.
    for anterior, siguiente in zip(tramos, tramos[1:], strict=False):
        assert anterior[-1] == siguiente[0]


def test_el_corredor_no_es_el_bbox_de_toda_la_ruta() -> None:
    """Un rectángulo único de Madrid a Barcelona mete dentro media península."""
    store = StoreFalso([])
    seleccionar(store, ruta_recta(600), COCHE, parametros=ParametrosSeleccion(tramo_km=50.0))

    assert len(store.consultas) > 5
    anchuras = [max_lon - min_lon for _, min_lon, _, max_lon in store.consultas]
    # Cada rectángulo cubre su tramo, no los 600 km (~7 grados de longitud).
    assert max(anchuras) < 2.0


def test_solo_entran_las_estaciones_del_corredor() -> None:
    dentro = estacion(1, 40.0, -2.0)
    lejos = estacion(2, 42.5, -2.0)  # 275 km al norte
    store = StoreFalso([(dentro, precio(1, 1500)), (lejos, precio(2, 1000))])

    candidatas = seleccionar(store, ruta_recta(400), COCHE)

    assert [c.estacion.id for c in candidatas] == [1]


# ---------------------------------------------------------------------------
# El recorte por precio
# ---------------------------------------------------------------------------


def test_de_cada_tramo_pasan_las_mas_baratas() -> None:
    caras = [(estacion(i, 40.0, -2.9 + i * 0.001), precio(i, 1900)) for i in range(1, 6)]
    barata = (estacion(99, 40.0, -2.895), precio(99, 1200))
    store = StoreFalso([*caras, barata])

    candidatas = seleccionar(
        store, ruta_recta(300), COCHE, parametros=ParametrosSeleccion(max_candidatas=6)
    )

    assert 99 in [c.estacion.id for c in candidatas]


def test_el_cupo_se_reparte_por_tramos_y_no_se_concentra() -> None:
    """Las N más baratas podrían estar todas juntas y dejar 300 km sin parada.

    Aquí las cinco baratas están al principio y las caras repartidas por el
    resto de la ruta: el reparto por tramos tiene que conservar cobertura hasta
    el final, aunque salgan más caras.
    """
    baratas = [(estacion(i, 40.0, -2.99 + i * 0.002), precio(i, 1100)) for i in range(1, 6)]
    lejanas = [(estacion(50 + i, 40.0, -2.0 + i * 0.9), precio(50 + i, 1800)) for i in range(5)]
    store = StoreFalso([*baratas, *lejanas])

    candidatas = seleccionar(
        store, ruta_recta(600), COCHE, parametros=ParametrosSeleccion(max_candidatas=20)
    )

    longitudes = [c.estacion.lon for c in candidatas]
    assert min(longitudes) < -2.9, "faltan candidatas al principio de la ruta"
    assert max(longitudes) > 0.0, "faltan candidatas al final de la ruta"


def test_las_candidatas_salen_ordenadas_por_avance() -> None:
    revueltas = [
        (estacion(1, 40.0, 1.0), precio(1, 1500)),
        (estacion(2, 40.0, -2.5), precio(2, 1500)),
        (estacion(3, 40.0, -0.5), precio(3, 1500)),
    ]
    store = StoreFalso(revueltas)

    candidatas = seleccionar(store, ruta_recta(600), COCHE)

    longitudes = [c.estacion.lon for c in candidatas]
    assert longitudes == sorted(longitudes)


def test_el_filtro_de_marca_llega_al_store() -> None:
    store = StoreFalso(
        [
            (estacion(1, 40.0, -2.0, "REPSOL"), precio(1, 1500)),
            (estacion(2, 40.0, -1.9, "MOEVE"), precio(2, 1400)),
        ]
    )

    candidatas = seleccionar(store, ruta_recta(400), COCHE, rotulos=["MOEVE"])

    assert [c.estacion.rotulo for c in candidatas] == ["MOEVE"]


# ---------------------------------------------------------------------------
# Depurar: lo que OSRM no supo resolver (§8.4) y lo que se desvía de más (§8.2)
# ---------------------------------------------------------------------------


def candidata(id_: int, lon: float) -> EstacionCandidata:
    return EstacionCandidata(estacion(id_, 40.0, lon), precio(id_, 1500))


def matriz_de(distancias: list[list[float]], sin_respuesta=()) -> MatrizRuta:
    return MatrizRuta(
        distancias_km=tuple(tuple(f) for f in distancias),
        duraciones_s=tuple(tuple(v * 36 for v in f) for f in distancias),
        indices_sin_respuesta=tuple(sin_respuesta),
    )


def test_depurar_quita_las_candidatas_sin_respuesta() -> None:
    candidatas = [candidata(1, -2.0), candidata(2, -1.0)]
    inf = math.inf
    matriz = matriz_de(
        [
            [0, 100, inf, 300],
            [100, 0, inf, 200],
            [inf, inf, 0, inf],
            [300, 200, inf, 0],
        ],
        sin_respuesta=(2,),
    )

    depurada = depurar_matriz(candidatas, matriz)

    assert [c.estacion.id for c in depurada.candidatas] == [1]
    assert [c.estacion.id for c in depurada.descartadas] == [2]
    # La matriz queda 3x3: origen, la candidata buena y destino.
    assert len(depurada.distancias_km) == 3
    assert depurada.distancias_km[0][1] == 100
    assert depurada.distancias_km[1][2] == 200


def test_depurar_reordena_por_distancia_real_desde_el_origen() -> None:
    """El orden por tramos es una estimación; el kilometraje real manda."""
    candidatas = [candidata(1, -2.0), candidata(2, -1.0)]
    matriz = matriz_de(
        [
            [0, 250, 90, 400],
            [250, 0, 160, 150],
            [90, 160, 0, 310],
            [400, 150, 310, 0],
        ]
    )

    depurada = depurar_matriz(candidatas, matriz)

    assert [c.estacion.id for c in depurada.candidatas] == [2, 1]
    assert depurada.distancias_km[0][1] == 90


@pytest.mark.parametrize("extremo", [0, 3])
def test_si_el_extremo_no_tiene_ruta_no_hay_nada_que_optimizar(extremo: int) -> None:
    candidatas = [candidata(1, -2.0), candidata(2, -1.0)]
    matriz = matriz_de([[0.0] * 4 for _ in range(4)], sin_respuesta=(extremo,))

    with pytest.raises(ExtremosSinRuta):
        depurar_matriz(candidatas, matriz)


# ---------------------------------------------------------------------------
# Estaciones cercanas (§7)
# ---------------------------------------------------------------------------


def test_las_cercanas_salen_ordenadas_por_distancia() -> None:
    store = StoreFalso(
        [
            (estacion(1, 40.02, -3.0), precio(1, 1500)),  # ~2 km
            (estacion(2, 40.001, -3.0), precio(2, 1900)),  # ~100 m
            (estacion(3, 40.01, -3.0), precio(3, 1400)),  # ~1 km
        ]
    )

    cercanas = estaciones_cercanas(store, Coordenada(40.0, -3.0), "diesel")

    assert [c.estacion.id for c in cercanas] == [2, 3, 1]
    assert cercanas[0].distancia_km < cercanas[-1].distancia_km
    assert cercanas[0].distancia_km == pytest.approx(0.11, abs=0.05)


def test_las_cercanas_ensanchan_el_radio_hasta_encontrar_algo() -> None:
    """A 60 km no hay nada cerca, pero tampoco se devuelve una lista vacía."""
    store = StoreFalso([(estacion(1, 40.55, -3.0), precio(1, 1500))])

    cercanas = estaciones_cercanas(store, Coordenada(40.0, -3.0), "diesel")

    assert [c.estacion.id for c in cercanas] == [1]
    assert len(store.consultas) > 1  # ha tenido que ampliar


def test_sin_estaciones_en_ningun_radio_devuelve_vacio() -> None:
    assert estaciones_cercanas(StoreFalso([]), Coordenada(40.0, -3.0), "diesel") == []


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def test_puntos_para_la_matriz_respeta_el_convenio_del_dp() -> None:
    origen, destino = Coordenada(40.0, -3.0), Coordenada(41.0, 2.0)
    puntos = puntos_para_la_matriz(origen, [candidata(1, -2.0), candidata(2, -1.0)], destino)

    assert puntos[0] == origen
    assert puntos[-1] == destino
    assert [p.lon for p in puntos[1:-1]] == [-2.0, -1.0]


@pytest.mark.parametrize(
    ("puntos", "esperado"),
    [(52, 4), (2, 1), (50, 1), (252, 36)],
)
def test_peticiones_estimadas(puntos: int, esperado: int) -> None:
    """Sirve para saber si al usuario le vas a hacer esperar 3 s o 30."""
    assert peticiones_estimadas(puntos) == esperado


# ---------------------------------------------------------------------------
# El límite de desvío, que es lo que sustituye al precio de la hora (§8.2)
# ---------------------------------------------------------------------------


def test_el_desvio_se_mide_contra_la_ruta_directa() -> None:
    """Ir por la estación menos ir de largo. Ni línea recta ni perpendiculares."""
    candidatas = [candidata(1, -2.0)]
    matriz = matriz_de(
        [
            [0, 110, 300],
            [110, 0, 200],
            [300, 200, 0],
        ]
    )

    depurada = depurar_matriz(candidatas, matriz, max_desvio_km=20.0)

    # 110 de ida + 200 de vuelta - 300 de largo = 10 km.
    assert depurada.desvios[0].km == pytest.approx(10.0)
    assert depurada.desvios[0].minutos == pytest.approx(6.0)


def test_la_estacion_demasiado_desviada_no_llega_al_dp() -> None:
    """La barata a la que hay que salirse 15 km no es una opción, por barata que sea."""
    candidatas = [candidata(1, -2.0), candidata(2, -1.0)]
    matriz = matriz_de(
        [
            [0, 130, 160, 300],
            [130, 0, 40, 200],
            [160, 40, 0, 145],
            [300, 200, 145, 0],
        ]
    )

    depurada = depurar_matriz(candidatas, matriz, max_desvio_km=10.0)

    # La 1 desvía 30 km (130 + 200 - 300); la 2, 5 km (160 + 145 - 300).
    assert [c.estacion.id for c in depurada.candidatas] == [2]
    assert [c.estacion.id for c in depurada.descartadas_por_desvio] == [1]
    assert depurada.descartadas == []


def test_el_tope_en_minutos_caza_lo_que_los_kilometros_no_ven() -> None:
    """Un kilómetro fuera de la vía, pero diez minutos por dentro del pueblo.

    Es el caso del que avisa §8: en kilómetros no se nota y el combustible extra
    no compensa el ahorro, así que con el objetivo en euros puros el DP se iría a
    ella. Lo que la deja fuera es el tope de tiempo, no un precio de la hora.
    """
    candidatas = [candidata(1, -2.0)]
    distancias = [
        [0, 151, 300],
        [151, 0, 150],
        [300, 150, 0],
    ]
    matriz = MatrizRuta(
        distancias_km=tuple(tuple(f) for f in distancias),
        # Ir y volver del surtidor cuesta 10 minutos por sentido.
        duraciones_s=(
            (0, 151 * 36 + 600, 300 * 36),
            (151 * 36 + 600, 0, 150 * 36 + 600),
            (300 * 36, 150 * 36 + 600, 0),
        ),
        indices_sin_respuesta=(),
    )

    holgado = depurar_matriz(candidatas, matriz, max_desvio_km=10.0, max_desvio_min=60.0)
    apretado = depurar_matriz(candidatas, matriz, max_desvio_km=10.0, max_desvio_min=15.0)

    assert [c.estacion.id for c in holgado.candidatas] == [1]
    assert apretado.candidatas == []
    assert [c.estacion.id for c in apretado.descartadas_por_desvio] == [1]


def test_el_corredor_se_ensancha_para_no_tirar_lo_que_el_filtro_fino_admitiria() -> None:
    estrecho = ParametrosSeleccion(margen_km=5.0, max_desvio_km=10.0)
    ancho = ParametrosSeleccion(margen_km=5.0, max_desvio_km=40.0)

    assert estrecho.margen_efectivo_km == 5.0
    assert ancho.margen_efectivo_km == 20.0, "un desvío de 40 km es 20 km de vía"
