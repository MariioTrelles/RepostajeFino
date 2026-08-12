# Repostaje Fino

Calcula **dónde repostar y cuánto** en un trayecto por carretera para minimizar el
coste total del viaje, con precios reales de las gasolineras españolas (Geoportal de
Carburantes del MITECO) y coste real del desvío.

> **Documento de referencia: [`ARQUITECTURA.md`](ARQUITECTURA.md).**
> Es el documento vivo de decisiones técnicas y la fuente de verdad del proyecto
> (stack, arquitectura hexagonal, modelo de datos, algoritmo). Antes de tocar código,
> léelo. Si algo del código contradice ese documento, el error está en el código.

## Estado

Siguiendo el orden de [`ARQUITECTURA.md` §12](ARQUITECTURA.md):

- [x] Paso 0 — esqueleto y tooling
- [x] Paso 1 — ingesta + esquema (`geoportal_client.py`, `sqlite_adapter.py`)
- [ ] Paso 2 — dominio aislado (`models.py`, `dp_optimizer.py`)
- [ ] Paso 3 — `osrm_adapter.py`
- [ ] Paso 4 — API FastAPI
- [ ] Paso 5 — frontend Leaflet

## Puesta en marcha

Requiere **Python >= 3.12**. El motivo es concreto: el SQLite que empaqueta Python 3.9
(3.35.5) viene compilado **sin el módulo R\*Tree**, que `ARQUITECTURA.md` §2 y §8 dan
por disponible para el filtro espacial. Python 3.12 trae SQLite 3.49 con R\*Tree.

```bash
py -3.12 -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"
copy .env.example .env          # y ajustar
```

## Comandos

```bash
pytest                  # tests (no tocan red ni la API real)
ruff check .            # lint
ruff format .           # formato
python -m app.jobs.ingest   # ingesta manual (2 veces/día vía cron, §9)
```

## Estructura

```
app/domain/     núcleo: modelos + DP. Sin imports de adapters, red ni SQLite.
app/adapters/   routing (OSRM), storage (SQLite), ingestion (Geoportal)
app/api/        FastAPI
app/jobs/       ingesta, proceso aparte de la web
tests/          espejo de app/, un directorio por capa
```
