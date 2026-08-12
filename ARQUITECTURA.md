# Proyecto: Optimizador de repostaje en ruta

Documento vivo de decisiones técnicas. Actualizar conforme evolucione el proyecto.

---

## 1. Objetivo

Aplicación web que, dada una ruta origen-destino y las características del vehículo,
calcula **dónde repostar y cuánto** para minimizar el coste total del viaje, usando
precios reales de las gasolineras españolas.

Diferenciador frente al Geoportal oficial: no solo muestra precios, sino que **optimiza
la decisión de repostaje a lo largo de un trayecto**, teniendo en cuenta el coste real
del desvío.

---

## 2. Stack

| Capa | Tecnología | Notas |
|---|---|---|
| Backend | Python + FastAPI | async; `httpx` para llamadas externas |
| Datos | SQLite (WAL activado) | módulo R*Tree nativo para filtro espacial |
| Precios | API REST Geoportal Carburantes (MITECO) | sin auth, formato XML/JSON |
| Recarga eléctrica | NAP DGT, formato DATEX2 | fuente **distinta**, ingesta aparte |
| Rutas | OSRM | público para prototipo, autoalojado en Docker para producción |
| Frontend | Web sencilla + Leaflet | tiles OSM (vigilar política de uso si crece) |
| Algoritmo | Gas station problem con programación dinámica | variante con coste de desvío |

---

## 3. Arquitectura

**Hexagonal (puertos y adaptadores), aplicada de forma pragmática.**

No hexagonal "pura": se definen puertos solo donde hay puntos de cambio reales
identificados. El resto se mantiene simple.

### Estructura de carpetas

```
app/
├── domain/                    # núcleo, sin dependencias externas
│   ├── models.py              # Estacion, Precio, Vehiculo, TramoRuta, Recomendacion
│   ├── dp_optimizer.py        # gas station problem + coste de desvío
│   └── ports/
│       ├── routing_provider.py
│       └── price_store.py
│
├── adapters/
│   ├── routing/
│   │   └── osrm_adapter.py
│   ├── storage/
│   │   └── sqlite_adapter.py
│   └── ingestion/
│       ├── geoportal_client.py    # API MITECO, carburantes
│       └── ev_charger_client.py   # NAP DGT, DATEX2 (fase 2)
│
├── api/
│   ├── routes/
│   └── dependencies.py        # inyección: qué adaptador implementa cada puerto
│
└── jobs/
    └── ingest.py               # proceso independiente (cron / systemd timer)
```

### Puertos definidos

- **`RoutingProvider`** → hoy OSRM público, mañana OSRM/Valhalla propio
- **`PriceStore`** → hoy SQLite, mañana Postgres si hace falta escalar horizontalmente

### Puertos futuros (NO implementar aún)

- **`EVChargerProvider`** → cuando se añada el filtro de recarga eléctrica
- **`VehicleCatalogProvider`** → cuando se añada el catálogo de modelos de coche

### Persistencia de usuario: stateless (decidido)

Sin login, sin perfiles guardados, sin sesión persistida en servidor. Cada request
a la API de rutas lleva todo lo necesario (origen, destino, `Vehiculo` completo).

Consecuencias prácticas:
- No hace falta tabla de usuarios ni de vehículos guardados en el esquema
- El frontend puede guardar el último `Vehiculo` introducido en el propio
  navegador (localStorage) para no repetir el formulario cada vez — eso es
  cosa del cliente, no cambia nada del backend
- Si en el futuro se quiere añadir login, es una capa nueva por encima, no un
  cambio en el diseño actual del dominio ni de la API

### Dónde NO aplicar hexagonal

- El job de ingesta: es un script, reutiliza `PriceStore` para escribir y ya
- Los routers de FastAPI: son adaptadores de entrada de facto, no necesitan otra capa
- El DP: vive en `domain/`, recibe listas de objetos ya construidos y una matriz de
  distancias ya calculada. No conoce SQLite ni OSRM.

### Inyección de dependencias

Sin framework. Funciones factory en `api/dependencies.py`:

