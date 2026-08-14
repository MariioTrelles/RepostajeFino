"""Tests del adaptador de OSRM, con el servidor simulado por respx.

Ningún test sale a la red: el OSRM público no da garantías de uptime y el build
no puede depender de él (ARQUITECTURA.md §9). Todos los adaptadores se
construyen con ``intervalo_minimo_s=0`` para no pagar el límite de ritmo en los
tests; hay un test aparte que comprueba que ese límite existe.
"""

from __future__ import annotations

import math
from typing import Any

import httpx
import pytest
import respx

from app.adapters.routing.osrm_adapter import (
    ErrorOSRM,
    OSRMAdapter,
    RoutingNoDisponible,
    decodificar_polilinea,
)
from app.domain.ports.routing_provider import Coordenada

BASE = "https://osrm.invalid"
MADRID = Coordenada(lat=40.416775, lon=-3.703790)
BARCELONA = Coordenada(lat=41.385064, lon=2.173404)
ZARAGOZA = Coordenada(lat=41.648823, lon=-0.889085)

# Ejemplo canónico del formato de Google, el mismo que usa OSRM.
POLILINEA = "_p~iF~ps|U_ulLnnqC_mqNvxq`@"


def adaptador(**extra: Any) -> OSRMAdapter:
    opciones: dict[str, Any] = {"intervalo_minimo_s": 0.0}
    opciones.update(extra)
    return OSRMAdapter(base_url=BASE, **opciones)


def respuesta_ruta(distancia_m: float = 620_000.0, duracion_s: float = 21_600.0) -> dict[str, Any]:
    return {
        "code": "Ok",
        "routes": [{"distance": distancia_m, "duration": duracion_s, "geometry": POLILINEA}],
    }


def respuesta_table(distancias: list[list[Any]], duraciones: list[list[Any]]) -> dict[str, Any]:
    return {"code": "Ok", "distances": distancias, "durations": duraciones}


# ---------------------------------------------------------------------------
# Polilínea
# ---------------------------------------------------------------------------


def test_decodifica_la_polilinea() -> None:
    puntos = decodificar_polilinea(POLILINEA)
    assert [(round(p.lat, 5), round(p.lon, 5)) for p in puntos] == [
        (38.5, -120.2),
        (40.7, -120.95),
        (43.252, -126.453),
    ]


def test_polilinea_vacia() -> None:
    assert decodificar_polilinea("") == ()


def test_polilinea_truncada_falla_claro() -> None:
    with pytest.raises(ValueError, match="truncada"):
        decodificar_polilinea("_p~iF~ps|U_ulL")


# ---------------------------------------------------------------------------
# /route
# ---------------------------------------------------------------------------


@respx.mock
async def test_ruta_devuelve_distancia_duracion_y_geometria() -> None:
    ruta_mock = respx.get(url__startswith=f"{BASE}/route/v1/driving/").mock(
        return_value=httpx.Response(200, json=respuesta_ruta())
    )

    ruta = await adaptador().ruta(MADRID, BARCELONA)

    assert ruta.distancia_km == pytest.approx(620.0)
    assert ruta.duracion_s == pytest.approx(21_600.0)
    assert len(ruta.geometria) == 3
    assert ruta_mock.called


@respx.mock
async def test_las_coordenadas_van_en_orden_lon_lat() -> None:
    """El error clásico de OSRM. Invertirlo no falla: manda la ruta a Marruecos."""
    ruta_mock = respx.get(url__startswith=f"{BASE}/route/v1/driving/").mock(
        return_value=httpx.Response(200, json=respuesta_ruta())
    )

    await adaptador().ruta(MADRID, BARCELONA)

    ruta_pedida = str(ruta_mock.calls.last.request.url)
    assert "-3.703790,40.416775;2.173404,41.385064" in ruta_pedida


@respx.mock
async def test_ruta_sin_resultados_falla_claro() -> None:
    respx.get(url__startswith=f"{BASE}/route/v1/").mock(
        return_value=httpx.Response(200, json={"code": "Ok", "routes": []})
    )

    with pytest.raises(ErrorOSRM, match="ninguna ruta"):
        await adaptador().ruta(MADRID, BARCELONA)


@respx.mock
async def test_code_distinto_de_ok_no_se_traga() -> None:
    respx.get(url__startswith=f"{BASE}/route/v1/").mock(
        return_value=httpx.Response(200, json={"code": "NoRoute", "message": "No route found"})
    )

    with pytest.raises(ErrorOSRM, match="NoRoute"):
        await adaptador().ruta(MADRID, BARCELONA)


