/**
 * La carcasa: la hoja inferior (tarjeta izquierda en escritorio) y sus pestañas.
 *
 * No sabe nada de rutas ni de gasolineras. Escucha el evento `vista:cambiada`
 * que emite panel.js y decide tres cosas: qué pestañas tocan, qué botón va en
 * el pie y a qué altura se pone la hoja. Además publica `margenes()`, que es lo
 * que el mapa necesita para encajar la ruta en la parte que se ve y no debajo
 * de la carcasa.
 */

const ESCRITORIO = window.matchMedia("(min-width: 900px)");

/** Los tres escalones a los que engancha el arrastre. `auto` no está aquí a
 *  propósito: es una altura de entrada, no un sitio al que se pueda tirar. */
const SNAPS = ["peek", "medio", "alto"];

/**
 * Qué pestañas puede haber en cada momento (ARQUITECTURA.md §13.3).
 *
 * Son candidatas, no una lista fija: solo se monta la pestaña de los paneles
 * que existan en el DOM. Así, cuando no hace falta repostar y no hay
 * alternativas que ofrecer, `panel.js` no pinta `#panel-alternativas` y la
 * pestaña desaparece sola, sin que la carcasa sepa nada del dominio.
 */
const JUEGOS = {
  formulario: [
    ["vehiculo", "Vehículo"],
    ["marcas", "Marcas"],
    ["ajustes", "Ajustes"],
  ],
  // El plan primero: es la respuesta, y de paso la pestaña por defecto.
  resultado: [
    ["plan", "El plan"],
    ["alternativas", "Alternativas"],
  ],
  espera: [],
  error: [],
};

/** Qué botón manda en el pie y a qué altura se pone la hoja al entrar. */
const PIE = {
  formulario: { calcular: true, volver: false, snap: "medio" },
  // La espera y el error traen sus propios botones dentro y miden lo que miden.
  espera: { calcular: false, volver: false, snap: "auto" },
  resultado: { calcular: false, volver: true, snap: "medio" },
  error: { calcular: false, volver: false, snap: "auto" },
};

