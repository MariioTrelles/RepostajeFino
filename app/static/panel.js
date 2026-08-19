/**
 * El panel: formulario, buscador de direcciones y pintado del resultado.
 *
 * Aquí vive la regla de §13.3 que más importa: cada fallo de la tabla de §6.2
 * tiene su propia pantalla. Un `bajo_reserva` no es un error rojo, es una
 * respuesta útil con las gasolineras más cerca del origen.
 */

import { crearBuscador } from "./geocoder.js";
import { LEYENDA_CADUCADO, LEYENDA_PRECIO } from "./mapa.js";
import { antiguedad, escapar, euros, eurosLitro, duracion, km, litros } from "./formato.js";

const $ = (selector) => document.querySelector(selector);

// ---------------------------------------------------------------------------
// Vistas
// ---------------------------------------------------------------------------

const VISTAS = ["formulario", "espera", "resultado", "error"];

export function mostrarVista(nombre) {
  for (const vista of VISTAS) {
    $(`#vista-${vista}`).hidden = vista !== nombre;
  }
}

// ---------------------------------------------------------------------------
// Campo de búsqueda con autocompletado (§13.2)
// ---------------------------------------------------------------------------

export function crearCampoBusqueda(raiz, { alElegir, sesgo }) {
  const entrada = raiz.querySelector("input");
  const lista = raiz.querySelector(".sugerencias");
  const estado = raiz.querySelector(".estado-busqueda");
  let opciones = [];
  let activo = -1;

  const buscador = crearBuscador({
    alEmpezar: () => {
      estado.textContent = "Buscando…";
    },
    alResolver: (resultados) => {
      estado.textContent = "";
      opciones = resultados;
      pintar();
    },
    alFallar: (error) => {
      estado.textContent = error.message || "El buscador no responde.";
      cerrar();
    },
  });

  function pintar() {
    lista.innerHTML = "";
    activo = -1;
    if (opciones.length === 0) {
      cerrar();
      return;
    }
    opciones.forEach((opcion, indice) => {
      const elemento = document.createElement("li");
      elemento.setAttribute("role", "option");
      elemento.id = `${lista.id}-${indice}`;
      elemento.innerHTML =
        `<span class="sugerencia-principal">${escapar(opcion.principal)}</span>` +
        (opcion.secundario
          ? `<span class="sugerencia-secundaria">${escapar(opcion.secundario)}</span>`
          : "");
      // `mousedown` y no `click`: el `blur` del input llega antes que el click
      // y cerraría la lista debajo del cursor.
      elemento.addEventListener("mousedown", (evento) => {
        evento.preventDefault();
        elegir(indice);
      });
      lista.append(elemento);
    });
    lista.hidden = false;
    entrada.setAttribute("aria-expanded", "true");
  }

  function cerrar() {
    lista.hidden = true;
    lista.innerHTML = "";
    opciones = [];
    activo = -1;
    entrada.setAttribute("aria-expanded", "false");
    entrada.removeAttribute("aria-activedescendant");
  }

  function marcar(indice) {
    const elementos = [...lista.children];
    elementos.forEach((el, i) => el.classList.toggle("activa", i === indice));
    activo = indice;
    if (elementos[indice]) {
      entrada.setAttribute("aria-activedescendant", elementos[indice].id);
      elementos[indice].scrollIntoView({ block: "nearest" });
    }
  }

  function elegir(indice) {
    const opcion = opciones[indice];
    if (!opcion) return;
    entrada.value = opcion.etiqueta;
    cerrar();
    alElegir({ lat: opcion.lat, lon: opcion.lon, etiqueta: opcion.etiqueta });
  }

  entrada.addEventListener("input", () => {
    buscador.consultar(entrada.value, sesgo());
  });

  entrada.addEventListener("keydown", (evento) => {
    if (lista.hidden) return;
    if (evento.key === "ArrowDown") {
      evento.preventDefault();
      marcar((activo + 1) % opciones.length);
    } else if (evento.key === "ArrowUp") {
      evento.preventDefault();
      marcar((activo - 1 + opciones.length) % opciones.length);
    } else if (evento.key === "Enter") {
      // Enter con la lista abierta elige; nunca envía el formulario a medias.
      evento.preventDefault();
      elegir(activo >= 0 ? activo : 0);
    } else if (evento.key === "Escape") {
      cerrar();
    }
  });

  entrada.addEventListener("blur", () => setTimeout(cerrar, 120));

  return {
    /** Cuando el punto se fija desde el mapa, el texto lo pone el programa. */
    fijarTexto(texto) {
      buscador.cancelar();
      cerrar();
      entrada.value = texto;
      estado.textContent = "";
    },
  };
}

