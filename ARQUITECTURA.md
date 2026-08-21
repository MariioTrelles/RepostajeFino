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

| Capa              | Tecnología                                    | Notas                                                         |
| ----------------- | --------------------------------------------- | ------------------------------------------------------------- |
| Backend           | Python >= 3.12 + FastAPI                      | async; `httpx` para llamadas externas                         |
| Datos             | SQLite (WAL activado)                         | módulo R*Tree nativo para filtro espacial                     |
| Precios           | API REST Geoportal Carburantes (MITECO)       | sin auth, formato XML/JSON                                    |
| Recarga eléctrica | NAP DGT, formato DATEX2                       | fuente **distinta**, ingesta aparte                            |
| Rutas             | OSRM                                          | público para prototipo, autoalojado en Docker para producción |
| Frontend          | Web sencilla + Leaflet, sin build             | tiles OSM (vigilar política de uso si crece) — ver §13        |
| Geocoding         | Photon (komoot), desde el navegador           | sin clave; el servidor no geocodifica (§6.2 y §13.2)          |
| Algoritmo         | Gas station problem con programación dinámica | variante con coste de desvío                                  |

### Python >= 3.12 es un requisito duro, no una preferencia

El SQLite que empaqueta **Python 3.9 (3.35.5) viene compilado sin el módulo
R*Tree** (`no such module: rtree`), y de ese módulo depende todo el filtro
espacial de §8. Python 3.12 trae SQLite 3.49 con R*Tree incluido.

Comprobación antes de dar por bueno un intérprete:

```python
import sqlite3

sqlite3.connect(":memory:").execute(
    "CREATE VIRTUAL TABLE t USING rtree(id, minx, maxx, miny, maxy)"
)
```

Si eso falla, las alternativas son `pysqlite3-binary` o `apsw` (embeben su propio
SQLite), pero salen más caras que exigir un intérprete moderno.

Comprobado: 3.12.10 trae SQLite 3.49.1 y 3.13.2 trae 3.45.3; **los dos con
R\*Tree**, así que cualquiera de los dos vale. Ojo con lo contrario de lo que uno
esperaría: el intérprete más nuevo no trae necesariamente el SQLite más nuevo.

---

## 3. Arquitectura

**Hexagonal (puertos y adaptadores), aplicada de forma pragmática.**

No hexagonal "pura": se definen puertos solo donde hay puntos de cambio reales
identificados. El resto se mantiene simple.

### Estructura de carpetas

```
app/
├── config.py                  # settings desde .env (OSRM_URL, DB_PATH...)
│
├── domain/                    # núcleo, sin dependencias externas
│   ├── models.py              # Estacion, Precio, Vehiculo, EstacionCandidata,
│   │                          # TramoRuta, Parada, Recomendacion
│   ├── dp_optimizer.py        # gas station problem + coste de desvío
│   ├── precio_efectivo.py     # precioEfectivo(estacion, usuario) — ver §6.1
│   ├── seleccion_candidatas.py  # corredor + recorte por precio — ver §8.5
│   └── ports/
│       ├── routing_provider.py   # Coordenada, Ruta, MatrizRuta
│       └── price_store.py
│
├── adapters/
│   ├── routing/
│   │   └── osrm_adapter.py
│   ├── storage/
│   │   └── sqlite_adapter.py
│   └── ingestion/
│       ├── geoportal_client.py    # API MITECO, carburantes
│       ├── rotulo_normalizer.py   # diccionario manual de marcas (§4.1)
│       ├── productos.py           # campo de la API -> código interno (§6)
│       └── ev_charger_client.py   # NAP DGT, DATEX2 (fase 2)
│
├── main.py                    # app FastAPI: monta routers y estáticos (§9.1)
│
├── api/
│   ├── routes/
│   │   ├── ruta.py            # POST /api/ruta-optima — ver §6.2
│   │   └── estaciones.py      # GET /api/estaciones (mapa) — ver §6.3, paso 5
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
  - Esto no se deja a la buena voluntad: `tests/domain/test_aislamiento_dominio.py`
    recorre por AST todos los módulos de `app/domain/` y falla si alguno importa
    algo que no sea librería estándar o `app.domain`. Si un día hace falta añadir
    ese import, la respuesta correcta es revisar el diseño, no relajar el test.

### Qué sí es dominio aunque no lo parezca

**La selección de candidatas vive en `domain/`** (`seleccion_candidatas.py`), no en
el router. Es tentador tomarla por fontanería —consulta la BD, prepara una llamada
a OSRM— pero lo que hace es decidir *qué* estaciones compiten por entrar en el
plan, y eso es la regla de negocio más determinante que hay después del propio DP.

El criterio para distinguirlo: el módulo **solo habla con los puertos**, no con
SQLite ni con httpx, así que pasa el test de aislamiento igual que el DP y se
prueba con dobles, sin levantar nada. Metido en el router habría quedado imposible
de probar sin HTTP, que es justo lo que §12 evita al poner el paso 2 antes del 3.

Regla general: si algo se puede escribir contra los puertos, es dominio; si
necesita conocer al adaptador concreto, no lo es.

### Inyección de dependencias

Sin framework. Funciones factory en `api/dependencies.py`:

```python
@lru_cache(maxsize=1)
def get_routing_provider() -> RoutingProvider:
    return OSRMAdapter(base_url=settings.osrm_url)


@lru_cache(maxsize=1)
def get_price_store() -> PriceStore:
    return SQLiteAdapter(db_path=settings.db_path)
