"""Mapeo entre los nombres de campo del Geoportal y códigos internos limpios.

Se aplica **en la ingesta, no en consulta** (ARQUITECTURA.md §6): la BD guarda ya
``gasolina95``, no ``Precio Gasolina 95 E5``.

Se ingieren **todos** los productos que devuelve la API: el snapshot ya viene
completo, guardar el resto es marginal y evita una migración futura. El filtro
por defecto de la UI (95 y gasóleo A) es cosa de la capa de presentación.
"""

from __future__ import annotations

# Campo tal cual lo devuelve la API -> código interno.
# Nombres verificados contra un snapshot real de agosto de 2026.
CODIGOS_PRODUCTO: dict[str, str] = {
    "Precio Gasolina 95 E5": "gasolina95",
    "Precio Gasolina 95 E10": "gasolina95_e10",
    "Precio Gasolina 95 E25": "gasolina95_e25",
    "Precio Gasolina 95 E85": "gasolina95_e85",
    "Precio Gasolina 95 E5 Premium": "gasolina95_premium",
    "Precio Gasolina 98 E5": "gasolina98",
    "Precio Gasolina 98 E10": "gasolina98_e10",
    "Precio Gasolina Renovable": "gasolina_renovable",
    "Precio Gasoleo A": "diesel",
    "Precio Gasoleo B": "diesel_b",
    "Precio Gasoleo Premium": "diesel_premium",
    "Precio Diésel Renovable": "diesel_renovable",
    "Precio Biodiesel": "biodiesel",
    "Precio Bioetanol": "bioetanol",
    "Precio Gases licuados del petróleo": "glp",
    "Precio Gas Natural Comprimido": "gnc",
    "Precio Gas Natural Licuado": "gnl",
    "Precio Biogas Natural Comprimido": "biognc",
    "Precio Biogas Natural Licuado": "biognl",
    "Precio Hidrogeno": "hidrogeno",
    "Precio Metanol": "metanol",
    "Precio Amoniaco": "amoniaco",
    "Precio Adblue": "adblue",
}

# Marcados por defecto en la UI (ARQUITECTURA.md §6). Ojo: esto es visualización.
# El combustible que optimiza el DP es uno solo, el de `Vehiculo.tipo_combustible`.
PRODUCTOS_POR_DEFECTO_UI: tuple[str, ...] = ("gasolina95", "diesel")

# Productos que no son carburante de automoción: nunca deben llegar al DP.
PRODUCTOS_NO_PROPULSION: frozenset[str] = frozenset({"adblue", "amoniaco"})

_PREFIJO_PRECIO = "Precio "


def es_campo_de_precio(campo: str) -> bool:
    return campo.startswith(_PREFIJO_PRECIO)


def campos_de_precio_desconocidos(campos: object) -> set[str]:
    """Campos ``Precio *`` que la API devuelve y este mapeo no contempla.

    Si el Ministerio añade un producto nuevo queremos enterarnos por un aviso en
    la ingesta, no que desaparezca en silencio.
    """
    if not hasattr(campos, "__iter__"):
        return set()
    return {
        str(c) for c in campos if es_campo_de_precio(str(c)) and str(c) not in CODIGOS_PRODUCTO
    }
