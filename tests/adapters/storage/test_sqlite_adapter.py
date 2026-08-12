"""Tests del adaptador SQLite.

El foco está en lo que ARQUITECTURA.md §4 marca como innegociable: append-only y
diffing de precios. Un bug ahí no se ve hasta que la tabla tiene millones de
filas o hasta que el histórico deja de ser fiable.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.adapters.storage.sqlite_adapter import SQLiteAdapter
from app.domain.models import Estacion, Precio

T0 = datetime(2026, 8, 12, 7, 0, 0)
T1 = T0 + timedelta(hours=8)


@pytest.fixture
def store(tmp_path: Path) -> SQLiteAdapter:
    adaptador = SQLiteAdapter(tmp_path / "test.db")
    adaptador.crear_esquema()
    return adaptador


def estacion(id_: int, rotulo: str = "REPSOL", lat: float = 40.4, lon: float = -3.7) -> Estacion:
    return Estacion(
        id=id_,
        rotulo=rotulo,
        rotulo_raw=f"E.S. {rotulo}",
        lat=lat,
        lon=lon,
        municipio="Madrid",
        provincia="MADRID",
        horario="L-D: 24H",
    )


def precio(id_: int, milesimas: int, producto: str = "diesel", momento: datetime = T0) -> Precio:
    return Precio(
        estacion_id=id_, producto=producto, precio_milesimas=milesimas, valid_from=momento
    )


# ---------------------------------------------------------------------------
# Esquema
# ---------------------------------------------------------------------------


def test_crear_esquema_es_idempotente(store: SQLiteAdapter) -> None:
    store.crear_esquema()
    store.crear_esquema()
    assert store.contar("estaciones") == 0


def test_wal_activado(store: SQLiteAdapter) -> None:
    with store._conexion() as conexion:  # noqa: SLF001
        modo = conexion.execute("PRAGMA journal_mode").fetchone()[0]
    assert modo.lower() == "wal"


def test_contar_rechaza_tablas_arbitrarias(store: SQLiteAdapter) -> None:
    with pytest.raises(ValueError):
        store.contar("estaciones; DROP TABLE precios")


# ---------------------------------------------------------------------------
# Estaciones: tabla mutable
# ---------------------------------------------------------------------------


def test_upsert_inserta_y_luego_actualiza(store: SQLiteAdapter) -> None:
    store.upsert_estaciones([estacion(1, "REPSOL")], momento=T0)
    assert store.contar("estaciones") == 1

    # La misma gasolinera cambia de bandera: se actualiza in-place, sin fila nueva.
    store.upsert_estaciones([estacion(1, "MOEVE")], momento=T1)
    assert store.contar("estaciones") == 1

    encontradas = store.estaciones_en_bbox(40.0, -4.0, 41.0, -3.0, "diesel")
    assert encontradas == []  # todavía no hay precios

    store.guardar_precios_si_cambian([precio(1, 1599)])
    (est, _), = store.estaciones_en_bbox(40.0, -4.0, 41.0, -3.0, "diesel")
    assert est.rotulo == "MOEVE"


def test_upsert_de_lista_vacia_no_falla(store: SQLiteAdapter) -> None:
    assert store.upsert_estaciones([]) == 0


# ---------------------------------------------------------------------------
# Precios: append-only + diffing
# ---------------------------------------------------------------------------


def test_solo_inserta_cuando_el_precio_cambia(store: SQLiteAdapter) -> None:
    store.upsert_estaciones([estacion(1)])

    assert store.guardar_precios_si_cambian([precio(1, 1599, momento=T0)]) == 1
    # Segunda pasada de ingesta con el mismo valor: no debe generar fila.
    assert store.guardar_precios_si_cambian([precio(1, 1599, momento=T1)]) == 0
    assert store.contar("precios") == 1

    # Ahora sí cambia.
    assert store.guardar_precios_si_cambian([precio(1, 1649, momento=T1)]) == 1
    assert store.contar("precios") == 2


def test_el_historico_no_se_pisa(store: SQLiteAdapter) -> None:
    """Append-only: el precio viejo sigue ahí después de insertar el nuevo."""
    store.upsert_estaciones([estacion(1)])
    store.guardar_precios_si_cambian([precio(1, 1599, momento=T0)])
    store.guardar_precios_si_cambian([precio(1, 1649, momento=T1)])

    with store._conexion() as conexion:  # noqa: SLF001
        valores = [
            fila[0]
            for fila in conexion.execute("SELECT precio_milesimas FROM precios ORDER BY id")
        ]
    assert valores == [1599, 1649]


def test_ultimo_precio_devuelve_el_mas_reciente(store: SQLiteAdapter) -> None:
    store.upsert_estaciones([estacion(1)])
    store.guardar_precios_si_cambian([precio(1, 1599, momento=T0)])
    store.guardar_precios_si_cambian([precio(1, 1649, momento=T1)])

    ultimo = store.ultimo_precio(1, "diesel")
    assert ultimo is not None
    assert ultimo.precio_milesimas == 1649
    assert ultimo.valid_from == T1
    assert store.ultimo_precio(1, "gasolina95") is None


def test_el_diffing_es_por_estacion_y_producto(store: SQLiteAdapter) -> None:
    store.upsert_estaciones([estacion(1), estacion(2, lat=40.5)])
    store.guardar_precios_si_cambian(
        [
            precio(1, 1599, "diesel"),
            precio(1, 1699, "gasolina95"),
            precio(2, 1599, "diesel"),
        ]
    )
    assert store.contar("precios") == 3

    # Solo cambia el gasóleo de la estación 1: solo debe entrar esa fila.
    insertados = store.guardar_precios_si_cambian(
        [
            precio(1, 1579, "diesel", momento=T1),
            precio(1, 1699, "gasolina95", momento=T1),
            precio(2, 1599, "diesel", momento=T1),
        ]
    )
    assert insertados == 1
    assert store.contar("precios") == 4


def test_duplicados_dentro_de_la_misma_tanda(store: SQLiteAdapter) -> None:
    """Si la API repite la misma estación en un ciclo, no se duplica la fila."""
    store.upsert_estaciones([estacion(1)])
    insertados = store.guardar_precios_si_cambian([precio(1, 1599), precio(1, 1599)])
    assert insertados == 1


def test_precios_de_lista_vacia_no_falla(store: SQLiteAdapter) -> None:
    assert store.guardar_precios_si_cambian([]) == 0


# ---------------------------------------------------------------------------
# Filtro espacial
# ---------------------------------------------------------------------------


def test_bbox_filtra_por_rectangulo(store: SQLiteAdapter) -> None:
    store.upsert_estaciones(
        [
            estacion(1, lat=40.40, lon=-3.70),  # Madrid, dentro
            estacion(2, lat=41.39, lon=2.17),  # Barcelona, fuera
            estacion(3, lat=40.45, lon=-3.60),  # Madrid, dentro
        ]
    )
    store.guardar_precios_si_cambian(
        [precio(1, 1599), precio(2, 1499), precio(3, 1699)]
    )

    dentro = store.estaciones_en_bbox(40.0, -4.0, 41.0, -3.0, "diesel")
    assert sorted(e.id for e, _ in dentro) == [1, 3]
    assert {p.precio_milesimas for _, p in dentro} == {1599, 1699}


def test_bbox_solo_devuelve_el_precio_vigente(store: SQLiteAdapter) -> None:
    store.upsert_estaciones([estacion(1)])
    store.guardar_precios_si_cambian([precio(1, 1599, momento=T0)])
    store.guardar_precios_si_cambian([precio(1, 1649, momento=T1)])

    (_, p), = store.estaciones_en_bbox(40.0, -4.0, 41.0, -3.0, "diesel")
    assert p.precio_milesimas == 1649


def test_bbox_ignora_estaciones_sin_ese_producto(store: SQLiteAdapter) -> None:
    store.upsert_estaciones([estacion(1), estacion(2, lat=40.45)])
    store.guardar_precios_si_cambian([precio(1, 1599, "diesel"), precio(2, 1699, "gasolina95")])

    encontradas = store.estaciones_en_bbox(40.0, -4.0, 41.0, -3.0, "diesel")
    assert [e.id for e, _ in encontradas] == [1]


def test_bbox_filtra_por_rotulo(store: SQLiteAdapter) -> None:
    store.upsert_estaciones(
        [estacion(1, "REPSOL"), estacion(2, "MOEVE", lat=40.45), estacion(3, "BP", lat=40.42)]
    )
    store.guardar_precios_si_cambian([precio(1, 1599), precio(2, 1499), precio(3, 1699)])

    encontradas = store.estaciones_en_bbox(
        40.0, -4.0, 41.0, -3.0, "diesel", rotulos=["MOEVE", "BP"]
    )
    assert sorted(e.id for e, _ in encontradas) == [2, 3]


def test_el_rtree_se_mantiene_al_mover_una_estacion(store: SQLiteAdapter) -> None:
    store.upsert_estaciones([estacion(1, lat=40.4, lon=-3.7)])
    store.guardar_precios_si_cambian([precio(1, 1599)])
    assert store.estaciones_en_bbox(40.0, -4.0, 41.0, -3.0, "diesel")

    # Corrigen las coordenadas en origen: el índice espacial tiene que seguirlas.
    store.upsert_estaciones([estacion(1, lat=41.39, lon=2.17)])
    assert store.estaciones_en_bbox(40.0, -4.0, 41.0, -3.0, "diesel") == []
    assert store.estaciones_en_bbox(41.0, 2.0, 42.0, 3.0, "diesel")
