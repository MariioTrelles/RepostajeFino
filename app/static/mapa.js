/**
 * Capa Leaflet (ARQUITECTURA.md §13.3).
 *
 * Cuatro capas: la polilínea de la ruta directa, las candidatas, las opciones
 * donde se puede elegir parar y las paradas del plan.
 * Este módulo no decide nada del plan; solo pinta lo que la API ya calculó.
 */

import { antiguedad, duracion, escapar, euros, eurosLitro, km, litros } from "./formato.js";

// Rampa secuencial de un solo tono, clara -> oscura, para el precio efectivo.
// Nada de verde-rojo: el precio es una magnitud, no una polaridad, y además ese
// es justo el par que peor distingue un daltónico. Los pasos están validados
// para que el más claro siga viéndose sobre el color de los tiles de OSM.
const RAMPA_PRECIO = ["#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"];
const COLOR_CADUCADO = "#898781";
const ANILLO = "#fcfcfb";

const CENTRO_ESPAÑA = [40.2, -3.6];

export function crearMapa(idNodo, { alClicar } = {}) {
  const mapa = L.map(idNodo, { zoomControl: true }).setView(CENTRO_ESPAÑA, 6);

  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(mapa);

  // Los iconos por defecto se buscan a partir del CSS; con Leaflet vendorizado
  // conviene decirlo explícitamente en vez de fiarlo a la detección.
  L.Icon.Default.imagePath = "/vendor/leaflet/images/";

  const capaRuta = L.layerGroup().addTo(mapa);
  const capaCandidatas = L.layerGroup().addTo(mapa);
  const capaOpciones = L.layerGroup().addTo(mapa);
  const capaParadas = L.layerGroup().addTo(mapa);
  // Para que pinchar una opción en el panel abra la del mapa (§8.6).
  const opcionesPorEstacion = new Map();
  const extremos = { origen: null, destino: null };

  if (alClicar) {
    mapa.on("click", (evento) => alClicar(evento.latlng.lat, evento.latlng.lng));
  }

  function fijarExtremo(cual, punto, alMover) {
    const posicion = [punto.lat, punto.lon];
    if (extremos[cual]) {
      extremos[cual].setLatLng(posicion);
    } else {
      extremos[cual] = L.marker(posicion, {
        draggable: true,
        icon: iconoExtremo(cual),
        zIndexOffset: 1000,
      }).addTo(mapa);
      extremos[cual].on("dragend", (evento) => {
        const { lat, lng } = evento.target.getLatLng();
        alMover?.(cual, lat, lng);
      });
    }
    extremos[cual].bindTooltip(escapar(punto.etiqueta ?? cual), { direction: "top" });
    if (!extremos.origen || !extremos.destino) mapa.setView(posicion, 12);
  }

  function encajarExtremos() {
    const puestos = [extremos.origen, extremos.destino].filter(Boolean);
    if (puestos.length === 2) {
      mapa.fitBounds(L.latLngBounds(puestos.map((m) => m.getLatLng())), { padding: [60, 60] });
    }
  }

  function limpiarResultado() {
    capaRuta.clearLayers();
    capaCandidatas.clearLayers();
    capaOpciones.clearLayers();
    capaParadas.clearLayers();
    opcionesPorEstacion.clear();
  }

  function pintarPlan(datos) {
    limpiarResultado();

    const trazado = L.polyline(datos.geometria, {
      color: "#0b0b0b",
      weight: 4,
      opacity: 0.55,
    }).addTo(capaRuta);

    const cortes = cortesDePrecio(datos.candidatas);
    for (const candidata of datos.candidatas) {
      marcadorCandidata(candidata, cortes).addTo(capaCandidatas);
    }
    // Las opciones van por encima de las candidatas y por debajo de las paradas:
    // son más que una gasolinera cualquiera y menos que el plan recomendado.
    for (const opcion of datos.opciones ?? []) {
      const marcador = marcadorOpcion(opcion).addTo(capaOpciones);
      opcionesPorEstacion.set(opcion.estacion.id, marcador);
    }
    datos.paradas.forEach((parada, indice) => {
      marcadorParada(parada, indice + 1).addTo(capaParadas);
    });

    mapa.fitBounds(trazado.getBounds(), { padding: [40, 40] });
  }

  function pintarCercanas(cercanas) {
    limpiarResultado();
    const puntos = [];
    for (const cercana of cercanas) {
      const { estacion, precio, distancia_km } = cercana;
      puntos.push([estacion.lat, estacion.lon]);
      L.circleMarker([estacion.lat, estacion.lon], estiloCandidata(precio.vigente, RAMPA_PRECIO[1]))
        .bindPopup(
          `<strong>${escapar(estacion.rotulo)}</strong><br>${escapar(estacion.direccion ?? "")}` +
            `<br>${eurosLitro(precio.eur_litro)} · a ${km(distancia_km)} del origen`,
        )
        .addTo(capaCandidatas);
    }
    if (puntos.length > 0) {
      mapa.fitBounds(L.latLngBounds(puntos), { padding: [60, 60], maxZoom: 13 });
    }
  }

  /** Enseña en el mapa la opción que se acaba de pinchar en el panel. */
  function enfocarOpcion(estacionId) {
    const marcador = opcionesPorEstacion.get(estacionId);
    if (!marcador) return;
    mapa.setView(marcador.getLatLng(), Math.max(mapa.getZoom(), 11));
    marcador.openPopup();
  }

  return {
    mapa,
    fijarExtremo,
    encajarExtremos,
    limpiarResultado,
    pintarPlan,
    pintarCercanas,
    enfocarOpcion,
  };
}

