"""Puerto ``PriceStore``: acceso a estaciones y precios.

Hoy lo implementa SQLite; mañana Postgres si hace falta escalar horizontalmente
(ARQUITECTURA.md §3). El dominio solo conoce esta interfaz.

Es un ``Protocol`` (tipado estructural): los adaptadores no heredan de nada, solo
cumplen la forma. Así el adaptador no importa el dominio para "registrarse" y la
dependencia apunta siempre hacia dentro.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Protocol

from app.domain.models import Estacion, Precio


class PriceStore(Protocol):
    # --- Escritura (la usa el job de ingesta) ---

    def upsert_estaciones(self, estaciones: Iterable[Estacion], momento: datetime) -> int:
        """Inserta o actualiza metadatos de estaciones. Devuelve cuántas ha tocado."""
        ...

    def guardar_precios_si_cambian(self, precios: Iterable[Precio]) -> int:
        """Inserta solo los precios que difieren del último conocido de (estación, producto).

        Append-only: nunca hace UPDATE sobre el histórico. Devuelve cuántas filas
        ha insertado realmente.
        """
        ...

    # --- Lectura (la usa el cálculo de ruta) ---

    def estaciones_en_bbox(
        self,
        min_lat: float,
        min_lon: float,
        max_lat: float,
        max_lon: float,
        producto: str,
        rotulos: Sequence[str] | None = None,
    ) -> list[tuple[Estacion, Precio]]:
        """Estaciones dentro del rectángulo con precio vigente de ``producto``.

        Primer filtro (grueso y barato) de la selección de candidatas: el desvío
        real se calcula después con el ``RoutingProvider``, nunca con distancia
        perpendicular a la polilínea (ARQUITECTURA.md §8).
        """
        ...

    def ultimo_precio(self, estacion_id: int, producto: str) -> Precio | None:
        """Último precio conocido de (estación, producto), o ``None`` si no hay."""
        ...
