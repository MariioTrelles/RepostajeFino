"""Inyección de dependencias: qué adaptador implementa cada puerto.

Sin framework de DI, solo funciones factory (ARQUITECTURA.md §3). Los routers de
FastAPI (paso 4) las usarán con ``Depends``; hoy sirven ya para no repetir el
cableado en scripts y pruebas manuales.

Ninguna URL se escribe aquí: salen de ``settings``, que las lee del ``.env`` (§9).
"""

from __future__ import annotations

from functools import lru_cache

from app.adapters.routing.osrm_adapter import OSRMAdapter
from app.adapters.storage.sqlite_adapter import SQLiteAdapter
from app.config import settings
from app.domain.ports.price_store import PriceStore
from app.domain.ports.routing_provider import RoutingProvider


# `lru_cache` es una desviación consciente del ejemplo de §3, que devuelve una
# instancia nueva cada vez: el adaptador de OSRM lleva dentro el limitador de
# ~1 req/s del servidor público, y ese contador solo sirve si todas las
# peticiones comparten el mismo objeto.
@lru_cache(maxsize=1)
def get_routing_provider() -> RoutingProvider:
    return OSRMAdapter(base_url=settings.osrm_url)


@lru_cache(maxsize=1)
def get_price_store() -> PriceStore:
    return SQLiteAdapter(db_path=settings.db_path)
