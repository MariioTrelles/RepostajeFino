"""Modelos del dominio.

Objetos planos, sin dependencias de adaptadores, red ni base de datos. Los
construyen tanto la ingesta como el DP, y son la moneda de cambio de los puertos.

Los modelos del optimizador (``Vehiculo``, ``TramoRuta``, ``Recomendacion``) se
añaden en el paso 2; aquí están solo las entidades que necesita ya la ingesta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Estacion:
    """Metadatos de una estación de servicio. Mutable en BD: se actualiza in-place.

    ``rotulo`` va ya normalizado (ver ``rotulo_normalizer``); ``rotulo_raw``
    conserva el valor original del Ministerio por si hay que revisar o ampliar
    el diccionario más adelante.
    """

    id: int
    rotulo: str
    rotulo_raw: str
    lat: float
    lon: float
    municipio: str | None = None
    provincia: str | None = None
    horario: str | None = None


@dataclass(frozen=True, slots=True)
class Precio:
    """Precio de un producto en una estación, en un instante dado.

    En **milésimas de euro como entero** (``1,659 €/L`` -> ``1659``). Nunca
    ``float``: el DP acumula error y desestabiliza los desempates entre
    estaciones que difieren en una milésima.
    """

    estacion_id: int
    producto: str
    precio_milesimas: int
    valid_from: datetime

    @property
    def euros_por_litro(self) -> Decimal:
        """Valor en euros, exacto. ``Decimal`` a propósito, no ``float``."""
        return Decimal(self.precio_milesimas) / Decimal(1000)
