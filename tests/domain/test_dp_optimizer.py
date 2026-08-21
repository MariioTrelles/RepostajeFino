"""Tests del DP, con estaciones y matrices inventadas a mano.

Sin BD, sin red, sin OSRM. Los números están elegidos para poder comprobarlos a
mano: el coche gasta 10 L/100km, así que 1 litro = 10 km. El paso de
discretización es de 0,25 L (§8.1), o sea 2,5 km, y el consumo de cada tramo se
redondea siempre hacia arriba: por eso alguna cuenta sale en cuartos de litro.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.dp_optimizer import (
    ParametrosOptimizacion,
    TrayectoInviable,
    VehiculoInvalido,
    optimizar_repostaje,
    sobrecoste_por_candidata,
)
from app.domain.models import Vehiculo
from app.domain.precio_efectivo import PerfilDescuento

from .conftest import EstacionEnRuta, escenario

# ---------------------------------------------------------------------------
# Caso simple: elegir la combinación más barata
# ---------------------------------------------------------------------------


def test_elige_la_estacion_mas_barata_al_alcance(coche: Vehiculo) -> None:
    """4 estaciones, ninguna desviada: hay que comprar todo en la más barata.

    El viaje son 300 km = 30 L. Se sale con 20 L y hay que llegar con 5, así que
    hay que comprar exactamente 15 L. Comprarlos al mejor precio alcanzable
    (1,400) da 21,00 €, y es cota inferior: no hay plan mejor.
    """
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=50, precio_milesimas=1900),
            EstacionEnRuta(pk_km=100, precio_milesimas=1400),
            EstacionEnRuta(pk_km=150, precio_milesimas=1700),
            EstacionEnRuta(pk_km=200, precio_milesimas=1850),
        ],
        largo_km=300,
    )

    plan = optimizar_repostaje(coche, candidatas, distancias, duraciones)

    assert plan.numero_paradas == 1
    parada = plan.paradas[0]
    assert parada.candidata.precio_milesimas == 1400
    assert parada.km_desde_origen == pytest.approx(100.0)
    assert parada.litros == pytest.approx(15.0)
    assert plan.coste_combustible_eur == Decimal("21")
    assert plan.nivel_llegada_destino_l == pytest.approx(5.0)


def test_no_para_si_llega_con_lo_que_lleva(coche: Vehiculo) -> None:
    candidatas, distancias, duraciones = escenario(
        [EstacionEnRuta(pk_km=50, precio_milesimas=1400)], largo_km=100
    )

    plan = optimizar_repostaje(coche, candidatas, distancias, duraciones)

    assert plan.paradas == []
    assert plan.coste_combustible_eur == Decimal(0)
    assert plan.litros_repostados == 0.0
    assert plan.nivel_llegada_destino_l == pytest.approx(10.0)


def test_desempata_por_una_milesima(coche: Vehiculo) -> None:
    """Dos estaciones idénticas salvo una milésima: gana la barata, siempre.

    Es la razón de que el DP trabaje en enteros. Con float, este desempate
    depende de por dónde haya pasado la acumulación (§4).
    """
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=100, precio_milesimas=1699),
            EstacionEnRuta(pk_km=100, precio_milesimas=1698),
        ],
        largo_km=200,
    )

    plan = optimizar_repostaje(coche, candidatas, distancias, duraciones)

    assert plan.numero_paradas == 1
    assert plan.paradas[0].candidata.precio_milesimas == 1698


# ---------------------------------------------------------------------------
# Reserva mínima: restricción dura del dominio
# ---------------------------------------------------------------------------


def test_nunca_llega_a_una_estacion_por_debajo_de_la_reserva(coche: Vehiculo) -> None:
    """El óptimo matemático llegaría con el depósito a cero. Inaceptable (§7)."""
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=150, precio_milesimas=1400),
            EstacionEnRuta(pk_km=280, precio_milesimas=1600),
        ],
        largo_km=400,
    )

    plan = optimizar_repostaje(coche, candidatas, distancias, duraciones)

    assert plan.paradas
    for parada in plan.paradas:
        assert parada.nivel_llegada_l >= coche.reserva_minima_l
    for tramo in plan.tramos:
        assert tramo.nivel_llegada_l >= coche.reserva_minima_l
    assert plan.nivel_llegada_destino_l >= coche.reserva_minima_l


def test_la_reserva_aprieta_de_verdad(coche: Vehiculo) -> None:
    """Con reserva 0 el mismo trayecto se resuelve con menos combustible.

    Si el plan fuera idéntico con y sin reserva, la restricción no estaría
    haciendo nada y el test anterior pasaría por casualidad.
    """
    candidatas, distancias, duraciones = escenario(
        [EstacionEnRuta(pk_km=150, precio_milesimas=1400)], largo_km=400
    )

    con_reserva = optimizar_repostaje(coche, candidatas, distancias, duraciones)

    sin_reserva = optimizar_repostaje(
        Vehiculo(
            consumo_l_100km=coche.consumo_l_100km,
            tipo_combustible=coche.tipo_combustible,
            capacidad_deposito_l=coche.capacidad_deposito_l,
            nivel_actual_l=coche.nivel_actual_l,
            reserva_minima_l=0.0,
        ),
        candidatas,
        distancias,
        duraciones,
    )

    assert con_reserva.litros_repostados > sin_reserva.litros_repostados
    assert con_reserva.nivel_llegada_destino_l == pytest.approx(5.0)
    assert sin_reserva.nivel_llegada_destino_l == pytest.approx(0.0)


@pytest.mark.parametrize("reserva", [0.0, 5.0, 12.0, 20.0])
def test_la_reserva_se_respeta_sea_cual_sea(reserva: float) -> None:
    coche = Vehiculo(
        consumo_l_100km=10.0,
        tipo_combustible="diesel",
        capacidad_deposito_l=50.0,
        nivel_actual_l=45.0,
        reserva_minima_l=reserva,
    )
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=120, precio_milesimas=1700),
            EstacionEnRuta(pk_km=240, precio_milesimas=1450),
            EstacionEnRuta(pk_km=360, precio_milesimas=1800),
        ],
        largo_km=500,
    )

    plan = optimizar_repostaje(coche, candidatas, distancias, duraciones)

    for parada in plan.paradas:
        assert parada.nivel_llegada_l >= reserva
        assert parada.nivel_salida_l <= coche.capacidad_deposito_l
    assert plan.nivel_llegada_destino_l >= reserva


# ---------------------------------------------------------------------------
# Repostaje parcial: donde el algoritmo aporta valor de verdad
# ---------------------------------------------------------------------------


def test_repostar_solo_lo_justo_en_la_cara_y_completar_en_la_barata() -> None:
    """Llenar en la primera estación es peor que echar lo justo para llegar a la barata.

    Trayecto de 250 km. Se sale con 10 L (50 km útiles), justo para llegar a A.

        A (PK 50, 1,800 €/L)  ->  echa 10 L, lo justo para alcanzar B
        B (PK 150, 1,500 €/L) ->  echa 10 L, lo justo para llegar al destino

    Coste: 10 x 1,80 + 10 x 1,50 = 33,00 €.
    Llenar el depósito en A costaría 45 L x 1,80 = 81,00 €.
    """
    coche = Vehiculo(
        consumo_l_100km=10.0,
        tipo_combustible="diesel",
        capacidad_deposito_l=50.0,
        nivel_actual_l=10.0,
        reserva_minima_l=5.0,
    )
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=50, precio_milesimas=1800),
            EstacionEnRuta(pk_km=150, precio_milesimas=1500),
        ],
        largo_km=250,
    )

    plan = optimizar_repostaje(coche, candidatas, distancias, duraciones)

    assert plan.numero_paradas == 2
    cara, barata = plan.paradas
    assert cara.candidata.precio_milesimas == 1800
    assert cara.litros == pytest.approx(10.0)
    assert barata.candidata.precio_milesimas == 1500
    assert barata.litros == pytest.approx(10.0)

    # Lo importante: en la cara NO llena el depósito.
    assert cara.nivel_salida_l == pytest.approx(15.0)
    assert cara.nivel_salida_l < coche.capacidad_deposito_l

    assert plan.coste_combustible_eur == Decimal("33.00")


def test_prefiere_dos_paradas_baratas_a_una_cara() -> None:
    """La alternativa de una sola parada existe y es viable, pero sale más cara.

    Parar solo en A: 20 L x 1,80 = 36,00 €.
    Parar en A y B:  10 L x 1,80 + 10 L x 1,50 = 33,00 €.

    Con el objetivo en euros de combustible (§8.2), 33 < 36 y gana el de dos
    paradas. Los cinco minutos de la segunda parada están en la componente de
    tiempo, que solo desempata: no compiten con tres euros.
    """
    coche = Vehiculo(
        consumo_l_100km=10.0,
        tipo_combustible="diesel",
        capacidad_deposito_l=50.0,
        nivel_actual_l=10.0,
        reserva_minima_l=5.0,
    )
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=50, precio_milesimas=1800),
            EstacionEnRuta(pk_km=150, precio_milesimas=1500),
        ],
        largo_km=250,
    )

    plan = optimizar_repostaje(coche, candidatas, distancias, duraciones)

    assert plan.numero_paradas == 2
    assert plan.coste_combustible_eur == Decimal("33.00")


# ---------------------------------------------------------------------------
# Coste del desvío
# ---------------------------------------------------------------------------


def test_el_dp_no_arbitra_el_desvio_lento(coche: Vehiculo) -> None:
    """La estación a 1 km de la vía pero a 10 minutos por sentido: el DP se va a ella.

    Y hace bien, porque a él ya no le toca decidirlo. Con el objetivo en euros
    puros (§8.2) desviarse sale a cuenta: 0,2 L de más contra 0,70 € de ahorro.
    Lo que antes lo impedía era ponerle precio a la hora del conductor; lo que lo
    impide ahora es que una estación así **no llega al DP**, porque el límite de
    desvío la deja fuera antes (ver ``test_seleccion_candidatas``).

    El test existe para dejarlo dicho: si alguien vuelve a meter tiempo en la
    función objetivo, este test se cae y hay que releer §8.2 antes de tocarlo.
    """
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=100, precio_milesimas=1700),
            EstacionEnRuta(pk_km=105, precio_milesimas=1300, desvio_km=1.0, desvio_s=600.0),
        ],
        largo_km=200,
    )

    plan = optimizar_repostaje(coche, candidatas, distancias, duraciones)

    assert plan.numero_paradas == 1
    assert plan.paradas[0].candidata.precio_milesimas == 1300
    # 202 km son 20,2 L; se sale con 20 y hay que llegar con 5, así que se compran
    # 5,2 L redondeados al alza al paso de 0,25: 5,5 L a 1,300.
    assert plan.coste_combustible_eur == Decimal("7.15")


def test_si_el_ahorro_compensa_el_desvio_se_desvia(coche: Vehiculo) -> None:
    """Misma estructura, pero con una diferencia de precio que sí paga el desvío."""
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=100, precio_milesimas=1700),
            EstacionEnRuta(pk_km=105, precio_milesimas=1200, desvio_km=1.0, desvio_s=60.0),
        ],
        largo_km=200,
    )

    plan = optimizar_repostaje(coche, candidatas, distancias, duraciones)

    assert plan.numero_paradas == 1
    assert plan.paradas[0].candidata.precio_milesimas == 1200
    assert plan.desvio_km == pytest.approx(2.0)
    # 60 s por sentido de desvío más los 300 s de repostar (§8.2).
    assert plan.desvio_s == pytest.approx(420.0)


# ---------------------------------------------------------------------------
# Precio efectivo: el DP nunca optimiza sobre el precio nominal (§6.1)
# ---------------------------------------------------------------------------


def _dos_estaciones():
    """La barata de cara al Geoportal está en el PK 100; la cara, en el 150."""
    return escenario(
        [
            EstacionEnRuta(pk_km=100, precio_milesimas=1700, rotulo="SIN_DESCUENTO"),
            EstacionEnRuta(pk_km=150, precio_milesimas=1900, rotulo="CON_DESCUENTO"),
        ],
        largo_km=300,
    )


def test_sin_descuentos_el_efectivo_es_el_nominal(coche: Vehiculo) -> None:
    candidatas, distancias, duraciones = _dos_estaciones()

    plan = optimizar_repostaje(coche, candidatas, distancias, duraciones)

    parada = plan.paradas[0]
    assert parada.candidata.estacion.rotulo == "SIN_DESCUENTO"
    assert parada.precio_efectivo_milesimas == parada.candidata.precio_milesimas == 1700


def test_un_descuento_puede_cambiar_la_estacion_elegida(
    coche: Vehiculo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La prueba de que la abstracción sirve: fase 2 no toca el DP.

    Se sustituye `precio_efectivo` por una implementación con descuentos, como
    la que traerá la fase 2, y el plan cambia de estación sin haber cambiado una
    línea del optimizador. Es justo el argumento de §6.1 para escribir la
    función antes de que haga nada.
    """

    def con_descuento(estacion, precio_nominal, usuario=None):
        return 1000 if estacion.rotulo == "CON_DESCUENTO" else precio_nominal

    monkeypatch.setattr("app.domain.dp_optimizer.precio_efectivo", con_descuento)
    candidatas, distancias, duraciones = _dos_estaciones()

    plan = optimizar_repostaje(coche, candidatas, distancias, duraciones, usuario=PerfilDescuento())

    parada = plan.paradas[0]
    assert parada.candidata.estacion.rotulo == "CON_DESCUENTO"
    # El nominal se conserva; la cuenta se hace con el efectivo.
    assert parada.candidata.precio_milesimas == 1900
    assert parada.precio_efectivo_milesimas == 1000
    assert parada.litros == pytest.approx(15.0)
    assert parada.coste_eur == Decimal("15.00")
    assert plan.coste_combustible_eur == Decimal("15.00")