/** Los cinco tramos de la rampa, por cuantiles: informativa sea cual sea la horquilla. */
function cortesDePrecio(candidatas) {
  const precios = candidatas
    .filter((c) => c.precio.vigente)
    .map((c) => c.precio_efectivo_eur_litro)
    .sort((a, b) => a - b);
  if (precios.length === 0) return [];
  return [0.2, 0.4, 0.6, 0.8].map((q) => precios[Math.floor(q * (precios.length - 1))]);
}

function colorDePrecio(precio, cortes) {
  let paso = 0;
  while (paso < cortes.length && precio > cortes[paso]) paso += 1;
  return RAMPA_PRECIO[paso];
}

function marcadorCandidata(candidata, cortes) {
  const { estacion, precio } = candidata;
  const color = precio.vigente
    ? colorDePrecio(candidata.precio_efectivo_eur_litro, cortes)
    : COLOR_CADUCADO;

  const aviso = precio.vigente
    ? ""
    : `<br><em class="caducado">Sin actualizar ${antiguedad(precio.antiguedad_horas)}</em>`;

  return L.circleMarker(
    [estacion.lat, estacion.lon],
    estiloCandidata(precio.vigente, color),
  ).bindPopup(
    `<strong>${escapar(estacion.rotulo)}</strong><br>` +
      `${escapar(estacion.direccion ?? "")}${estacion.municipio ? `<br>${escapar(estacion.municipio)}` : ""}` +
      `<br><strong>${eurosLitro(candidata.precio_efectivo_eur_litro)}</strong>${aviso}`,
  );
}

/**
 * El precio caducado no se distingue solo por el color: va también sin relleno
 * y con el borde discontinuo. Color a secas nunca debe cargar con un dato (§4.2
 * ya pide marcarlo de forma explícita, no sutil).
 */
function estiloCandidata(vigente, color) {
  return {
    radius: 6,
    color: vigente ? ANILLO : color,
    weight: 2,
    dashArray: vigente ? null : "3 3",
    fillColor: color,
    fillOpacity: vigente ? 1 : 0.15,
  };
}

/**
 * Una opción no lleva número: no es un orden que haya que seguir, es un sitio
 * entre los que elegir. El anillo la distingue de una candidata cualquiera.
 */
function marcadorOpcion(opcion) {
  const { estacion } = opcion;
  const extra =
    opcion.sobrecoste_eur > 0
      ? `<strong>+${euros(opcion.sobrecoste_eur)}</strong> más que parar en la más barata`
      : "<strong>la parada más barata</strong>";

  return L.marker([estacion.lat, estacion.lon], {
    icon: L.divIcon({
      className: `marcador-opcion${opcion.es_la_mas_barata ? " marcador-opcion-mejor" : ""}`,
      html: "<span></span>",
      iconSize: [22, 22],
      iconAnchor: [11, 11],
    }),
    zIndexOffset: 250,
  }).bindPopup(
    `<strong>${escapar(estacion.rotulo)}</strong><br>` +
      `${escapar(estacion.direccion ?? "")}<br>` +
      `A ${duracion(opcion.tiempo_desde_origen_s)} de salir · km ${Math.round(
        opcion.km_desde_origen,
      )}<br>` +
      `<strong>${litros(opcion.litros)}</strong> a ${eurosLitro(
        opcion.precio_efectivo_eur_litro,
      )}<br>` +
      `Desvío: ${km(opcion.desvio_km)} y ${opcion.desvio_min} min<br>` +
      extra,
  );
}

function marcadorParada(parada, numero) {
  const { estacion } = parada;
  return L.marker([estacion.lat, estacion.lon], {
    icon: L.divIcon({
      className: "marcador-parada",
      html: `<span>${numero}</span>`,
      iconSize: [28, 28],
      iconAnchor: [14, 14],
    }),
    zIndexOffset: 500,
  }).bindPopup(
    `<strong>Parada ${numero} · ${escapar(estacion.rotulo)}</strong><br>` +
      `${escapar(estacion.direccion ?? "")}<br>` +
      `<strong>${litros(parada.litros)}</strong> a ${eurosLitro(parada.precio_efectivo_eur_litro)}<br>` +
      `En el km ${Math.round(parada.km_desde_origen)} · llegas con ${litros(parada.nivel_llegada_l)}, ` +
      `sales con ${litros(parada.nivel_salida_l)}`,
  );
}

function iconoExtremo(cual) {
  return L.divIcon({
    className: `marcador-extremo marcador-${cual}`,
    html: `<span>${cual === "origen" ? "A" : "B"}</span>`,
    iconSize: [30, 30],
    iconAnchor: [15, 15],
  });
}

export const LEYENDA_PRECIO = RAMPA_PRECIO;
export const LEYENDA_CADUCADO = COLOR_CADUCADO;