```python
def get_routing_provider() -> RoutingProvider:
    return OSRMAdapter(base_url=settings.osrm_url)

def get_price_store() -> PriceStore:
    return SQLiteAdapter(db_path=settings.db_path)
```

---

## 4. Modelo de datos

### Principios

- **Precios: append-only**, nunca UPDATE
- **Insertar solo cuando el precio cambia** respecto al último conocido para
  (estación, producto). La API devuelve snapshot completo (~11.500 estaciones,
  ~10 MB); insertar todo cada ciclo generaría cientos de miles de filas/día
  innecesarias. Los precios cambian como mucho una vez al día.
- **Precios como enteros en milésimas de euro** (`1,659 €/L` → `1659`).
  Parsear con `Decimal`, nunca `float`: el DP acumula error y desestabiliza
  los desempates entre estaciones.
- **Ojo con el formato**: la API usa **coma decimal** en precios, latitud y longitud.

### Esquema

```sql
-- Mutable: metadatos, se actualiza in-place
CREATE TABLE estaciones (
    id INTEGER PRIMARY KEY,           -- ID del propio Geoportal
    rotulo TEXT NOT NULL,             -- "REPSOL", normalizado
    rotulo_raw TEXT,                  -- valor original del ministerio
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    municipio TEXT,
    provincia TEXT,
    horario TEXT,
    updated_at TIMESTAMP
);

CREATE INDEX idx_estaciones_rotulo ON estaciones(rotulo);

-- Append-only: histórico
CREATE TABLE precios (
    id INTEGER PRIMARY KEY,
    estacion_id INTEGER NOT NULL REFERENCES estaciones(id),
    producto TEXT NOT NULL,
    precio_milesimas INTEGER NOT NULL,
    valid_from TIMESTAMP NOT NULL
);

-- Fase 2: fuente distinta (NAP DGT), relación N:1
CREATE TABLE puntos_recarga (
    id INTEGER PRIMARY KEY,
    estacion_id INTEGER REFERENCES estaciones(id),  -- NULL si no casa con ninguna
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    potencia_kw REAL,
    tipo_conector TEXT,
    operador TEXT,
    updated_at TIMESTAMP
);
```

### Notas sobre los datos del Geoportal

Campos que devuelve `EstacionesTerrestres`:
`C.P., Dirección, Horario, Latitud, Localidad, Longitud, Margen, Municipio,
Provincia, Rótulo, Tipo Venta` + un `Precio_X` por producto.

- **`Rótulo`** = la marca. **No está normalizado**: aparecen variantes como
  "REPSOL", "REPSOL S.A.", "E.S. REPSOL". Necesita tabla de mapeo o normalización
  en la ingesta para las marcas grandes.
- **`Tipo Venta`**: filtrar por pública (`P`) en la ingesta, antes de guardar.
- El rótulo puede cambiar (una gasolinera cambia de bandera) → va en la tabla
  mutable, no en el histórico.
- **No hay campo de recarga eléctrica** en esta API, ni booleano ni nada.

### 4.1. Normalización de rótulos (decidido: diccionario manual)

El campo `Rótulo` no viene limpio: la misma marca aparece con variantes
("REPSOL", "REPSOL S.A.", "E.S. REPSOL"...) porque cada gestora lo introduce
a mano. Sin normalizar, el filtro por marca deja fuera estaciones que sí son
de esa marca.

**Proceso para construir el diccionario** (una tarea de una tarde, hacer antes
de programar el filtro):

1. Bajar un snapshot completo de la API
2. `SELECT DISTINCT rotulo` (o equivalente en pandas) sobre el snapshot crudo
3. Identificar visualmente las 15-20 marcas grandes que dominan y agrupar sus
   variantes
4. Lo que no se reconozca va a `INDEPENDIENTE` o se guarda con su `rotulo_raw`
   tal cual — no hace falta cubrir el 100%, solo las marcas que la gente
   filtrará

