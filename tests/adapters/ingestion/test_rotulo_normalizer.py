"""Tests de la normalización de rótulos.

Los casos "trampa" no son inventados: salen de rótulos reales del snapshot del
Geoportal de agosto de 2026.
"""

from __future__ import annotations

import pytest

from app.adapters.ingestion.rotulo_normalizer import INDEPENDIENTE, normalizar_rotulo


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        # Marcas limpias
        ("REPSOL", "REPSOL"),
        ("repsol", "REPSOL"),
        ("  GALP  ", "GALP"),
        # Variantes con ruido alrededor
        ("REPSOL S.A.", "REPSOL"),
        ("E.S. REPSOL", "REPSOL"),
        ("ES DULANTZI REPSOL", "REPSOL"),
        ("ESTACION DE SERVICIO REPSOL ** CASI **", "REPSOL"),
        ("ALCAMPO, S.A.", "ALCAMPO"),
        ("GALP&GO", "GALP"),
        ('INLOCOR S.L. "CEPSA"', "MOEVE"),
        ("E.S.  AVINYÓ   -CEPSA-", "MOEVE"),
        # Separadores no estándar: el guion bajo también separa
        ("DISA_SHELL", "SHELL"),
        # Cepsa y Moeve agrupadas bajo el nombre comercial actual
        ("CEPSA", "MOEVE"),
        ("CEPSA-MOEVE", "MOEVE"),
        ("MOEVE", "MOEVE"),
        ("CEPSA ESPAÑA S.A.", "MOEVE"),
        # Sin marca reconocible
        ("Nº 10.935", INDEPENDIENTE),
        ("ES LA VENTA", INDEPENDIENTE),
        ("", INDEPENDIENTE),
    ],
)
def test_normaliza_marcas(crudo: str, esperado: str) -> None:
    assert normalizar_rotulo(crudo) == esperado


@pytest.mark.parametrize(
    ("crudo", "esperado"),
    [
        # El motivo de comparar por palabra completa y no por substring:
        # con `"AVIA" in raw` estas tres estaciones se clasificarían como AVIA.
        ("CEPSA LA GAVIA 365", "MOEVE"),
        ("BP LA GAVIA 365", "BP"),
        ("BP VALDAVIA", "BP"),
        ("CAMPIEZO -AVIA", "AVIA"),
        ("AVIA-KANTOI", "AVIA"),
    ],
)
def test_no_confunde_avia_con_gavia(crudo: str, esperado: str) -> None:
    assert normalizar_rotulo(crudo) == esperado


def test_todo_rotulo_devuelve_algo_utilizable() -> None:
    """Nunca None ni cadena vacía: `estaciones.rotulo` es NOT NULL."""
    for crudo in ["", "   ", "???", "12345", "Ñ"]:
        assert normalizar_rotulo(crudo)
