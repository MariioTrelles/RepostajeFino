"""Adaptador SQLite del puerto ``PriceStore``.

Puntos que no son negociables (ARQUITECTURA.md §4):

- ``precios`` es **append-only**: nunca un UPDATE sobre el histórico.
- Solo se inserta cuando el precio **cambia** respecto al último conocido de
  (estación, producto). La API devuelve ~11.500 estaciones cada ciclo; insertar
  todo generaría cientos de miles de filas al día sin información nueva.
- ``estaciones`` sí es mutable: una gasolinera puede cambiar de bandera.

WAL activado: las lecturas concurrentes no bloquean, y solo escribe el job de
ingesta (§9).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from app.domain.models import Estacion, Precio

logger = logging.getLogger(__name__)

ESQUEMA = """
CREATE TABLE IF NOT EXISTS estaciones (
    id INTEGER PRIMARY KEY,
    rotulo TEXT NOT NULL,
    rotulo_raw TEXT,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    municipio TEXT,
    provincia TEXT,
    horario TEXT,
    updated_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_estaciones_rotulo ON estaciones(rotulo);

CREATE TABLE IF NOT EXISTS precios (
    id INTEGER PRIMARY KEY,
    estacion_id INTEGER NOT NULL REFERENCES estaciones(id),
    producto TEXT NOT NULL,
    precio_milesimas INTEGER NOT NULL,
    valid_from TIMESTAMP NOT NULL
);

-- Soporte para "último precio de (estación, producto)", que es la consulta
-- caliente tanto del diffing de la ingesta como del cálculo de ruta.
CREATE INDEX IF NOT EXISTS idx_precios_estacion_producto
    ON precios(estacion_id, producto, id DESC);

-- Filtro espacial (ARQUITECTURA.md §2 y §8). Geometría de punto: min = max.
CREATE VIRTUAL TABLE IF NOT EXISTS estaciones_rtree USING rtree(
    id, min_lat, max_lat, min_lon, max_lon
);

-- Fase 2: fuente distinta (NAP DGT). Se crea vacía para no migrar después.
CREATE TABLE IF NOT EXISTS puntos_recarga (
    id INTEGER PRIMARY KEY,
    estacion_id INTEGER REFERENCES estaciones(id),
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    potencia_kw REAL,
    tipo_conector TEXT,
    operador TEXT,
    updated_at TIMESTAMP
);
"""


class SQLiteAdapter:
    """Implementa ``PriceStore`` sobre un fichero SQLite."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        if self._db_path.parent and str(self._db_path) != ":memory:":
            self._db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def _conexion(self) -> Iterator[sqlite3.Connection]:
        conexion = sqlite3.connect(self._db_path)
        conexion.row_factory = sqlite3.Row
        try:
            conexion.execute("PRAGMA journal_mode=WAL")
            conexion.execute("PRAGMA foreign_keys=ON")
            yield conexion
            conexion.commit()
        except Exception:
            conexion.rollback()
            raise
        finally:
            conexion.close()

    def crear_esquema(self) -> None:
        with self._conexion() as conexion:
            conexion.executescript(ESQUEMA)

    # ------------------------------------------------------------------
    # Escritura
    # ------------------------------------------------------------------

    def upsert_estaciones(
        self, estaciones: Iterable[Estacion], momento: datetime | None = None
    ) -> int:
        momento = momento or datetime.now()
        filas = [
            (e.id, e.rotulo, e.rotulo_raw, e.lat, e.lon, e.municipio, e.provincia, e.horario,
             momento.isoformat(sep=" ", timespec="seconds"))
            for e in estaciones
        ]
        if not filas:
            return 0

        with self._conexion() as conexion:
            conexion.executemany(
                """
                INSERT INTO estaciones
                    (id, rotulo, rotulo_raw, lat, lon, municipio, provincia, horario, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    rotulo = excluded.rotulo,
                    rotulo_raw = excluded.rotulo_raw,
                    lat = excluded.lat,
                    lon = excluded.lon,
                    municipio = excluded.municipio,
                    provincia = excluded.provincia,
                    horario = excluded.horario,
                    updated_at = excluded.updated_at
                """,
                filas,
            )
            # El R*Tree no tiene ON CONFLICT: se borra y se reinserta.
            ids = [(fila[0],) for fila in filas]
            conexion.executemany("DELETE FROM estaciones_rtree WHERE id = ?", ids)
            conexion.executemany(
                "INSERT INTO estaciones_rtree (id, min_lat, max_lat, min_lon, max_lon) "
                "VALUES (?, ?, ?, ?, ?)",
                [(fila[0], fila[3], fila[3], fila[4], fila[4]) for fila in filas],
            )
        return len(filas)

    def guardar_precios_si_cambian(self, precios: Iterable[Precio]) -> int:
        """Inserta solo los precios distintos del último conocido. Append-only."""
        precios = list(precios)
        if not precios:
            return 0

        with self._conexion() as conexion:
            ultimos = self._ultimos_precios(conexion)

            nuevos: list[tuple[int, str, int, str]] = []
            # Dentro de una misma tanda puede venir la misma clave repetida; nos
            # quedamos con la primera y evitamos insertar duplicados idénticos.
            vistos: dict[tuple[int, str], int] = {}
            for precio in precios:
                clave = (precio.estacion_id, precio.producto)
                anterior = vistos.get(clave, ultimos.get(clave))
                if anterior == precio.precio_milesimas:
                    continue
                vistos[clave] = precio.precio_milesimas
                nuevos.append(
                    (
                        precio.estacion_id,
                        precio.producto,
                        precio.precio_milesimas,
                        precio.valid_from.isoformat(sep=" ", timespec="seconds"),
                    )
                )

            if nuevos:
                conexion.executemany(
                    "INSERT INTO precios (estacion_id, producto, precio_milesimas, valid_from) "
                    "VALUES (?, ?, ?, ?)",
                    nuevos,
                )
        return len(nuevos)

    @staticmethod
    def _ultimos_precios(conexion: sqlite3.Connection) -> dict[tuple[int, str], int]:
        cursor = conexion.execute(
            """
            SELECT estacion_id, producto, precio_milesimas
            FROM precios
            WHERE id IN (SELECT MAX(id) FROM precios GROUP BY estacion_id, producto)
            """
        )
        return {(fila[0], fila[1]): fila[2] for fila in cursor}

    # ------------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------------

    def ultimo_precio(self, estacion_id: int, producto: str) -> Precio | None:
        with self._conexion() as conexion:
            fila = conexion.execute(
                """
                SELECT estacion_id, producto, precio_milesimas, valid_from
                FROM precios
                WHERE estacion_id = ? AND producto = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (estacion_id, producto),
            ).fetchone()
        return None if fila is None else _fila_a_precio(fila)

    def estaciones_en_bbox(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
        producto: str,
        rotulos: Sequence[str] | None = None,
    ) -> list[tuple[Estacion, Precio]]:
        # El R*Tree guarda coordenadas en float32, así que su filtro es
        # aproximado por diseño: se reconfirma con lat/lon exactas de la tabla.
        sql = """
            SELECT e.id, e.rotulo, e.rotulo_raw, e.lat, e.lon, e.municipio, e.provincia,
                   e.horario, p.producto, p.precio_milesimas, p.valid_from
            FROM estaciones_rtree r
            JOIN estaciones e ON e.id = r.id
            JOIN precios p ON p.id = (
                SELECT MAX(id) FROM precios
                WHERE estacion_id = e.id AND producto = :producto
            )
            WHERE r.max_lat >= :min_lat AND r.min_lat <= :max_lat
              AND r.max_lon >= :min_lon AND r.min_lon <= :max_lon
              AND e.lat BETWEEN :min_lat AND :max_lat
              AND e.lon BETWEEN :min_lon AND :max_lon
        """
        parametros: dict[str, object] = {
            "producto": producto,
            "min_lat": min_lat,
            "max_lat": max_lat,
            "min_lon": min_lon,
            "max_lon": max_lon,
        }
        if rotulos:
            marcadores = ", ".join(f":rotulo_{i}" for i in range(len(rotulos)))
            sql += f" AND e.rotulo IN ({marcadores})"
            parametros.update({f"rotulo_{i}": r for i, r in enumerate(rotulos)})

        with self._conexion() as conexion:
            filas = conexion.execute(sql, parametros).fetchall()

        return [
            (
                Estacion(
                    id=fila["id"],
                    rotulo=fila["rotulo"],
                    rotulo_raw=fila["rotulo_raw"],
                    lat=fila["lat"],
                    lon=fila["lon"],
                    municipio=fila["municipio"],
                    provincia=fila["provincia"],
                    horario=fila["horario"],
                ),
                _fila_a_precio(fila, estacion_id=fila["id"]),
            )
            for fila in filas
        ]

    # ------------------------------------------------------------------
    # Utilidades de diagnóstico (las usa el job de ingesta)
    # ------------------------------------------------------------------

    def contar(self, tabla: str) -> int:
        if tabla not in {"estaciones", "precios", "puntos_recarga"}:
            raise ValueError(f"Tabla no permitida: {tabla!r}")
        with self._conexion() as conexion:
            return int(conexion.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0])


def _fila_a_precio(fila: sqlite3.Row, estacion_id: int | None = None) -> Precio:
    return Precio(
        estacion_id=estacion_id if estacion_id is not None else fila["estacion_id"],
        producto=fila["producto"],
        precio_milesimas=fila["precio_milesimas"],
        valid_from=datetime.fromisoformat(fila["valid_from"]),
    )