```python
# adapters/ingestion/rotulo_normalizer.py
NORMALIZACION_ROTULOS = {
    "REPSOL": ["REPSOL"],
    "CEPSA": ["CEPSA", "C.E.P.S.A"],
    "SHELL": ["SHELL"],
    "GALP": ["GALP"],
    "BP": ["BP", "B.P."],
    # completar tras revisar el snapshot real
}

def normalizar_rotulo(raw: str) -> str:
    raw_upper = raw.strip().upper()
    for canonico, patrones in NORMALIZACION_ROTULOS.items():
        if any(p in raw_upper for p in patrones):
            return canonico
    return "INDEPENDIENTE"
```

Se aplica en la ingesta (`geoportal_client.py`), no en consulta: `estaciones.rotulo`
guarda ya el valor normalizado, `rotulo_raw` conserva el original por si hace
falta revisar o ampliar el diccionario más adelante.

---

## 5. Recarga eléctrica (fase 2)

- **No viene en la API de carburantes.** Fuente separada: NAP de la DGT, formato
  **DATEX2** (estándar europeo, más pesado de parsear que el XML del Geoportal).
- Regulado por la Orden TED/445/2023. Existe intención de unificarlo en el
  Geoportal en el futuro, pero **no diseñar asumiendo que ocurra**.
- El cruce con `estaciones` se hace **por proximidad geográfica** (ej. <50 m).
  Por eso es tabla propia y no un booleano: el cruce es una inferencia que puede
  fallar y conviene mantener trazable.
- Dimensionar el adaptador DATEX2 como trabajo aparte, no como "una llamada más".

---

## 6. Filtros de la app

### Combustibles (decidido)

**Ingerir todos** los productos que devuelve la API. El coste de guardar los demás
es marginal (la llamada ya trae el snapshot completo) y evita una migración futura.

**Filtro en la UI con gasolina 95 y gasóleo A marcados por defecto**, que cubren la
gran mayoría de casos.

Distinción importante entre dos usos del combustible, que no deben confundirse:

| Concepto | Cardinalidad | Uso |
|---|---|---|
| `Vehiculo.tipo_combustible` | **uno solo** | el que consume el DP para optimizar |
| Filtro de la UI | varios | qué precios se muestran en el mapa / listado |

El DP optimiza sobre **un** combustible: el del coche. El filtro es de visualización.
Mantenerlos separados en el modelo evita un lío conceptual difícil de deshacer después.

Necesario: tabla de mapeo entre los nombres de campo de la API
(`Precio_Gasolina_95_E5`, `Precio_Gasoleo_A`...) y códigos internos limpios
(`gasolina95`, `diesel`). Hacerlo en la ingesta, no en consulta.

### Otros filtros

- **Por marca**: `WHERE rotulo IN (...)` sobre tabla mutable, indexado
- **Con cargador eléctrico**: `LEFT JOIN puntos_recarga ... HAVING COUNT(...) > 0`,
  permitiendo además filtrar por potencia mínima (los usuarios de VE filtran por kW,
  no solo por "tiene o no tiene")

---

## 7. Vehículo y consumo

**Fase 1 (ahora):** el usuario introduce su consumo manualmente.

**Modelo de depósito: capacidad + nivel actual** (decidido). Más preciso que
autonomía simplificada y necesario para que el DP decida *cuánto* repostar, no
solo dónde.

```python
@dataclass
class Vehiculo:
    consumo_l_100km: float      # o kWh/100km si se añaden eléctricos
    tipo_combustible: str       # "gasolina95", "diesel"...
    capacidad_deposito_l: float
    nivel_actual_l: float
    reserva_minima_l: float = 5.0   # el DP nunca baja de aquí
```

Implicaciones para el DP:
- El **estado** del DP es el nivel de combustible al llegar a cada estación
  candidata. Discretizar en pasos (ej. 1 L) o usar la formulación exacta.
- **Reserva mínima obligatoria**: el óptimo matemático llega a las estaciones
  con el depósito a cero. Inaceptable en la práctica. Es una restricción dura del
  dominio, no un detalle de UI.
