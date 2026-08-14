"""Adaptador OSRM del puerto ``RoutingProvider``.

Dos operaciones (ARQUITECTURA.md §12, paso 3):

- ``ruta``: la polilínea del trayecto directo, que es el punto de partida del
  filtro espacial de candidatas.
- ``matriz``: ``/table`` en batch, que da el desvío **real por carretera** de
  cada candidata. Nunca distancia perpendicular a la polilínea (§8 punto 2):
  con línea recta el plan acaba mandando a alguien campo a través.

Todo lo desagradable de depender de un servicio público vive aquí y no se
asoma al puerto ni al dominio (§8.4):

- **Reintentos con backoff acotado** ante timeout, 5xx o 429.
- **Ningún fallback silencioso**: si se agotan los reintentos se lanza
  ``RoutingNoDisponible``. No se aproxima nada con distancia euclídea, porque
  eso cambiaría la calidad del plan sin que el usuario se entere.
- **Degradación explícita**: los puntos que OSRM no sabe resolver salen
  marcados en ``MatrizRuta.indices_sin_respuesta`` con sus celdas a ``inf``,
  jamás a cero.
- **1 petición por segundo** por defecto, que es lo que pide la política del
  servidor público (§9). Con OSRM propio se pone a 0 y desaparece la espera.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx

from app.domain.ports.routing_provider import Coordenada, MatrizRuta, Ruta

logger = logging.getLogger(__name__)

# El OSRM público acepta como mucho 100 coordenadas por petición a `/table`.
# Como un bloque cruzado manda orígenes y destinos en la misma lista, el tamaño
# de bloque efectivo es la mitad.
LIMITE_PUNTOS_TABLE = 100

CODIGOS_REINTENTABLES = frozenset({429, 500, 502, 503, 504})


class ErrorOSRM(RuntimeError):
    """OSRM contestó, pero con algo que no sirve."""


class RoutingNoDisponible(ErrorOSRM):
    """OSRM no contesta y se han agotado los reintentos (§8.4).

    Es un error a propósito: la alternativa sería devolver un plan calculado con
    distancias inventadas, que es peor porque nadie se entera.
    """


class OSRMAdapter:
    """Implementa ``RoutingProvider`` contra un servidor OSRM."""

    def __init__(
        self,
        base_url: str,
        perfil: str = "driving",
        timeout_s: float = 30.0,
        intentos: int = 3,
        backoff_inicial_s: float = 0.5,
        intervalo_minimo_s: float = 1.0,
        max_puntos_por_peticion: int = LIMITE_PUNTOS_TABLE,
        cliente: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._perfil = perfil
        self._timeout_s = timeout_s
        self._intentos = max(1, intentos)
        self._backoff_inicial_s = backoff_inicial_s
        self._intervalo_minimo_s = intervalo_minimo_s
        self._max_puntos_por_peticion = max(2, max_puntos_por_peticion)
        self._cliente = cliente
        self._turno = asyncio.Lock()
        self._ultima_peticion = float("-inf")

    # ------------------------------------------------------------------
    # Puerto
    # ------------------------------------------------------------------

    async def ruta(self, origen: Coordenada, destino: Coordenada) -> Ruta:
        url = f"{self._base_url}/route/v1/{self._perfil}/{_coords([origen, destino])}"
        async with self._sesion() as cliente:
            cuerpo = await self._pedir(
                cliente, url, {"overview": "full", "geometries": "polyline", "steps": "false"}
            )

        rutas = cuerpo.get("routes") or []
        if not rutas:
            raise ErrorOSRM("OSRM no devolvió ninguna ruta para ese origen y destino.")
        ruta = rutas[0]
        return Ruta(
            distancia_km=float(ruta["distance"]) / 1000.0,
            duracion_s=float(ruta["duration"]),
            geometria=decodificar_polilinea(ruta.get("geometry", "")),
        )

    async def matriz(self, puntos: Sequence[Coordenada]) -> MatrizRuta:
        n = len(puntos)
        if n < 2:
            raise ValueError("Hacen falta al menos dos puntos para pedir una matriz.")

        # `inf` como valor de partida, nunca 0: si un bloque no se rellenara por
        # lo que sea, el DP tiene que verlo como "no se puede ir", no como gratis.
        distancias = [[math.inf] * n for _ in range(n)]
        duraciones = [[math.inf] * n for _ in range(n)]

        tam = max(1, self._max_puntos_por_peticion // 2)
        bloques = [range(i, min(i + tam, n)) for i in range(0, n, tam)]
        async with self._sesion() as cliente:
            for filas in bloques:
                for columnas in bloques:
                    await self._rellenar_bloque(
                        cliente, puntos, filas, columnas, distancias, duraciones
                    )

        sin_respuesta = tuple(
            i for i in range(n) if all(math.isinf(distancias[i][j]) for j in range(n) if j != i)
        )
        if sin_respuesta:
            logger.warning(
                "OSRM no supo resolver %d de %d puntos: %s. Hay que descartarlos antes "
                "de llamar al DP.",
                len(sin_respuesta),
                n,
                sin_respuesta,
            )
        return MatrizRuta(
            distancias_km=tuple(tuple(fila) for fila in distancias),
            duraciones_s=tuple(tuple(fila) for fila in duraciones),
            indices_sin_respuesta=sin_respuesta,
        )

    # ------------------------------------------------------------------
    # `/table` por bloques
    # ------------------------------------------------------------------

    async def _rellenar_bloque(
        self,
        cliente: httpx.AsyncClient,
        puntos: Sequence[Coordenada],
        filas: range,
        columnas: range,
        distancias: list[list[float]],
        duraciones: list[list[float]],
    ) -> None:
        """Pide un submatriz ``filas x columnas`` y la copia en su sitio.

        Trocear no es un capricho: con 250 candidatas la matriz completa se sale
        del límite de coordenadas de `/table` y el servidor devuelve un error.
        """
        parametros: dict[str, str] = {"annotations": "distance,duration"}
        if filas.start == columnas.start:
            coordenadas = [puntos[i] for i in filas]
        else:
            coordenadas = [puntos[i] for i in filas] + [puntos[j] for j in columnas]
            parametros["sources"] = ";".join(str(i) for i in range(len(filas)))
            parametros["destinations"] = ";".join(
                str(i) for i in range(len(filas), len(filas) + len(columnas))
            )

        url = f"{self._base_url}/table/v1/{self._perfil}/{_coords(coordenadas)}"
        cuerpo = await self._pedir(cliente, url, parametros)

        bloque_distancias = cuerpo.get("distances")
        bloque_duraciones = cuerpo.get("durations")
        if bloque_distancias is None or bloque_duraciones is None:
            raise ErrorOSRM(
                "La respuesta de /table no trae 'distances' y 'durations'. Este servidor "
                "OSRM no soporta annotations=distance, y sin distancias reales el coste "
                "del desvío no se puede calcular (§8.2)."
            )

        for indice_fila, i in enumerate(filas):
            for indice_columna, j in enumerate(columnas):
                metros = bloque_distancias[indice_fila][indice_columna]
                segundos = bloque_duraciones[indice_fila][indice_columna]
                distancias[i][j] = math.inf if metros is None else float(metros) / 1000.0
                duraciones[i][j] = math.inf if segundos is None else float(segundos)

    # ------------------------------------------------------------------
    # HTTP: reintentos, backoff y límite de ritmo (§8.4, §9)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _sesion(self) -> AsyncIterator[httpx.AsyncClient]:
        if self._cliente is not None:
            yield self._cliente
            return
        async with httpx.AsyncClient(timeout=self._timeout_s) as cliente:
            yield cliente

    async def _pedir(
        self, cliente: httpx.AsyncClient, url: str, parametros: dict[str, str]
    ) -> dict:
        ultimo: Exception | None = None

        for intento in range(self._intentos):
            if intento:
                espera = self._backoff_inicial_s * 2 ** (intento - 1)
                logger.warning(
                    "OSRM falló (%s). Reintento %d/%d en %.1f s.",
                    ultimo,
                    intento + 1,
                    self._intentos,
                    espera,
                )
                await asyncio.sleep(espera)
            await self._esperar_turno()

            try:
                respuesta = await cliente.get(url, params=parametros)
            except (httpx.TimeoutException, httpx.TransportError) as error:
                ultimo = error
                continue

            if respuesta.status_code in CODIGOS_REINTENTABLES:
                ultimo = httpx.HTTPStatusError(
                    f"OSRM devolvió {respuesta.status_code}",
                    request=respuesta.request,
                    response=respuesta,
                )
                continue

            # Un 4xx que no sea 429 es culpa de la petición: reintentarlo solo
            # gasta el presupuesto de peticiones del servidor público.
            respuesta.raise_for_status()

            try:
                cuerpo = respuesta.json()
            except ValueError as error:
                raise ErrorOSRM(f"OSRM devolvió algo que no es JSON: {error}") from error

            codigo = cuerpo.get("code")
            if codigo != "Ok":
                raise ErrorOSRM(
                    f"OSRM respondió code={codigo!r}: {cuerpo.get('message', 'sin detalle')}"
                )
            return cuerpo

        raise RoutingNoDisponible(
            f"OSRM no responde tras {self._intentos} intentos ({ultimo}). No se calcula el "
            "plan con distancias aproximadas: el resultado sería silenciosamente peor."
        ) from ultimo

    async def _esperar_turno(self) -> None:
        """Espacia las peticiones para respetar el ~1 req/s del OSRM público (§9)."""
        if self._intervalo_minimo_s <= 0:
            return
        async with self._turno:
            pendiente = self._ultima_peticion + self._intervalo_minimo_s - time.monotonic()
            if pendiente > 0:
                await asyncio.sleep(pendiente)
            self._ultima_peticion = time.monotonic()


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------


def _coords(puntos: Sequence[Coordenada]) -> str:
    """Formatea para OSRM, que quiere ``lon,lat`` y no ``lat,lon``.

    Invertirlo no da error: da rutas por Marruecos. Toda la conversión pasa por
    aquí para que solo haya un sitio donde equivocarse.
    """
    return ";".join(f"{p.lon:.6f},{p.lat:.6f}" for p in puntos)


def decodificar_polilinea(codificada: str, precision: int = 5) -> tuple[Coordenada, ...]:
    """Decodifica el *encoded polyline* de Google que devuelve OSRM.

    Se pide ``geometries=polyline`` en vez de GeoJSON porque la polilínea de una
    ruta larga con ``overview=full`` son cientos de kilobytes en GeoJSON.
    """
    if not codificada:
        return ()

    factor = 10.0**precision
    coordenadas: list[Coordenada] = []
    indice = lat = lon = 0

    while indice < len(codificada):
        for eje in ("lat", "lon"):
            resultado = desplazamiento = 0
            while True:
                if indice >= len(codificada):
                    raise ValueError("Polilínea truncada: se acabó la cadena a mitad de un número.")
                byte = ord(codificada[indice]) - 63
                indice += 1
                resultado |= (byte & 0x1F) << desplazamiento
                desplazamiento += 5
                if byte < 0x20:
                    break
            # El bit bajo marca el signo; el resto es el delta respecto al punto
            # anterior, que es lo que hace compacto el formato.
            delta = ~(resultado >> 1) if resultado & 1 else resultado >> 1
            if eje == "lat":
                lat += delta
            else:
                lon += delta
        coordenadas.append(Coordenada(lat=lat / factor, lon=lon / factor))

    return tuple(coordenadas)