def test_bbox_de_la_ruta_envuelve_la_geometria() -> None:
    from app.domain.ports.routing_provider import Ruta

    ruta = Ruta(distancia_km=1.0, duracion_s=1.0, geometria=(MADRID, BARCELONA))

    min_lat, min_lon, max_lat, max_lon = ruta.bbox()
    assert (min_lat, max_lat) == (MADRID.lat, BARCELONA.lat)
    assert (min_lon, max_lon) == (MADRID.lon, BARCELONA.lon)

    # Con margen, el rectángulo crece en las cuatro direcciones.
    con_margen = ruta.bbox(margen_km=10.0)
    assert con_margen[0] < min_lat and con_margen[1] < min_lon
    assert con_margen[2] > max_lat and con_margen[3] > max_lon


# ---------------------------------------------------------------------------
# /table
# ---------------------------------------------------------------------------


@respx.mock
async def test_matriz_en_una_sola_peticion() -> None:
    table = respx.get(url__startswith=f"{BASE}/table/v1/driving/").mock(
        return_value=httpx.Response(
            200,
            json=respuesta_table(
                distancias=[[0, 300_000, 620_000], [300_000, 0, 320_000], [620_000, 320_000, 0]],
                duraciones=[[0, 10_800, 21_600], [10_800, 0, 10_800], [21_600, 10_800, 0]],
            ),
        )
    )

    matriz = await adaptador().matriz([MADRID, ZARAGOZA, BARCELONA])

    assert table.call_count == 1
    assert matriz.distancias_km[0][2] == pytest.approx(620.0)
    assert matriz.duraciones_s[0][1] == pytest.approx(10_800.0)
    assert matriz.completa
    assert "annotations=distance%2Cduration" in str(table.calls.last.request.url)


@respx.mock
async def test_la_matriz_se_trocea_cuando_no_cabe_en_una_peticion() -> None:
    """Con 250 candidatas la matriz entera se sale del límite de `/table`.

    Con ``max_puntos_por_peticion=4`` el bloque es de 2, así que 4 puntos son
    2x2 bloques = 4 peticiones, y la matriz reconstruida tiene que ser la misma
    que si hubiera cabido de una vez.
    """
    puntos = [Coordenada(lat=40.0 + i, lon=-3.0 + i) for i in range(4)]

    # Distancia inventada pero reconocible: de i a j son (j - i) * 100 km.
    def responder(request: httpx.Request) -> httpx.Response:
        crudas = str(request.url).split("/table/v1/driving/")[1].split("?")[0]
        indices = [round(float(par.split(",")[1]) - 40.0) for par in crudas.split(";")]
        origenes = request.url.params.get("sources")
        if origenes is None:
            filas = columnas = indices
        else:
            corte = len(origenes.split(";"))
            filas, columnas = indices[:corte], indices[corte:]
        return httpx.Response(
            200,
            json=respuesta_table(
                distancias=[[(j - i) * 100_000 for j in columnas] for i in filas],
                duraciones=[[(j - i) * 3_600 for j in columnas] for i in filas],
            ),
        )

    table = respx.get(url__startswith=f"{BASE}/table/v1/driving/").mock(side_effect=responder)

    matriz = await adaptador(max_puntos_por_peticion=4).matriz(puntos)

    assert table.call_count == 4
    assert matriz.completa
    for i in range(4):
        for j in range(4):
            assert matriz.distancias_km[i][j] == pytest.approx((j - i) * 100.0)
            assert matriz.duraciones_s[i][j] == pytest.approx((j - i) * 3_600.0)


@respx.mock
async def test_un_punto_irresoluble_sale_marcado_y_a_infinito() -> None:
    """Degradación explícita (§8.4): nunca un cero, que para el DP sería gratis."""
    respx.get(url__startswith=f"{BASE}/table/v1/").mock(
        return_value=httpx.Response(
            200,
            json=respuesta_table(
                distancias=[[0, None, 620_000], [None, 0, None], [620_000, None, 0]],
                duraciones=[[0, None, 21_600], [None, 0, None], [21_600, None, 0]],
            ),
        )
    )

    matriz = await adaptador().matriz([MADRID, ZARAGOZA, BARCELONA])

    assert matriz.indices_sin_respuesta == (1,)
    assert not matriz.completa
    assert math.isinf(matriz.distancias_km[0][1])
    assert math.isinf(matriz.duraciones_s[0][1])
    assert matriz.distancias_km[0][2] == pytest.approx(620.0)


