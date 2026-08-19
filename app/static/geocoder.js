/**
 * Buscador de direcciones (ARQUITECTURA.md §13.2).
 *
 * Photon, llamado directamente desde el navegador: sirve
 * `Access-Control-Allow-Origin: *`, así que no hace falta proxy y el servidor
 * sigue sin geocodificar (§6.2).
 *
 * Toda la higiene de peticiones vive aquí dentro —retardo, cancelación y
 * caché— porque la instancia pública es gratuita y pide buen uso. Cambiar de
 * proveedor, o añadir CartoCiudad para afinar el portal, es tocar este fichero
 * y ninguno más.
 *
 * Tres cosas comprobadas contra el servicio y fáciles de redescubrir a base de
 * golpes:
 *   - `lang=es` NO existe: solo default, de, en, fr. Mandarlo devuelve un 400.
 *   - no hay filtro por país; España se filtra aquí por `countrycode`.
 *   - `properties.name` viene a null en los portales, así que la etiqueta hay
 *     que componerla con `street`, `housenumber` y `city`.
 */

const PHOTON = "https://photon.komoot.io/api/";
const MIN_CARACTERES = 3;
const RETARDO_MS = 300;
const MAX_RESULTADOS = 6;
const MAX_CACHE = 60;

const cache = new Map();

/**
 * Crea un buscador con retardo, cancelación y caché.
 *
 * @param {object} manejadores  alEmpezar / alResolver(resultados) / alFallar(error)
 */
export function crearBuscador({ alEmpezar, alResolver, alFallar }) {
  let temporizador = null;
  let control = null;

  function cancelar() {
    clearTimeout(temporizador);
    temporizador = null;
    control?.abort();
    control = null;
  }

  function consultar(texto, cerca) {
    cancelar();
    const limpio = texto.trim();
    if (limpio.length < MIN_CARACTERES) {
      alResolver([]);
      return;
    }

    const enCache = cache.get(claveCache(limpio, cerca));
    if (enCache) {
      alResolver(enCache);
      return;
    }

    // Una petición por pausa al teclear, nunca una por pulsación.
    temporizador = setTimeout(async () => {
      control = new AbortController();
      alEmpezar?.();
      try {
        const resultados = await buscar(limpio, cerca, control.signal);
        guardarEnCache(claveCache(limpio, cerca), resultados);
        alResolver(resultados);
      } catch (error) {
        if (error.name === "AbortError") return;
        alFallar?.(error);
      } finally {
        control = null;
      }
    }, RETARDO_MS);
  }

  return { consultar, cancelar };
}

/** Una búsqueda suelta, sin retardo ni caché. `cerca` sesga por proximidad. */
export async function buscar(texto, cerca, señal) {
  const parametros = new URLSearchParams({
    q: texto,
    limit: String(MAX_RESULTADOS * 3), // se filtra por país después
  });
  if (cerca) {
    parametros.set("lat", cerca.lat.toFixed(3));
    parametros.set("lon", cerca.lon.toFixed(3));
  }

  const respuesta = await fetch(`${PHOTON}?${parametros}`, { signal: señal });
  if (!respuesta.ok) {
    throw new Error(`El buscador ha respondido ${respuesta.status}.`);
  }
  const cuerpo = await respuesta.json();
  return normalizar(cuerpo.features ?? []);
}

function normalizar(features) {
  const vistos = new Set();
  const resultados = [];

  for (const rasgo of features) {
    const p = rasgo.properties ?? {};
    if (p.countrycode !== "ES") continue;

    const [lon, lat] = rasgo.geometry?.coordinates ?? [];
    if (typeof lat !== "number" || typeof lon !== "number") continue;

    const { principal, secundario } = etiquetar(p);
    const clave = `${principal}|${secundario}`;
    if (vistos.has(clave)) continue;
    vistos.add(clave);

    resultados.push({ principal, secundario, etiqueta: etiquetaPlana(principal, secundario), lat, lon });
    if (resultados.length >= MAX_RESULTADOS) break;
  }
  return resultados;
}

function etiquetar(p) {
  const via = [p.street, p.housenumber].filter(Boolean).join(" ");
  const principal = p.name || via || p.city || p.county || p.state || "Sin nombre";

  const partes = [];
  if (via && via !== principal) partes.push(via);
  for (const campo of [p.postcode, p.city, p.county, p.state]) {
    if (campo && campo !== principal && !partes.includes(campo)) partes.push(campo);
  }
  return { principal, secundario: partes.join(" · ") };
}

function etiquetaPlana(principal, secundario) {
  return secundario ? `${principal}, ${secundario}` : principal;
}

function claveCache(texto, cerca) {
  const sesgo = cerca ? `${cerca.lat.toFixed(1)},${cerca.lon.toFixed(1)}` : "-";
  return `${texto.toLowerCase()}@${sesgo}`;
}

function guardarEnCache(clave, resultados) {
  if (cache.size >= MAX_CACHE) cache.delete(cache.keys().next().value);
  cache.set(clave, resultados);
}
