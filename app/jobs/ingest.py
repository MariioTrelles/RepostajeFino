"""Job de ingesta: baja el snapshot del Geoportal y lo guarda en SQLite.

Proceso **independiente** de FastAPI, a propósito: no montar un scheduler dentro
de la API (ARQUITECTURA.md §9). Se lanza a mano o desde cron, 2 veces al día:

    python -m app.jobs.ingest                    # descarga y guarda snapshot crudo
    python -m app.jobs.ingest --snapshot ruta.json   # reingesta sin tocar la red

Cron sugerido: ``0 7,15 * * *``.

No aplica hexagonal: es un script que reutiliza ``PriceStore`` para escribir (§3).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from app.adapters.ingestion.geoportal_client import GeoportalClient, ResultadoIngesta
from app.adapters.storage.sqlite_adapter import SQLiteAdapter
from app.config import settings

logger = logging.getLogger("ingest")


async def ejecutar(snapshot: Path | None = None, db_path: Path | None = None) -> int:
    cliente = GeoportalClient(
        url=settings.geoportal_url,
        user_agent=settings.geoportal_user_agent,
        snapshot_dir=settings.snapshot_dir,
    )

    if snapshot is not None:
        logger.info("Ingiriendo desde snapshot local: %s", snapshot)
        resultado: ResultadoIngesta = cliente.parsear(GeoportalClient.cargar_snapshot(snapshot))
    else:
        logger.info("Descargando de %s", settings.geoportal_url)
        resultado = await cliente.obtener()

    logger.info("Parseado: %s", resultado.resumen())
    if resultado.productos_desconocidos:
        logger.warning(
            "Hay productos nuevos sin mapear en productos.py: %s",
            sorted(resultado.productos_desconocidos),
        )

    store = SQLiteAdapter(db_path or settings.db_path)
    store.crear_esquema()
    n_estaciones = store.upsert_estaciones(resultado.estaciones, momento=resultado.fecha)
    n_precios = store.guardar_precios_si_cambian(resultado.precios)

    logger.info(
        "Guardado: %d estaciones upserted, %d precios nuevos de %d leídos "
        "(%.1f%% han cambiado). Totales en BD: %d estaciones, %d precios.",
        n_estaciones,
        n_precios,
        len(resultado.precios),
        100.0 * n_precios / len(resultado.precios) if resultado.precios else 0.0,
        store.contar("estaciones"),
        store.contar("precios"),
    )
    return n_precios


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingesta de precios del Geoportal de Carburantes")
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="Ingerir desde un snapshot JSON local en vez de llamar a la API",
    )
    parser.add_argument("--db", type=Path, default=None, help="Ruta del fichero SQLite")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    asyncio.run(ejecutar(snapshot=args.snapshot, db_path=args.db))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
