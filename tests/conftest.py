"""Fixtures compartidas.

Ningún test toca la red ni la API real del Ministerio: la ingesta se prueba
contra el snapshot reducido de ``tests/fixtures/`` (ARQUITECTURA.md §9).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.adapters.ingestion.geoportal_client import GeoportalClient

RUTA_FIXTURES = Path(__file__).parent / "fixtures"
RUTA_SNAPSHOT = RUTA_FIXTURES / "geoportal_snapshot_reducido.json"


@pytest.fixture(scope="session")
def ruta_snapshot() -> Path:
    return RUTA_SNAPSHOT


@pytest.fixture(scope="session")
def payload_snapshot() -> dict[str, Any]:
    return GeoportalClient.cargar_snapshot(RUTA_SNAPSHOT)


@pytest.fixture
def cliente() -> GeoportalClient:
    return GeoportalClient(url="https://ejemplo.invalid/api", user_agent="tests/1.0")