# ---------------------------------------------------------------------------
# Trayecto inviable: avisar con contexto, no devolver "sin solución"
# ---------------------------------------------------------------------------


def test_primer_tramo_por_encima_de_la_autonomia_sin_estaciones(coche: Vehiculo) -> None:
    candidatas, distancias, duraciones = escenario([], largo_km=500)

    with pytest.raises(TrayectoInviable) as excinfo:
        optimizar_repostaje(coche, candidatas, distancias, duraciones)

    diag = excinfo.value.diagnostico
    assert diag.desde == "Origen"
    assert diag.hasta == "Destino"
    assert diag.distancia_km == pytest.approx(500.0)
    assert diag.autonomia_km == pytest.approx(150.0)
    assert diag.faltan_km == pytest.approx(350.0)
    assert diag.candidatas_totales == 0
    assert "ninguna estación candidata" in str(excinfo.value)


def test_primer_tramo_por_encima_de_la_autonomia_con_estaciones_lejanas(
    coche: Vehiculo,
) -> None:
    """Se sale con 150 km de autonomía y la primera estación está a 200 km."""
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=200, precio_milesimas=1500),
            EstacionEnRuta(pk_km=400, precio_milesimas=1400),
        ],
        largo_km=600,
    )

    with pytest.raises(TrayectoInviable) as excinfo:
        optimizar_repostaje(coche, candidatas, distancias, duraciones)

    diag = excinfo.value.diagnostico
    assert diag.desde == "Origen"
    assert diag.distancia_km == pytest.approx(200.0)
    assert diag.autonomia_km == pytest.approx(150.0)
    assert diag.faltan_km == pytest.approx(50.0)
    assert diag.candidatas_totales == 2
    assert diag.candidatas_alcanzables == 0
    assert "ampliar el radio de búsqueda" in str(excinfo.value)


