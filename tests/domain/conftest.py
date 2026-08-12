"""Utilidades para montar escenarios de ruta a mano.

Nada de esto toca la BD ni la red: son estaciones y matrices inventadas. Esa es
justamente la prueba de que el dominio está aislado (ARQUITECTURA.md §12).

Convenio de los escenarios: cada estación se coloca en un **punto kilométrico**
del trayecto, y opcionalmente con un ``desvio_km`` (lo que cuesta salir de la vía
y volver). La matriz de distancias se deriva de ahí, que es como se comportaría
OSRM: ir por una estación desviada cuesta el desvío de ida y vuelta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import pytest

from app.domain.models import Estacion, EstacionCandidata, Precio, Vehiculo

MOMENTO = datetime(2026, 8, 12, 7, 0, 0)


@dataclass
class EstacionEnRuta:
    """Una estación situada en un punto kilométrico, con precio y desvío."""

    pk_km: float
    precio_milesimas: int
    rotulo: str = "REPSOL"
    desvio_km: float = 0.0
    desvio_s: float = 0.0

    def candidata(self, id_: int) -> EstacionCandidata:
        return EstacionCandidata(
            estacion=Estacion(
                id=id_,
                rotulo=self.rotulo,
                rotulo_raw=self.rotulo,
                lat=40.0,
                lon=-3.0,
                municipio=f"PK {self.pk_km:g}",
            ),
            precio=Precio(
                estacion_id=id_,
                producto="diesel",
                precio_milesimas=self.precio_milesimas,
                valid_from=MOMENTO,
            ),
        )


def escenario(
    estaciones: list[EstacionEnRuta],
    largo_km: float,
    velocidad_kmh: float = 100.0,
) -> tuple[list[EstacionCandidata], list[list[float]], list[list[float]]]:
    """Construye ``(candidatas, distancias_km, duraciones_s)``.

    Índices: 0 = origen, 1..n = estaciones en orden de PK, n+1 = destino.
    Distancia de i a j = avance por la ruta + el desvío de salir en i y el de
    entrar en j (ida y vuelta al surtidor).
    """
    estaciones = sorted(estaciones, key=lambda e: e.pk_km)
    candidatas = [e.candidata(id_=100 + i) for i, e in enumerate(estaciones)]

    pks = [0.0, *(e.pk_km for e in estaciones), largo_km]
    desvios = [0.0, *(e.desvio_km for e in estaciones), 0.0]
    desvios_s = [0.0, *(e.desvio_s for e in estaciones), 0.0]
    n = len(pks)

    distancias = [[0.0] * n for _ in range(n)]
    duraciones = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            avance = pks[j] - pks[i]
            distancias[i][j] = avance + desvios[i] + desvios[j]
            duraciones[i][j] = (
                avance / velocidad_kmh * 3600.0 + desvios_s[i] + desvios_s[j]
            )
    return candidatas, distancias, duraciones


@pytest.fixture
def coche() -> Vehiculo:
    """Consumo de 10 L/100km a propósito: 1 litro = 10 km, cuentas a mano."""
    return Vehiculo(
        consumo_l_100km=10.0,
        tipo_combustible="diesel",
        capacidad_deposito_l=50.0,
        nivel_actual_l=20.0,
        reserva_minima_l=5.0,
    )
