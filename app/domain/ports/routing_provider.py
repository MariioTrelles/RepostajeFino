"""Puerto ``RoutingProvider``: distancias y tiempos por carretera real.

Hoy lo implementa OSRM público; mañana un OSRM autoalojado o Valhalla, sin que
el dominio se entere (ARQUITECTURA.md §3 y §9). Cambiar de host es cambiar
``OSRM_URL``; cambiar de motor es escribir otro adaptador.

Es un ``Protocol`` (tipado estructural), igual que ``PriceStore``: el adaptador
no hereda de nada, solo cumple la forma, y la dependencia sigue apuntando hacia
dentro.

Los tipos que viajan por el puerto se definen aquí y no en ``models.py``: son el
vocabulario de esta frontera, no del plan de repostaje.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

# Grados de latitud por kilómetro. Constante suficiente para ensanchar un
# rectángulo de búsqueda; no se usa para medir nada que importe (para eso está
# la distancia real por carretera, §8 punto 2).
_KM_POR_GRADO_LAT = 111.32


@dataclass(frozen=True, slots=True)
class Coordenada:
    """Punto geográfico en WGS84. Siempre (lat, lon), en ese orden.

    Ojo al pasarlo a OSRM, que espera ``lon,lat``: invertirlo es el error clásico
    y no da fallo, solo resultados absurdos. La conversión es cosa del adaptador.
    """

    lat: float
    lon: float


def bbox_de(
    puntos: Sequence[Coordenada], margen_km: float = 0.0
) -> tuple[float, float, float, float]:
    """``(min_lat, min_lon, max_lat, max_lon)`` que envuelve ``puntos``.

    El margen se aplica en kilómetros y se traduce a grados, que es lo que
    entiende el R*Tree. Un grado de longitud se estrecha según se sube en
    latitud, así que no vale usar el mismo número en los dos ejes.
    """
    if not puntos:
        raise ValueError("No hay puntos con los que calcular un bbox.")
    lats = [p.lat for p in puntos]
    lons = [p.lon for p in puntos]
    margen_lat = margen_km / _KM_POR_GRADO_LAT
    coseno = max(math.cos(math.radians(max(abs(min(lats)), abs(max(lats))))), 0.01)
    margen_lon = margen_km / (_KM_POR_GRADO_LAT * coseno)
    return (
        min(lats) - margen_lat,
        min(lons) - margen_lon,
        max(lats) + margen_lat,
        max(lons) + margen_lon,
    )


def distancia_haversine_km(a: Coordenada, b: Coordenada) -> float:
    """Distancia en línea recta. **Solo para trocear y ordenar**, nunca para costes.

    El coste de un desvío se mide por carretera con ``/table`` (§8 punto 2): la
    línea recta lleva a mandar a la gente campo a través.
    """
    radio = 6371.0
    lat1, lat2 = math.radians(a.lat), math.radians(b.lat)
    dlat = lat2 - lat1
    dlon = math.radians(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * radio * math.asin(math.sqrt(h))


@dataclass(frozen=True, slots=True)
class Ruta:
    """Trayecto directo de origen a destino, sin paradas."""

    distancia_km: float
    duracion_s: float
    geometria: tuple[Coordenada, ...]

    def bbox(self, margen_km: float = 0.0) -> tuple[float, float, float, float]:
        """``(min_lat, min_lon, max_lat, max_lon)`` que envuelve la ruta.

        Es el primer filtro, grueso y barato, de la selección de candidatas
        (§8 punto 2): se pasa a ``PriceStore.estaciones_en_bbox`` y el desvío
        real se calcula después con ``matriz``.

        Para una ruta larga este rectángulo es enorme y mete dentro media
        península. Trocear la polilínea en un corredor de rectángulos pequeños
        es trabajo de la selección de candidatas (``seleccion_candidatas``).
        """
        if not self.geometria:
            raise ValueError("La ruta no tiene geometría: no se puede calcular su bbox.")
        return bbox_de(self.geometria, margen_km)


@dataclass(frozen=True, slots=True)
class MatrizRuta:
    """Distancias y tiempos entre todos los pares de puntos, puerta a puerta.

    Es lo que come el DP (§8.2): con distancias reales, el combustible del
    desvío sale solo, y con duraciones se puede valorar el tiempo perdido.

    ``indices_sin_respuesta`` son los puntos que el motor no supo resolver (una
    gasolinera mal geocodificada, un punto en mitad del mar). Sus celdas quedan
    a ``inf``, nunca a cero: un cero silencioso sería un viaje gratis para el DP.
    Quien monte las candidatas tiene que **descartar esos puntos** antes de
    llamar al optimizador — degradación explícita, no un plan peor a escondidas
    (§8.4).
    """

    distancias_km: tuple[tuple[float, ...], ...]
    duraciones_s: tuple[tuple[float, ...], ...]
    indices_sin_respuesta: tuple[int, ...] = ()

    @property
    def completa(self) -> bool:
        return not self.indices_sin_respuesta


class RoutingProvider(Protocol):
    async def ruta(self, origen: Coordenada, destino: Coordenada) -> Ruta:
        """Ruta directa origen -> destino, con su polilínea."""
        ...

    async def matriz(self, puntos: Sequence[Coordenada]) -> MatrizRuta:
        """Matriz ``n x n`` de distancias y duraciones entre ``puntos``.

        El convenio de índices del DP (0 = origen, 1..n = candidatas en orden de
        avance, n+1 = destino) lo impone quien construye la lista, no el puerto.
        """
        ...
