"""Tests del cliente del Geoportal, contra el snapshot local.

No se llama a la API real en ningún test: la del Ministerio no es fiable y no
queremos que el build dependa de ella (ARQUITECTURA.md §9).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.adapters.ingestion.geoportal_client import (
    ErrorGeoportal,
    GeoportalClient,
    ResultadoIngesta,
    parsear_coordenada,
    parsear_decimal_es,
    parsear_precio_milesimas,
)


@pytest.fixture(scope="module")
def resultado() -> ResultadoIngesta:
    payload = GeoportalClient.cargar_snapshot(
        Path(__file__).parents[2] / "fixtures" / "geoportal_snapshot_reducido.json"
    )
    cliente = GeoportalClient(url="https://ejemplo.invalid", user_agent="tests/1.0")
    return cliente.parsear(payload)


def _por_id(resultado: ResultadoIngesta, estacion_id: int):
    return next((e for e in resultado.estaciones if e.id == estacion_id), None)


# ---------------------------------------------------------------------------
# Parseo de números en formato español
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("1,699", Decimal("1.699")),
        ("-1,539167", Decimal("-1.539167")),
        ("0,0", Decimal("0")),
        ("1.234,56", Decimal("1234.56")),  # punto como separador de miles
        ("1.699", Decimal("1.699")),  # solo punto: ya es decimal anglosajón
        ("  1,459  ", Decimal("1.459")),
        ("", None),
        (None, None),
        ("N/D", None),
    ],
)
def test_parsear_decimal_es(crudo: str | None, esperado: Decimal | None) -> None:
    assert parsear_decimal_es(crudo) == esperado


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        ("1,699", 1699),
        ("1,459", 1459),
        ("2,0", 2000),
        ("0,000", None),  # cero no es un precio, es ausencia de producto
        ("", None),
        (None, None),
    ],
)
def test_parsear_precio_milesimas(crudo: str | None, esperado: int | None) -> None:
    assert parsear_precio_milesimas(crudo) == esperado


def test_precio_es_entero_nunca_float() -> None:
    valor = parsear_precio_milesimas("1,659")
    assert valor == 1659
    assert isinstance(valor, int)
    assert not isinstance(valor, float)


def test_parsear_coordenada_usa_coma_decimal() -> None:
    assert parsear_coordenada("39,211417") == pytest.approx(39.211417)
    assert parsear_coordenada("-1,539167") == pytest.approx(-1.539167)
    assert parsear_coordenada("") is None


# ---------------------------------------------------------------------------
# Parseo del snapshot completo
# ---------------------------------------------------------------------------


def test_filtra_tipo_venta_no_publica(resultado: ResultadoIngesta) -> None:
    """`Tipo Venta = R` se descarta en la ingesta, antes de guardar (§4)."""
    assert _por_id(resultado, 900001) is None
    assert resultado.descartadas_no_publicas == 1
    assert all("PRIVADA" not in e.rotulo_raw.upper() for e in resultado.estaciones)


def test_descarta_estaciones_sin_coordenadas(resultado: ResultadoIngesta) -> None:
    assert _por_id(resultado, 900002) is None
    assert resultado.descartadas_sin_coordenadas == 1


def test_descarta_id_no_numerico(resultado: ResultadoIngesta) -> None:
    assert resultado.descartadas_sin_id == 1


def test_lee_longitud_con_nombre_de_campo_alternativo(resultado: ResultadoIngesta) -> None:
    """El campo real es "Longitud (WGS84)"; se acepta también "Longitud"."""
    estacion = _por_id(resultado, 900006)
    assert estacion is not None
    assert estacion.lat == pytest.approx(40.416775)
    assert estacion.lon == pytest.approx(-3.703790)


def test_lee_la_direccion(resultado: ResultadoIngesta) -> None:
    """El frontend necesita la calle, no solo el municipio (§4)."""
    estacion = _por_id(resultado, 4375)
    assert estacion is not None
    assert estacion.direccion == "AVENIDA CASTILLA LA MANCHA, 26"
    assert all(e.direccion for e in resultado.estaciones)


def test_estacion_sin_direccion_no_rompe(cliente: GeoportalClient) -> None:
    payload = {
        "Fecha": "12/08/2026 13:15:46",
        "ListaEESSPrecio": [
            {
                "IDEESS": "1",
                "Latitud": "40,0",
                "Longitud (WGS84)": "-3,0",
                "Rótulo": "REPSOL",
                "Tipo Venta": "P",
            }
        ],
    }
    (estacion,) = cliente.parsear(payload).estaciones
    assert estacion.direccion is None


def test_precio_cero_no_genera_precio(resultado: ResultadoIngesta) -> None:
    estacion = _por_id(resultado, 900003)
    assert estacion is not None
    productos = {p.producto for p in resultado.precios if p.estacion_id == 900003}
    assert "diesel" not in productos
    assert "gasolina95" in productos


def test_estacion_sin_precios_se_conserva(resultado: ResultadoIngesta) -> None:
    """La estación existe aunque no publique precios: sus metadatos siguen valiendo."""
    assert _por_id(resultado, 900004) is not None
    assert [p for p in resultado.precios if p.estacion_id == 900004] == []


def test_avisa_de_productos_no_mapeados(resultado: ResultadoIngesta) -> None:
    """Si el Ministerio añade un carburante, tiene que saltar un aviso, no perderse."""
    assert "Precio Combustible Del Futuro" in resultado.productos_desconocidos


def test_normaliza_rotulos_y_conserva_el_original(resultado: ResultadoIngesta) -> None:
    por_raw = {e.rotulo_raw: e for e in resultado.estaciones}
    assert por_raw["CEPSA LA GAVIA 365"].rotulo == "MOEVE"
    assert por_raw["Nº 10.935"].rotulo == "INDEPENDIENTE"
    assert por_raw["Nº 10.935"].rotulo_raw == "Nº 10.935"
    assert por_raw["ALCAMPO, S.A."].rotulo == "ALCAMPO"


def test_precios_en_milesimas_enteras(resultado: ResultadoIngesta) -> None:
    assert resultado.precios
    for precio in resultado.precios:
        assert isinstance(precio.precio_milesimas, int)
        assert 100 < precio.precio_milesimas < 100_000
        assert precio.valid_from == resultado.fecha


def test_todas_las_estaciones_tienen_coordenadas_plausibles(resultado: ResultadoIngesta) -> None:
    for estacion in resultado.estaciones:
        assert 27.0 <= estacion.lat <= 44.0, estacion
        assert -19.0 <= estacion.lon <= 5.0, estacion


def test_fecha_del_snapshot_se_usa_como_valid_from(resultado: ResultadoIngesta) -> None:
    assert resultado.fecha.year == 2026
    assert resultado.fecha.month == 8


def test_payload_sin_lista_falla_claro(cliente: GeoportalClient) -> None:
    with pytest.raises(ErrorGeoportal, match="ListaEESSPrecio"):
        cliente.parsear({"Fecha": "12/08/2026 13:15:46"})


# ---------------------------------------------------------------------------
# Capa HTTP
# ---------------------------------------------------------------------------


@respx.mock
async def test_descarga_manda_user_agent_de_navegador_y_guarda_snapshot(
    tmp_path: Path, payload_snapshot: dict[str, Any]
) -> None:
    url = "https://ejemplo.invalid/api"
    ruta = respx.get(url).mock(
        return_value=httpx.Response(200, json=payload_snapshot),
    )
    cliente = GeoportalClient(
        url=url,
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0",
        snapshot_dir=tmp_path,
    )

    resultado = await cliente.obtener()

    enviado = ruta.calls.last.request
    assert "Mozilla/5.0" in enviado.headers["user-agent"]

    snapshots = list(tmp_path.glob("geoportal_*.json"))
    assert len(snapshots) == 1
    assert json.loads(snapshots[0].read_text(encoding="utf-8"))["ListaEESSPrecio"]
    assert resultado.estaciones


@respx.mock
async def test_error_http_se_propaga(tmp_path: Path) -> None:
    url = "https://ejemplo.invalid/api"
    respx.get(url).mock(return_value=httpx.Response(503))
    cliente = GeoportalClient(url=url, user_agent="tests/1.0", snapshot_dir=tmp_path)

    with pytest.raises(httpx.HTTPStatusError):
        await cliente.obtener()

    assert list(tmp_path.glob("*.json")) == []
