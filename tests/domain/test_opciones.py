"""Las opciones que se le ofrecen al conductor (ARQUITECTURA.md §8.6).

Sin BD ni red, como todo el dominio. El coche gasta 10 L/100km, así que 1 litro
son 10 km y las cuentas se comprueban a mano.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain import opciones
from app.domain.dp_optimizer import optimizar_repostaje
from app.domain.models import Desvio, Vehiculo

from .conftest import EstacionEnRuta, escenario

SIN_DESVIO = Desvio(km=0.0, segundos=0.0)


def calcular(coche: Vehiculo, estaciones: list[EstacionEnRuta], largo_km: float):
    candidatas, distancias, duraciones = escenario(estaciones, largo_km=largo_km)
    optima = optimizar_repostaje(coche, candidatas, distancias, duraciones)
    desvios = [
        Desvio(e.desvio_km * 2, e.desvio_s * 2) for e in sorted(estaciones, key=lambda x: x.pk_km)
    ]
    return optima, opciones.calcular(coche, candidatas, desvios, distancias, duraciones, optima)


# ---------------------------------------------------------------------------
# Cuántas y dónde
# ---------------------------------------------------------------------------


def test_un_viaje_de_dos_horas_da_un_punado_de_opciones(coche: Vehiculo) -> None:
    """200 km a 100 km/h son 2 h: se piden 3 o 4 sitios donde elegir parar."""
    _, elegidas = calcular(
        coche,
        [
            EstacionEnRuta(pk_km=40, precio_milesimas=1700),
            EstacionEnRuta(pk_km=80, precio_milesimas=1650),
            EstacionEnRuta(pk_km=120, precio_milesimas=1600),
            EstacionEnRuta(pk_km=160, precio_milesimas=1750),
        ],
        largo_km=200,
    )
    assert 3 <= len(elegidas) <= 4


def test_las_opciones_van_en_el_orden_en_que_se_las_cruza(coche: Vehiculo) -> None:
    _, elegidas = calcular(
        coche,
        [
            EstacionEnRuta(pk_km=50, precio_milesimas=1700),
            EstacionEnRuta(pk_km=100, precio_milesimas=1500),
            EstacionEnRuta(pk_km=150, precio_milesimas=1600),
        ],
        largo_km=250,
    )
    tiempos = [o.tiempo_desde_origen_s for o in elegidas]
    assert tiempos == sorted(tiempos)


def test_no_se_ofrecen_estaciones_fuera_del_alcance(coche: Vehiculo) -> None:
    """Con 20 L y reserva de 5 se recorren 150 km: lo de más allá no es elegible.

    No es una limitación, es la verdad: no se puede elegir parar a las tres horas
    si el depósito se acaba a las dos.
    """
    _, elegidas = calcular(
        coche,
        [
            EstacionEnRuta(pk_km=100, precio_milesimas=1700),
            EstacionEnRuta(pk_km=200, precio_milesimas=1400),
        ],
        largo_km=300,
    )
    assert [o.km_desde_origen for o in elegidas] == [100.0]


# ---------------------------------------------------------------------------
# El sobrecoste, que es la cifra que hace elegir
# ---------------------------------------------------------------------------


def test_se_marca_una_sola_opcion_y_es_la_que_menos_cuesta(coche: Vehiculo) -> None:
    """Marcar "la del plan óptimo" sería marcar algo que no siempre se puede elegir.

    El plan óptimo puede repartir el repostaje en dos paradas y salir más barato
    que cualquier parada única, y entonces ninguna opción tiene sobrecoste cero.
    Lo que se marca es la mejor de las que el usuario sí puede escoger.
    """
    _, elegidas = calcular(
        coche,
        [
            EstacionEnRuta(pk_km=50, precio_milesimas=1700),
            EstacionEnRuta(pk_km=100, precio_milesimas=1500),
            EstacionEnRuta(pk_km=140, precio_milesimas=1600),
        ],
        largo_km=250,
    )
    marcadas = [o for o in elegidas if o.es_la_mas_barata]
    assert len(marcadas) == 1
    assert marcadas[0].sobrecoste_eur == min(o.sobrecoste_eur for o in elegidas)


def test_el_sobrecoste_nunca_es_negativo_y_cuadra_con_su_plan(coche: Vehiculo) -> None:
    """La cuenta que el usuario puede hacer a mano tiene que salir."""
    optima, elegidas = calcular(
        coche,
        [
            EstacionEnRuta(pk_km=50, precio_milesimas=1900),
            EstacionEnRuta(pk_km=100, precio_milesimas=1500),
            EstacionEnRuta(pk_km=140, precio_milesimas=1750),
        ],
        largo_km=250,
    )
    assert elegidas
    for opcion in elegidas:
        assert opcion.sobrecoste_eur >= 0
        assert opcion.sobrecoste_eur == (
            opcion.plan.coste_combustible_eur - optima.coste_combustible_eur
        )


def test_parar_en_la_cara_cuesta_la_diferencia_de_precio(coche: Vehiculo) -> None:
    """Dos estaciones al alcance, mismo depósito que llenar: la cuenta es directa.

    El viaje son 250 km = 25 L; se sale con 20 y hay que llegar con 5, así que se
    compran 10 L. A 1,900 en vez de a 1,500 son 4,00 € más, ni un céntimo de
    "tiempo" por medio.
    """
    _, elegidas = calcular(
        coche,
        [
            EstacionEnRuta(pk_km=50, precio_milesimas=1900),
            EstacionEnRuta(pk_km=100, precio_milesimas=1500),
        ],
        largo_km=250,
    )
    cara = next(o for o in elegidas if o.candidata.precio_milesimas == 1900)
    assert cara.litros == pytest.approx(10.0)
    assert cara.sobrecoste_eur == Decimal("4.00")


def test_cada_opcion_es_una_parada_de_verdad(coche: Vehiculo) -> None:
    """Nada de "echa un litro aquí": si se para, se reposta lo que hace falta."""
    _, elegidas = calcular(
        coche,
        [
            EstacionEnRuta(pk_km=50, precio_milesimas=1800),
            EstacionEnRuta(pk_km=100, precio_milesimas=1500),
            EstacionEnRuta(pk_km=140, precio_milesimas=1600),
        ],
        largo_km=250,
    )
    assert all(o.litros >= 5.0 for o in elegidas)
    assert all(o.plan.paradas for o in elegidas)


def test_si_no_hace_falta_repostar_no_hay_nada_que_elegir(coche: Vehiculo) -> None:
    """Ofrecer paradas a quien llega de sobra sería inventarse una decisión."""
    optima, elegidas = calcular(
        coche,
        [EstacionEnRuta(pk_km=50, precio_milesimas=1500)],
        largo_km=120,
    )
    assert optima.numero_paradas == 0
    assert elegidas == []


# ---------------------------------------------------------------------------
# El reparto por ventanas, aislado
# ---------------------------------------------------------------------------


def test_el_numero_de_ventanas_sigue_a_la_duracion() -> None:
    assert opciones.numero_de_ventanas(2 * 3600) == 3
    assert opciones.numero_de_ventanas(5 * 3600) == 7
    assert opciones.numero_de_ventanas(0) == opciones.MIN_OPCIONES
    assert opciones.numero_de_ventanas(20 * 3600) == opciones.MAX_OPCIONES


def test_si_hay_menos_gasolineras_que_ventanas_se_ofrecen_todas() -> None:
    """Repartir tres entre tres huecos solo serviría para esconder alternativas."""
    candidaturas = [
        opciones.Candidatura(1, 600, Decimal("2.00")),
        opciones.Candidatura(2, 3000, Decimal("0.50")),
        opciones.Candidatura(3, 5400, Decimal("1.00")),
    ]
    assert opciones.repartir_por_ventanas(candidaturas, 7200) == [1, 2, 3]


def test_de_cada_ventana_sale_la_mas_barata() -> None:
    """Con más candidatas que huecos, cada tramo aporta la que menos cuesta."""
    candidaturas = [
        opciones.Candidatura(1, 300, Decimal("2.00")),
        opciones.Candidatura(2, 600, Decimal("0.50")),
        opciones.Candidatura(3, 3000, Decimal("1.00")),
        opciones.Candidatura(4, 3300, Decimal("3.00")),
        opciones.Candidatura(5, 6000, Decimal("0.10")),
    ]
    assert opciones.repartir_por_ventanas(candidaturas, 7200) == [2, 3, 5]


def test_las_ventanas_vacias_no_se_rellenan_con_otra_cosa() -> None:
    """Un tramo largo sin gasolineras es un hueco, y decirlo es mejor que taparlo.

    Cuatro estaciones amontonadas en los primeros minutos de un viaje de cinco
    horas no se convierten en cuatro opciones repartidas: no las hay.
    """
    candidaturas = [
        opciones.Candidatura(1, 300, Decimal("1.00")),
        opciones.Candidatura(2, 320, Decimal("2.00")),
        opciones.Candidatura(3, 340, Decimal("3.00")),
        opciones.Candidatura(4, 360, Decimal("4.00")),
        opciones.Candidatura(5, 380, Decimal("5.00")),
        opciones.Candidatura(6, 400, Decimal("6.00")),
        opciones.Candidatura(7, 420, Decimal("7.00")),
        opciones.Candidatura(8, 440, Decimal("8.00")),
    ]
    elegidas = opciones.repartir_por_ventanas(candidaturas, 5 * 3600)
    assert len(elegidas) < len(candidaturas)
    assert elegidas[0] == 1, "la más barata del primer tramo"


def test_la_del_plan_optimo_entra_aunque_pierda_su_ventana() -> None:
    """La parada del plan óptimo se ofrece siempre, gane o no su tramo."""
    candidaturas = [
        opciones.Candidatura(1, 300, Decimal("1.00")),
        opciones.Candidatura(2, 320, Decimal("2.00")),
        opciones.Candidatura(3, 340, Decimal("3.00")),
        opciones.Candidatura(4, 360, Decimal("4.00")),
        opciones.Candidatura(5, 380, Decimal("5.00")),
        opciones.Candidatura(6, 400, Decimal("6.00")),
        opciones.Candidatura(7, 420, Decimal("7.00")),
        opciones.Candidatura(8, 440, Decimal("8.00")),
    ]
    sin_obligar = opciones.repartir_por_ventanas(candidaturas, 5 * 3600)
    con_obligar = opciones.repartir_por_ventanas(candidaturas, 5 * 3600, obligatorias=[8])

    assert 8 not in sin_obligar
    assert 8 in con_obligar