- Permite la decisión "repostar parcialmente aquí porque 80 km más adelante está
  más barato", que es donde el algoritmo aporta valor real frente a "llena siempre".
- Hay que validar que `nivel_actual_l <= capacidad_deposito_l` y que el trayecto
  es factible (si el primer tramo excede la autonomía máxima, avisar en vez de
  devolver "sin solución").

**Fase 2 (futuro):** catálogo de modelos típicos → puerto `VehicleCatalogProvider`
con adaptador propio (tabla estática de consumos WLTP o API externa).

**Clave**: el DP recibe un `Vehiculo` desde el día uno. No le importa de dónde salió
el dato. Añadir el catálogo después = añadir un puerto + un adaptador, sin tocar
el dominio ni la firma del DP.

Guardar siempre el consumo en **L/100km** (nunca en €/100km ni mezclado con precio),
para que el dato sea reutilizable venga de donde venga.

---

## 8. Algoritmo

Variante del **gas station problem** (Khuller, Malekian, Mestre) resuelto con
programación dinámica.

### Puntos críticos

1. **El cuello de botella NO es el DP**, es la selección de estaciones candidatas.
2. **No usar distancia perpendicular a la polilínea** para estimar el desvío.
   Filtrar primero con R*Tree por bounding box, luego calcular el desvío real
   con el servicio `/table` de OSRM en batch.
3. **Modelar el coste del desvío**: combustible + tiempo de ida y vuelta al
   surtidor. Sin esto, el algoritmo manda al usuario 4 km fuera de la A-2 para
   ahorrar 60 céntimos.
4. El DP vive en `domain/` y es testeable con datos falsos, sin BD ni red.

### 8.1. Discretización del nivel de combustible (decidido: 1 litro)

El estado del DP es (estación, litros en el depósito al llegar). Como el nivel de
combustible es continuo, hay que discretizarlo en pasos fijos para que el DP tenga
un número finito de estados que tabular.

```python
PASO_DISCRETIZACION_L = 1.0  # constante ajustable, solo afecta a rendimiento
```

Con depósitos típicos (40-70 L) esto da 40-70 estados por estación candidata,
trivial computacionalmente. Es una constante de ajuste fino, no una decisión de
diseño: si en el futuro el DP tarda más de lo razonable con rutas muy largas,
se sube a 2-5 L sin tocar la lógica del algoritmo.

---

## 9. Escalado

### Estado actual: **despliegue local** (decidido)

Todo en una máquina. SQLite + FastAPI + OSRM público. Sin Docker obligatorio,
sin proxy inverso, sin nada.

Consecuencias de trabajar en local por ahora:
- **OSRM público es aceptable** mientras seas el único usuario (respetando 1 req/s).
- **Pero mete un `.env` con `OSRM_URL` desde el primer commit.** Cuando quieras pasar
  a OSRM propio, es cambiar una variable, no buscar URLs hardcodeadas por el código.
  El puerto `RoutingProvider` ya cubre el cambio de motor; la variable cubre el
  cambio de host.
- **La ingesta la lanzas a mano o con cron local.** No montes un scheduler dentro
  de FastAPI: cuando llegue el despliegue real querrás que sea un proceso aparte,
  y ya está diseñado así.
- **Frecuencia de ingesta: 2 veces al día** (decidido). Cubre el caso normal
  (la mayoría de estaciones actualiza precio una vez de madrugada/mañana) más
  margen para las que actualizan más tarde o cambios intradía puntuales, sin
  arriesgarse a que el Ministerio limite por exceso de peticiones. Configurar
  como cron local (ej. `0 7,15 * * *`) o `systemd timer` cuando se automatice;
  mientras tanto, lanzar `python -m jobs.ingest` a mano vale perfectamente.
- **Ojo con la API del Ministerio**: hay reportes recurrentes de que corta la
  conexión según User-Agent o IP de origen. Manda un User-Agent de navegador desde
  el principio y **guarda un snapshot local de la respuesta** para poder desarrollar
  y testear sin depender de que el servicio esté vivo.

