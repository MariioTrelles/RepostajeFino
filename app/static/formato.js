/** Formateo para presentación. Lo comparten el panel y los popups del mapa. */

const EUROS = new Intl.NumberFormat("es-ES", { style: "currency", currency: "EUR" });
const DECIMAL = new Intl.NumberFormat("es-ES", { maximumFractionDigits: 1 });

export const euros = (valor) => EUROS.format(valor);

/** El precio por litro se mira con tres decimales: la milésima decide (§4). */
export const eurosLitro = (valor) => `${valor.toFixed(3)} €/L`;

export const litros = (valor) => `${DECIMAL.format(valor)} L`;

export const km = (valor) => `${DECIMAL.format(valor)} km`;

export function duracion(segundos) {
  const total = Math.round(segundos / 60);
  const horas = Math.floor(total / 60);
  const minutos = total % 60;
  if (horas === 0) return `${minutos} min`;
  return minutos === 0 ? `${horas} h` : `${horas} h ${minutos} min`;
}

/** "sin actualizar desde hace X" del §4.2, en la unidad que menos ruido mete. */
export function antiguedad(horas) {
  if (horas < 48) return `hace ${Math.round(horas)} h`;
  return `hace ${Math.round(horas / 24)} días`;
}

export function escapar(texto) {
  const nodo = document.createElement("span");
  nodo.textContent = texto ?? "";
  return nodo.innerHTML;
}