// ---------------------------------------------------------------------------
// Formulario
// ---------------------------------------------------------------------------

export function rellenarCombustibles(catalogo) {
  const select = $("#combustible");
  select.innerHTML = "";
  for (const { codigo, etiqueta } of catalogo) {
    const opcion = document.createElement("option");
    opcion.value = codigo;
    opcion.textContent = etiqueta;
    select.append(opcion);
  }
}

export function rellenarMarcas(lista) {
  const caja = $("#marcas");
  caja.innerHTML = "";
  for (const marca of lista) {
    const etiqueta = document.createElement("label");
    etiqueta.className = "marca";
    etiqueta.innerHTML = `<input type="checkbox" value="${escapar(marca)}"> ${escapar(marca)}`;
    caja.append(etiqueta);
  }
}

export function leerFormulario() {
  const rotulos = [...document.querySelectorAll("#marcas input:checked")].map((c) => c.value);
  return {
    vehiculo: {
      tipo_combustible: $("#combustible").value,
      consumo_l_100km: numero("#consumo"),
      capacidad_deposito_l: numero("#capacidad"),
      nivel_actual_l: numero("#nivel"),
      reserva_minima_l: numero("#reserva"),
    },
    // Sin marcas marcadas se manda `null`, que es "todas, independientes
    // incluidas" (§6). Una lista vacía sería otra cosa.
    rotulos: rotulos.length > 0 ? rotulos : null,
    tiempo_parada_s: numero("#tiempo-parada"),
    max_desvio_km: numero("#max-desvio"),
    max_candidatas: numero("#max-candidatas"),
    margen_corredor_km: numero("#margen-corredor"),
  };
}

export function escribirFormulario(guardado) {
  if (!guardado) return;
  const { vehiculo = {}, ajustes = {} } = guardado;
  asignar("#combustible", vehiculo.tipo_combustible);
  asignar("#consumo", vehiculo.consumo_l_100km);
  asignar("#capacidad", vehiculo.capacidad_deposito_l);
  asignar("#nivel", vehiculo.nivel_actual_l);
  asignar("#reserva", vehiculo.reserva_minima_l);
  asignar("#max-desvio", ajustes.max_desvio_km);
  asignar("#tiempo-parada", ajustes.tiempo_parada_s);
  asignar("#max-candidatas", ajustes.max_candidatas);
  asignar("#margen-corredor", ajustes.margen_corredor_km);
}

function asignar(selector, valor) {
  if (valor === undefined || valor === null) return;
  const nodo = $(selector);
  if (nodo) nodo.value = valor;
}

const numero = (selector) => Number($(selector).value);

// ---------------------------------------------------------------------------
// Resultado
// ---------------------------------------------------------------------------

export function pintarResultado(datos) {
  const avisos =
    datos.avisos.length > 0
      ? `<p class="aviso aviso-atencion">${datos.avisos.map(escapar).join(" ")}</p>`
      : "";

  $("#vista-resultado").innerHTML = `
    ${avisos}
    <div class="cifra">
      <span class="cifra-valor">${euros(datos.coste_combustible_eur)}</span>
      <span class="cifra-etiqueta">combustible del viaje</span>
    </div>
    <dl class="fila-datos">
      <div><dt>Repostado</dt><dd>${litros(datos.litros_repostados)}</dd></div>
      <div><dt>Paradas</dt><dd>${datos.paradas.length}</dd></div>
      <div><dt>Llegas con</dt><dd>${litros(datos.nivel_llegada_destino_l)}</dd></div>
    </dl>
    <p class="apunte">
      ${km(datos.distancia_total_km)} en ${duracion(datos.duracion_total_s)}.
      Parar a repostar te cuesta <strong>${duracion(datos.desvio_s)}</strong> y
      <strong>${km(datos.desvio_km)}</strong> más que ir de largo
      (${km(datos.distancia_directa_km)}).
    </p>
    ${listaParadas(datos)}
    ${listaOpciones(datos)}
    ${leyenda(datos.candidatas.length)}
    <button type="button" class="secundario" id="volver">Cambiar los datos</button>
  `;
}

