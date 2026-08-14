"""Cliente de la API REST del Geoportal de Carburantes (MITECO).

Trampas de esta API, todas ellas fuente conocida de bugs (ARQUITECTURA.md §4 y §9):

- **Coma decimal** en precios, latitud y longitud: ``"1,699"``, ``"-1,539167"``.
- El campo de longitud se llama ``"Longitud (WGS84)"``, no ``"Longitud"``.
- Precios como cadena vacía cuando la estación no vende ese producto.
- ``Tipo Venta``: hay que quedarse solo con la pública (``P``).
- El servicio corta la conexión según User-Agent: se manda uno de navegador.

La descarga y el parseo están separados a propósito: ``parsear()`` es una función
pura sobre el JSON ya cargado, así que los tests corren contra un snapshot local
y no dependen de que el Ministerio esté vivo.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from app.adapters.ingestion.productos import (
    CODIGOS_PRODUCTO,
    campos_de_precio_desconocidos,
)
from app.adapters.ingestion.rotulo_normalizer import normalizar_rotulo
from app.domain.models import Estacion, Precio

logger = logging.getLogger(__name__)

CLAVE_LISTA = "ListaEESSPrecio"
CLAVE_FECHA = "Fecha"
FORMATO_FECHA = "%d/%m/%Y %H:%M:%S"
TIPO_VENTA_PUBLICA = "P"

# La API a veces escribe el nombre del campo de una forma u otra según el endpoint.
CAMPOS_LONGITUD = ("Longitud (WGS84)", "Longitud")


@dataclass
class ResultadoIngesta:
    """Lo que sale de una pasada de ingesta, listo para entregar al ``PriceStore``."""

    fecha: datetime
    estaciones: list[Estacion] = field(default_factory=list)
    precios: list[Precio] = field(default_factory=list)
    descartadas_no_publicas: int = 0
    descartadas_sin_coordenadas: int = 0
    descartadas_sin_id: int = 0
    precios_invalidos: int = 0
    productos_desconocidos: set[str] = field(default_factory=set)

    def resumen(self) -> str:
        return (
            f"fecha={self.fecha:%Y-%m-%d %H:%M:%S} "
            f"estaciones={len(self.estaciones)} precios={len(self.precios)} "
            f"descartadas(no_publicas={self.descartadas_no_publicas}, "
            f"sin_coords={self.descartadas_sin_coordenadas}, "
            f"sin_id={self.descartadas_sin_id}) "
            f"precios_invalidos={self.precios_invalidos}"
        )


class ErrorGeoportal(RuntimeError):
    """La respuesta del Geoportal no tiene la forma esperada."""


# --------------------------------------------------------------------------
# Parseo de números en formato español
# --------------------------------------------------------------------------


def parsear_decimal_es(bruto: str | None) -> Decimal | None:
    """Convierte ``"1,699"`` o ``"-1.234,56"`` en ``Decimal``.

    ``Decimal`` y no ``float`` en todo el camino: el DP acumula error y
    desestabiliza los desempates entre estaciones (ARQUITECTURA.md §4).

    Regla para no romperse con separador de miles: si aparecen punto y coma, el
    punto es separador de miles y la coma es decimal. Si solo hay coma, es
    decimal. Si solo hay punto, ya viene en formato anglosajón.
    """
    if bruto is None:
        return None
    texto = str(bruto).strip()
    if not texto:
        return None
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return Decimal(texto)
    except InvalidOperation:
        return None


def parsear_precio_milesimas(bruto: str | None) -> int | None:
    """``"1,699"`` -> ``1699``. Devuelve ``None`` si no hay precio o no es válido."""
    valor = parsear_decimal_es(bruto)
    if valor is None or valor <= 0:
        return None
    return int((valor * 1000).to_integral_value())


def parsear_coordenada(bruto: str | None) -> float | None:
    """Latitud/longitud a ``float``.

    Aquí sí vale ``float``: son coordenadas para un filtro geográfico grueso, no
    entran en la aritmética de costes. El paso por ``Decimal`` es solo para
    aplicar la misma regla de coma decimal.
    """
    valor = parsear_decimal_es(bruto)
    return None if valor is None else float(valor)


# --------------------------------------------------------------------------
# Cliente
# --------------------------------------------------------------------------


class GeoportalClient:
    def __init__(
        self,
        url: str,
        user_agent: str,
        snapshot_dir: Path | str | None = None,
        timeout: float = 180.0,
    ) -> None:
        self._url = url
        self._user_agent = user_agent
        self._snapshot_dir = Path(snapshot_dir) if snapshot_dir else None
        self._timeout = timeout

    # -- Descarga --

    async def descargar(self, guardar_snapshot: bool = True) -> dict[str, Any]:
        """Baja el snapshot completo y (por defecto) lo deja en disco tal cual.

        Guardar el crudo permite desarrollar y testear sin depender de que el
        servicio esté disponible (ARQUITECTURA.md §9).
        """
        cabeceras = {
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as cliente:
            respuesta = await cliente.get(self._url, headers=cabeceras)
            respuesta.raise_for_status()
            crudo = respuesta.content

        if guardar_snapshot and self._snapshot_dir is not None:
            self.guardar_snapshot(crudo)

        return json.loads(crudo.decode("utf-8-sig"))

    def guardar_snapshot(self, crudo: bytes) -> Path:
        assert self._snapshot_dir is not None, "snapshot_dir no configurado"
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        destino = self._snapshot_dir / f"geoportal_{datetime.now():%Y%m%d_%H%M%S}.json"
        destino.write_bytes(crudo)
        logger.info("Snapshot crudo guardado en %s (%d bytes)", destino, len(crudo))
        return destino

    @staticmethod
    def cargar_snapshot(ruta: Path | str) -> dict[str, Any]:
        """Carga un snapshot guardado en disco, para desarrollo y tests."""
        return json.loads(Path(ruta).read_text(encoding="utf-8-sig"))

    async def obtener(self, guardar_snapshot: bool = True) -> ResultadoIngesta:
        return self.parsear(await self.descargar(guardar_snapshot=guardar_snapshot))

    # -- Parseo (puro: sin red ni disco) --

    def parsear(self, payload: dict[str, Any]) -> ResultadoIngesta:
        if CLAVE_LISTA not in payload:
            raise ErrorGeoportal(
                f"La respuesta no trae {CLAVE_LISTA!r}. Claves: {sorted(payload)}"
            )

        fecha = self._parsear_fecha(payload.get(CLAVE_FECHA))
        resultado = ResultadoIngesta(fecha=fecha)

        for bruto in payload[CLAVE_LISTA]:
            resultado.productos_desconocidos |= campos_de_precio_desconocidos(bruto.keys())

            # Filtrar tipo de venta ANTES de guardar (ARQUITECTURA.md §4).
            if (bruto.get("Tipo Venta") or "").strip().upper() != TIPO_VENTA_PUBLICA:
                resultado.descartadas_no_publicas += 1
                continue

            estacion = self._parsear_estacion(bruto, resultado)
            if estacion is None:
                continue

            resultado.estaciones.append(estacion)
            resultado.precios.extend(self._parsear_precios(bruto, estacion.id, fecha, resultado))

        if resultado.productos_desconocidos:
            logger.warning(
                "Productos no contemplados en productos.py: %s",
                sorted(resultado.productos_desconocidos),
            )
        return resultado

    @staticmethod
    def _parsear_fecha(bruto: str | None) -> datetime:
        if bruto:
            try:
                return datetime.strptime(bruto.strip(), FORMATO_FECHA)
            except ValueError:
                logger.warning("Fecha no parseable en la respuesta: %r", bruto)
        return datetime.now()

    @staticmethod
    def _parsear_estacion(
        bruto: dict[str, Any], resultado: ResultadoIngesta
    ) -> Estacion | None:
        id_bruto = (bruto.get("IDEESS") or "").strip()
        if not id_bruto.isdigit():
            resultado.descartadas_sin_id += 1
            return None

        lat = parsear_coordenada(bruto.get("Latitud"))
        lon = next(
            (
                valor
                for campo in CAMPOS_LONGITUD
                if (valor := parsear_coordenada(bruto.get(campo))) is not None
            ),
            None,
        )
        if lat is None or lon is None:
            resultado.descartadas_sin_coordenadas += 1
            return None

        rotulo_raw = (bruto.get("Rótulo") or "").strip()
        return Estacion(
            id=int(id_bruto),
            rotulo=normalizar_rotulo(rotulo_raw),
            rotulo_raw=rotulo_raw,
            lat=lat,
            lon=lon,
            direccion=(bruto.get("Dirección") or "").strip() or None,
            municipio=(bruto.get("Municipio") or "").strip() or None,
            provincia=(bruto.get("Provincia") or "").strip() or None,
            horario=(bruto.get("Horario") or "").strip() or None,
        )

    @staticmethod
    def _parsear_precios(
        bruto: dict[str, Any],
        estacion_id: int,
        fecha: datetime,
        resultado: ResultadoIngesta,
    ) -> list[Precio]:
        precios: list[Precio] = []
        for campo, producto in CODIGOS_PRODUCTO.items():
            valor_bruto = bruto.get(campo)
            if valor_bruto is None or not str(valor_bruto).strip():
                continue  # la estación no vende ese producto
            milesimas = parsear_precio_milesimas(valor_bruto)
            if milesimas is None:
                resultado.precios_invalidos += 1
                continue
            precios.append(
                Precio(
                    estacion_id=estacion_id,
                    producto=producto,
                    precio_milesimas=milesimas,
                    valid_from=fecha,
                )
            )
        return precios
