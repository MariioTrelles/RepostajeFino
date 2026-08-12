"""El dominio no depende de nada de fuera. Verificado, no prometido.

ARQUITECTURA.md §3 y §12: el DP vive en ``domain/``, recibe objetos ya
construidos y una matriz ya calculada, y no conoce SQLite ni OSRM. Si algún día
alguien necesita importar un adaptador aquí, la señal correcta es revisar el
diseño, no añadir el import — y este test es quien da la señal.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

RAIZ_DOMINIO = Path(__file__).parents[2] / "app" / "domain"

MODULOS_PROHIBIDOS = {
    "app.adapters",
    "app.api",
    "app.jobs",
    "app.config",
    "sqlite3",
    "httpx",
    "fastapi",
    "pydantic",
    "pydantic_settings",
    "requests",
}


def modulos_del_dominio() -> list[Path]:
    return sorted(RAIZ_DOMINIO.rglob("*.py"))


def imports_de(ruta: Path) -> set[str]:
    arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))
    nombres: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            nombres.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.level == 0 and nodo.module:
            nombres.add(nodo.module)
    return nombres


def test_hay_modulos_que_revisar() -> None:
    """Si el dominio se quedara vacío, los tests de abajo pasarían por vacuidad."""
    rutas = modulos_del_dominio()
    assert any(r.name == "dp_optimizer.py" for r in rutas)
    assert any(r.name == "models.py" for r in rutas)


@pytest.mark.parametrize("ruta", modulos_del_dominio(), ids=lambda r: r.name)
def test_el_dominio_no_importa_infraestructura(ruta: Path) -> None:
    for modulo in imports_de(ruta):
        for prohibido in MODULOS_PROHIBIDOS:
            assert not (modulo == prohibido or modulo.startswith(prohibido + ".")), (
                f"{ruta.relative_to(RAIZ_DOMINIO.parents[1])} importa {modulo!r}. "
                "El dominio tiene que poder ejecutarse sin BD, sin red y sin FastAPI."
            )


@pytest.mark.parametrize("ruta", modulos_del_dominio(), ids=lambda r: r.name)
def test_el_dominio_solo_usa_la_libreria_estandar(ruta: Path) -> None:
    """Más estricto que la lista negra: ni una dependencia de terceros, ninguna."""
    for modulo in imports_de(ruta):
        raiz = modulo.split(".")[0]
        if raiz == "app":
            assert modulo.startswith("app.domain"), (
                f"{ruta.name} importa {modulo!r}, que está fuera de app.domain."
            )
            continue
        assert raiz in sys.stdlib_module_names, (
            f"{ruta.name} importa {modulo!r}, que no es de la librería estándar."
        )


def test_el_dp_se_puede_usar_sin_importar_la_app_entera() -> None:
    """Importar el DP no puede arrastrar httpx, FastAPI ni sqlite3 por la puerta de atrás."""
    codigo = (
        "import sys;"
        "import app.domain.dp_optimizer;"
        "cargados = {'httpx', 'fastapi', 'pydantic', 'sqlite3'} & set(sys.modules);"
        "print(sorted(cargados))"
    )
    import subprocess

    salida = subprocess.run(
        [sys.executable, "-c", codigo],
        capture_output=True,
        text=True,
        cwd=RAIZ_DOMINIO.parents[1],
        check=True,
    )
    assert salida.stdout.strip() == "[]", (
        f"Importar el DP carga infraestructura: {salida.stdout.strip()}"
    )