def test_el_hueco_inviable_puede_estar_a_mitad_de_ruta(coche: Vehiculo) -> None:
    """No siempre falla el primer tramo: aquí se llega a la primera y ahí se acaba.

    Con el depósito lleno son 450 km de autonomía, y el siguiente punto está a
    500 km. El aviso tiene que señalar ese hueco, no el origen.
    """
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=100, precio_milesimas=1500),
            EstacionEnRuta(pk_km=600, precio_milesimas=1400),
        ],
        largo_km=700,
    )

    with pytest.raises(TrayectoInviable) as excinfo:
        optimizar_repostaje(coche, candidatas, distancias, duraciones)

    diag = excinfo.value.diagnostico
    assert diag.desde.endswith("(PK 100)")
    assert diag.hasta.endswith("(PK 600)")
    assert diag.distancia_km == pytest.approx(500.0)
    assert diag.autonomia_km == pytest.approx(450.0)
    assert diag.faltan_km == pytest.approx(50.0)
    assert diag.candidatas_alcanzables == 1


# ---------------------------------------------------------------------------
# Validación del vehículo
# ---------------------------------------------------------------------------


def test_nivel_por_encima_de_la_capacidad(coche: Vehiculo) -> None:
    malo = Vehiculo(
        consumo_l_100km=10.0,
        tipo_combustible="diesel",
        capacidad_deposito_l=50.0,
        nivel_actual_l=60.0,
        reserva_minima_l=5.0,
    )
    candidatas, distancias, _ = escenario([], largo_km=100)

    with pytest.raises(VehiculoInvalido, match="no puede superar la capacidad"):
        optimizar_repostaje(malo, candidatas, distancias)


