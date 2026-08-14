"""Reglas que viven en los modelos del dominio.

Sobre todo la antigüedad de precios (ARQUITECTURA.md §4.2): la regla es del
dominio, aunque quien la aplique en SQL sea el adaptador.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.models import PRECIO_MAX_ANTIGUEDAD_H, Precio

T0 = datetime(2026, 8, 12, 7, 0, 0)


def precio(momento: datetime = T0) -> Precio:
    return Precio(estacion_id=1, producto="diesel", precio_milesimas=1599, valid_from=momento)


def test_el_umbral_por_defecto_son_48_horas() -> None:
    assert PRECIO_MAX_ANTIGUEDAD_H == 48


@pytest.mark.parametrize(
    ("horas", "vigente"),
    [(0, True), (1, True), (47, True), (48, True), (48.5, False), (72, False), (24 * 7, False)],
)
def test_vigencia_segun_la_antiguedad(horas: float, vigente: bool) -> None:
    assert precio().esta_vigente(ahora=T0 + timedelta(hours=horas)) is vigente


def test_antiguedad_en_horas() -> None:
    assert precio().antiguedad_horas(ahora=T0 + timedelta(hours=30)) == pytest.approx(30.0)


def test_el_umbral_es_ajustable() -> None:
    """Es una constante de ajuste, no una decisión de diseño (§4.2)."""
    caducado = precio().esta_vigente(ahora=T0 + timedelta(hours=72))
    assert not caducado
    assert precio().esta_vigente(ahora=T0 + timedelta(hours=72), max_antiguedad_h=96)


def test_euros_por_litro_es_decimal_exacto() -> None:
    """Ni un float en el camino del dinero (§4, §8.3)."""
    valor = Precio(
        estacion_id=1, producto="diesel", precio_milesimas=1659, valid_from=T0
    ).euros_por_litro
    assert valor == Decimal("1.659")
    assert isinstance(valor, Decimal)