@respx.mock
async def test_table_sin_distancias_avisa_de_annotations() -> None:
    respx.get(url__startswith=f"{BASE}/table/v1/").mock(
        return_value=httpx.Response(200, json={"code": "Ok", "durations": [[0, 1], [1, 0]]})
    )

    with pytest.raises(ErrorOSRM, match="annotations=distance"):
        await adaptador().matriz([MADRID, BARCELONA])


async def test_matriz_de_un_solo_punto_no_tiene_sentido() -> None:
    with pytest.raises(ValueError, match="al menos dos puntos"):
        await adaptador().matriz([MADRID])


# ---------------------------------------------------------------------------
# Resiliencia (§8.4)
# ---------------------------------------------------------------------------


@pytest.fixture
def sin_esperas(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Se queda con los tiempos de backoff en vez de dormirlos de verdad."""
    esperas: list[float] = []

    async def falso_sleep(segundos: float) -> None:
        esperas.append(segundos)

    monkeypatch.setattr("app.adapters.routing.osrm_adapter.asyncio.sleep", falso_sleep)
    return esperas


@respx.mock
async def test_reintenta_un_5xx_transitorio(sin_esperas: list[float]) -> None:
    respx.get(url__startswith=f"{BASE}/route/v1/").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json=respuesta_ruta()),
        ]
    )

    ruta = await adaptador().ruta(MADRID, BARCELONA)

    assert ruta.distancia_km == pytest.approx(620.0)
    assert sin_esperas == [0.5]


@respx.mock
async def test_reintenta_un_timeout(sin_esperas: list[float]) -> None:
    respx.get(url__startswith=f"{BASE}/route/v1/").mock(
        side_effect=[
            httpx.TimeoutException("agotado"),
            httpx.Response(200, json=respuesta_ruta()),
        ]
    )

    assert (await adaptador().ruta(MADRID, BARCELONA)).duracion_s == pytest.approx(21_600.0)


@respx.mock
async def test_el_backoff_es_exponencial_y_acotado(sin_esperas: list[float]) -> None:
    ruta_mock = respx.get(url__startswith=f"{BASE}/route/v1/").mock(
        return_value=httpx.Response(503)
    )

    with pytest.raises(RoutingNoDisponible):
        await adaptador(intentos=4).ruta(MADRID, BARCELONA)

    assert ruta_mock.call_count == 4
    assert sin_esperas == [0.5, 1.0, 2.0]


@respx.mock
async def test_agotados_los_reintentos_falla_en_vez_de_inventarse_distancias(
    sin_esperas: list[float],
) -> None:
    """§8.4: nada de fallback silencioso a distancia euclídea."""
    respx.get(url__startswith=f"{BASE}/table/v1/").mock(return_value=httpx.Response(500))

    with pytest.raises(RoutingNoDisponible, match="silenciosamente peor"):
        await adaptador().matriz([MADRID, BARCELONA])


@respx.mock
async def test_un_4xx_no_se_reintenta(sin_esperas: list[float]) -> None:
    """Un 400 es culpa de la petición: reintentarlo solo molesta al servidor."""
    ruta_mock = respx.get(url__startswith=f"{BASE}/route/v1/").mock(
        return_value=httpx.Response(400)
    )

    with pytest.raises(httpx.HTTPStatusError):
        await adaptador().ruta(MADRID, BARCELONA)

    assert ruta_mock.call_count == 1
    assert sin_esperas == []


@respx.mock
async def test_el_429_si_se_reintenta(sin_esperas: list[float]) -> None:
    """Es el "vas demasiado rápido" del servidor público: esperar y repetir."""
    respx.get(url__startswith=f"{BASE}/route/v1/").mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json=respuesta_ruta())]
    )

    assert await adaptador().ruta(MADRID, BARCELONA)
    assert sin_esperas == [0.5]


@respx.mock
async def test_espacia_las_peticiones_al_ritmo_configurado(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El OSRM público pide ~1 req/s (§9). Con OSRM propio se pone a 0."""
    esperas: list[float] = []

    async def falso_sleep(segundos: float) -> None:
        esperas.append(segundos)

    monkeypatch.setattr("app.adapters.routing.osrm_adapter.asyncio.sleep", falso_sleep)
    respx.get(url__startswith=f"{BASE}/route/v1/").mock(
        return_value=httpx.Response(200, json=respuesta_ruta())
    )

    cliente = OSRMAdapter(base_url=BASE, intervalo_minimo_s=1.0)
    await cliente.ruta(MADRID, BARCELONA)
    await cliente.ruta(MADRID, BARCELONA)

    # La primera sale sin esperar; la segunda espera lo que falte del segundo.
    assert len(esperas) == 1
    assert 0 < esperas[0] <= 1.0