def test_nivel_por_debajo_de_la_reserva_se_avisa_aparte() -> None:
    """No es un trayecto inviable: es que la pregunta correcta es otra."""
    malo = Vehiculo(
        consumo_l_100km=10.0,
        tipo_combustible="diesel",
        capacidad_deposito_l=50.0,
        nivel_actual_l=3.0,
        reserva_minima_l=5.0,
    )
    candidatas, distancias, _ = escenario(
        [EstacionEnRuta(pk_km=10, precio_milesimas=1500)], largo_km=100
    )

    with pytest.raises(VehiculoInvalido, match="por debajo de la reserva"):
        optimizar_repostaje(malo, candidatas, distancias)


@pytest.mark.parametrize(
    ("consumo", "capacidad", "reserva"),
    [(0.0, 50.0, 5.0), (-5.0, 50.0, 5.0), (10.0, 0.0, 5.0), (10.0, 50.0, 50.0)],
)
def test_vehiculos_incoherentes(consumo: float, capacidad: float, reserva: float) -> None:
    malo = Vehiculo(
        consumo_l_100km=consumo,
        tipo_combustible="diesel",
        capacidad_deposito_l=capacidad,
        nivel_actual_l=min(10.0, capacidad),
        reserva_minima_l=reserva,
    )
    candidatas, distancias, _ = escenario([], largo_km=100)

    with pytest.raises(VehiculoInvalido):
        optimizar_repostaje(malo, candidatas, distancias)


