"""Precio efectivo: lo que de verdad paga el conductor (ARQUITECTURA.md §6.1).

El Geoportal publica el precio **nominal**, pero el que paga cada conductor
depende de sus descuentos de fidelización (tarjeta de marca, app de flota,
acuerdo de empresa...). Dos personas frente al mismo surtidor pueden tener un
coste distinto, y eso puede cambiar cuál es la estación *óptima*, no solo cuál
se ve más barata en el mapa.

Por eso el DP, el ranking y el mapa **nunca** usan ``precio_milesimas`` en
crudo: pasan por aquí.

**Fase 1 (ahora)**: no hay perfil de usuario — el diseño es stateless (§3), así
que ``usuario`` siempre llega a ``None`` y esta función es la identidad. Existe
igualmente, y a propósito: meterla más adelante obligaría a tocar el DP, el
caching y la presentación a la vez, porque los tres asumirían que "precio" es un
único número por estación. Con la función puesta desde el día uno, añadir
descuentos es cambiar la implementación de una función pura.

**Fase 2 (futuro)**: ``PerfilDescuento`` viaja en el propio request (coherente
con stateless, no se guarda en servidor) con las marcas y el descuento que
aplique. Solo cambia este módulo.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.models import Estacion


@dataclass(frozen=True, slots=True)
class PerfilDescuento:
    """Descuentos de fidelización del conductor. **Fase 2**, aún sin contenido.

    Está vacío a propósito: inventarle campos hoy sería adivinar cómo se
    modelan descuentos que todavía no se han diseñado. Lo que sí fija ya es el
    tipo que atraviesa la firma pública, para que añadirlo después no cambie
    ninguna signatura fuera de este módulo.
    """


def precio_efectivo(
    estacion: Estacion,
    precio_nominal: int,
    usuario: PerfilDescuento | None = None,
) -> int:
    """Precio en milésimas de euro que realmente paga ``usuario`` en ``estacion``.

    ``usuario`` es opcional para que fase 1 no tenga que inventarse un
    ``PerfilDescuento`` vacío solo para cumplir la firma.

    Devuelve un **entero en milésimas**, igual que el nominal: la aritmética de
    dinero de este proyecto no pasa por ``float`` en ningún punto (§8.3).

    Raises:
        NotImplementedError: si se pasa un perfil de descuento. Aplicar la
            identidad a un usuario con descuentos daría un precio silenciosamente
            equivocado, y con él un plan equivocado; mejor romper y que se vea.
    """
    if usuario is not None:
        raise NotImplementedError(
            "Los descuentos de fidelización son fase 2 (ARQUITECTURA.md §6.1). "
            "Hoy `precio_efectivo` es la identidad y solo acepta usuario=None."
        )
    return precio_nominal