### A ~100 usuarios concurrentes
- **OSRM público queda descartado**: su política limita a ~1 req/s y no da garantías
  de uptime ni de permanencia del acceso. Autoalojar OSRM (extracto España de
  Geofabrik, ~1 GB, perfil coche MLD) en su propio contenedor.
- **SQLite sigue siendo válido**: en WAL las lecturas concurrentes no son problema,
  y solo escribe el job de ingesta. No migrar a Postgres solo por esto.
- **Cachear agresivamente** el resultado del cálculo de ruta y las llamadas a OSRM
  (mismo trayecto pedido por distintos usuarios es lo normal).
- Varios workers de Uvicorn/Gunicorn en una sola máquina suele bastar.

### Cuándo sí migrar a Postgres
Cuando se necesiten **múltiples instancias de la API en máquinas distintas**
(SQLite es un fichero en un disco). No antes.

---

## 10. Ideas para el futuro

- **Precio esperado por estación** derivado del histórico (media móvil, patrón por
  día de la semana) → el DP no solo responde "dónde repostar ahora" sino "merece la
  pena aguantar 30 km porque esa gasolinera suele bajar los martes". Es algo que el
  Geoportal oficial no hace y aprovecha el histórico append-only.

- **Estaciones cercanas en tiempo real durante el trayecto** (ej. "¿qué tengo a
  15 min de desvío desde donde estoy ahora?"). Caso de uso tipo parada de baño /
  repostaje oportunista, distinto de la optimización de ruta completa.
  - **No es una extensión del DP**: es una consulta puntual sobre la posición
    actual, no una optimización sobre todo el trayecto.
  - **No requiere arquitectura nueva**: reutiliza el índice R*Tree de `estaciones`
    y el `RoutingProvider` (`/table` de OSRM) que ya están previstos para el DP.
    Sería un endpoint nuevo y sencillo, no un puerto ni adaptador nuevos.
  - **Sí requiere algo que hoy no está previsto**: geolocalización del usuario en
    tiempo real desde el frontend (hoy solo se contempla origen/destino fijos).
  - El filtro "con baño" no tiene fuente de datos disponible en el Geoportal ni
    en el NAP; si se quiere ese filtro literal habría que integrar una fuente
    externa (tipo Google Places). Sin eso, "gasolinera cercana" ya cubre la
    mayoría del caso de uso.

---

## 11. Decisiones

### Cerradas

- [x] **Combustibles**: ingerir todos, filtro en UI con 95 y gasóleo A por defecto
- [x] **Depósito**: capacidad + nivel actual + reserva mínima
- [x] **Despliegue**: local por ahora
- [x] **Arquitectura**: hexagonal pragmática, dos puertos (routing y storage)
- [x] **Normalización de rótulos**: diccionario manual (ver sección 4.1)
- [x] **Persistencia de usuario**: stateless, sin perfiles guardados por ahora
- [x] **Frecuencia de ingesta**: 2 veces al día
- [x] **Paso de discretización del DP**: 1 litro (ver sección 8.1)

### Pendientes

Ninguna por ahora. Se irán añadiendo según surjan durante la implementación.

---

## 12. Orden sugerido para empezar a programar

1. **Ingesta + esquema**: `geoportal_client.py` + `sqlite_adapter.py`. Baja los datos,
   parsea coma decimal, normaliza rótulos, guarda con el diffing de precios.
   Sin API ni frontend todavía. Verifica que el volumen de filas es el esperado.
2. **Dominio aislado**: `models.py` + `dp_optimizer.py` con tests usando estaciones
   y matriz de distancias inventadas. Aquí está el valor del proyecto; que funcione
   antes de tocar red o mapa.
3. **`osrm_adapter.py`**: polilínea + `/table` para desvíos reales.
4. **API FastAPI**: un solo endpoint de ruta óptima que una las tres piezas.
5. **Frontend Leaflet**: mapa, formulario de vehículo, filtros.

El paso 2 antes del 3 es deliberado: si el DP depende de OSRM para poder probarse,
pierdes la ventaja principal de haber elegido hexagonal.