/** Las opciones son la respuesta a "¿y si prefiero parar antes?" (§8.6). */
function listaOpciones(datos) {
  const opciones = datos.opciones ?? [];
  if (opciones.length === 0) return "";

  const filas = opciones
    .map((opcion) => {
      const extra =
        opcion.sobrecoste_eur > 0
          ? `<span class="opcion-sobrecoste">+${euros(opcion.sobrecoste_eur)}</span>`
          : `<span class="opcion-sobrecoste opcion-mejor">la más barata</span>`;
      const desvio =
        opcion.desvio_km > 0
          ? `+${km(opcion.desvio_km)} y +${opcion.desvio_min} min de desvío`
          : "sin desvío";
      const otras =
        opcion.paradas.length > 1
          ? ` · y otra parada más adelante`
          : "";
      return `
      <li class="opcion${opcion.es_la_mas_barata ? " opcion-optima" : ""}"
          data-estacion="${opcion.estacion.id}">
        <p class="opcion-cabecera">
          <span class="opcion-hora">${duracion(opcion.tiempo_desde_origen_s)}</span>
          <span class="opcion-rotulo">${escapar(opcion.estacion.rotulo)}</span>
          ${extra}
        </p>
        <p class="parada-lugar">${escapar(
          [opcion.estacion.direccion, opcion.estacion.municipio].filter(Boolean).join(", "),
        )}</p>
        <p class="parada-detalle">
          ${litros(opcion.litros)} a ${eurosLitro(opcion.precio_efectivo_eur_litro)} ·
          km ${Math.round(opcion.km_desde_origen)} · ${desvio}${otras}
        </p>
      </li>`;
    })
    .join("");

  return `
    <h2 class="titulo-seccion">Dónde puedes parar</h2>
    <p class="apunte">
      El sobrecoste es lo que te cuesta de más el viaje entero si repostas ahí en vez
      de en la más barata. El tiempo va en minutos: una hora tuya no vale euros.
    </p>
    <ul class="opciones">${filas}</ul>`;
}

function listaParadas(datos) {
  if (datos.paradas.length === 0) {
    return `<p class="apunte">
      <strong>No hace falta repostar</strong>: con lo que llevas en el depósito llegas
      al destino con ${litros(datos.nivel_llegada_destino_l)}.
    </p>`;
  }

  const filas = datos.paradas
    .map(
      (parada, indice) => `
      <li>
        <span class="numero-parada">${indice + 1}</span>
        <div class="parada-cuerpo">
          <p class="parada-titulo">${escapar(parada.estacion.rotulo)}</p>
          <p class="parada-lugar">${escapar(
            [parada.estacion.direccion, parada.estacion.municipio].filter(Boolean).join(", "),
          )}</p>
          <p class="parada-cifras">
            <strong>${litros(parada.litros)}</strong> a ${eurosLitro(
              parada.precio_efectivo_eur_litro,
            )} = <strong>${euros(parada.coste_eur)}</strong>
          </p>
          <p class="parada-detalle">
            Km ${Math.round(parada.km_desde_origen)} · llegas con
            ${litros(parada.nivel_llegada_l)}, sales con ${litros(parada.nivel_salida_l)}
          </p>
        </div>
      </li>`,
    )
    .join("");

  return `<h2 class="titulo-seccion">Paradas</h2><ol class="paradas">${filas}</ol>`;
}

