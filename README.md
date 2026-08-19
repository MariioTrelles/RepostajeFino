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
- [x] Paso 2 — dominio aislado (`models.py`, `dp_optimizer.py`, `precio_efectivo.py`)
- [x] Paso 3 — `osrm_adapter.py` (polilínea + `/table` por bloques, con reintentos)
- [x] Paso 4 — API FastAPI (`POST /api/ruta-optima`)
- [x] Paso 5 — frontend Leaflet, v1 mínima ([§13](ARQUITECTURA.md))
- [ ] Paso 6 — filtro multi-combustible: `GET /api/estaciones` ([§6.3](ARQUITECTURA.md))

Con la app levantada, la interfaz está en <http://127.0.0.1:8000/>: la sirve el
propio FastAPI, en el mismo origen que la API ([§9.1](ARQUITECTURA.md)).

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
python -m app.jobs.ingest        # ingesta manual (2 veces/día vía cron, §9)
uvicorn app.main:app --reload    # API en http://127.0.0.1:8000/docs
```

## La API

Un solo endpoint de negocio. Es **stateless**: el request lleva origen, destino y el
vehículo completo (§3). Sin ingesta previa no hay estaciones que ofrecer.

```bash
curl -X POST http://127.0.0.1:8000/api/ruta-optima -H "Content-Type: application/json" -d '{
  "origen":  {"lat": 40.416775, "lon": -3.703790},
  "destino": {"lat": 41.385064, "lon": 2.173404},
  "vehiculo": {"consumo_l_100km": 6.5, "tipo_combustible": "diesel",
               "capacidad_deposito_l": 55, "nivel_actual_l": 15, "reserva_minima_l": 5}
}'
```

Devuelve el plan (paradas, tramos y costes desglosados en combustible y tiempo), la
polilínea para el mapa y las candidatas consideradas con su marca de vigencia, de
forma que el paso 5 se pinte con una sola llamada.

Los fallos no se disfrazan de plan: `422` si el trayecto es inviable (con el hueco
concreto), `422` con las gasolineras más cercanas si el conductor ya va por debajo de
la reserva, `503` si OSRM no responde. Nunca una ruta calculada con distancias
aproximadas (§8.4).

`max_candidatas` (50 por defecto) es el mando del tiempo de respuesta: con el OSRM
público a 1 req/s, 50 candidatas son unos segundos y 250 casi medio minuto.

## Estructura

```
app/domain/     núcleo: modelos + DP + precio efectivo. Sin adapters, red ni SQLite.
app/adapters/   routing (OSRM), storage (SQLite), ingestion (Geoportal)
app/api/        FastAPI
app/jobs/       ingesta, proceso aparte de la web
tests/          espejo de app/, un directorio por capa
```
