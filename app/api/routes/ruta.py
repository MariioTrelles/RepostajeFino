"""Endpoint de ruta óptima: une las tres piezas (ARQUITECTURA.md §12, paso 4).

El router es un adaptador de entrada de facto y no necesita otra capa por
debajo (§3): traduce JSON a objetos del dominio, orquesta
``RoutingProvider`` + ``PriceStore`` + DP, y traduce el resultado de vuelta.
Toda la lógica de qué se optimiza está en ``domain/``.

Stateless (§3): el request trae todo —origen, destino y el ``Vehiculo``
completo—. No hay sesión ni perfil guardado en el servidor.

Los fallos no se disfrazan de éxito (§8.4): un trayecto inviable, un conductor
por debajo de la reserva o un OSRM caído tienen cada uno su código y su cuerpo
explicando qué pasa y con qué números.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.adapters.ingestion.productos import (
    CODIGOS_PRODUCTO,
    PRODUCTOS_NO_PROPULSION,
)
from app.adapters.ingestion.rotulo_normalizer import MARCAS_FILTRABLES
from app.adapters.routing.osrm_adapter import ErrorOSRM, RoutingNoDisponible
from app.api.dependencies import get_price_store, get_routing_provider
from app.domain import seleccion_candidatas as seleccion
from app.domain.dp_optimizer import (
    ParametrosOptimizacion,
    TrayectoInviable,
    VehiculoInvalido,
    optimizar_repostaje,
)
from app.domain.models import Estacion, EstacionCandidata, Parada, Precio, Vehiculo
from app.domain.ports.price_store import PriceStore
from app.domain.ports.routing_provider import Coordenada, RoutingProvider
from app.domain.precio_efectivo import precio_efectivo

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["ruta"])

PRODUCTOS_VALIDOS = frozenset(CODIGOS_PRODUCTO.values()) - PRODUCTOS_NO_PROPULSION

# El número y no `status.HTTP_422_...`: la constante cambió de nombre entre
# versiones de Starlette y el 422 lleva ahí desde siempre.
HTTP_NO_PROCESABLE = 422


# ---------------------------------------------------------------------------
# Entrada
# ---------------------------------------------------------------------------


class CoordenadaIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)

    def a_dominio(self) -> Coordenada:
        return Coordenada(lat=self.lat, lon=self.lon)


class VehiculoIn(BaseModel):
    """Los mismos campos que ``domain.Vehiculo``, validados en el borde.

    La validación de coherencia (nivel <= capacidad, reserva razonable) no se
    duplica aquí: la hace el DP, que es de quien es la regla.
    """

    consumo_l_100km: float = Field(gt=0, le=100)
    tipo_combustible: str
    capacidad_deposito_l: float = Field(gt=0, le=1000)
    nivel_actual_l: float = Field(ge=0, le=1000)
    reserva_minima_l: float = Field(default=5.0, ge=0, le=100)

    @model_validator(mode="after")
    def _combustible_conocido(self) -> VehiculoIn:
        if self.tipo_combustible in PRODUCTOS_NO_PROPULSION:
            raise ValueError(
                f"{self.tipo_combustible!r} no es carburante de automoción: no se puede "
                "optimizar un viaje con él."
            )
        if self.tipo_combustible not in PRODUCTOS_VALIDOS:
            raise ValueError(
                f"Combustible desconocido: {self.tipo_combustible!r}. "
                f"Válidos: {sorted(PRODUCTOS_VALIDOS)}"
            )
        return self

    def a_dominio(self) -> Vehiculo:
        return Vehiculo(
            consumo_l_100km=self.consumo_l_100km,
            tipo_combustible=self.tipo_combustible,
            capacidad_deposito_l=self.capacidad_deposito_l,
            nivel_actual_l=self.nivel_actual_l,
            reserva_minima_l=self.reserva_minima_l,
        )


class RutaOptimaRequest(BaseModel):
    origen: CoordenadaIn
    destino: CoordenadaIn
    vehiculo: VehiculoIn
    rotulos: list[str] | None = Field(
        default=None,
        description=(
            "Filtro por marca. Sin él salen todas, incluidas las independientes. "
            "`INDEPENDIENTE` no es una opción filtrable (§6)."
        ),
    )
    valor_tiempo_eur_h: float = Field(default=15.0, ge=0, le=500)
    tiempo_parada_s: float = Field(default=300.0, ge=0, le=7200)
    max_candidatas: int = Field(default=50, ge=2, le=250)
    margen_corredor_km: float = Field(default=5.0, gt=0, le=50)

    @model_validator(mode="after")
    def _marcas_filtrables(self) -> RutaOptimaRequest:
        if self.rotulos is not None:
            desconocidas = sorted(set(self.rotulos) - set(MARCAS_FILTRABLES))
            if desconocidas:
                raise ValueError(
                    f"Marcas no filtrables: {desconocidas}. "
                    f"Las filtrables son: {list(MARCAS_FILTRABLES)}"
                )
        return self


# ---------------------------------------------------------------------------
# Salida
# ---------------------------------------------------------------------------


class EstacionOut(BaseModel):
    id: int
    rotulo: str
    direccion: str | None
    municipio: str | None
    provincia: str | None
    lat: float
    lon: float

    @classmethod
    def de(cls, estacion: Estacion) -> EstacionOut:
        return cls(
            id=estacion.id,
            rotulo=estacion.rotulo,
            direccion=estacion.direccion,
            municipio=estacion.municipio,
            provincia=estacion.provincia,
            lat=estacion.lat,
            lon=estacion.lon,
        )


class PrecioOut(BaseModel):
    """Precio para pintar. ``vigente`` implementa §4.2: caducado se muestra marcado."""

    producto: str
    eur_litro: float
    valid_from: datetime
    vigente: bool
    antiguedad_horas: float

    @classmethod
    def de(cls, precio: Precio, ahora: datetime) -> PrecioOut:
        return cls(
            producto=precio.producto,
            eur_litro=float(precio.euros_por_litro),
            valid_from=precio.valid_from,
            vigente=precio.esta_vigente(ahora),
            antiguedad_horas=round(precio.antiguedad_horas(ahora), 1),
        )


class CandidataOut(BaseModel):
    estacion: EstacionOut
    precio: PrecioOut
    precio_efectivo_eur_litro: float

    @classmethod
    def de(cls, candidata: EstacionCandidata, ahora: datetime) -> CandidataOut:
        efectivo = precio_efectivo(candidata.estacion, candidata.precio_milesimas)
        return cls(
            estacion=EstacionOut.de(candidata.estacion),
            precio=PrecioOut.de(candidata.precio, ahora),
            precio_efectivo_eur_litro=efectivo / 1000,
        )


class ParadaOut(BaseModel):
    estacion: EstacionOut
    precio_efectivo_eur_litro: float
    litros: float
    coste_eur: float
    km_desde_origen: float
    nivel_llegada_l: float
    nivel_salida_l: float

    @classmethod
    def de(cls, parada: Parada) -> ParadaOut:
        return cls(
            estacion=EstacionOut.de(parada.estacion),
            precio_efectivo_eur_litro=parada.precio_efectivo_milesimas / 1000,
            litros=round(parada.litros, 2),
            coste_eur=_euros(parada.coste_eur),
            km_desde_origen=round(parada.km_desde_origen, 1),
            nivel_llegada_l=round(parada.nivel_llegada_l, 2),
            nivel_salida_l=round(parada.nivel_salida_l, 2),
        )


class TramoOut(BaseModel):
    desde: str
    hasta: str
    distancia_km: float
    duracion_s: float
    combustible_l: float
    nivel_llegada_l: float


class RutaOptimaResponse(BaseModel):
    """El plan, más lo que el mapa necesita para pintarse de una sola llamada."""

    distancia_directa_km: float
    duracion_directa_s: float
    geometria: list[tuple[float, float]] = Field(
        description="Polilínea de la ruta directa, en pares (lat, lon), para el mapa."
    )
    paradas: list[ParadaOut]
    tramos: list[TramoOut]
    coste_combustible_eur: float
    coste_tiempo_eur: float
    coste_total_eur: float
    litros_repostados: float
    distancia_total_km: float
    duracion_total_s: float
    desvio_km: float
    desvio_s: float
    nivel_llegada_destino_l: float
    candidatas: list[CandidataOut]
    avisos: list[str]


class EstacionCercanaOut(BaseModel):
    estacion: EstacionOut
    precio: PrecioOut
    distancia_km: float


class BajoReservaOut(BaseModel):
    """Respuesta al conductor que ya va por debajo de la reserva (§7)."""

    detalle: str
    tipo: Literal["bajo_reserva"] = "bajo_reserva"
    estaciones_cercanas: list[EstacionCercanaOut]


class TrayectoInviableOut(BaseModel):
    detalle: str
    tipo: Literal["trayecto_inviable"] = "trayecto_inviable"
    desde: str
    hasta: str
    distancia_km: float
    autonomia_km: float
    faltan_km: float
    candidatas_totales: int
    candidatas_alcanzables: int


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/ruta-optima",
    response_model=RutaOptimaResponse,
    summary="Dónde repostar y cuánto para minimizar el coste del viaje",
)
async def ruta_optima(
    peticion: RutaOptimaRequest,
    store: Annotated[PriceStore, Depends(get_price_store)],
    routing: Annotated[RoutingProvider, Depends(get_routing_provider)],
) -> RutaOptimaResponse:
    ahora = datetime.now()
    vehiculo = peticion.vehiculo.a_dominio()
    origen = peticion.origen.a_dominio()
    destino = peticion.destino.a_dominio()

    # El caso "ya voy por debajo de la reserva" se resuelve antes de gastar una
    # sola petición en OSRM: la pregunta correcta no es cuál es la ruta óptima.
    if vehiculo.nivel_actual_l < vehiculo.reserva_minima_l:
        raise _bajo_reserva(store, origen, vehiculo, ahora)

    ruta = await _con_osrm(routing.ruta(origen, destino))

    parametros_seleccion = seleccion.ParametrosSeleccion(
        margen_km=peticion.margen_corredor_km,
        max_candidatas=peticion.max_candidatas,
    )
    # SQLite es síncrono: fuera del hilo del bucle de eventos.
    candidatas = await asyncio.to_thread(
        seleccion.seleccionar,
        store,
        ruta,
        vehiculo,
        peticion.rotulos,
        parametros_seleccion,
        None,
        ahora,
    )
    if not candidatas:
        raise HTTPException(
            status_code=HTTP_NO_PROCESABLE,
            detail=(
                "No hay ninguna estación con precio vigente de "
                f"{vehiculo.tipo_combustible!r} en el corredor de la ruta. Prueba a "
                "ensanchar `margen_corredor_km` o a quitar el filtro de marcas."
            ),
        )

    puntos = seleccion.puntos_para_la_matriz(origen, candidatas, destino)
    logger.info(
        "Ruta de %.0f km: %d candidatas, ~%d peticiones a /table.",
        ruta.distancia_km,
        len(candidatas),
        seleccion.peticiones_estimadas(len(puntos)),
    )
    matriz = await _con_osrm(routing.matriz(puntos))

    try:
        depurada = seleccion.depurar_matriz(candidatas, matriz)
    except seleccion.ExtremosSinRuta as error:
        raise HTTPException(
            status_code=HTTP_NO_PROCESABLE, detail=str(error)
        ) from error

    avisos: list[str] = []
    if depurada.descartadas:
        # Degradación explícita: el plan sale con lo que OSRM sí resolvió, pero
        # se dice cuánto se ha quedado fuera (§8.4).
        avisos.append(
            f"{len(depurada.descartadas)} estaciones se han descartado porque OSRM no "
            "supo calcular la ruta hasta ellas."
        )
    if not depurada.candidatas:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="OSRM no ha sabido resolver ninguna de las estaciones candidatas.",
        )

    try:
        plan = optimizar_repostaje(
            vehiculo,
            depurada.candidatas,
            depurada.distancias_km,
            depurada.duraciones_s,
            ParametrosOptimizacion(
                valor_tiempo_eur_h=peticion.valor_tiempo_eur_h,
                tiempo_parada_s=peticion.tiempo_parada_s,
            ),
        )
    except TrayectoInviable as error:
        raise HTTPException(
            status_code=HTTP_NO_PROCESABLE,
            detail=_inviable(error).model_dump(mode="json"),
        ) from error
    except VehiculoInvalido as error:
        raise HTTPException(
            status_code=HTTP_NO_PROCESABLE, detail=str(error)
        ) from error

    return RutaOptimaResponse(
        distancia_directa_km=round(ruta.distancia_km, 1),
        duracion_directa_s=round(ruta.duracion_s),
        geometria=[(p.lat, p.lon) for p in ruta.geometria],
        paradas=[ParadaOut.de(p) for p in plan.paradas],
        tramos=[
            TramoOut(
                desde=t.desde,
                hasta=t.hasta,
                distancia_km=round(t.distancia_km, 1),
                duracion_s=round(t.duracion_s),
                combustible_l=round(t.combustible_l, 2),
                nivel_llegada_l=round(t.nivel_llegada_l, 2),
            )
            for t in plan.tramos
        ],
        coste_combustible_eur=_euros(plan.coste_combustible_eur),
        coste_tiempo_eur=_euros(plan.coste_tiempo_eur),
        coste_total_eur=_euros(plan.coste_total_eur),
        litros_repostados=round(plan.litros_repostados, 2),
        distancia_total_km=round(plan.distancia_total_km, 1),
        duracion_total_s=round(plan.duracion_total_s),
        desvio_km=round(plan.desvio_km, 1),
        desvio_s=round(plan.desvio_s),
        nivel_llegada_destino_l=round(plan.nivel_llegada_destino_l, 2),
        candidatas=[CandidataOut.de(c, ahora) for c in depurada.candidatas],
        avisos=avisos,
    )


@router.get("/marcas", summary="Marcas que se pueden usar en el filtro")
async def marcas() -> list[str]:
    """`INDEPENDIENTE` no está en la lista a propósito (§6 y §11)."""
    return list(MARCAS_FILTRABLES)


# ---------------------------------------------------------------------------
# Traducción de fallos
# ---------------------------------------------------------------------------


async def _con_osrm(espera):
    """Convierte los fallos del adaptador en respuestas HTTP honestas (§8.4)."""
    try:
        return await espera
    except RoutingNoDisponible as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)
        ) from error
    except ErrorOSRM as error:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(error)
        ) from error


def _bajo_reserva(
    store: PriceStore, origen: Coordenada, vehiculo: Vehiculo, ahora: datetime
) -> HTTPException:
    cercanas = seleccion.estaciones_cercanas(
        store, origen, vehiculo.tipo_combustible, ahora=ahora
    )
    cuerpo = BajoReservaOut(
        detalle=(
            f"El nivel actual ({vehiculo.nivel_actual_l:g} L) está por debajo de la "
            f"reserva mínima ({vehiculo.reserva_minima_l:g} L). Antes de optimizar un "
            "viaje hay que repostar: estas son las estaciones más cercanas al origen."
        ),
        estaciones_cercanas=[
            EstacionCercanaOut(
                estacion=EstacionOut.de(c.estacion),
                precio=PrecioOut.de(c.precio, ahora),
                distancia_km=round(c.distancia_km, 1),
            )
            for c in cercanas
        ],
    )
    # `mode="json"` no es cosmético: el cuerpo lleva un `datetime` y HTTPException
    # no pasa por el serializador de Pydantic.
    return HTTPException(
        status_code=HTTP_NO_PROCESABLE, detail=cuerpo.model_dump(mode="json")
    )


def _inviable(error: TrayectoInviable) -> TrayectoInviableOut:
    diagnostico = error.diagnostico
    return TrayectoInviableOut(
        detalle=str(error),
        desde=diagnostico.desde,
        hasta=diagnostico.hasta,
        distancia_km=round(diagnostico.distancia_km, 1),
        autonomia_km=round(diagnostico.autonomia_km, 1),
        faltan_km=round(diagnostico.faltan_km, 1),
        candidatas_totales=diagnostico.candidatas_totales,
        candidatas_alcanzables=diagnostico.candidatas_alcanzables,
    )


def _euros(valor: Decimal) -> float:
    """A céntimos para presentar. El dinero exacto vive en el dominio, en Decimal."""
    return float(round(valor, 2))