def test_matriz_de_tamano_incorrecto(coche: Vehiculo) -> None:
    candidatas, distancias, _ = escenario(
        [EstacionEnRuta(pk_km=50, precio_milesimas=1500)], largo_km=100
    )

    with pytest.raises(ValueError, match="3x3"):
        optimizar_repostaje(coche, candidatas, [[0.0, 100.0], [0.0, 0.0]])


# ---------------------------------------------------------------------------
# Discretización
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("paso", [0.5, 1.0, 2.0, 5.0])
def test_el_paso_de_discretizacion_no_rompe_nada(coche: Vehiculo, paso: float) -> None:
    """§8.1: subir el paso solo afecta al rendimiento, no a la lógica.

    Lo que no puede pasar bajo ningún paso es prometer un plan que en la
    realidad se quede sin combustible: los redondeos van siempre del lado seguro.
    """
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=90, precio_milesimas=1750),
            EstacionEnRuta(pk_km=190, precio_milesimas=1490),
            EstacionEnRuta(pk_km=310, precio_milesimas=1830),
        ],
        largo_km=430,
    )

    plan = optimizar_repostaje(
        coche,
        candidatas,
        distancias,
        duraciones,
        parametros=ParametrosOptimizacion(paso_discretizacion_l=paso),
    )

    assert plan.paradas
    for parada in plan.paradas:
        assert parada.nivel_llegada_l >= coche.reserva_minima_l
        assert parada.nivel_salida_l <= coche.capacidad_deposito_l
    assert plan.nivel_llegada_destino_l >= coche.reserva_minima_l


