/**
 * Orquestación: estado de la pantalla y pegamento entre mapa, panel y API.
 *
 * Stateless de servidor (§3): lo único que se recuerda entre visitas es el
 * vehículo, y lo recuerda el navegador.
 */

import * as api from "./api.js";
import * as panel from "./panel.js";
import { crearMapa } from "./mapa.js";

const CLAVE_GUARDADO = "repostaje-fino:formulario";
const MARGEN_MAXIMO_KM = 50;

const estado = {
  origen: null,
  destino: null,
  armado: "origen",
  ultimaPeticion: null,
  enCurso: null,
};

const mapa = crearMapa("mapa", { alClicar: fijarDesdeMapa });

const campos = {
  origen: panel.crearCampoBusqueda(document.querySelector('[data-punto="origen"]'), {
    alElegir: (punto) => fijarPunto("origen", punto),
    sesgo: centroDelMapa,
  }),
  destino: panel.crearCampoBusqueda(document.querySelector('[data-punto="destino"]'), {
    alElegir: (punto) => fijarPunto("destino", punto),
    sesgo: centroDelMapa,
  }),
};

arrancar();

async function arrancar() {
  try {
    const [combustibles, marcas] = await Promise.all([api.combustibles(), api.marcas()]);
    panel.rellenarCombustibles(combustibles);
    panel.rellenarMarcas(marcas);
    const porDefecto = combustibles.find((c) => c.por_defecto);
    if (porDefecto) document.querySelector("#combustible").value = porDefecto.codigo;
  } catch (error) {
    document.querySelector("#error-formulario").textContent =
      `No se ha podido cargar el catálogo: ${error.message}`;
    document.querySelector("#error-formulario").hidden = false;
  }
  // Después del catálogo: lo guardado debe ganar al valor por defecto.
  panel.escribirFormulario(leerGuardado());
  actualizarArmado();
}

// ---------------------------------------------------------------------------
// Origen y destino
// ---------------------------------------------------------------------------

function centroDelMapa() {
  const centro = mapa.mapa.getCenter();
  return { lat: centro.lat, lon: centro.lng };
}

function fijarPunto(cual, punto) {
  estado[cual] = punto;
  campos[cual].fijarTexto(punto.etiqueta);
  mapa.fijarExtremo(cual, punto, alArrastrar);
  document.querySelector(`[data-coordenada="${cual}"]`).textContent = coordenadas(punto);
  if (estado.origen && estado.destino) mapa.encajarExtremos();
  estado.armado = cual === "origen" && !estado.destino ? "destino" : estado.armado;
  actualizarArmado();
}

function fijarDesdeMapa(lat, lon) {
  fijarPunto(estado.armado, { lat, lon, etiqueta: coordenadas({ lat, lon }) });
}

function alArrastrar(cual, lat, lon) {
  fijarPunto(cual, { lat, lon, etiqueta: coordenadas({ lat, lon }) });
}

function coordenadas({ lat, lon }) {
  return `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
}

function actualizarArmado() {
  document.querySelector("#punto-armado").textContent =
    estado.armado === "origen" ? "origen" : "destino";
  for (const boton of document.querySelectorAll(".fijar-en-mapa")) {
    boton.setAttribute("aria-pressed", String(boton.dataset.fijar === estado.armado));
  }
}

document.querySelector("#panel").addEventListener("click", (evento) => {
  // Elegir dónde parar es lo que el usuario ha venido a hacer: que pinchar una
  // opción de la lista la enseñe en el mapa (§8.6).
  const opcion = evento.target.closest(".opcion");
  if (opcion) {
    mapa.enfocarOpcion(Number(opcion.dataset.estacion));
    return;
  }

  const boton = evento.target.closest("button");
  if (!boton) return;

  if (boton.classList.contains("fijar-en-mapa")) {
    estado.armado = boton.dataset.fijar;
    actualizarArmado();
  } else if (boton.id === "volver") {
    panel.mostrarVista("formulario");
  } else if (boton.id === "reintentar") {
    if (estado.ultimaPeticion) enviar(estado.ultimaPeticion);
  } else if (boton.id === "ensanchar") {
    const peticion = {
      ...estado.ultimaPeticion,
      margen_corredor_km: margenEnsanchado(estado.ultimaPeticion.margen_corredor_km),
    };
    enviar(peticion);
  }
});

/** Ensanchar es duplicar, con suelo en 10 km y el techo que acepta la API. */
function margenEnsanchado(actual) {
  return Math.min(MARGEN_MAXIMO_KM, Math.max(10, (actual || 5) * 2));
}

// ---------------------------------------------------------------------------
// Cálculo
// ---------------------------------------------------------------------------

document.querySelector("#formulario").addEventListener("submit", (evento) => {
  evento.preventDefault();
  const error = document.querySelector("#error-formulario");
  error.hidden = true;

  if (!estado.origen || !estado.destino) {
    // Lo único que se valida en el cliente: el resto de reglas son del dominio
    // y las contesta el backend con su propio mensaje (§3).
    error.textContent = "Marca un origen y un destino: escríbelos o haz clic en el mapa.";
    error.hidden = false;
    return;
  }

  const formulario = panel.leerFormulario();
  guardar(formulario);
  enviar({
    origen: { lat: estado.origen.lat, lon: estado.origen.lon },
    destino: { lat: estado.destino.lat, lon: estado.destino.lon },
    ...formulario,
  });
});

async function enviar(peticion) {
  estado.ultimaPeticion = peticion;
  estado.enCurso?.abort();
  estado.enCurso = new AbortController();

  document.querySelector("#detalle-espera").textContent =
    `Consultando OSRM con hasta ${peticion.max_candidatas} candidatas. ` +
    `El servidor público va a 1 petición por segundo, así que cuantas más, más tarda.`;
  panel.mostrarVista("espera");

  try {
    const datos = await api.rutaOptima(peticion, estado.enCurso.signal);
    mapa.pintarPlan(datos);
    panel.pintarResultado(datos);
    panel.mostrarVista("resultado");
  } catch (error) {
    if (error.name === "AbortError") return;
    if (error.tipo === "bajo_reserva") {
      mapa.pintarCercanas(error.datos.estaciones_cercanas ?? []);
    } else {
      mapa.limpiarResultado();
    }
    panel.pintarError(error, { margenSugerido: margenEnsanchado(peticion.margen_corredor_km) });
    panel.mostrarVista("error");
  } finally {
    estado.enCurso = null;
  }
}

// ---------------------------------------------------------------------------
// Lo que recuerda el navegador (§3: el servidor no guarda nada)
// ---------------------------------------------------------------------------

function guardar(formulario) {
  const { vehiculo, ...ajustes } = formulario;
  delete ajustes.rotulos;
  try {
    localStorage.setItem(CLAVE_GUARDADO, JSON.stringify({ vehiculo, ajustes }));
  } catch {
    // Modo privado o cuota llena: no guardar no rompe nada.
  }
}

function leerGuardado() {
  try {
    return JSON.parse(localStorage.getItem(CLAVE_GUARDADO) ?? "null");
  } catch {
    return null;
  }
}
