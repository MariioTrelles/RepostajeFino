/**
 * Cliente de la API propia.
 *
 * Su único trabajo interesante es el de ARQUITECTURA.md §13.3: traducir cada
 * fila de la tabla de fallos de §6.2 a algo que el panel pueda pintar como una
 * pantalla útil. El backend es honesto con sus errores; aquí no se pierde esa
 * honestidad convirtiéndolos todos en "ha habido un problema".
 */

/** Error normalizado. `tipo` es lo que decide qué pantalla se pinta. */
export class ErrorApi extends Error {
  constructor(tipo, mensaje, datos = null) {
    super(mensaje);
    this.name = "ErrorApi";
    this.tipo = tipo;
    this.datos = datos;
  }
}

export async function rutaOptima(peticion, señal) {
  return await pedir("/api/ruta-optima", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(peticion),
    signal: señal,
  });
}

export async function marcas() {
  return await pedir("/api/marcas");
}

export async function combustibles() {
  return await pedir("/api/combustibles");
}

async function pedir(url, opciones = {}) {
  let respuesta;
  try {
    respuesta = await fetch(url, opciones);
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ErrorApi(
      "sin_red",
      "No se ha podido contactar con el servidor. ¿Sigue levantado uvicorn?",
    );
  }
  if (respuesta.ok) return await respuesta.json();
  throw await traducirFallo(respuesta);
}

async function traducirFallo(respuesta) {
  const detalle = await extraerDetalle(respuesta);

  if (respuesta.status === 503) {
    return new ErrorApi(
      "osrm_caido",
      textoDe(detalle) ||
        "El servicio de rutas no responde ahora mismo. Vuelve a intentarlo.",
    );
  }
  if (respuesta.status === 502) {
    return new ErrorApi(
      "osrm_error",
      textoDe(detalle) || "El servicio de rutas ha respondido con un error.",
    );
  }

  // Los dos fallos con cuerpo tipado: llevan su propio `tipo` y sus números,
  // que es lo que permite pintarlos como respuesta y no como error (§13.3).
  if (detalle && typeof detalle === "object" && !Array.isArray(detalle)) {
    if (detalle.tipo === "bajo_reserva" || detalle.tipo === "trayecto_inviable") {
      return new ErrorApi(detalle.tipo, detalle.detalle, detalle);
    }
  }

  // Validación de Pydantic: `detail` es una lista de errores por campo.
  if (Array.isArray(detalle)) {
    const mensajes = detalle.map((e) => e.msg ?? String(e));
    return new ErrorApi("peticion_invalida", mensajes.join(". "), detalle);
  }

  const texto = textoDe(detalle);
  // El 422 de "no hay candidatas" viaja como cadena, no como cuerpo tipado, así
  // que hay que reconocerlo por el texto. No es tan frágil como parece: las dos
  // palabras están fijadas por tests/api/test_ruta.py. El día que ese error
  // gane su propio `tipo`, esta condición sobra.
  if (/corredor|vigente/i.test(texto)) {
    return new ErrorApi("sin_candidatas", texto);
  }
  return new ErrorApi("peticion_invalida", texto || "La petición no es válida.");
}

async function extraerDetalle(respuesta) {
  try {
    const cuerpo = await respuesta.json();
    return cuerpo?.detail ?? cuerpo;
  } catch {
    return null;
  }
}

function textoDe(detalle) {
  if (typeof detalle === "string") return detalle;
  if (detalle && typeof detalle === "object" && typeof detalle.detalle === "string") {
    return detalle.detalle;
  }
  return "";
}