def test_un_paso_mas_fino_nunca_sale_mas_caro(coche: Vehiculo) -> None:
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=90, precio_milesimas=1750),
            EstacionEnRuta(pk_km=190, precio_milesimas=1490),
        ],
        largo_km=380,
    )

    fino = optimizar_repostaje(
        coche,
        candidatas,
        distancias,
        duraciones,
        parametros=ParametrosOptimizacion(paso_discretizacion_l=0.5),
    )
    grueso = optimizar_repostaje(
        coche,
        candidatas,
        distancias,
        duraciones,
        parametros=ParametrosOptimizacion(paso_discretizacion_l=5.0),
    )

    assert fino.coste_combustible_eur <= grueso.coste_combustible_eur


# ---------------------------------------------------------------------------
# Contabilidad del plan
# ---------------------------------------------------------------------------


def test_el_plan_cuadra_consigo_mismo(coche: Vehiculo) -> None:
    """Balance de combustible: lo que entra menos lo que se gasta es lo que queda."""
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=100, precio_milesimas=1700),
            EstacionEnRuta(pk_km=250, precio_milesimas=1450),
            EstacionEnRuta(pk_km=380, precio_milesimas=1900),
        ],
        largo_km=500,
    )

    plan = optimizar_repostaje(coche, candidatas, distancias, duraciones)

    consumido = coche.litros_para(plan.distancia_total_km)
    esperado = coche.nivel_actual_l + plan.litros_repostados - consumido
    assert plan.nivel_llegada_destino_l == pytest.approx(esperado, abs=1.0)

    assert plan.litros_repostados == pytest.approx(sum(p.litros for p in plan.paradas))
    assert plan.coste_combustible_eur == sum((p.coste_eur for p in plan.paradas), Decimal(0))
    assert plan.distancia_total_km == pytest.approx(sum(t.distancia_km for t in plan.tramos))
    # La duración del plan es conducir *más* repostar (§8.2).
    conduccion = sum(t.duracion_s for t in plan.tramos)
    assert plan.duracion_total_s == pytest.approx(conduccion + plan.numero_paradas * 300.0)
    assert plan.tramos[0].desde == "Origen"
    assert plan.tramos[-1].hasta == "Destino"


def test_funciona_sin_matriz_de_duraciones(coche: Vehiculo) -> None:
    """Sin duraciones el DP sigue resolviendo; solo se pierde el desempate por tiempo."""
    candidatas, distancias, _ = escenario(
        [EstacionEnRuta(pk_km=100, precio_milesimas=1500)], largo_km=300
    )

    plan = optimizar_repostaje(coche, candidatas, distancias)

    assert plan.numero_paradas == 1
    # Sin conducción cronometrada solo queda el rato de repostar: una parada, 300 s.
    assert plan.duracion_total_s == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# Repostaje mínimo: el DP no puede proponer paradas que nadie haría (§8.1)
# ---------------------------------------------------------------------------


def _escenario_del_dedal() -> tuple[list, list[list[float]], list[list[float]]]:
    """El caso que hace falta un mínimo: una cara justo antes de una barata lejos.

    Son 300 km. Con 20 L y 5 L de reserva se llega al km 150 y ni un metro más,
    así que la barata del km 155 queda a 5 km de distancia inalcanzable. Sin
    mínimo, al DP le sale rentabilísimo entrar en la cara del km 145 a echar
    medio litro —lo justo para alcanzar la barata— y llenar allí.
    """
    return escenario(
        [
            EstacionEnRuta(pk_km=145, precio_milesimas=1900),
            EstacionEnRuta(pk_km=155, precio_milesimas=1000),
        ],
        largo_km=300,
    )


