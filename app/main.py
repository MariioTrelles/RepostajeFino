"""Aplicación FastAPI.

    uvicorn app.main:app --reload

Un solo endpoint de negocio, el de ruta óptima (ARQUITECTURA.md §12, paso 4).
Aquí no hay lógica: solo montar el router y comprobar al arrancar que la BD
está en su sitio, que es el fallo más probable en una máquina nueva.

Nada de schedulers dentro de la API: la ingesta es un proceso aparte (§9).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes.ruta import router as router_ruta
from app.config import settings

logger = logging.getLogger(__name__)

DIRECTORIO_ESTATICO = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def _ciclo_de_vida(app: FastAPI) -> AsyncIterator[None]:
    if not settings.db_path.exists():
        logger.warning(
            "No hay base de datos en %s. Lanza `python -m app.jobs.ingest` antes de "
            "pedir rutas, o no habrá ninguna estación que ofrecer.",
            settings.db_path,
        )
    logger.info("OSRM en %s", settings.osrm_url)
    yield


app = FastAPI(
    title="Repostaje Fino",
    description=(
        "Calcula dónde repostar y cuánto en un trayecto por carretera, con precios "
        "reales del Geoportal de Carburantes y el coste real del desvío."
    ),
    version="0.1.0",
    lifespan=_ciclo_de_vida,
)

app.include_router(router_ruta)


@app.get("/salud", tags=["salud"])
async def salud() -> dict[str, object]:
    return {"estado": "ok", "bd": settings.db_path.exists()}


# El frontend lo sirve la propia app, en el mismo origen y sin CORS (§9.1).
# Este `mount` va **el último** a propósito: monta en la raíz, así que cualquier
# ruta declarada después de él dejaría de resolverse. Con `html=True`, "/" sirve
# `index.html`.
app.mount("/", StaticFiles(directory=DIRECTORIO_ESTATICO, html=True), name="estaticos")