```

El `lru_cache` no es optimización prematura: el adaptador de OSRM lleva dentro el
limitador de ~1 req/s del servidor público (§9), y ese contador solo cuenta si
todas las peticiones comparten la misma instancia. Devolver un adaptador nuevo
cada vez equivale a no tener límite.

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
- **El campo de longitud se llama `Longitud (WGS84)`**, no `Longitud`. Es la
  primera cosa que rompe una ingesta escrita a partir de la documentación.
  Conviene aceptar los dos nombres.
- Un precio a `"0,000"` no es un precio: es ausencia de producto mal codificada.
  Descartarlo, no guardarlo como cero.

### Esquema

```sql
-- Mutable: metadatos, se actualiza in-place
CREATE TABLE estaciones (
    id INTEGER PRIMARY KEY,           -- ID del propio Geoportal
    rotulo TEXT NOT NULL,             -- "REPSOL", normalizado
    rotulo_raw TEXT,                  -- valor original del ministerio
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    direccion TEXT,                   -- "Avenida Castilla-La Mancha, 26"
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

-- "Último precio de (estación, producto)" es la consulta caliente: la usan tanto
-- el diffing de la ingesta como el cálculo de ruta.
CREATE INDEX idx_precios_estacion_producto ON precios(estacion_id, producto, id DESC);

-- Filtro espacial (§2 y §8). Geometría de punto: min = max.
-- El R*Tree guarda coordenadas en float32, así que su filtro es aproximado por
-- diseño: hay que reconfirmar con lat/lon exactas de `estaciones` en la misma
-- consulta. No tiene ON CONFLICT, así que al hacer upsert de una estación hay
-- que borrar y reinsertar su fila aquí.
CREATE VIRTUAL TABLE estaciones_rtree USING rtree(
    id, min_lat, max_lat, min_lon, max_lon
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

Campos que devuelve `EstacionesTerrestres`, verificados contra un snapshot real
del 12/08/2026 (11.514 estaciones, 12 MB):

`C.P., Dirección, Horario, Latitud, Localidad, Longitud (WGS84), Margen, Municipio,
Provincia, Remisión, Rótulo, Tipo Venta, % BioEtanol, % Éster metílico, IDEESS,
IDMunicipio, IDProvincia, IDCCAA` + un `Precio X` por producto.

- **`Rótulo`** = la marca. **No está normalizado**: aparecen variantes como
  "REPSOL", "REPSOL S.A.", "E.S. REPSOL". Necesita tabla de mapeo o normalización
  en la ingesta para las marcas grandes.
- **`Tipo Venta`**: filtrar por pública (`P`) en la ingesta, antes de guardar.
  Ojo: hoy el endpoint público devuelve **el 100% de registros con `P`**, así que
  el filtro no descarta nada. Se mantiene igualmente porque nada garantiza que
  siga siendo así, pero conviene saber que ahora mismo no está ejerciendo.
- **`IDEESS`** es el identificador de la estación y viene como texto. Es la clave
  primaria de `estaciones`.
- **Los productos son 23, no los cuatro o cinco de siempre**: además de las
  gasolinas y gasóleos habituales hay `Adblue`, `Amoniaco`, `Metanol`,
  `Diésel Renovable`, `Gasolina Renovable`, `Gasolina 95 E25`, `Gasolina 95 E85`,
  biogás comprimido y licuado. Conviene que la ingesta **avise** cuando aparezca
  un campo `Precio ...` que el mapeo no conoce, en vez de perderlo en silencio.
  `Adblue` y `Amoniaco` no son carburante de automoción: no deben llegar al DP.
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

**No comparar por substring** (decidido, tras revisar el snapshot real). La forma
obvia, `any(p in raw_upper for p in patrones)`, produce falsos positivos sobre
datos reales: `"AVIA" in "CEPSA LA GAVIA 365"` clasifica una estación de Cepsa
como Avia, y hay varios rótulos así (`BP LA GAVIA 365`, `BP VALDAVIA`...).

La comparación es **por palabra completa**, normalizando antes cualquier carácter
no alfanumérico a espacio para que `DISA_SHELL`, `-CEPSA-` o `E.S. AVINYÓ` se
tokenicen igual. El orden del diccionario es el orden de prioridad: gana el
primero que casa.

```python
# adapters/ingestion/rotulo_normalizer.py
NORMALIZACION_ROTULOS = {
    "REPSOL": ["REPSOL"],
    "PETRONOR": ["PETRONOR"],
    "MOEVE": ["MOEVE", "CEPSA", "C E P S A"],  # ver nota abajo
    "GALP": ["GALP", "GALP&GO"],
    "SHELL": ["SHELL"],
    "BP": ["BP", "B P"],
    # ...hasta ~20 marcas; ver el módulo para la lista completa
}

_NO_ALFANUM = re.compile(r"[\W_]+")  # `\w` es unicode-aware: los acentos sobreviven


def normalizar_rotulo(raw: str) -> str:
    limpio = _NO_ALFANUM.sub(" ", raw).strip().upper()
    for canonico, patron in _PATRONES:  # patrón: (?<!\w)ALTERNATIVAS(?!\w)
        if patron.search(limpio):
            return canonico
    return "INDEPENDIENTE"
```

**Cepsa y Moeve se agrupan bajo `MOEVE`** (decidido). Moeve es el nombre comercial
actual del grupo, pero en los datos conviven los dos rótulos como marcas
separadas (596 y 585 estaciones en el snapshot de agosto de 2026), porque el
rebranding va a medias. Quien filtre por la marca espera ver las ~1.180 del
grupo, no la mitad.

**Cobertura conseguida**: 20 marcas cubren el **66,5%** de las 11.514 estaciones.
El 33,5% restante cae en `INDEPENDIENTE`, que es lo esperado: son gasolineras de
gestor único cuyo rótulo es su propio nombre o un número de expediente.

Se aplica en la ingesta (`geoportal_client.py`), no en consulta: `estaciones.rotulo`
guarda ya el valor normalizado, `rotulo_raw` conserva el original por si hace
falta revisar o ampliar el diccionario más adelante.

### 4.2. Antigüedad de precios (decidido: descartar a partir de 48 h)

Ninguna API garantiza que una estación siga reportando. Una gasolinera puede
cerrar, cambiar de gestor o simplemente dejar de actualizar sin que el Geoportal
lo señale de ninguna forma explícita — sigue apareciendo en el snapshot con el
último precio conocido, por antiguo que sea. Con ~11.500 estaciones y una
frecuencia de ingesta de 2x/día, es cuestión de tiempo que esto pase.

**Regla**: un precio se considera **vigente** si `valid_from` está dentro de las
últimas **48 horas** respecto al momento de la consulta. Ese umbral es una
constante ajustable (`PRECIO_MAX_ANTIGUEDAD_H = 48`), no una decisión de diseño:
cubre con margen dos ciclos de ingesta fallidos seguidos sin ser tan laxo como
para dar por bueno un precio de hace una semana.

Consecuencias prácticas:

- El DP y el filtro de marca/mapa **no descartan la estación por completo**:
  la estación sigue existiendo y puede tener otros productos vigentes. Lo que
  se descarta es el precio concreto de ese producto si está caducado.
- Una estación sin ningún precio vigente para el `tipo_combustible` del
  `Vehiculo` queda fuera de las candidatas del DP, igual que si nunca hubiera
  tenido ese producto.
- En el mapa/listado, un precio caducado se puede seguir mostrando pero
  **marcado explícitamente** ("sin actualizar desde hace X días") en vez de
  ocultarlo sin más — es información útil para el usuario, no solo un filtro
  interno del DP.
- Esto se resuelve en la consulta (comparando `valid_from` contra `now()`),
  no en la ingesta: no hace falta un job de limpieza ni tocar el histórico
  append-only, que sigue intacto.

**Cómo quedó**: la regla es del dominio (`Precio.esta_vigente()` y la constante
en `models.py`), y el filtro lo aplica la consulta con
`estaciones_en_bbox(solo_vigentes=...)`. Va **apagado por defecto** a propósito,
que es lo que permite las dos lecturas de arriba con una sola consulta: el mapa
lo deja apagado y marca lo caducado, el DP lo enciende y no lo ve.

Consecuencia que sorprende en desarrollo: si dejas la BD dos días sin reingerir,
la API empieza a devolver "no hay ninguna estación con precio vigente" y parece
un bug. No lo es: es esta regla. Reingiere y vuelve.

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

## 6. Filtros y API de la app

### Combustibles (decidido)

**Ingerir todos** los productos que devuelve la API. El coste de guardar los demás
es marginal (la llamada ya trae el snapshot completo) y evita una migración futura.

**Filtro en la UI con gasolina 95 y gasóleo A marcados por defecto**, que cubren la
gran mayoría de casos.

Distinción importante entre dos usos del combustible, que no deben confundirse:

| Concepto                    | Cardinalidad | Uso                                          |
| ---------------------------- | ------------ | --------------------------------------------- |
| `Vehiculo.tipo_combustible` | **uno solo** | el que consume el DP para optimizar          |
| Filtro de la UI             | varios       | qué precios se muestran en el mapa / listado |

El DP optimiza sobre **un** combustible: el del coche. El filtro es de visualización.
Mantenerlos separados en el modelo evita un lío conceptual difícil de deshacer después.

Necesario: tabla de mapeo entre los nombres de campo de la API
(`Precio Gasolina 95 E5`, `Precio Gasoleo A`...) y códigos internos limpios
(`gasolina95`, `diesel`). Hacerlo en la ingesta, no en consulta. Está en
`adapters/ingestion/productos.py`, con los 23 productos reales.

### 6.1. Precio efectivo vs. precio nominal (decidido: abstracción desde el día uno)

El precio que muestra el Geoportal es el **nominal**, pero el que de verdad paga
el conductor depende de sus descuentos de fidelización (tarjetas de marca,
apps de flota, acuerdos de empresa...). Dos conductores frente a la misma
estación pueden tener un coste real distinto, y eso puede cambiar cuál es la
estación *óptima* — no solo cuál se muestra más barata en el mapa.

Por eso el precio con el que trabajan el DP y el ranking **nunca es el campo
`precio_milesimas` directamente**, sino el resultado de una función:

```python
# domain/precio_efectivo.py
def precio_efectivo(
    estacion: Estacion, precio_nominal: int, usuario: PerfilDescuento | None = None
) -> int:
    """
    Devuelve el precio en milésimas de euro que realmente paga el usuario
    en `estacion`, aplicando los descuentos de fidelización que apliquen.
    Fase 1: usuario=None (no hay perfil, diseño stateless) -> precio_efectivo == precio_nominal.
    """
    ...
```

**Fase 1 (ahora)**: no hay perfil de usuario (persistencia stateless, §3), así
que `usuario` siempre es `None` y `precio_efectivo` es la identidad — devuelve
el precio nominal tal cual. La función existe igualmente y es la que consumen
el DP, el ranking y el mapa, nunca `precio_milesimas` en crudo. `usuario` es
opcional precisamente para que fase 1 no tenga que inventarse un
`PerfilDescuento` vacío solo para cumplir la firma.

**Por qué abstraer ya, aunque hoy no haga nada**: introducir esto más adelante
obligaría a tocar el DP, el caching y la capa de presentación a la vez, porque
los tres asumirían hoy que "precio" es un único número por estación. Poner la
función desde el principio, aunque sea la identidad, hace que añadir descuentos
después sea cambiar la implementación de una función pura, no un rediseño.

**Fase 2 (futuro)**: `PerfilDescuento` viaja en el propio request (coherente con
el diseño stateless — no se guarda en servidor), con las marcas y el
porcentaje/importe de descuento que aplique el usuario. `precio_efectivo` pasa
a tener lógica real; el DP, el ranking y el mapa no cambian ni una línea.

**Con un perfil, la función falla en vez de aplicar la identidad** (decidido). Si
alguien pasa un `PerfilDescuento` hoy, salta `NotImplementedError`. La tentación
era ignorarlo y devolver el nominal, pero eso daría un precio silenciosamente
equivocado y, con él, un plan equivocado: exactamente el fallo que esta
abstracción existe para prevenir. Mismo criterio que §8.4 con OSRM.

**Cómo lo consume el DP**: los precios efectivos se resuelven **una sola vez** al
entrar al optimizador y esa lista es la única fuente de precios del resto del
cálculo, incluida la reconstrucción del plan que se le enseña al usuario. Así la
cuenta con la que se decide y la que se muestra no pueden divergir. El nominal
sigue disponible en `EstacionCandidata`, pero solo para mostrarlo.

### Otros filtros

- **Por marca**: `WHERE rotulo IN (...)` sobre tabla mutable, indexado
  - **`INDEPENDIENTE` no es una opción filtrable** (decidido). El filtro de marca
    solo ofrece las ~20 marcas del diccionario de normalización (§4.1), que son
    las que agrupan a un número significativo de estaciones bajo un nombre
    reconocible. `INDEPENDIENTE` no es una marca real — es "todo lo que no
    encajó" (un tercio de las estaciones, cada una con su propio `rotulo_raw`),
    así que no tiene sentido como checkbox equivalente a "REPSOL" o "SHELL".
  - Las estaciones `INDEPENDIENTE` **están siempre presentes por defecto**,
    con o sin filtro de marca activo: no se ocultan salvo que el usuario
    seleccione explícitamente una o varias marcas del diccionario, en cuyo
    caso el filtro se comporta como cabría esperar (`WHERE rotulo IN (...)`
    deja fuera todo lo que no sea esa marca, incluidas las independientes).
    Esto evita el caso raro de "no toco ningún filtro y desaparece un tercio
    del mapa".
- **Con cargador eléctrico**: `LEFT JOIN puntos_recarga ... HAVING COUNT(...) > 0`,
  permitiendo además filtrar por potencia mínima (los usuarios de VE filtran por kW,
  no solo por "tiene o no tiene")

El filtro de marca lo aplica ya la consulta de bbox, y la lista de marcas
ofrecibles la sirve `GET /api/marcas` desde el propio diccionario de §4.1: la UI
no tiene que mantener su propia copia, y `INDEPENDIENTE` no aparece porque no
está en el diccionario. Pedir una marca que no esté en esa lista es un 422, no
un resultado vacío silencioso.

### 6.2. La API (decidido: un endpoint, coordenadas, plan + mapa en una llamada)

`POST /api/ruta-optima`. Stateless (§3): el request lleva origen, destino y el
`Vehiculo` completo, más los ajustes opcionales de §8.2 (`max_desvio_km`,
`max_desvio_min`, `tiempo_parada_s`) y el filtro de marca.

**Origen y destino son coordenadas, no texto** (decidido). Geocodificar en el
servidor obligaría a un puerto y un adaptador que §3 no contempla, y a cargar con
la política de uso de otro servicio público más. El paso 5 tiene el mapa delante:
resolver "Madrid" es cosa del cliente.

Esto **no** significa que el usuario tenga que clicar en un mapa para decir
"Madrid": el buscador de direcciones existe, vive en el navegador y está
especificado en §13.2. Lo que decide esta sección es dónde *no* vive.

**La respuesta trae el plan, las opciones, la polilínea y las candidatas
consideradas** (decidido) con su precio, su desvío y su marca de vigencia (§4.2).
El frontend pinta mapa, plan y opciones con una sola llamada, sin un segundo
endpoint que repetiría el trabajo de OSRM.

**Una sola cifra de dinero**, `coste_combustible_eur`, y son los euros del
surtidor. No hay `coste_tiempo_eur` ni `coste_total_eur`: el tiempo va en minutos
y como exceso sobre la ruta directa (§8.2). Cada opción lleva su `sobrecoste_eur`,
que el usuario puede comprobar restando dos cifras que también ve.

**El request rechaza campos que no conoce** (`extra="forbid"`). Un cliente que
mandara `valor_tiempo_eur_h`, que existió hasta el paso 6, estaría esperando un
comportamiento que ya no está; ignorárselo en silencio sería devolverle un plan
que no es el que pidió, y esta API no hace eso (§8.4).

**Los fallos no se disfrazan de plan**, cada uno con su código y sus números:

| Situación                              | Código | Cuerpo                                            |
| -------------------------------------- | ------ | ------------------------------------------------- |
| Conductor por debajo de la reserva (§7) | 422    | aviso + gasolineras más cercanas **al origen**    |
| Trayecto inviable (§7)                 | 422    | el hueco concreto: entre qué dos puntos y cuántos km |
| Ninguna candidata con precio vigente   | 422    | sugerencia de ensanchar el corredor o quitar filtros |
| Todas las candidatas se desvían de más (§8.2) | 422 | sugerencia de subir `max_desvio_km`               |
| OSRM agotó los reintentos (§8.4)       | 503    | el cálculo no está disponible ahora mismo          |
| OSRM respondió pero mal                | 502    | el código de error que devolvió                    |

Si OSRM resuelve unas candidatas y otras no, la petición **sale con 200 y un
aviso** diciendo cuántas se quedaron fuera: es la degradación explícita de §8.4,
ni un 500 ni un plan silenciosamente peor. Lo mismo con las que el límite de
desvío deja fuera: son gasolineras que existen y que el usuario puede estar
buscando en el mapa, así que se dice cuántas y por qué, en vez de que desaparezcan
sin explicación.

Este endpoint conoce **un solo** combustible, el del coche, así que el mapa no
puede pintar otros desde aquí. Eso no es una carencia: es la tabla del principio
de §6 llevada hasta el final. La visualización multi-producto va por su propio
endpoint, §6.3.

### 6.3. Endpoint de mapa (decidido: `GET /api/estaciones` aparte, multi-producto)

El filtro de la UI muestra varios combustibles a la vez; el DP optimiza sobre
uno. Meter una lista de productos en `POST /api/ruta-optima` mezclaría las dos
cosas que §6 se esfuerza en mantener separadas, y obligaría al endpoint de
optimización a cargar con precios que el DP no va a mirar.

**Va aparte**: `GET /api/estaciones?min_lat=&min_lon=&max_lat=&max_lon=&productos=&rotulos=`.
Es exponer lo que ya existe —la consulta de bbox del `PriceStore` con su filtro
de marca (§6)— no lógica nueva: sin R\*Tree adicional, sin OSRM y sin tocar el
dominio.

- **`solo_vigentes` apagado**, que es el modo para el que §4.2 lo dejó así por
  defecto: el mapa muestra el precio caducado marcado ("sin actualizar desde
  hace X días") en vez de hacer desaparecer la estación.
- **Las marcas son las mismas** que sirve `GET /api/marcas`, con la regla de
  `INDEPENDIENTE` de §6 intacta: pedir una marca fuera del diccionario es 422.
- **Varios productos, una llamada por producto** contra el puerto, que hoy
  acepta `producto` en singular. Con los uno o dos productos que marca un
  usuario y SQLite en el mismo proceso, el coste es despreciable. Si algún día
  molesta, se ensancha la firma a `productos: Sequence[str]`; no hay nada en
  este diseño que lo impida.
- **El bbox lo manda el cliente**, que es quien sabe qué trozo de mapa se está
  mirando. Conviene un tope de área o de resultados: "toda España con cuatro
  productos" son decenas de miles de filas que nadie va a leer.

No sustituye a las candidatas que devuelve `/api/ruta-optima`: aquellas son las
que compitieron por entrar en el plan (§8.5), estas son simplemente lo que hay
dentro del recuadro que el usuario tiene delante.

---

## 7. Vehículo y consumo

**Fase 1 (ahora):** el usuario introduce su consumo manualmente.

**Modelo de depósito: capacidad + nivel actual** (decidido). Más preciso que
autonomía simplificada y necesario para que el DP decida *cuánto* repostar, no
solo dónde.

```python
@dataclass
class Vehiculo:
    consumo_l_100km: float  # o kWh/100km si se añaden eléctricos
    tipo_combustible: str  # "gasolina95", "diesel"...
    capacidad_deposito_l: float
    nivel_actual_l: float
    reserva_minima_l: float = 5.0  # el DP nunca baja de aquí
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
  - **El hueco inviable no siempre está en el primer tramo.** Puede llegarse a la
    primera estación y romperse después, si entre dos candidatas consecutivas hay
    más kilómetros de los que da el depósito lleno. El aviso tiene que señalar
    *ese* hueco: entre qué dos puntos, cuántos km hay, cuántos se alcanzan y
    cuántas candidatas quedaron fuera de alcance.
  - **`nivel_actual_l < reserva_minima_l` es un caso aparte**, no un trayecto
    inviable. Si el conductor ya va por debajo de la reserva, la pregunta correcta
    no es "cuál es la ruta óptima" sino "dónde está la gasolinera más cercana"
    (§10). Avisar de eso explícitamente en vez de devolver un plan imposible.
    **Aclaración**: "más cercana" se calcula respecto al **origen del trayecto**
    indicado en el request, no respecto a una posición en tiempo real del
    usuario — eso último es la función descrita en §10, que depende de
    geolocalización del frontend y hoy no está implementada. Con el diseño
    actual (origen/destino fijos, sin geolocalización), el origen es la única
    posición que la API conoce, así que es el punto de referencia correcto
    para este caso hasta que exista la función de posición en tiempo real.
    Implementado así en §6.2: la API responde 422 con las gasolineras más
    cercanas al origen, y lo resuelve **antes** de gastar una sola petición en
    OSRM, porque en ese caso no hay ninguna ruta que calcular.

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
3. **Acotar el desvío**: sin un límite, el algoritmo manda al usuario 4 km fuera
   de la A-2 para ahorrar 60 céntimos. Se resuelve con una restricción dura antes
   del DP, no poniéndole precio a la hora del conductor (§8.2).
4. El DP vive en `domain/` y es testeable con datos falsos, sin BD ni red.

Medido con la implementación actual: **250 candidatas en una ruta de 1.500 km se
resuelven en 262 ms**. Confirma el punto 1: preocuparse por la selección de
candidatas, no por el DP.

Con la API entera montada el punto 1 se ve todavía más claro. Una petición real
de Madrid a Barcelona (617 km, 33 candidatas) tarda **1,5 s de principio a fin**,
y de esos el DP son milisegundos: **todo lo demás es esperar a OSRM**. Cualquier
esfuerzo de optimización que no vaya dirigido a reducir peticiones a `/table`
está mirando al sitio equivocado.

### 8.1. Discretización del nivel de combustible (decidido: 0,25 litros)

El estado del DP es (estación, litros en el depósito al llegar). Como el nivel de
combustible es continuo, hay que discretizarlo en pasos fijos para que el DP tenga
un número finito de estados que tabular.

```python
PASO_DISCRETIZACION_L = 0.25  # constante ajustable, pero no inocua: ver abajo
```

Con depósitos típicos (40-70 L) esto da 160-280 estados por estación candidata,
barato. Sigue siendo una constante de ajuste fino y no una decisión de diseño,
pero **el valor importa más de lo que parece**, y por eso ya no es 1 L.

El consumo de cada tramo se redondea *hacia arriba* (§8.3), así que el paso es
también el error del plan. Medido en Madrid-Barcelona con 23 candidatas:

| Paso | Coste del plan | Ruido entre opciones | DP completo |
|------|----------------|----------------------|-------------|
| 1 L | 26,89 € | hasta 1,70 € | ~90 ms |
| 0,25 L | 25,67 € | ~0,4 € | ~490 ms |

Con 1 L, dos gasolineras separadas por una milésima aparecían separadas por
1,70 € en la lista de opciones (§8.6), solo porque a una le tocaba redondear un
litro más. Es decir: el ruido de la discretización era **mayor que la señal que
las opciones existen para enseñar**, y además el plan se encarecía 1,3 € sobre el
papel. Con 0,25 L el ruido baja a ~0,4 € y el DP sigue costando medio segundo,
que en una petición donde esperar a OSRM son dos segundos y medio no se nota.

Si alguna vez hay que recortar tiempo, subirlo es legítimo, pero hay que mirar
antes qué le hace a la comparación entre opciones.

#### Repostaje mínimo útil (decidido el 21/08/2026: 5 litros)

`REPOSTAJE_MINIMO_L = 5.0`. Al contrario que el paso, **esto no es un ajuste
fino: es una restricción del mundo** que el DP no puede deducir de sus propios
datos.

Optimizando solo el precio salen planes que paran en una gasolinera cara a echar
un dedal. Medido en Madrid-Barcelona antes de la restricción: el plan óptimo
proponía **tres** paradas y dos de ellas eran de 0,25 L y 3,75 L, una de ellas en
una REPSOL a 1,769 €/L. En la matriz esos céntimos salen a cuenta; en la
carretera nadie se desvía, hace cola y pasa la tarjeta por un cuarto de litro. Un
plan así es óptimo y a la vez inservible, que es la peor combinación posible: da
un número que no se puede ejecutar y desluce justo el plan que la interfaz
destaca (§13.3).

Con el mínimo puesto, el mismo viaje sale con **dos** paradas de 5 L y 20,75 L y
cuesta 39,06 € en vez de 38,98 €. Los ocho céntimos son el precio de que el plan
se pueda cumplir.

Implementación: entra como un **retardo en el barrido** de `_repostar_en`. Donde
antes un reposte que termina en el nivel `m` podía haber arrancado en `m-1`,
ahora arranca como muy tarde en `m - min_u`. Sigue siendo O(niveles) y con
`min_u = 1` produce exactamente la tabla anterior, que es la forma barata de
comprobar que solo generaliza. La pasada de vuelta (`_entrar_y_repostar`) lleva
la misma restricción, mirando desde `llegada + min_u` en vez de `llegada + 1`:
si solo estuviera en la ida, las dos tablas dejarían de cuadrar.

Dos consecuencias que conviene tener presentes:

- **La viabilidad no cambia en la práctica.** Repostar exige llegar con
  `nivel <= capacidad - 5 L`, y quien llega por encima de eso no necesita
  gasolina.
- **Una candidata donde no quepan 5 L deja de ser ofrecible** (§8.6): la parada
  obligatoria sale inviable y esa estación desaparece sola de la lista. Es lo
  correcto: parar ahí no es una opción que tenga sentido ofrecer.

### 8.2. Coste del desvío (decidido: restricción dura, no precio de la hora)

> **Esta sección se reescribió el 19/08/2026.** Antes decía que el coste era
> `combustible + valor_tiempo_eur_h * tiempo`, con 15 €/h por defecto. Ver
> "Decisiones revisadas" en §11 para el porqué del cambio.

El punto 3 de arriba dice *qué* hay que modelar. La respuesta ya no pasa por
ponerle precio a la hora del conductor:

```
minimizar   euros de combustible comprado
sujeto a    desvío de cada estación <= max_desvio_km  (y <= max_desvio_min)
            el nivel nunca baja de la reserva
desempate   menos tiempo total
```

**Por qué no €/hora.** Una hora del conductor no cuesta dinero, y fijarle un
precio era una decisión que nadie había tomado: quedaba enterrada en una
constante y contaminaba la única cifra real del viaje, que son los euros que se
pagan en el surtidor. Además producía un desglose que no significa nada
("Tiempo: 1,25 €") y que el usuario no puede comprobar contra nada.

**Qué lo sustituye.** Lo que impedía mandar a nadie 4 km fuera de la A-2 por 60
céntimos era ese término de tiempo. Ahora lo impide el **límite de desvío**, que
se aplica *antes* del DP, al elegir qué candidatas entran
(`seleccion_candidatas.depurar_matriz`). El DP solo ve estaciones a las que ya
merece la pena ir, y dentro de ese conjunto puede mandar el dinero sin producir
disparates.

**Cómo se mide el desvío.** Contra el viaje sin parar, en kilómetros de
conducción real de la matriz de OSRM:

```
desvio = d(origen -> estación) + d(estación -> destino) - base
```

Es un número **por estación**, no por plan, así que se calcula antes del DP, sirve
de filtro y se puede enseñar en el mapa. Sigue sin usarse la distancia
perpendicular a la polilínea (§8, punto 2).

**Ojo con la `base`: no es la celda `[0][destino]`.** Esa fue la primera versión y
estaba mal, de una forma que engañaba hacia el lado peligroso. `/table` devuelve
la distancia **del camino más rápido**, no la del más corto, y el más rápido a
veces da un rodeo por autovía. Resultado: la celda directa puede ser más larga que
un camino que pase por una gasolinera pegada a la ruta.

Medido con origen y destino a las afueras de Madrid: la celda directa daba
642,4 km cuando por una estación de Arganda se llegaba en 636,1. Usar 642,4 de
referencia le restaba **6,2 km a todos los desvíos** y colaba como "a menos de
10 km" gasolineras que estaban a dieciséis. El sesgo depende de lo bien que
enganchen origen y destino con la red: 0,6 km desde el centro de Madrid, 3,7 en
Madrid-Sevilla, 6,2 desde un punto en mitad del campo. Por eso los resultados
parecían erráticos.

La referencia es ahora **el camino más corto que la matriz conoce**: la celda
directa o, si alguna estación lo mejora, esa (`seleccion_candidatas.linea_base`).
Nunca infravalora un desvío, que de los dos errores posibles es el único que
importa: el que manda al conductor lejos sin avisar.

- `max_desvio_km`, por defecto **6 km**: son los kilómetros de más, ida y vuelta.
  Una gasolinera a 3 km de la carretera son ~6 km de desvío. Es el mando que ve y
  toca el usuario. Empezó en 10 km —los "5 km de la carretera" que se pidieron—,
  pero con la medida ya corregida 10 seguía admitiendo gasolineras que a ojo están
  lejos, así que se apretó.
- `max_desvio_min`, por defecto **10 min**: red de seguridad, no mando principal.
  Cubre el caso que los kilómetros no distinguen: la gasolinera a un kilómetro de
  la vía pero a diez minutos por dentro del pueblo. Sin este tope, con el objetivo
  en euros puros el DP se iría a ella (hay un test que lo fija).
- `tiempo_parada_s`, por defecto **300 s**: sobrevive, pero en la componente de
  **tiempo**, jamás en la de dinero. Sigue evitando paradas gratuitas —dos
  repostajes al mismo precio pierden contra uno— sin ponerle precio a nada.

**Consecuencia que hay que aceptar**: con objetivo en euros puros y límite duro,
el plan sí acepta 9 km de desvío para ahorrar unos céntimos, mientras 9 km esté
dentro de lo admitido. Es coherente: dentro de lo aceptable manda el dinero. Y
está compensado por diseño, porque si al conductor no le compensa ese desvío, la
lista de opciones (§8.6) le ofrece la alternativa con la cifra exacta de lo que le
cuesta. La decisión vuelve al usuario en vez de esconderse en una constante.

**El desempate**, en `Coste`, es una tupla `(micro-euros, milisegundos)` que
Python compara lexicográficamente: primero el dinero, y solo si empata, el tiempo.
Que el tiempo esté en otra componente y no sumado es exactamente la diferencia
entre "tu hora vale 15 €" y "entre dos planes que cuestan lo mismo, prefiero el
más rápido". Lo segundo no le pone precio a nada. Todo entero, ningún float (§8.3).

El DP sigue necesitando **la matriz de duraciones**, ahora para el desempate y
para poder decirle al usuario los minutos. Sin ella el DP resuelve igual.

Al presentar el resultado, el tiempo va **en minutos y solo en minutos**, y como
exceso sobre la ruta directa: si no, el número no significa nada.

### 8.3. Aritmética: enteros, ningún float

La función objetivo del DP trabaja en **micro-euros enteros**, no en euros float.
No es purismo: dos estaciones que difieren en una milésima empatan o se ordenan
según por dónde haya pasado la acumulación, y el plan cambia entre ejecuciones.
Con `paso = 1 L`, una unidad de combustible cuesta exactamente
`precio_milesimas * 1000` micro-euros, así que la conversión ni siquiera pierde
precisión.

Los redondeos de la discretización van **siempre del lado seguro**: consumo hacia
arriba, capacidad y nivel inicial hacia abajo. Ningún paso de discretización
puede producir un plan que en la realidad se quede sin combustible.

### 8.4. Resiliencia ante fallos de OSRM (decidido: reintentos + degradación explícita, sin fallback silencioso)

OSRM público (§9) no da garantías de uptime ni respeta siempre el límite de
1 req/s bajo carga, y `/table` es una dependencia dura tanto para la selección
de candidatas como para el coste del desvío (§8.2). Una petición de ruta óptima
no debería romperse por completo por un fallo transitorio de un servicio externo.

**Enfoque elegido**:

1. **Reintentos con backoff exponencial acotado** (ej. 3 intentos, 0.5 s / 1 s / 2 s)
   ante timeout o 5xx de OSRM. Cubre el caso más común: un pico de carga puntual
   del servidor público.
2. **Si los reintentos se agotan, no se aproxima el `/table` con distancia
   euclídea u otro sucedáneo silencioso.** El coste de desvío depende de
   carretera real, no de línea recta, y el punto 2 de §8 ya explica por qué la
   distancia en línea recta lleva a recomendaciones absurdas (mandar a alguien
   campo a través). Un fallback silencioso cambiaría la calidad del resultado
   sin que el usuario lo sepa.
3. **En su lugar, se degrada explícitamente**: la API devuelve el mejor plan
   calculable con lo que sí respondió OSRM (si hubo respuesta parcial) o un
   error claro indicando que el cálculo de ruta no está disponible ahora mismo,
   en vez de un plan silenciosamente peor o un 500 genérico. El frontend puede
   entonces decidir cómo mostrarlo (reintentar, avisar al usuario), pero esa
   decisión no se toma escondida dentro del adaptador.
4. Esto vive enteramente en `osrm_adapter.py`: es un detalle del adaptador, no
   del puerto `RoutingProvider` ni del dominio. Cuando se pase a OSRM
   autoalojado (§9, ~100 usuarios), el mismo mecanismo sigue siendo válido —
   incluso un servidor propio puede tener un mal momento — aunque los timeouts
   probablemente puedan acortarse al no depender de una red pública compartida.

**Cómo se representa "no hay ruta"**: con `inf` en la celda de la matriz, nunca
con un cero ni con un número grande. Un cero le diría al DP que ese viaje es
gratis, que es el peor error posible aquí. Los puntos que OSRM no supo resolver
salen además listados aparte (`MatrizRuta.indices_sin_respuesta`), y **hay que
descartarlos antes de llamar al DP**; de eso se encarga la selección de
candidatas (§8.5). El DP entiende `inf` como "esa arista no existe" y sigue
trabajando con el resto.

Lo que se aprendió montándolo contra el servidor público (agosto de 2026):

- **`/table` acepta como mucho 100 coordenadas por petición.** Con las 250
  candidatas de §8 la matriz no cabe ni de lejos, así que se pide **por
  bloques**. Como un bloque cruzado manda orígenes y destinos en la misma lista,
  el tamaño de bloque efectivo es la mitad: 50. Esto no es un detalle de
  implementación, es lo que hace que el número de candidatas se pague al
  cuadrado (§8.5).
- **El servidor público sí soporta `annotations=distance`**, que era la duda
  razonable: sin distancias reales, el coste del desvío de §8.2 no se puede
  calcular y habría que replantear el modelo. Si algún día un servidor no lo
  soporta, el adaptador lo dice con un error explícito en vez de apañárselas.
- **El límite de ~1 req/s hay que respetarlo en el código**, no en la buena
  intención: el adaptador espacia sus propias peticiones. Con OSRM propio se
  pone a 0 y desaparece la espera.

### 8.5. Selección de candidatas (decidido: corredor por tramos + cupo por precio)

El punto 1 de §8 dice que aquí está el cuello de botella pero no cómo resolverlo.
Procedimiento elegido, en dos fases:

1. **Filtro grueso y barato.** La polilínea se trocea en tramos de ~50 km y de
   cada tramo sale un rectángulo estrecho (5 km de margen por defecto) para el
   R\*Tree. **Por tramos y no con un rectángulo único**: el bbox de
   Madrid-Barcelona mete dentro media península, con lo que el filtro espacial
   no filtra nada. Los tramos comparten el punto de unión para que ninguna
   estación se cuele por la juntura.
2. **Recorte por precio antes de gastar red.** De cada tramo pasan solo las más
   baratas. Es lo que permite bajar de miles de estaciones a unas decenas sin
   que el plan empeore: una estación cara rodeada de baratas no entra en el
   óptimo por muy bien situada que esté.

**El cupo se reparte por tramos, no globalmente** (decidido, y es la parte que
tiene truco). Coger "las 50 más baratas de la ruta" puede dejarlas todas en la
misma provincia y 300 km sin una sola parada posible: el resultado no es un plan
peor, es un `TrayectoInviable`. Repartir el cupo garantiza cobertura de punta a
punta aunque algunos tramos aporten estaciones caras.

**`max_candidatas` = 50 por defecto.** Es el mando que gobierna el tiempo de
respuesta, y conviene entender por qué: con bloques de 50, `n` candidatas cuestan
`ceil((n+2)/50)²` peticiones a `/table`, a un segundo cada una con el servidor
público. 50 candidatas es una petición y unos segundos; 250 son 25 peticiones y
casi medio minuto de espera. Con OSRM propio (§9) el número puede subir sin
miedo, porque desaparece el límite de ritmo.

**El orden por tramos es provisional.** El DP exige las candidatas ordenadas por
avance en la ruta, y el tramo al que pertenece cada una es una buena
aproximación, pero solo eso. Cuando llega la matriz ya se conoce el kilometraje
real desde el origen, así que **se reordena con ese dato** en el mismo paso en
que se descartan las que OSRM no resolvió.


### 8.6. Opciones: dónde puede elegir parar el conductor (decidido: ventanas de conducción)

El plan óptimo contesta "dónde repostar más barato". Es la respuesta correcta a
una pregunta que casi nadie se hace así. La de verdad es: *"salgo a las ocho,
¿paro a las nueve, a las diez o a las once?"*. Un plan cerrado no sirve para eso,
y obliga al usuario a aceptar la parada que le toque o a no usar la herramienta.

Así que la respuesta final no es un plan, es **un plan más un abanico de opciones**.

**Qué es una opción.** "Mi próxima parada es esta": no se reposta antes, se
reposta ahí, y a partir de ahí el plan vuelve a ser libre. Se intenta primero sin
más paradas —repostar lo que haga falta y llegar—, que es lo que quiere decir el
conductor al elegir dónde parar; si el depósito no da para tanto, se repite
dejando repostar después, y la opción enseña esa segunda parada, que es justo lo
que hay que saber antes de elegir.

Esto no es un detalle: sin la restricción de "y sigue", el plan más barato que
para en una gasolinera cara es **echar un litro** ahí y comprar de verdad cien
kilómetros después. Es correcto y es inútil, porque nadie llama a eso parar.

**Cuánto cuesta cada una.** `sobrecoste_eur` = euros de combustible del viaje
entero parando ahí, menos los del plan óptimo. En euros, porque son euros, y
comprobable a mano restando dos cifras que también se enseñan. Puede que ninguna
opción valga cero: el plan óptimo es libre de repartir el repostaje en dos paradas
y salir más barato que cualquier parada única. Por eso lo que se marca es **la más
barata de las ofrecidas**, que es entre lo que el usuario elige de verdad.

**Cómo se reparten.** Ventanas de ~45 min de conducción, la mejor de cada una.
Da unas 3 opciones en un viaje de 2 h y unas 7 en uno de 5 h. Mismo argumento que
el cupo por tramos de §8.5, aplicado al tiempo en vez de al espacio: las N más
baratas pueden estar todas en el mismo tramo de cien kilómetros y no le sirven a
quien quiere parar antes o después. Dos reglas de borde:

- Si hay menos gasolineras viables que ventanas, se ofrecen todas: repartir solo
  serviría para esconder alternativas que caben de sobra en la pantalla.
- Las ventanas cubren hasta donde **de verdad se puede llegar**, no hasta el
  destino. Si el depósito se acaba a las dos horas, ofrecer un hueco para las
  cuatro sería ofrecer una decisión que no existe. Por lo mismo, un viaje largo
  con el depósito casi vacío da pocas opciones, y eso es la verdad, no un fallo.
- Si no hace falta repostar, no hay opciones. Comparar contra un plan que no
  compra combustible daría sobrecostes que no significan nada.

**Cómo se calcula sin que cueste un DP por candidata.** Tres pasadas para todas
las candidatas a la vez:

1. la de ida normal, que da el plan óptimo;
2. la de ida **en ayunas** (prohibido repostar), que da con cuánto se llega a cada
   estación sin haber repostado antes;
3. la de **vuelta**, que da lo que cuesta seguir desde cada estación al destino.

Pegando 2 y 3 por el nivel del depósito sale, en O(niveles) por candidata, el
mejor plan cuya próxima parada es esa. Eso ordena las candidatas dentro de cada
ventana; el plan completo solo se reconstruye para el puñado que se va a enseñar.
La pieza que lo hace barato es la rama "ya he empezado a repostar" que
`_repostar_en` calculaba desde siempre y tiraba a la basura.

Medido con 23 candidatas: plan óptimo 95 ms, sobrecostes 206 ms, 8 planes de
opción ~190 ms. Medio segundo de CPU en una petición donde esperar a OSRM son dos
segundos y medio; el cuello de botella sigue siendo el de §8, punto 1.

**Lo que deja preparado**: la parada de emergencia de §10 ("la más cercana sin
desviarme mucho") necesitaba exactamente el desvío por estación que §8.2 introduce.

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

- **Y corta el handshake TLS con OpenSSL 3 por defecto** (comprobado el
  18/08/2026, y es la trampa más cara de las suyas). El síntoma es un
  `httpx.ConnectError` con "conexión forzada por el host remoto": clavado a una
  caída de red, así que lo primero que uno hace es sospechar del User-Agent, que
  es lo que avisa todo el mundo. No es eso. Con **el mismo URL y el mismo
  User-Agent**, `curl` baja los 12 MB y `httpx` no; forzar TLS 1.2 tampoco
  arregla nada. Lo que sobra es el `SECLEVEL=2` por defecto de OpenSSL 3, que ya
  no admite los cifrados que ofrece ese IIS:

  ```python
  contexto = ssl.create_default_context()
  contexto.set_ciphers("DEFAULT@SECLEVEL=1")  # el certificado se sigue verificando
  httpx.AsyncClient(verify=contexto)
  ```

  Está en `contexto_tls_geoportal()`, y solo afecta a esa llamada. Nada de
  `verify=False`: bajar el nivel de cifrado para hablar con un servidor viejo es
  una cosa, dejar de comprobar con quién hablas es otra muy distinta.

- **El endpoint bueno es este**:

  ```
  https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes/PreciosCarburantes/EstacionesTerrestres/
  ```

  El que citan la mayoría de fuentes y tutoriales, con `PrestacionesServicios` en
  vez de `PreciosCarburantes`, **devuelve 404** (comprobado en agosto de 2026). El
  404 lo sirve el IIS del Ministerio, así que parece un cambio de ruta y no una
  caída. Documentación de las operaciones:
  `https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes/PreciosCarburantes/help`

  Este es el fallo más fácil de dejar escondido: si los tests van contra un
  snapshot local —que es lo correcto (§9)— la URL mala no la coge nadie hasta que
  alguien intenta una ingesta de verdad. La bueno confirmada contra el servicio
  vivo el 14/08/2026: 200, 12,2 MB, 11.507 estaciones.

- **Volumen esperado por ingesta**, para comparar y detectar que algo va mal:
  ~11.500 estaciones y ~43.000 precios en la primera carga. A partir de ahí, una
  reingesta del mismo snapshot debe insertar **0 filas** en `precios`; si inserta
  más, el diffing está roto.

  Cifras reales de dos ingestas separadas por 48 h: 11.514 → 11.507 estaciones
  (el censo se mueve un poco, es normal) y **24.745 precios nuevos de 43.078
  leídos, un 57%**. Es el orden de magnitud a esperar en una ingesta con días de
  por medio; si un ciclo normal inserta el 100%, el diffing no está funcionando,
  y si inserta el 0% con horas de diferencia, sospecha de la fecha del snapshot.

- **Snapshots en el repositorio**: el crudo completo (~12 MB) no se versiona. Sí
  se versiona un subconjunto reducido en `tests/fixtures/`, con los casos límite
  que el snapshot público no contiene (`Tipo Venta = R`, coordenadas ausentes,
  precio cero, producto sin mapear, campo de longitud alternativo).

### 9.1. Cómo se sirve el frontend (decidido: mismo origen, desde FastAPI)

El paso 5 son ficheros estáticos (HTML, JS, Leaflet) y hay que decidir quién los
entrega antes de escribirlos, porque cambia el arranque.

**Los sirve la propia app**, con `StaticFiles` montado en la raíz y la API
colgando de `/api`. Mismo origen, así que **no hay CORS que configurar**: en un
despliegue local (§9) y sin login ni cookies (§3), montar CORS sería
configuración a cambio de nada, y una lista de orígenes permitidos mal puesta es
de los fallos que solo aparecen al desplegar.

- El router de la API va montado **antes** que los estáticos, para que
  `/api/...` nunca lo resuelva el servidor de ficheros.
- Si algún día el frontend se sirve aparte (CDN, otro host), añadir
  `CORSMiddleware` es una línea y no cambia nada del diseño: esta decisión no
  cierra esa puerta, solo evita pagar hoy por ella.
- No convierte a FastAPI en un servidor web serio: cuando haya despliegue real
  con proxy inverso delante, los estáticos los puede servir él y la app se queda
  igual.

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
  - **Ya tiene la pieza que le faltaba**: "sin desviarme mucho" necesitaba una
    medida de desvío por estación, y §8.2 la introdujo (`Desvio`, calculado en
    `seleccion_candidatas.desvio_de`). El endpoint de emergencia sería el mismo
    filtro aplicado a la posición actual en vez de a la ruta entera.
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
- [x] **Paso de discretización del DP**: 0,25 litros (ver sección 8.1)
- [x] **Versión de Python**: >= 3.12, por el módulo R*Tree (ver §2)
- [x] **Coste del desvío**: restricción dura de desvío (6 km y 10 min por
      defecto) con el objetivo en euros de combustible; el tiempo no se cobra
      (ver §8.2)
- [x] **Opciones al usuario**: no un plan cerrado sino un abanico repartido por
      ventanas de ~45 min, con el sobrecoste en euros de cada una (ver §8.6)
- [x] **Qué es una opción**: "mi próxima parada es esta", repostando lo que haga
      falta para llegar; no "echa un litro aquí" (ver §8.6)
- [x] **Referencia del desvío**: el camino más corto que conoce la matriz, no la
      celda directa, que con `/table` puede ser más larga (ver §8.2)
- [x] **Aritmética del DP**: enteros en micro-euros, ningún float (ver §8.3)
- [x] **Matcher de rótulos**: por palabra completa, no por substring (ver §4.1)
- [x] **Cepsa y Moeve**: agrupadas bajo `MOEVE` (ver §4.1)
- [x] **Precio efectivo vs. nominal**: abstracción `precio_efectivo()` desde el
      día uno, identidad en fase 1 (ver sección 6.1)
- [x] **Antigüedad de precios**: se descartan como vigentes a partir de 48 h,
      la estación se marca como "sin actualizar" en vez de ocultarse (ver §4.2)
- [x] **Resiliencia ante fallos de OSRM**: reintentos con backoff acotado, sin
      fallback silencioso a distancia euclídea; degradación explícita si se
      agotan los reintentos (ver §8.4)
- [x] **"Gasolinera más cercana" cuando `nivel_actual_l < reserva_minima_l`**:
      se calcula respecto al origen del trayecto, no a una posición en tiempo
      real (ver §7)
- [x] **`direccion` en la tabla `estaciones`**: columna nueva en la tabla mutable
      (ver esquema en §4)
- [x] **`INDEPENDIENTE` en el filtro por marca**: no es filtrable como opción;
      solo las ~20 marcas del diccionario lo son. Las independientes se muestran
      siempre por defecto y solo desaparecen si el usuario filtra activamente
      por otra marca (ver §6)
- [x] **Selección de candidatas**: corredor troceado en tramos de ~50 km y cupo
      repartido por tramo, no global; 50 candidatas por defecto (ver §8.5)
- [x] **Dónde vive la selección de candidatas**: en `domain/`, porque se escribe
      solo contra los puertos (ver §3)
- [x] **Origen y destino**: coordenadas, sin geocoding en el servidor (ver §6.2)
- [x] **Respuesta de la API**: plan + polilínea + candidatas en una sola llamada,
      para que el mapa no necesite un segundo endpoint (ver §6.2)
- [x] **"No hay ruta" en las matrices**: se representa con `inf` y con la lista
      de índices sin respuesta; nunca con un cero (ver §8.4)
- [x] **`precio_efectivo` con un perfil de descuento**: lanza
      `NotImplementedError` en vez de aplicar la identidad (ver §6.1)
- [x] **Filtro multi-combustible para el mapa**: endpoint propio
      `GET /api/estaciones`, con varios productos y sin tocar el de ruta óptima,
      que sigue conociendo solo el combustible del coche (ver §6.3)
- [x] **Cómo se sirve el frontend**: desde el propio FastAPI, mismo origen y sin
      CORS; la API en `/api` y los estáticos en la raíz (ver §9.1)
- [x] **Alcance de la v1 del frontend**: solo lo que devuelve `/api/ruta-optima`;
      el filtro multi-combustible es un paso 6 aparte (ver §13.1)
- [x] **Herramientas del frontend**: sin build, módulos ES y Leaflet vendorizado
      en el repo, no por CDN (ver §13.1)
- [x] **Buscador de direcciones**: Photon desde el navegador, con autocompletado;
      Nominatim descartado por su política y CartoCiudad por no servir para texto
      a medias. El servidor sigue sin geocodificar (ver §13.2)

### Revisadas

Decisiones que estuvieron cerradas y se reabrieron. Se dejan escritas con su
motivo: si no, dentro de un año alguien las vuelve a tomar como estaban.

- **Coste del desvío: de €/hora a restricción dura** (19/08/2026). Estaba cerrado
  como "15 €/h sobre el exceso de tiempo + 300 s por parada". Se cambió por dos
  motivos. El primero, que ponerle precio a la hora del conductor es una decisión
  que nadie había tomado, quedaba enterrada en una constante y ensuciaba la única
  cifra real del viaje —lo que se paga en el surtidor— con un "coste de tiempo"
  que el usuario no puede comprobar contra nada. El segundo, que un plan cerrado
  no contesta la pregunta que se hace de verdad quien conduce, que es *cuándo*
  parar; para poder ofrecerle varias opciones y decirle lo que cuesta cada una
  hacía falta que "cuesta" significase euros y nada más. Lo que el término de
  tiempo protegía —no mandar a nadie fuera de la autovía por céntimos— lo protege
  ahora el límite de desvío, que además el usuario ve y puede tocar. Ver §8.2.

- **Paso de discretización: de 1 L a 0,25 L** (19/08/2026). Era "constante de
  ajuste fino, solo afecta a rendimiento", y dejó de serlo al aparecer las
  opciones (§8.6): con 1 L el ruido de redondeo entre dos opciones llegaba a
  1,70 €, más que las diferencias de precio que la lista existe para enseñar, y
  el plan se encarecía 1,3 € sobre el papel. Ver §8.1.

### Pendientes

Ninguna abierta. Lo que queda fuera del alcance de hoy no es una decisión sin
tomar sino trabajo aplazado a propósito, y ya tiene su sitio: recarga eléctrica
(§5), catálogo de vehículos y `PerfilDescuento` real (§7 y §6.1), escalado con
sus disparadores (§9) e ideas de futuro (§10).

---

## 12. Orden sugerido para empezar a programar

1. ✅ **Ingesta + esquema**: `geoportal_client.py` + `sqlite_adapter.py`. Baja los datos,
   parsea coma decimal, normaliza rótulos, guarda con el diffing de precios.
   Sin API ni frontend todavía. Verifica que el volumen de filas es el esperado.
2. ✅ **Dominio aislado**: `models.py` + `dp_optimizer.py` con tests usando estaciones
   y matriz de distancias inventadas. Aquí está el valor del proyecto; que funcione
   antes de tocar red o mapa.
3. ✅ **`osrm_adapter.py`**: polilínea + `/table` para desvíos reales. Troceado en
   bloques por el límite de 100 coordenadas, con reintentos y sin fallback (§8.4).
4. ✅ **API FastAPI**: un solo endpoint de ruta óptima que une las tres piezas
   (§6.2), más la selección de candidatas que las pega (§8.5).
5. ✅ **Frontend Leaflet** (§13): v1 mínima —mapa, formulario, plan y pantallas de
   error— servida por la propia app en la raíz, sin CORS (§9.1), con buscador de
   direcciones Photon en el cliente (§13.2).
6. ✅ **Opciones y desvío acotado** (§8.2 y §8.6): fuera el precio de la hora, la
   respuesta pasa de un plan cerrado a un abanico de paradas posibles con su
   sobrecoste en euros. Tocó dominio, API y frontend a la vez porque el cambio es
   del modelo, no de la presentación.
7. **Filtro multi-combustible**: `GET /api/estaciones` (§6.3) y los controles de
   producto y marca sobre el mapa. Va al final a propósito: es lo único que queda
   que no cambia el modelo, solo añade visualización.

El paso 2 antes del 3 es deliberado: si el DP depende de OSRM para poder probarse,
pierdes la ventaja principal de haber elegido hexagonal. Visto en retrospectiva,
salió bien: cuando llegó OSRM, lo único que hubo que enseñarle al DP fue que una
distancia puede ser `inf`.

---

## 13. Frontend (pasos 5 y 6)

### 13.1. Alcance y forma (decidido: v1 mínima, una pantalla, sin build)

**La v1 pinta solo lo que ya devuelve `POST /api/ruta-optima`**: mapa, formulario
de vehículo, plan y pantallas de error. El filtro multi-combustible sobre
`GET /api/estaciones` (§6.3) es una segunda tanda, no un requisito para ver el
proyecto funcionando. Hasta hoy el backend solo se puede usar con `curl`, y hay
una clase de error —un plan que a ojo no tiene sentido— que ningún test cuenta y
un mapa sí.

**Sin build: HTML, módulos ES y Leaflet vendorizado en el repo.** Es lo que §2 ya
anticipaba con "web sencilla". Un formulario, un mapa y una lista no justifican
Vite ni `node_modules`, y el proyecto no tiene hoy ninguna dependencia de JS que
mantener. Si algún día crece, migrar son cuatrocientas líneas.

Lo de vendorizar Leaflet **no es para funcionar sin internet** —los tiles, OSRM y
el buscador necesitan red igual—: es para no depender de la disponibilidad ni de
la política de un CDN, por el mismo motivo por el que los tests van contra un
snapshot local (§9).

```
app/static/
├── index.html
├── app.js          # estado de la pantalla y orquestación
├── api.js          # fetch + traducción de los errores tipados de §6.2
├── geocoder.js     # buscar(texto, cerca) -> [{etiqueta, lat, lon}] — §13.2
├── mapa.js         # Leaflet: polilínea, capas, marcadores, popups
├── panel.js        # formulario, buscador, plan y opciones
├── hoja.js         # la carcasa: hoja arrastrable, pestañas y márgenes del mapa
├── formato.js      # euros, litros, km y duraciones; lo comparten panel y mapa
├── estilos.css
├── logo.jpg        # copia de SmallSquareLogoJpg.jpg: el mount solo sirve esto
└── vendor/leaflet/ # 1.9.4, con sus imágenes
```

El catálogo del desplegable de combustibles lo sirve `GET /api/combustibles`,
por el mismo motivo que `/marcas` (§6): que la UI no mantenga su propia copia de
los códigos y se desincronice con la ingesta.

**Una sola pantalla, sin router.** El modelo es el de una app de mapas de móvil,
porque de móvil va a venir la mayoría: **el mapa ocupa la ventana entera** y la
interfaz flota encima en dos piezas.

- **Barra superior**, siempre visible: logotipo, origen y destino con el hilo que
  los une, la chincheta que dice cuál fija el próximo clic y el botón de dar la
  vuelta al viaje.
- **Hoja inferior** arrastrable, con tres escalones (`peek`, `medio`, `alto`) más
  una altura `auto` para las pantallas que miden lo que miden. `alto` se topa
  contra la barra a propósito: si la hoja se le metiera detrás, el asa dejaría de
  poder agarrarse. En ≥900 px las dos piezas se apilan en una tarjeta a la
  izquierda y el arrastre se desactiva; es el mismo DOM.

Los cuatro estados siguen siendo los de siempre —formulario, esperando, resultado
y error—, y el error sigue ocupando el sitio del resultado, nunca un `alert`. Lo
que cambia es que ahora cada estado trae su juego de pestañas y su botón de pie:

| estado | pestañas | pie | altura |
|---|---|---|---|
| formulario | Vehículo · Marcas · Ajustes | `Calcular ruta óptima` | `medio` |
| esperando | — | — | `auto` |
| resultado | El plan · Alternativas | `Cambiar los datos` | `medio` |
| error | — | los suyos, dentro | `auto` |

Las pestañas de la tabla son **candidatas, no una lista fija**: `hoja.js` monta
solo las de los paneles que existan en el DOM, y si queda una sola no monta
ninguna. Así, cuando no hace falta repostar, `panel.js` no pinta
`#panel-alternativas` y la pestaña desaparece sin que la carcasa sepa nada del
dominio.

`panel.mostrarVista()` avisa con el evento `vista:cambiada` y `hoja.js` reacciona.
Un evento y no una llamada directa para que `panel.js` no tenga que importar la
carcasa —y para que la carcasa pueda leer lo que `panel.js` acaba de pintar.

#### El titular es la referencia (reescrito el 21/08/2026)

La cifra en euros vive en `#hoja-resumen`, fuera del cuerpo con scroll y por
encima de las pestañas: lo que el usuario ha venido a ver no se esconde detrás de
una pestaña. De ahí sale también el alto del escalón `peek`, que se mide en vez
de fijarse (`--alto-peek`).

Pero además **es el punto de comparación de toda la pantalla**, y por eso lleva
encima una ceja que lo dice: *"el plan más barato"*. El diseño anterior partía el
resultado en tres pestañas (Opciones / Plan / Datos) y enseñaba en la lista solo
el `sobrecoste_eur`. Eso era ilegible por una razón que no es de maquetación: el
plan óptimo puede repartir el repostaje en varias paradas y **salir más barato
que cualquier alternativa de parada única**, así que ninguna opción marcaba
"+0,00 €" y el usuario no tenía forma de ver contra qué se comparaba. Encima, la
mejor de la lista se etiquetaba "la más barata", que era falso.

Ahora:

- El titular, siempre visible desde las dos pestañas, es el plan y su precio.
- Cada alternativa enseña **su precio total del viaje** como cifra principal y el
  sobrecoste debajo. La fila se basta sola, y `total − titular = sobrecoste` es
  una resta que el usuario puede hacer con los dos números en pantalla. Ese
  invariante es lo que hay que proteger si esta pantalla se vuelve a tocar.
- La mejor alternativa se marca como **"la mejor alternativa"**, no como "la más
  barata": la más barata es el plan.

La leyenda de la escala de precios se fue a una píldora flotando sobre el mapa
(`#leyenda-mapa`, la pinta `mapa.js`): explica los colores al lado de los puntos
que colorea, deja de gastar una pestaña entera y de paso `panel.js` deja de
importar de `mapa.js`. Las atribuciones viven solo en la pestaña Ajustes.

**Márgenes del mapa.** Con el mapa a pantalla completa, encajar la ruta contra los
bordes del contenedor la metería debajo de la carcasa. `hoja.margenes()` dice
cuántos píxeles tapa cada lado y `mapa.js` los usa en todos sus `fitBounds`. Por
eso el orden en `app.js` es pintar el panel → enseñarlo (que recoloca la hoja) →
pintar el mapa.

**Color.** El acento es el naranja del logotipo (`--marca-color`). La rampa de
precios del mapa sigue siendo azul y no se toca: es dato, no decoración, y un
azul de marca competiría con ella. Hay modo oscuro siguiendo el del sistema; en
él se invierte **solo** `.leaflet-tile-pane`, que es donde viven los tiles: los
marcadores y la polilínea están en otros panes y conservan su color. `--trazado`
y `--anillo` sí cambian con el tema, porque un trazo negro no se ve sobre un mapa
oscuro, y `mapa.js` los lee del CSS en vez de llevarlos escritos.

El `Vehiculo` se guarda en `localStorage`, que es exactamente lo que §3 previó al
decidir stateless: el servidor no recuerda nada y el navegador no obliga a
repetir el formulario.

### 13.2. Buscador de direcciones (decidido: Photon, desde el navegador)

Escribir "Gran Vía 1, Madrid" es **obligatorio**, no un extra: obligar a clicar en
el mapa para fijar origen y destino es peor producto. §6.2 no lo impedía —lo que
decidió es que el servidor no geocodifica—, así que el buscador vive entero en el
cliente y no añade puerto, adaptador ni dependencia al backend.

**Los tres candidatos, comprobados contra el servicio vivo (agosto de 2026)**:

| Geocoder             | CORS | Clave | Texto a medias (`plaza catalu`)   | Dirección exacta         |
| -------------------- | ---- | ----- | --------------------------------- | ------------------------ |
| **Photon** (komoot)  | `*`  | no    | ✅ Plaza de Cataluña, Madrid/Leganés | encuentra Calle Mayor 1  |
| CartoCiudad (IGN)    | `*`  | no    | ❌ `gran via 5 val` → Pozuelo, Mont-roig | ✅ portal, CP y municipio oficiales |
| Nominatim            | `*`  | no    | su política **descarta el autocompletado** | correcto        |

- **Los tres sirven `Access-Control-Allow-Origin: *`**, así que ninguno necesita
  proxy. Era la duda que podía haber roto §6.2 y no la rompe.
- **Nominatim queda fuera por política, no por técnica**: la instancia pública no
  admite búsquedas por pulsación. Serviría para "buscar al pulsar Enter", que es
  justo la experiencia que se quiere evitar.
- **CartoCiudad no es un buscador, es un normalizador de direcciones.** Con la
  dirección completa es el mejor de los tres y encima oficial (tiene endpoint
  `candidates` en JSON limpio, sin envoltorio JSONP); con texto a medias devuelve
  cualquier cosa. Es el sitio al que ir si algún día hace falta clavar el número
  de portal, no el que alimenta el desplegable.

**Detalles que costaron una comprobación y conviene no redescubrir**:

- **Photon no acepta `lang=es`**: solo `default`, `de`, `en`, `fr`. Mandarlo
  devuelve un 400, no un aviso.
- **Photon no tiene filtro por país.** El sesgo se da con `lat`/`lon` al centro
  del mapa, y España se filtra en el cliente por
  `properties.countrycode === "ES"`.
- **`properties.name` viene a `null`** en portales, así que la etiqueta que ve el
  usuario hay que componerla con `street`, `housenumber` y `city`.

**Higiene de peticiones** (la instancia pública es gratuita y pide buen uso):
retardo de ~300 ms desde la última tecla, mínimo 3 caracteres, `AbortController`
para cancelar la búsqueda anterior y caché en memoria de lo ya consultado. Nada
de una petición por pulsación.

El clic en el mapa **sigue existiendo** y los marcadores son arrastrables, pero
como complemento del buscador, no como única vía.

Todo esto entra por `geocoder.js`, que expone una sola función. Cambiar de
proveedor, o añadir CartoCiudad para afinar el portal al elegir un resultado, es
tocar ese fichero y nada más — el mismo criterio de puertos de §3, aplicado en el
cliente.

**Cuándo dejaría de valer llamarlo desde el navegador**: con varios usuarios, cada
pestaña tiene su propio retardo y su propia caché, y el ritmo agregado contra
Photon no lo limita nadie. Es el mismo razonamiento que el `lru_cache` de §3 con
el limitador de OSRM. Si se llega ahí, el arreglo es un `GET /api/buscar` que haga
de proxy con caché compartida, y entonces sí habría que revisar §6.2. Hoy, con un
usuario, sería complicarse por adelantado.

### 13.3. Mapa, espera y fallos

**El mapa pinta cuatro capas**: la polilínea de la ruta directa; las candidatas
como círculos pequeños coloreados por precio efectivo, en gris las no vigentes
(§4.2 pide marcar lo caducado, no ocultarlo); las **opciones** (§8.6) como anillo,
sin número —no son un orden que seguir sino alternativas entre las que elegir—; y
las paradas del plan como marcador numerado, con popup de litros, €/L efectivo,
coste y nivel de llegada y salida.

**La escala de precio es secuencial de un solo tono** (azul claro → oscuro por
cuantiles), no el verde-rojo que pide el instinto: el precio es una magnitud, no
una polaridad, y verde-rojo es justo el par que peor distingue un daltónico. El
paso más claro está elegido para seguir viéndose sobre el color de los tiles de
OSM, que es más oscuro que un fondo blanco. **El precio caducado no se distingue
solo por el color**: va además sin relleno y con el borde discontinuo.

**El plan** lista las paradas con su kilómetro y enseña **una sola cifra de
dinero**: los euros de combustible. Debajo va la lista de opciones, en el orden en
que el conductor se las cruza, cada una con la hora aproximada, el desvío en km y
minutos, y el sobrecoste en euros. Pinchar una la enseña en el mapa: elegir dónde
parar es a lo que se ha venido, así que tiene que costar un clic.

El tiempo aparece **solo en minutos** y como exceso sobre la ruta directa — §8.2
es explícita en que ni el total del viaje ni un "coste de tiempo" en euros
significan nada para el usuario.

**En los ajustes hay un mando nuevo y falta el de antes**: entra "Desvío máximo"
en kilómetros (§8.2) y desaparece "Valor del tiempo €/h", que era la cara visible
de la decisión que el paso 6 revirtió.

**La espera es parte del diseño, no un spinner mudo.** Una petición va de 1,5 s a
casi medio minuto según `max_candidatas` (§8.5), así que hay barra de progreso y
un texto que dice que se está consultando OSRM. El mando de `max_candidatas` va
en los ajustes plegados junto a los de §8.2, con su advertencia de tiempo al
lado.

**Cada fila de la tabla de §6.2 tiene su pantalla**, que es lo que evita que la
honestidad del backend se pierda en el último metro:

- `bajo_reserva` no se pinta como error, sino como respuesta útil: las
  gasolineras más cercanas al origen, en el mapa y en la lista.
- `trayecto_inviable` nombra el hueco entre `desde` y `hasta` con sus cifras:
  cuántos km hay, cuántos da el depósito, cuántos faltan y cuántas candidatas
  quedaron a tiro. **En el panel, no en el mapa**: `desde` y `hasta` son
  etiquetas ("Origen", el rótulo de una estación), no coordenadas, así que hoy
  no hay con qué dibujar el tramo. Resaltarlo exigiría que la API devolviera
  también las coordenadas de los dos extremos del hueco; es barato, pero es un
  cambio en el backend y no entra en la v1.
- "Ninguna candidata con precio vigente" ofrece un botón que reintenta con el
  corredor ensanchado, en vez de dejar al usuario adivinando.
- `avisos` (la degradación explícita de §8.4) va en una banda sobre el plan: el
  plan es válido, pero se dice qué se quedó fuera.