def test_sin_minimo_el_dp_propone_una_parada_de_medio_litro(coche: Vehiculo) -> None:
    """El comportamiento de antes, que es lo que el mínimo viene a corregir.

    Sirve de doble check: fija el óptimo sin restringir (15,45 €) para que el
    test siguiente pueda enseñar exactamente lo que cuesta la restricción.
    """
    candidatas, distancias, duraciones = _escenario_del_dedal()

    plan = optimizar_repostaje(
        coche, candidatas, distancias, duraciones,
        ParametrosOptimizacion(repostaje_minimo_l=0.0),
    )

    assert plan.numero_paradas == 2
    dedal = plan.paradas[0]
    assert dedal.km_desde_origen == pytest.approx(145.0)
    assert dedal.litros == pytest.approx(0.5)
    assert plan.coste_combustible_eur == Decimal("15.45")


def test_el_minimo_destierra_las_paradas_simbolicas(coche: Vehiculo) -> None:
    """Con mínimo, la parada del km 145 pasa de 0,5 L a 5 L y el plan sigue en pie.

    Encarece el viaje de 15,45 € a 19,50 €, y eso está bien: los 4,05 € de
    diferencia compraban un plan que en la carretera nadie iba a ejecutar.
    """
    candidatas, distancias, duraciones = _escenario_del_dedal()

    plan = optimizar_repostaje(
        coche, candidatas, distancias, duraciones,
        ParametrosOptimizacion(repostaje_minimo_l=5.0),
    )

    assert plan.numero_paradas == 2
    assert all(parada.litros >= 5.0 for parada in plan.paradas)
    assert plan.paradas[0].litros == pytest.approx(5.0)
    assert plan.paradas[1].litros == pytest.approx(10.0)
    assert plan.coste_combustible_eur == Decimal("19.50")
    # La restricción no puede colarse en el balance del depósito.
    assert plan.nivel_llegada_destino_l == pytest.approx(5.0)


def test_el_minimo_por_debajo_del_paso_es_no_tener_minimo(coche: Vehiculo) -> None:
    """`min_u = 1` tiene que dar la tabla de siempre: el cambio solo generaliza."""
    candidatas, distancias, duraciones = _escenario_del_dedal()

    sin_minimo = optimizar_repostaje(
        coche, candidatas, distancias, duraciones,
        ParametrosOptimizacion(repostaje_minimo_l=0.0),
    )
    un_paso = optimizar_repostaje(
        coche, candidatas, distancias, duraciones,
        # Por debajo del paso de discretización no hay mínimo que imponer.
        ParametrosOptimizacion(repostaje_minimo_l=0.25),
    )

    assert un_paso.coste_combustible_eur == sin_minimo.coste_combustible_eur
    assert [p.litros for p in un_paso.paradas] == [p.litros for p in sin_minimo.paradas]


def test_no_se_ofrece_parar_donde_no_caben_los_litros_minimos() -> None:
    """Con el depósito casi lleno, parar en la primera deja de ser una opción.

    Sale con 48 de 50 L: al llegar al km 20 le caben 2 L, menos del mínimo. La
    respuesta correcta no es "para y echa 2 L", es que ahí no se para.
    """
    casi_lleno = Vehiculo(
        consumo_l_100km=10.0,
        tipo_combustible="diesel",
        capacidad_deposito_l=50.0,
        nivel_actual_l=48.0,
        reserva_minima_l=5.0,
    )
    candidatas, distancias, duraciones = escenario(
        [
            EstacionEnRuta(pk_km=20, precio_milesimas=1500),
            EstacionEnRuta(pk_km=200, precio_milesimas=1400),
        ],
        largo_km=300,
    )

    sobrecostes = sobrecoste_por_candidata(
        casi_lleno, candidatas, distancias, duraciones,
        ParametrosOptimizacion(repostaje_minimo_l=5.0),
    )

    assert sobrecostes[0] is None  # no caben 5 L: no es una parada que ofrecer
    assert sobrecostes[1] is not None  # la de más adelante sí, y con hueco de sobra
