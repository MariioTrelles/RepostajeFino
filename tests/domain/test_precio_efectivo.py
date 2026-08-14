"""Precio efectivo (ARQUITECTURA.md §6.1).

En fase 1 la función es la identidad, así que lo único que hay que fijar con
tests es justamente eso: que sea la identidad, que devuelva enteros y que no
finja saber aplicar descuentos que todavía no existen.
"""

from __future__ import annotations

import pytest

from app.domain.models import Estacion
from app.domain.precio_efectivo import PerfilDescuento, precio_efectivo

ESTACION = Estacion(
    id=1,
    rotulo="REPSOL",
    rotulo_raw="E.S. REPSOL",
    lat=40.4,
    lon=-3.7,
    direccion="CALLE MAYOR, 1",
)


@pytest.mark.parametrize("nominal", [1, 999, 1599, 1699, 25_000])
def test_en_fase_1_es_la_identidad(nominal: int) -> None:
    efectivo = precio_efectivo(ESTACION, nominal)
    assert efectivo == nominal
    assert isinstance(efectivo, int)
    assert not isinstance(efectivo, float)


def test_usuario_es_opcional() -> None:
    """Fase 1 no debe verse obligada a inventarse un perfil vacío (§6.1)."""
    assert precio_efectivo(ESTACION, 1599) == precio_efectivo(ESTACION, 1599, None)


def test_con_perfil_de_descuento_falla_en_vez_de_mentir() -> None:
    """Aplicar la identidad a alguien con descuentos daría un plan equivocado."""
    with pytest.raises(NotImplementedError, match="fase 2"):
        precio_efectivo(ESTACION, 1599, PerfilDescuento())
