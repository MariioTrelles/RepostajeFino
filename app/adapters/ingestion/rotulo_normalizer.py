"""Normalización de rótulos (marcas) del Geoportal.

El campo ``Rótulo`` no viene limpio: cada gestora lo escribe a mano, así que la
misma marca aparece con decenas de variantes. Sin normalizar, el filtro por marca
deja fuera estaciones que sí son de esa marca (ARQUITECTURA.md §4.1).

Diccionario construido sobre un snapshot real (11.514 estaciones, 3.558 rótulos
distintos): cubre las marcas grandes, que es lo que la gente filtra. Lo que no se
reconoce va a ``INDEPENDIENTE``, y ``rotulo_raw`` guarda siempre el original.

**Desviación consciente del ejemplo de §4.1**: allí el matcher es
``any(p in raw_upper for p in patrones)``, un substring pelado. Sobre datos
reales eso produce falsos positivos: ``"AVIA" in "CEPSA LA GAVIA 365"`` clasifica
una estación de Cepsa como Avia. Aquí se compara por **palabra completa**. La
decisión de §4.1 (diccionario manual, aplicado en la ingesta) no cambia.
"""

from __future__ import annotations

import re

INDEPENDIENTE = "INDEPENDIENTE"

# El orden importa: gana el primero que casa. Las marcas más específicas van
# antes que las que podrían solaparse con ellas.
NORMALIZACION_ROTULOS: dict[str, list[str]] = {
    "REPSOL": ["REPSOL"],
    "PETRONOR": ["PETRONOR"],
    "CAMPSA": ["CAMPSA"],
    # Moeve es el nombre comercial actual de Cepsa: se agrupan bajo el nuevo.
    "MOEVE": ["MOEVE", "CEPSA", "C E P S A"],
    "GALP": ["GALP", "GALP&GO", "GALP GO"],
    "BALLENOIL": ["BALLENOIL"],
    "PLENERGY": ["PLENERGY", "PLENOIL"],
    "SHELL": ["SHELL"],
    "PETROPRIX": ["PETROPRIX"],
    "BP": ["BP", "B P"],
    "CARREFOUR": ["CARREFOUR"],
    "AVIA": ["AVIA"],
    "Q8": ["Q8"],
    "ESCLATOIL": ["ESCLATOIL", "ESCLATOIL S A"],
    "BONAREA": ["BONAREA", "BONÀREA"],
    "ALCAMPO": ["ALCAMPO"],
    "EROSKI": ["EROSKI"],
    "DISA": ["DISA"],
    "MEROIL": ["MEROIL"],
    "GASEXPRESS": ["GASEXPRESS"],
}

# Cualquier cosa que no sea letra o dígito pasa a ser separador, de forma que
# "DISA_SHELL", "-CEPSA-" o "E.S. AVINYÓ" se tokenicen igual que "DISA SHELL".
# `\w` es unicode-aware, así que los acentos sobreviven; `_` sí es separador.
# Las únicas marcas que la UI y la API ofrecen como filtro (§6 y §11).
# `INDEPENDIENTE` no está y no debe estar: no es una marca, es "todo lo que no
# encajó" —un tercio de las estaciones, cada una con su propio rótulo—, así que
# como casilla no significa nada. Las independientes salen siempre por defecto y
# solo desaparecen si el usuario filtra activamente por otra marca.
MARCAS_FILTRABLES: tuple[str, ...] = tuple(NORMALIZACION_ROTULOS)

_NO_ALFANUM = re.compile(r"[\W_]+")


def _limpiar(texto: str) -> str:
    return _NO_ALFANUM.sub(" ", texto).strip().upper()


def _compilar() -> list[tuple[str, re.Pattern[str]]]:
    compilados: list[tuple[str, re.Pattern[str]]] = []
    for canonico, patrones in NORMALIZACION_ROTULOS.items():
        alternativas = "|".join(re.escape(_limpiar(p)) for p in patrones)
        # Palabra completa: `\bAVIA\b` no casa dentro de "GAVIA".
        compilados.append((canonico, re.compile(rf"(?<!\w)(?:{alternativas})(?!\w)")))
    return compilados


_PATRONES = _compilar()


def normalizar_rotulo(raw: str) -> str:
    """Devuelve la marca canónica de un rótulo crudo, o ``INDEPENDIENTE``."""
    if not raw:
        return INDEPENDIENTE
    limpio = _limpiar(raw)
    for canonico, patron in _PATRONES:
        if patron.search(limpio):
            return canonico
    return INDEPENDIENTE