function leyenda(cuantas) {
  const escalones = LEYENDA_PRECIO.map(
    (color) => `<span class="muestra" style="background:${color}"></span>`,
  ).join("");
  return `
    <h2 class="titulo-seccion">En el mapa</h2>
    <div class="leyenda">
      <div class="leyenda-escala">
        <span>más barata</span>${escalones}<span>más cara</span>
      </div>
      <p class="apunte">
        ${cuantas} estaciones compitieron por entrar en el plan. Las de precio
        caducado salen huecas y en gris
        <span class="muestra muestra-hueca" style="border-color:${LEYENDA_CADUCADO}"></span>,
        con la fecha en su ficha.
      </p>
    </div>`;
}

// ---------------------------------------------------------------------------
// Fallos, uno por fila de la tabla de §6.2
// ---------------------------------------------------------------------------

export function pintarError(error, { margenSugerido = 10 } = {}) {
  const cuerpos = {
    bajo_reserva: bajoReserva,
    trayecto_inviable: trayectoInviable,
    sin_candidatas: sinCandidatas,
  };
  const construir = cuerpos[error.tipo] ?? generico;
  $("#vista-error").innerHTML = construir(error, margenSugerido);
}

function bajoReserva(error) {
  const cercanas = (error.datos.estaciones_cercanas ?? [])
    .map(
      (cercana) => `
      <li>
        <p class="parada-titulo">${escapar(cercana.estacion.rotulo)}</p>
        <p class="parada-lugar">${escapar(
          [cercana.estacion.direccion, cercana.estacion.municipio].filter(Boolean).join(", "),
        )}</p>
        <p class="parada-cifras">
          ${eurosLitro(cercana.precio.eur_litro)} · a ${km(cercana.distancia_km)} del origen
          ${cercana.precio.vigente ? "" : `<em class="caducado">sin actualizar ${antiguedad(cercana.precio.antiguedad_horas)}</em>`}
        </p>
      </li>`,
    )
    .join("");

  return `
    <h2 class="titulo-aviso">Repón antes de salir</h2>
    <p>${escapar(error.message)}</p>
    <h3 class="titulo-seccion">Más cerca del origen</h3>
    <ul class="cercanas">${cercanas}</ul>
    <button type="button" class="secundario" id="volver">Cambiar los datos</button>
  `;
}

function trayectoInviable(error, margenSugerido) {
  const d = error.datos;
  return `
    <h2 class="titulo-aviso">El trayecto no se puede completar</h2>
    <p>Entre <strong>${escapar(d.desde)}</strong> y <strong>${escapar(d.hasta)}</strong>
       hay ${km(d.distancia_km)} y el depósito lleno da para ${km(d.autonomia_km)}:
       faltan <strong>${km(d.faltan_km)}</strong>.</p>
    <p class="apunte">
      De las ${d.candidatas_totales} estaciones candidatas, solo
      ${d.candidatas_alcanzables} quedaban a tiro. Prueba a ensanchar el corredor,
      quitar el filtro de marcas o subir el número de candidatas.
    </p>
    <button type="button" class="principal" id="ensanchar">Reintentar con el corredor a ${margenSugerido} km</button>
    <button type="button" class="secundario" id="volver">Cambiar los datos</button>
  `;
}

function sinCandidatas(error, margenSugerido) {
  return `
    <h2 class="titulo-aviso">Ninguna estación utilizable</h2>
    <p>${escapar(error.message)}</p>
    <button type="button" class="principal" id="ensanchar">Reintentar con el corredor a ${margenSugerido} km</button>
    <button type="button" class="secundario" id="volver">Cambiar los datos</button>
  `;
}

function generico(error) {
  const titulos = {
    osrm_caido: "El servicio de rutas no responde",
    osrm_error: "El servicio de rutas ha fallado",
    sin_red: "Sin conexión con el servidor",
    peticion_invalida: "Revisa los datos",
  };
  return `
    <h2 class="titulo-aviso">${titulos[error.tipo] ?? "Algo ha fallado"}</h2>
    <p>${escapar(error.message)}</p>
    <button type="button" class="principal" id="reintentar">Reintentar</button>
    <button type="button" class="secundario" id="volver">Cambiar los datos</button>
  `;
}
