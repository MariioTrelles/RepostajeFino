"""Endpoint de ruta óptima, con los dos puertos sustituidos por dobles.

Ni SQLite ni OSRM: el sentido de tener puertos es justamente poder probar el
endpoint entero sin levantar nada (ARQUITECTURA.md §3). Lo que se comprueba
aquí es sobre todo que los fallos salen con su código y sus números, y no
disfrazados de plan (§8.4).
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.adapters.routing.osrm_adapter import ErrorOSRM, RoutingNoDisponible
from app.api.dependencies import get_price_store, get_routing_provider
from app.domain.models import Estacion, Precio
from app.domain.ports.routing_provider import Coordenada, MatrizRuta, Ruta
from app.main import app

# ~85 km por grado de longitud a 40° de latitud: con la ruta sobre el paralelo,
# la longitud hace de punto kilométrico y las cuentas salen a mano.
KM_POR_GRADO = 85.0
KM_POR_GRADO_LAT = 111.0
ORIGEN = {"lat": 40.0, "lon": -3.0}
DESTINO = {"lat": 40.0, "lon": 2.0}  # ~425 km al este

COCHE = {
    "consumo_l_100km": 6.5,
    "tipo_combustible": "diesel",
    "capacidad_deposito_l": 55.0,
    "nivel_actual_l": 15.0,
    "reserva_minima_l": 5.0,
}


def estacion(id_: int, lon: float, rotulo: str = "REPSOL") -> Estacion:
    return Estacion(
        id=id_,
        rotulo=rotulo,
        rotulo_raw=rotulo,
        lat=40.0,
        lon=lon,
        direccion=f"CARRETERA N-II KM {id_}",
        municipio=f"Pueblo {id_}",
        provincia="GUADALAJARA",
    )


def precio(id_: int, milesimas: int, momento: datetime | None = None) -> Precio:
    return Precio(
        estacion_id=id_,
        producto="diesel",
        precio_milesimas=milesimas,
        valid_from=momento or datetime.now() - timedelta(hours=3),
    )


class StoreFalso:
    def __init__(self, estaciones: Sequence[tuple[Estacion, Precio]] = ()) -> None:
        self.estaciones = list(estaciones)

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
        return [
            (e, p)
            for e, p in self.estaciones
            if min_lat <= e.lat <= max_lat
            and min_lon <= e.lon <= max_lon
            and p.producto == producto
            and (not rotulos or e.rotulo in rotulos)
            and (not solo_vigentes or p.esta_vigente(ahora))
        ]


class RoutingFalso:
    """Distancias proporcionales a la longitud: la ruta va por el paralelo 40.

    La latitud también cuenta, y así una estación fuera del paralelo produce un
    desvío de verdad: hay que salirse para ir y volver a entrar para seguir.
    """

    def __init__(self, sin_respuesta: tuple[int, ...] = (), error: Exception | None = None) -> None:
        self.sin_respuesta = sin_respuesta
        self.error = error

    async def ruta(self, origen: Coordenada, destino: Coordenada) -> Ruta:
        if self.error:
            raise self.error
        pasos = 60
        geometria = tuple(
            Coordenada(lat=40.0, lon=origen.lon + (destino.lon - origen.lon) * i / (pasos - 1))
            for i in range(pasos)
        )
        km = abs(destino.lon - origen.lon) * KM_POR_GRADO
        return Ruta(distancia_km=km, duracion_s=km / 100 * 3600, geometria=geometria)

    async def matriz(self, puntos: Sequence[Coordenada]) -> MatrizRuta:
        if self.error:
            raise self.error
        n = len(puntos)
        distancias = [
            [
                math.inf
                if i in self.sin_respuesta or j in self.sin_respuesta
                else abs(puntos[j].lon - puntos[i].lon) * KM_POR_GRADO
                + abs(puntos[j].lat - puntos[i].lat) * KM_POR_GRADO_LAT
                for j in range(n)
            ]
            for i in range(n)
        ]
        for i in self.sin_respuesta:
            distancias[i][i] = 0.0
        duraciones = [
            [d / 100 * 3600 if math.isfinite(d) else math.inf for d in f] for f in distancias
        ]
        return MatrizRuta(
            distancias_km=tuple(tuple(f) for f in distancias),
            duraciones_s=tuple(tuple(f) for f in duraciones),
            indices_sin_respuesta=self.sin_respuesta,
        )


ESTACIONES = [
    (estacion(1, -2.0), precio(1, 1800)),  # PK 85
    (estacion(2, -1.0), precio(2, 1500)),  # PK 170
    (estacion(3, 0.0, "MOEVE"), precio(3, 1600)),  # PK 255
    (estacion(4, 1.0), precio(4, 1900)),  # PK 340
]


def cliente_con(store: StoreFalso, routing: RoutingFalso) -> Iterator[TestClient]:
    app.dependency_overrides[get_price_store] = lambda: store
    app.dependency_overrides[get_routing_provider] = lambda: routing
    with TestClient(app) as cliente:
        yield cliente
    app.dependency_overrides.clear()


@pytest.fixture
def cliente() -> Iterator[TestClient]:
    yield from cliente_con(StoreFalso(ESTACIONES), RoutingFalso())


def peticion(**cambios) -> dict:
    cuerpo = {"origen": ORIGEN, "destino": DESTINO, "vehiculo": dict(COCHE)}
    cuerpo.update(cambios)
    return cuerpo


# ---------------------------------------------------------------------------
# Camino feliz
# ---------------------------------------------------------------------------


def test_devuelve_un_plan_coherente(cliente: TestClient) -> None:
    respuesta = cliente.post("/api/ruta-optima", json=peticion())
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()

    assert cuerpo["distancia_directa_km"] == pytest.approx(425.0, abs=1)
    assert cuerpo["paradas"], "un coche con 15 L no llega a 425 km sin repostar"
    assert cuerpo["nivel_llegada_destino_l"] >= COCHE["reserva_minima_l"]
    # La única cifra de dinero son los euros del surtidor: el tiempo no se cobra.
    assert cuerpo["coste_combustible_eur"] > 0
    assert "coste_tiempo_eur" not in cuerpo
    assert "coste_total_eur" not in cuerpo
    # La primera parada solo puede ser la del PK 85: con 15 L se recorren 153 km
    # y la siguiente candidata está a 170.
    assert cuerpo["paradas"][0]["estacion"]["id"] == 1
    assert cuerpo["tramos"][0]["desde"] == "Origen"
    assert cuerpo["tramos"][-1]["hasta"] == "Destino"


def test_la_parada_trae_lo_que_el_frontend_necesita_ensenar(cliente: TestClient) -> None:
    parada = cliente.post("/api/ruta-optima", json=peticion()).json()["paradas"][0]

    assert parada["estacion"]["direccion"] == "CARRETERA N-II KM 1"
    assert parada["precio_efectivo_eur_litro"] == 1.8
    assert parada["litros"] > 0
    assert parada["coste_eur"] == pytest.approx(parada["litros"] * 1.8, abs=0.01)


def test_devuelve_las_candidatas_y_la_geometria_para_el_mapa(cliente: TestClient) -> None:
    """Un solo viaje: el paso 5 pinta el mapa sin un segundo endpoint."""
    cuerpo = cliente.post("/api/ruta-optima", json=peticion()).json()

    assert len(cuerpo["geometria"]) > 10
    assert cuerpo["geometria"][0] == [40.0, -3.0]
    assert cuerpo["candidatas"]
    candidata = cuerpo["candidatas"][0]
    assert candidata["precio"]["vigente"] is True
    assert candidata["precio"]["antiguedad_horas"] == pytest.approx(3.0, abs=0.1)
    assert candidata["precio_efectivo_eur_litro"] > 0
    assert candidata["desvio_km"] >= 0
    assert candidata["desvio_min"] >= 0


def test_el_filtro_de_marca_se_aplica(cliente: TestClient) -> None:
    """Con el depósito lleno, para aislar el filtro del alcance del coche.

    La única MOEVE está en el PK 255, fuera del alcance de un coche con 15 L;
    con 40 L el viaje se hace del tirón y lo que se mide es solo el filtro.
    """
    respuesta = cliente.post(
        "/api/ruta-optima",
        json=peticion(rotulos=["MOEVE"], vehiculo={**COCHE, "nivel_actual_l": 40.0}),
    )

    assert respuesta.status_code == 200
    assert {c["estacion"]["rotulo"] for c in respuesta.json()["candidatas"]} == {"MOEVE"}


def test_el_valor_del_tiempo_ya_no_es_un_parametro(cliente: TestClient) -> None:
    """Ponerle precio a la hora del conductor dejó de ser una opción (§8.2).

    El campo se rechaza en vez de ignorarse en silencio: quien lo mandara estaría
    esperando un comportamiento que ya no existe.
    """
    respuesta = cliente.post("/api/ruta-optima", json=peticion(valor_tiempo_eur_h=100.0))
    assert respuesta.status_code == 422


# ---------------------------------------------------------------------------
# Opciones: lo que de verdad se le ofrece al conductor (§8.6)
# ---------------------------------------------------------------------------


# Con 15 L solo se recorren 154 km y la única parada posible es la del PK 85:
# no hay nada que elegir, y eso también es una respuesta correcta. Para probar el
# reparto hace falta un coche con alcance para llegar a varias: 25 L dan 307 km,
# que alcanzan las tres primeras, y aun así obligan a repostar para hacer los 425.
CON_ALCANCE = {**COCHE, "nivel_actual_l": 25.0}


def test_ofrece_varias_opciones_repartidas_por_el_viaje(cliente: TestClient) -> None:
    cuerpo = cliente.post("/api/ruta-optima", json=peticion(vehiculo=CON_ALCANCE)).json()

    opciones = cuerpo["opciones"]
    assert len(opciones) >= 2, "un viaje de 425 km da para elegir dónde parar"

    # En el orden en que el conductor se las cruza, que es como se le enseñan.
    tiempos = [o["tiempo_desde_origen_s"] for o in opciones]
    assert tiempos == sorted(tiempos)

    # Exactamente una va marcada, y es la que menos cuesta de todas.
    marcadas = [o for o in opciones if o["es_la_mas_barata"]]
    assert len(marcadas) == 1
    assert marcadas[0]["sobrecoste_eur"] == min(o["sobrecoste_eur"] for o in opciones)
    assert all(o["sobrecoste_eur"] >= 0 for o in opciones)


def test_cada_opcion_es_una_parada_de_verdad_y_cuadra_la_cuenta(
    cliente: TestClient,
) -> None:
    """El sobrecoste tiene que poder comprobarse a mano restando dos cifras."""
    cuerpo = cliente.post("/api/ruta-optima", json=peticion(vehiculo=CON_ALCANCE)).json()
    optimo = cuerpo["coste_combustible_eur"]

    for opcion in cuerpo["opciones"]:
        assert opcion["litros"] > 0, "una opción es un sitio donde repostar"
        assert opcion["paradas"], "el plan de la opción tiene que venir entero"
        assert opcion["sobrecoste_eur"] == pytest.approx(
            opcion["coste_viaje_eur"] - optimo, abs=0.01
        )
        assert opcion["desvio_km"] <= 10.0, "el límite por defecto"


def test_una_estacion_demasiado_desviada_se_queda_fuera_y_se_dice() -> None:
    """El límite de desvío es lo que sustituye al precio de la hora (§8.2).

    La estación 9 está 0,1° al norte del paralelo: 11 km para salirse y otros 11
    para volver, 22 km de desvío. Es barata, así que sin el límite el plan se iría
    a ella; con el límite de 10 km ni siquiera llega al DP.

    El corredor va ancho a propósito. Con el de por defecto la estación ni sale
    del R*Tree, y lo que se quiere medir aquí es el filtro *fino*, el de las
    distancias reales, no el rectángulo grueso.
    """
    desviada = (
        Estacion(
            id=9,
            rotulo="REPSOL",
            rotulo_raw="REPSOL",
            lat=40.1,
            lon=-1.5,
            direccion="POLIGONO S/N",
            municipio="Pueblo 9",
            provincia="GUADALAJARA",
        ),
        precio(9, 1200),
    )
    store = StoreFalso([*ESTACIONES, desviada])

    for cliente in cliente_con(store, RoutingFalso()):
        estrecho = cliente.post(
            "/api/ruta-optima",
            json=peticion(vehiculo=CON_ALCANCE, margen_corredor_km=20.0),
        ).json()
        ancho = cliente.post(
            "/api/ruta-optima",
            json=peticion(vehiculo=CON_ALCANCE, margen_corredor_km=20.0, max_desvio_km=30.0),
        ).json()

    ids_estrecho = {c["estacion"]["id"] for c in estrecho["candidatas"]}
    ids_ancho = {c["estacion"]["id"] for c in ancho["candidatas"]}

    assert 9 not in ids_estrecho, "22 km de desvío no caben en el límite de 10"
    assert 9 in ids_ancho, "con el límite en 30 km sí es una opción viable"
    assert any("desviarse" in aviso for aviso in estrecho["avisos"])

    desvio_9 = next(c for c in ancho["candidatas"] if c["estacion"]["id"] == 9)
    assert desvio_9["desvio_km"] == pytest.approx(22.2, abs=0.5)


# ---------------------------------------------------------------------------
# Los fallos, que es donde se juega la honestidad de la API (§8.4)
# ---------------------------------------------------------------------------


def test_bajo_reserva_avisa_y_da_las_mas_cercanas_al_origen() -> None:
    """§7: la pregunta correcta no es cuál es la ruta óptima."""
    cerca = (estacion(9, -2.99), precio(9, 1500))
    store = StoreFalso([*ESTACIONES, cerca])
    for cliente in cliente_con(store, RoutingFalso()):
        respuesta = cliente.post(
            "/api/ruta-optima", json=peticion(vehiculo={**COCHE, "nivel_actual_l": 3.0})
        )

    assert respuesta.status_code == 422
    detalle = respuesta.json()["detail"]
    assert detalle["tipo"] == "bajo_reserva"
    assert "por debajo de la reserva" in detalle["detalle"]
    assert detalle["estaciones_cercanas"][0]["estacion"]["id"] == 9
    assert detalle["estaciones_cercanas"][0]["distancia_km"] < 2


def test_trayecto_inviable_dice_donde_se_rompe() -> None:
    """Nada de "sin solución": entre qué dos puntos y cuántos km faltan (§7)."""
    lejana = [(estacion(4, 1.0), precio(4, 1900))]  # PK 340, con 153 km de autonomía
    for cliente in cliente_con(StoreFalso(lejana), RoutingFalso()):
        respuesta = cliente.post("/api/ruta-optima", json=peticion())

    assert respuesta.status_code == 422
    detalle = respuesta.json()["detail"]
    assert detalle["tipo"] == "trayecto_inviable"
    assert detalle["desde"] == "Origen"
    assert detalle["autonomia_km"] == pytest.approx(153.8, abs=1)
    assert detalle["faltan_km"] > 0
    assert detalle["candidatas_alcanzables"] == 0


def test_osrm_caido_es_un_503_no_un_plan_peor() -> None:
    """§8.4: ningún fallback silencioso a distancia euclídea."""
    roto = RoutingFalso(error=RoutingNoDisponible("OSRM no responde tras 3 intentos"))
    for cliente in cliente_con(StoreFalso(ESTACIONES), roto):
        respuesta = cliente.post("/api/ruta-optima", json=peticion())

    assert respuesta.status_code == 503
    assert "no responde" in respuesta.json()["detail"]


def test_osrm_que_contesta_mal_es_un_502() -> None:
    for cliente in cliente_con(StoreFalso(ESTACIONES), RoutingFalso(error=ErrorOSRM("NoRoute"))):
        respuesta = cliente.post("/api/ruta-optima", json=peticion())

    assert respuesta.status_code == 502


def test_las_estaciones_que_osrm_no_resuelve_se_avisan_y_el_plan_sale() -> None:
    """Degradación explícita: el mejor plan con lo que sí respondió."""
    for cliente in cliente_con(StoreFalso(ESTACIONES), RoutingFalso(sin_respuesta=(2,))):
        respuesta = cliente.post("/api/ruta-optima", json=peticion())

    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["avisos"] and "OSRM no supo" in cuerpo["avisos"][0]
    assert len(cuerpo["candidatas"]) == len(ESTACIONES) - 1
    assert cuerpo["paradas"]


def test_sin_estaciones_en_el_corredor_lo_dice_claro() -> None:
    for cliente in cliente_con(StoreFalso([]), RoutingFalso()):
        respuesta = cliente.post("/api/ruta-optima", json=peticion())

    assert respuesta.status_code == 422
    assert "corredor" in respuesta.json()["detail"]


def test_los_precios_caducados_no_entran_como_candidatas() -> None:
    """§4.2: para el DP, un precio de hace tres días es como no tenerlo."""
    viejo = datetime.now() - timedelta(hours=72)
    caducadas = [(e, precio(e.id, p.precio_milesimas, viejo)) for e, p in ESTACIONES]
    for cliente in cliente_con(StoreFalso(caducadas), RoutingFalso()):
        respuesta = cliente.post("/api/ruta-optima", json=peticion())

    assert respuesta.status_code == 422
    assert "vigente" in respuesta.json()["detail"]


# ---------------------------------------------------------------------------
# Validación de la entrada
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("combustible", ["adblue", "amoniaco"])
def test_no_se_optimiza_un_viaje_con_adblue(cliente: TestClient, combustible: str) -> None:
    """§4: no son carburante de automoción, no deben llegar al DP."""
    respuesta = cliente.post(
        "/api/ruta-optima", json=peticion(vehiculo={**COCHE, "tipo_combustible": combustible})
    )
    assert respuesta.status_code == 422
    assert "automoción" in str(respuesta.json())


def test_combustible_inventado(cliente: TestClient) -> None:
    respuesta = cliente.post(
        "/api/ruta-optima", json=peticion(vehiculo={**COCHE, "tipo_combustible": "queroseno"})
    )
    assert respuesta.status_code == 422


def test_independiente_no_es_una_marca_filtrable(cliente: TestClient) -> None:
    """§6 y §11: no es una marca, es "todo lo que no encajó"."""
    respuesta = cliente.post("/api/ruta-optima", json=peticion(rotulos=["INDEPENDIENTE"]))
    assert respuesta.status_code == 422
    assert "no filtrables" in str(respuesta.json())


def test_coordenadas_imposibles(cliente: TestClient) -> None:
    respuesta = cliente.post("/api/ruta-optima", json=peticion(origen={"lat": 91.0, "lon": 0.0}))
    assert respuesta.status_code == 422


def test_nivel_por_encima_de_la_capacidad_lo_caza_el_dominio(cliente: TestClient) -> None:
    respuesta = cliente.post(
        "/api/ruta-optima", json=peticion(vehiculo={**COCHE, "nivel_actual_l": 90.0})
    )
    assert respuesta.status_code == 422
    assert "capacidad" in str(respuesta.json())


# ---------------------------------------------------------------------------
# Endpoints auxiliares
# ---------------------------------------------------------------------------


def test_las_marcas_filtrables_no_incluyen_independiente(cliente: TestClient) -> None:
    marcas = cliente.get("/api/marcas").json()
    assert "INDEPENDIENTE" not in marcas
    assert "REPSOL" in marcas and "MOEVE" in marcas
    assert len(marcas) == 20


def test_salud(cliente: TestClient) -> None:
    assert cliente.get("/salud").json()["estado"] == "ok"
