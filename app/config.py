"""Configuración leída del entorno (o de un ``.env`` local).

Ninguna URL de servicio externo debe estar hardcodeada en el código:
cambiar de OSRM público a OSRM propio tiene que ser cambiar una variable
(ARQUITECTURA.md §9).
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

RAIZ_PROYECTO = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    osrm_url: str = "https://router.project-osrm.org"
    db_path: Path = RAIZ_PROYECTO / "data" / "repostaje.db"

    # Ojo: la ruta que citan casi todos los tutoriales, con `PrestacionesServicios`
    # en vez de `PreciosCarburantes`, devuelve 404 (ARQUITECTURA.md §9).
    geoportal_url: str = (
        "https://sedeaplicaciones.minetur.gob.es"
        "/ServiciosRESTCarburantes/PreciosCarburantes/EstacionesTerrestres/"
    )
    # El Ministerio corta la conexión según User-Agent (ARQUITECTURA.md §9).
    geoportal_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
    snapshot_dir: Path = RAIZ_PROYECTO / "data" / "snapshots"


settings = Settings()