export function crearHoja() {
  const hoja = document.querySelector("#hoja");
  const barra = document.querySelector("#barra-superior");
  const asa = document.querySelector("#asa");
  const resumen = document.querySelector("#hoja-resumen");
  const pestanas = document.querySelector("#pestanas");
  const cuerpo = document.querySelector("#hoja-cuerpo");
  const pie = document.querySelector("#hoja-pie");
  const calcular = document.querySelector("#calcular");
  const volver = document.querySelector("#volver");

  let juego = [];
  let activa = 0;

  // -------------------------------------------------------------------------
  // Alto de la barra: la hoja de escritorio se cuelga justo debajo, y la barra
  // crece cuando aparece un mensaje de búsqueda o una coordenada.
  // -------------------------------------------------------------------------

  const medirBarra = () => {
    document.documentElement.style.setProperty("--barra-alto", `${barra.offsetHeight}px`);
  };
  new ResizeObserver(medirBarra).observe(barra);
  medirBarra();

  // -------------------------------------------------------------------------
  // Pestañas
  // -------------------------------------------------------------------------

  function configurar(vista) {
    // El resumen es del resultado y de nadie más.
    if (vista !== "resultado") resumen.innerHTML = "";

    // Solo las que tienen panel detrás: una pestaña vacía es peor que ninguna.
    juego = (JUEGOS[vista] ?? []).filter(([id]) => document.querySelector(`#panel-${id}`));
    // Con una sola sección no hay nada que elegir, y la barra sobra.
    if (juego.length < 2) juego = [];
    pestanas.innerHTML = "";
    for (const [id, etiqueta] of juego) {
      const boton = document.createElement("button");
      boton.type = "button";
      boton.className = "pestana";
      boton.id = `pestana-${id}`;
      boton.setAttribute("role", "tab");
      boton.setAttribute("aria-controls", `panel-${id}`);
      boton.dataset.panel = id;
      boton.textContent = etiqueta;
      pestanas.append(boton);
    }
    if (juego.length > 0) elegir(0);

    const modo = PIE[vista] ?? PIE.formulario;
    calcular.hidden = !modo.calcular;
    volver.hidden = !modo.volver;
    medirPeek();
    irA(modo.snap);
  }

  function elegir(indice) {
    activa = Math.max(0, Math.min(indice, juego.length - 1));
    juego.forEach(([id], i) => {
      const seleccionada = i === activa;
      const boton = pestanas.children[i];
      boton.setAttribute("aria-selected", String(seleccionada));
      boton.tabIndex = seleccionada ? 0 : -1;
      const panel = document.querySelector(`#panel-${id}`);
      if (panel) panel.hidden = !seleccionada;
    });
    cuerpo.scrollTop = 0;
  }

  pestanas.addEventListener("click", (evento) => {
    const boton = evento.target.closest(".pestana");
    if (boton) elegir([...pestanas.children].indexOf(boton));
  });

  pestanas.addEventListener("keydown", (evento) => {
    const saltos = { ArrowRight: 1, ArrowLeft: -1 };
    let destino = null;
    if (evento.key in saltos) destino = (activa + saltos[evento.key] + juego.length) % juego.length;
    else if (evento.key === "Home") destino = 0;
    else if (evento.key === "End") destino = juego.length - 1;
    if (destino === null) return;
    evento.preventDefault();
    elegir(destino);
    pestanas.children[destino].focus();
  });

  // -------------------------------------------------------------------------
  // Los tres alturas y el arrastre
  // -------------------------------------------------------------------------

  /**
   * `peek` no es un número fijo: es "todo menos el cuerpo con scroll". Medido y
   * no constante porque el resumen del resultado aparece y desaparece, y lo que
   * tiene que verse asomando es siempre lo mismo: la cifra, las pestañas y el
   * botón. El CSS lo lee en `--alto-peek`.
   */
  function medirPeek() {
    const alto =
      asa.offsetHeight + resumen.offsetHeight + pestanas.offsetHeight + pie.offsetHeight;
    hoja.style.setProperty("--alto-peek", `${alto}px`);
    return alto;
  }

  function alturas() {
    const ventana = window.innerHeight;
    const peek = medirPeek();
    // El tope es la barra, no la pantalla: por detrás de la barra el asa no se
    // podría agarrar y la hoja se quedaría arriba para siempre. Mismo cálculo
    // que `#hoja[data-snap="alto"]` en el CSS.
    const alto = Math.max(peek, ventana - barra.offsetHeight - 28);
    return { peek, medio: Math.max(peek + 60, ventana * 0.52), alto };
  }

  function irA(snap) {
    hoja.style.height = "";
    hoja.dataset.snap = snap;
  }

  let arrastre = null;

  function empezar(evento) {
    // En escritorio la hoja no se arrastra: es una tarjeta fija.
    if (ESCRITORIO.matches || evento.button > 0) return;
    arrastre = {
      y: evento.clientY,
      alto: hoja.getBoundingClientRect().height,
      movido: false,
      t: evento.timeStamp,
    };
    evento.currentTarget.setPointerCapture(evento.pointerId);
  }

  function mover(evento) {
    if (!arrastre) return;
    const delta = arrastre.y - evento.clientY;
    if (!arrastre.movido && Math.abs(delta) < 4) return;
    arrastre.movido = true;
    hoja.dataset.arrastrando = "";
    const { peek, alto } = alturas();
    hoja.style.height = `${Math.min(alto, Math.max(peek, arrastre.alto + delta))}px`;
  }

  function soltar(evento) {
    if (!arrastre) return;
    const { movido, y, t } = arrastre;
    arrastre = null;
    delete hoja.dataset.arrastrando;

    if (!movido) {
      // Un toque en el asa, sin arrastrar: al siguiente escalón, y al llegar
      // arriba vuelve abajo del todo.
      hoja.style.height = "";
      const i = SNAPS.indexOf(hoja.dataset.snap);
      irA(i < 0 ? "medio" : SNAPS[(i + 1) % SNAPS.length]);
      return;
    }

    // Manda dónde ha quedado la hoja; el impulso solo la empuja un escalón más
    // en la dirección del dedo, que es lo que hace que un tirón corto y rápido
    // se sienta como un tirón y no como un ajuste fino.
    const velocidad = (y - evento.clientY) / Math.max(1, evento.timeStamp - t);
    const actual = hoja.getBoundingClientRect().height;
    const escala = alturas();
    hoja.style.height = "";

    const cerca = SNAPS.reduce((mejor, snap) =>
      Math.abs(escala[snap] - actual) < Math.abs(escala[mejor] - actual) ? snap : mejor,
    );
    const empujon = Math.abs(velocidad) > 0.6 ? Math.sign(velocidad) : 0;
    const i = SNAPS.indexOf(cerca) + empujon;
    irA(SNAPS[Math.max(0, Math.min(SNAPS.length - 1, i))]);
  }

  // Se arrastra por el asa y por el resumen, que no tienen nada que pulsar
  // dentro. Las pestañas no: ahí un toque es un toque, no un tirón.
  for (const tirador of [asa, resumen]) {
    tirador.addEventListener("pointerdown", empezar);
    tirador.addEventListener("pointermove", mover);
    tirador.addEventListener("pointerup", soltar);
    tirador.addEventListener("pointercancel", soltar);
  }

  // Al escribir una dirección, la hoja estorba: el teclado ya ocupa media
  // pantalla y las sugerencias necesitan sitio.
  for (const entrada of document.querySelectorAll(".campo-busqueda input")) {
    entrada.addEventListener("focus", () => {
      if (!ESCRITORIO.matches) irA("peek");
    });
  }

  // -------------------------------------------------------------------------
  // Lo que el mapa necesita saber de la carcasa
  // -------------------------------------------------------------------------

  /**
   * Píxeles ocupados por la carcasa en cada borde del mapa. Con la hoja alta el
   * hueco libre sería ridículo, así que cada margen se limita al 40 % de la
   * pantalla: mejor una ruta un poco tapada que un `fitBounds` imposible.
   */
  function margenes() {
    if (ESCRITORIO.matches) {
      return { arriba: 0, abajo: 0, izquierda: hoja.getBoundingClientRect().right, derecha: 0 };
    }
    const tope = window.innerHeight * 0.4;
    // El alto del escalón, no el medido: durante la transición el medido miente.
    // Con `auto` no hay escalón que consultar y sí hay que medir.
    const abajo = alturas()[hoja.dataset.snap] ?? hoja.getBoundingClientRect().height;
    return {
      arriba: Math.min(tope, barra.getBoundingClientRect().bottom),
      abajo: Math.min(tope, abajo),
      izquierda: 0,
      derecha: 0,
    };
  }

  window.addEventListener("resize", medirPeek);
  document.addEventListener("vista:cambiada", (evento) => configurar(evento.detail));
  configurar("formulario");

  return { margenes, irA, elegir };
}
