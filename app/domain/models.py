"""Modelos del dominio.

Objetos planos, sin dependencias de adaptadores, red ni base de datos. Los
construyen tanto la ingesta como el DP, y son la moneda de cambio de los puertos.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import NamedTuple

# Un precio se considera vigente si su ``valid_from`` cae dentro de esta ventana
# (ARQUITECTURA.md §4.2). Ninguna API garantiza que una estación siga reportando:
# una gasolinera puede cerrar o dejar de actualizar y seguir apareciendo en el
# snapshot con su último precio, por antiguo que sea. 48 h cubre dos ciclos de
# ingesta fallidos seguidos sin dar por bueno un precio de la semana pasada.
# Constante ajustable, no decisión de diseño.
PRECIO_MAX_ANTIGUEDAD_H = 48


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
    direccion: str | None = None
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

    def antiguedad_horas(self, ahora: datetime | None = None) -> float:
        return ((ahora or datetime.now()) - self.valid_from).total_seconds() / 3600.0

    def esta_vigente(
        self,
        ahora: datetime | None = None,
        max_antiguedad_h: int = PRECIO_MAX_ANTIGUEDAD_H,
    ) -> bool:
        """¿Sigue siendo creíble este precio? (ARQUITECTURA.md §4.2)

        Un precio caducado **no descarta la estación**: puede tener otros
        productos vigentes, y en el mapa se sigue mostrando marcado como "sin
        actualizar". Lo que queda fuera es este precio concreto: para el DP, una
        estación sin precio vigente del combustible del coche es como si nunca
        hubiera tenido ese producto.
        """
        return (ahora or datetime.now()) - self.valid_from <= timedelta(hours=max_antiguedad_h)


@dataclass(frozen=True, slots=True)
class Vehiculo:
    """Depósito modelado como capacidad + nivel actual (ARQUITECTURA.md §7).

    Más preciso que una autonomía simplificada, y necesario para que el DP decida
    *cuánto* repostar y no solo dónde.

    El consumo va siempre en **L/100km**, nunca en €/100km ni mezclado con el
    precio, para que el dato sea reutilizable venga de donde venga (hoy lo teclea
    el usuario; mañana, quizá, un catálogo de modelos).

    ``tipo_combustible`` es **uno solo**: el que el DP optimiza. No confundir con
    el filtro de combustibles de la UI, que es de visualización y admite varios.
    """

    consumo_l_100km: float
    tipo_combustible: str
    capacidad_deposito_l: float
    nivel_actual_l: float
    reserva_minima_l: float = 5.0

    @property
    def autonomia_maxima_km(self) -> float:
        """Km que puede recorrer con el depósito lleno sin bajar de la reserva."""
        return (self.capacidad_deposito_l - self.reserva_minima_l) * 100.0 / self.consumo_l_100km

    @property
    def autonomia_actual_km(self) -> float:
        """Km que puede recorrer *ahora* sin bajar de la reserva."""
        return (self.nivel_actual_l - self.reserva_minima_l) * 100.0 / self.consumo_l_100km

    def litros_para(self, km: float) -> float:
        return km * self.consumo_l_100km / 100.0


@dataclass(frozen=True, slots=True)
class EstacionCandidata:
    """Estación considerada por el DP, con su precio ya resuelto.

    El DP no consulta precios: los recibe. Quién decidió que esta estación entra
    en la lista (bbox, desvío máximo, filtro de marca) es problema de fuera.
    """

    estacion: Estacion
    precio: Precio

    @property
    def precio_milesimas(self) -> int:
        """Precio **nominal**, el del Geoportal.

        No es el que debe entrar en ninguna cuenta de coste: para eso está
        ``precio_efectivo()`` (ARQUITECTURA.md §6.1), que es lo que aplica los
        descuentos de fidelización del conductor.
        """
        return self.precio.precio_milesimas

    @property
    def euros_por_litro(self) -> Decimal:
        """Precio nominal en euros. Misma advertencia que ``precio_milesimas``."""
        return self.precio.euros_por_litro


class Desvio(NamedTuple):
    """Lo que cuesta pasar por una estación en vez de seguir de largo.

    Se mide **contra la ruta directa**, no contra el plan:

        desvio = d(origen -> estación) + d(estación -> destino) - d(origen -> destino)

    Así es un número por estación, no por plan: se puede calcular antes del DP,
    usarse como filtro de viabilidad y enseñarse en el mapa. Con dos paradas la
    suma de los desvíos individuales no es exactamente el desvío del plan (la
    geometría no es aditiva), pero para rutas cuasi-lineales la diferencia es
    despreciable, y el desvío real del plan se sigue midiendo aparte en
    ``Recomendacion.desvio_km`` (ARQUITECTURA.md §8.2).

    Son kilómetros y segundos de **conducción real**, de la matriz de OSRM, nunca
    distancia perpendicular a la polilínea (§8, punto 2).
    """

    km: float
    segundos: float

    @property
    def minutos(self) -> float:
        return self.segundos / 60.0


@dataclass(frozen=True, slots=True)
class TramoRuta:
    """Un tramo del plan: de un punto al siguiente, sin paradas intermedias."""

    desde: str
    hasta: str
    distancia_km: float
    duracion_s: float
    combustible_l: float
    nivel_llegada_l: float


@dataclass(frozen=True, slots=True)
class Parada:
    """Una parada a repostar: dónde, cuánto y a qué precio.

    ``precio_efectivo_milesimas`` es el precio con el que se hizo la cuenta: el
    que de verdad paga este conductor (§6.1). En fase 1 coincide con el nominal
    de ``candidata``, pero es el que hay que mostrar y el que explica
    ``coste_eur``; el nominal sigue accesible por ``candidata.precio_milesimas``.
    """

    candidata: EstacionCandidata
    precio_efectivo_milesimas: int
    litros: float
    nivel_llegada_l: float
    nivel_salida_l: float
    coste_eur: Decimal
    km_desde_origen: float

    @property
    def estacion(self) -> Estacion:
        return self.candidata.estacion


@dataclass(frozen=True, slots=True)
class Recomendacion:
    """El plan de repostaje: qué hacer y cuánto cuesta.

    **Solo hay una cifra de dinero**, ``coste_combustible_eur``, y son los euros
    que se pagan en el surtidor. El tiempo del conductor no se convierte a euros
    (ARQUITECTURA.md §8.2): el desvío se controla con un límite de viabilidad, y
    lo que el viaje cuesta *de tiempo* se dice en minutos, que es la unidad en la
    que el usuario lo entiende y la única en la que el dato es real.

    ``desvio_km`` y ``desvio_s`` son el **exceso** del plan entero sobre la ruta
    directa, no el viaje completo: si no, el número no significa nada.
    """

    paradas: list[Parada] = field(default_factory=list)
    tramos: list[TramoRuta] = field(default_factory=list)
    coste_combustible_eur: Decimal = Decimal(0)
    litros_repostados: float = 0.0
    distancia_total_km: float = 0.0
    duracion_total_s: float = 0.0
    desvio_km: float = 0.0
    desvio_s: float = 0.0
    nivel_llegada_destino_l: float = 0.0

    @property
    def numero_paradas(self) -> int:
        return len(self.paradas)


@dataclass(frozen=True, slots=True)
class Opcion:
    """Una gasolinera donde el conductor *puede* parar, con lo que le cuesta.

    El plan óptimo responde "dónde repostar más barato". Esto responde otra
    pregunta, que es la que de verdad se hace el conductor: "¿y si prefiero parar
    a las dos horas en vez de a las tres, cuánto me cuesta esa comodidad?"
    (ARQUITECTURA.md §8.6).

    ``sobrecoste_eur`` es esa respuesta: euros de combustible del **viaje entero**
    parando aquí, menos los del plan óptimo. Nunca negativo, y comprobable a mano
    restando dos números que también se le enseñan.

    Ojo: no tiene por qué ser cero en ninguna opción. El plan óptimo puede repartir
    el repostaje en dos paradas y salir más barato que cualquier parada única; lo
    que marca ``es_la_mas_barata`` es la mejor **de las ofrecidas**, que es entre
    lo que el usuario elige de verdad.

    ``plan`` es el plan completo asociado, no solo esta parada: en un viaje largo,
    parar aquí puede seguir obligando a un segundo repostaje más adelante, y el
    usuario tiene que verlo antes de elegir.
    """

    candidata: EstacionCandidata
    desvio: Desvio
    km_desde_origen: float
    tiempo_desde_origen_s: float
    litros: float
    precio_efectivo_milesimas: int
    plan: Recomendacion
    sobrecoste_eur: Decimal
    es_la_mas_barata: bool = False

    @property
    def estacion(self) -> Estacion:
        return self.candidata.estacion

    @property
    def coste_viaje_eur(self) -> Decimal:
        return self.plan.coste_combustible_eur
