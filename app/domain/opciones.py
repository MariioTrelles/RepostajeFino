"""Las gasolineras entre las que el conductor elige (ARQUITECTURA.md §8.6).

El plan óptimo contesta "dónde repostar más barato". Es la respuesta correcta a
una pregunta que casi nadie se hace así. La de verdad es: *"salgo a las ocho,
¿paro a las nueve, a las diez o a las once?"*, y para contestarla hace falta
ofrecer varias y decir de cada una **cuánto cuesta esa comodidad**.

Dos reglas:

1. **Repartidas en el tiempo, no las N más baratas.** Las N más baratas pueden
   estar todas en el mismo tramo de cien kilómetros y no le sirven a quien
   quiere parar antes o después. La ruta se trocea en ventanas de conducción y
   de cada una sale la mejor. Es el mismo argumento que ya lleva el cupo por
   tramos de ``seleccion_candidatas`` (§8.5), aplicado al tiempo en vez de al
   espacio.
2. **Cada opción lleva su precio en euros de verdad**: ``sobrecoste_eur`` son
   euros de combustible del viaje entero, no una puntuación inventada. Que el
   número sea comprobable a mano es lo que hace que el usuario pueda decidir.

Este módulo vive en ``domain/`` por lo mismo que ``seleccion_candidatas``:
decide *qué* se le ofrece al usuario, que es negocio, y solo habla con objetos
del dominio.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from typing import NamedTuple

from app.domain.dp_optimizer import (
    ParametrosOptimizacion,
    TrayectoInviable,
    optimizar_repostaje,
    sobrecoste_por_candidata,
)
from app.domain.models import (
    Desvio,
    EstacionCandidata,
    Opcion,
    Parada,
    Recomendacion,
    Vehiculo,
)
from app.domain.precio_efectivo import PerfilDescuento

# Una ventana de tres cuartos de hora es lo que da el reparto que se pedía: unas
# 3 opciones en un viaje de 2 h y unas 7 en uno de 5 h. Constante de ajuste, no
# decisión de diseño: subirla da menos opciones y más separadas.
VENTANA_S = 45 * 60.0
MIN_OPCIONES = 3
MAX_OPCIONES = 8


class Candidatura(NamedTuple):
    """Una estación en la carrera por ser una de las opciones que se ofrecen.

    Lo mínimo para repartir: dónde cae en el viaje y cómo de cara sale. El plan
    completo se calcula solo para las que ganan, que son un puñado.

    ``referencia_eur`` **no es el sobrecoste que se le enseña al usuario**: es la
    cota barata que sale de las tablas del DP, y sirve solo para ordenar dentro de
    cada ventana. El número que se publica sale del plan que de verdad se propone
    (``Opcion.sobrecoste_eur``), para que el usuario pueda comprobarlo restando
    dos cifras que también ve.
    """

    indice: int  # base 1, el convenio de índices de las matrices del DP
    tiempo_desde_origen_s: float
    referencia_eur: Decimal


def numero_de_ventanas(duracion_directa_s: float) -> int:
    """Cuántas paradas posibles tiene sentido ofrecer en un viaje de esta duración."""
    if duracion_directa_s <= 0:
        return MIN_OPCIONES
    return max(MIN_OPCIONES, min(MAX_OPCIONES, round(duracion_directa_s / VENTANA_S)))


def repartir_por_ventanas(
    candidaturas: Sequence[Candidatura],
    duracion_directa_s: float,
    obligatorias: Sequence[int] = (),
) -> list[int]:
    """Índices elegidos: la mejor de cada ventana, más las paradas del plan óptimo.

    Las ventanas vacías se saltan sin más: en un tramo de autovía sin gasolineras
    no hay nada que ofrecer, y rellenar el hueco con una de otra ventana rompería
    justo lo que se quiere conseguir.

    Las del plan óptimo entran siempre aunque compartan ventana con otra mejor
    repartida: son las que responden "¿y si no quiero pensar?".
    """
    if not candidaturas:
        return []

    n_ventanas = numero_de_ventanas(duracion_directa_s)
    if len(candidaturas) <= n_ventanas:
        # Hay menos sitios donde parar que huecos que llenar: se ofrecen todos.
        # Repartir aquí solo serviría para esconderle al usuario alternativas que
        # existen y que caben de sobra en la pantalla.
        return [c.indice for c in sorted(candidaturas, key=lambda c: c.tiempo_desde_origen_s)]

    # Las ventanas cubren hasta donde de verdad se puede llegar, no hasta el
    # destino: si el depósito se acaba a las dos horas, ofrecer un hueco para las
    # cuatro sería ofrecer una decisión que no existe.
    tope = max(c.tiempo_desde_origen_s for c in candidaturas) or 1.0
    ancho = tope / n_ventanas

    mejor_por_ventana: dict[int, Candidatura] = {}
    for candidatura in candidaturas:
        ventana = min(int(candidatura.tiempo_desde_origen_s / ancho), n_ventanas - 1)
        actual = mejor_por_ventana.get(ventana)
        clave = (candidatura.referencia_eur, candidatura.tiempo_desde_origen_s)
        if actual is None or clave < (actual.referencia_eur, actual.tiempo_desde_origen_s):
            mejor_por_ventana[ventana] = candidatura

    elegidas = {c.indice for c in mejor_por_ventana.values()} | set(obligatorias)
    por_indice = {c.indice: c for c in candidaturas}
    return sorted(
        (i for i in elegidas if i in por_indice),
        key=lambda i: por_indice[i].tiempo_desde_origen_s,
    )


def calcular(
    vehiculo: Vehiculo,
    candidatas: Sequence[EstacionCandidata],
    desvios: Sequence[Desvio],
    distancias_km: Sequence[Sequence[float]],
    duraciones_s: Sequence[Sequence[float]] | None,
    optima: Recomendacion,
    parametros: ParametrosOptimizacion | None = None,
    usuario: PerfilDescuento | None = None,
) -> list[Opcion]:
    """Las opciones que se le ofrecen al conductor, en el orden en que se las cruza.

    ``optima`` es el plan sin restricciones, ya calculado: sirve de referencia
    para el sobrecoste y aporta sus propias paradas a la lista.

    El coste computacional está donde debe: el sobrecoste de **todas** las
    candidatas sale de dos pasadas del DP, y el plan completo solo se calcula
    para el puñado que se va a enseñar.
    """
    parametros = parametros or ParametrosOptimizacion()
    if not candidatas:
        return []
    if not optima.paradas:
        # Se llega sin repostar. No hay decisión que ofrecer, y comparar contra un
        # plan que no compra combustible daría sobrecostes que no significan nada.
        return []

    sobrecostes = sobrecoste_por_candidata(
        vehiculo, candidatas, distancias_km, duraciones_s, parametros, usuario
    )

    def avance_s(indice: int) -> float:
        if duraciones_s is None:
            return distancias_km[0][indice]
        return duraciones_s[0][indice]

    candidaturas = [
        Candidatura(indice=i + 1, tiempo_desde_origen_s=avance_s(i + 1), referencia_eur=coste)
        for i, coste in enumerate(sobrecostes)
        if coste is not None
    ]
    if not candidaturas:
        return []

    destino = len(candidatas) + 1
    duracion_directa_s = duraciones_s[0][destino] if duraciones_s is not None else 0.0

    # Solo la *primera* parada del plan óptimo puede ser una opción de sobrecoste
    # cero: las siguientes son consecuencia de haber parado antes donde se paró, y
    # forzarlas como próxima parada daría otro plan, peor.
    de_la_optima = _primera_parada_de(optima, candidatas)
    obligatorias = [de_la_optima] if de_la_optima is not None else []
    elegidas = repartir_por_ventanas(candidaturas, duracion_directa_s, obligatorias)

    por_indice = {c.indice: c for c in candidaturas}
    opciones: list[Opcion] = []
    for indice in elegidas:
        plan = _plan_parando_en(
            indice, vehiculo, candidatas, distancias_km, duraciones_s, parametros, usuario
        )
        if plan is None:
            continue
        parada = _parada_en(plan, candidatas[indice - 1])
        if parada is None:
            continue  # el plan no reposta ahí: no es una opción que ofrecer
        opciones.append(
            Opcion(
                candidata=candidatas[indice - 1],
                desvio=desvios[indice - 1],
                km_desde_origen=distancias_km[0][indice],
                tiempo_desde_origen_s=por_indice[indice].tiempo_desde_origen_s,
                litros=parada.litros,
                precio_efectivo_milesimas=parada.precio_efectivo_milesimas,
                plan=plan,
                sobrecoste_eur=max(
                    Decimal(0),
                    plan.coste_combustible_eur - optima.coste_combustible_eur,
                ),
            )
        )

    return _marcar_la_mas_barata(opciones)


def _marcar_la_mas_barata(opciones: list[Opcion]) -> list[Opcion]:
    """Señala la mejor **de las ofrecidas**, que no siempre es la del plan óptimo.

    El plan óptimo puede repartir el repostaje en dos paradas y salir más barato
    que cualquier parada única; entonces ninguna opción tiene sobrecoste cero.
    Marcar "la del plan óptimo" sería marcar algo que el usuario no puede elegir
    tal cual, así que se marca la más barata de las que sí puede.
    """
    if not opciones:
        return opciones
    mejor = min(opciones, key=lambda o: o.sobrecoste_eur)
    return [replace(opcion, es_la_mas_barata=opcion is mejor) for opcion in opciones]


def _plan_parando_en(
    indice: int,
    vehiculo: Vehiculo,
    candidatas: Sequence[EstacionCandidata],
    distancias_km: Sequence[Sequence[float]],
    duraciones_s: Sequence[Sequence[float]] | None,
    parametros: ParametrosOptimizacion,
    usuario: PerfilDescuento | None,
) -> Recomendacion | None:
    """El plan que sale de decir "mi próxima parada es esta".

    Se intenta primero **sin más paradas**: repostar ahí lo que haga falta y
    llegar. Eso es lo que quiere decir el conductor al elegir dónde parar, y es lo
    que evita la respuesta absurda de echar un litro en una estación cara porque
    hay otra más barata cien kilómetros después.

    Si el depósito no da para llegar de una sentada, se repite dejando repostar
    después. El plan resultante enseñará esa segunda parada, que es justo lo que
    el usuario necesita saber antes de elegir.
    """
    for parada_unica in (True, False):
        try:
            return optimizar_repostaje(
                vehiculo,
                candidatas,
                distancias_km,
                duraciones_s,
                parametros,
                usuario,
                primera_parada_en=indice,
                parada_unica=parada_unica,
            )
        except TrayectoInviable:
            continue
    return None


def _primera_parada_de(plan: Recomendacion, candidatas: Sequence[EstacionCandidata]) -> int | None:
    """Índice (base 1) de la primera estación donde reposta ``plan``."""
    posicion = {c.estacion.id: i + 1 for i, c in enumerate(candidatas)}
    for parada in plan.paradas:
        if parada.estacion.id in posicion:
            return posicion[parada.estacion.id]
    return None


def _parada_en(plan: Recomendacion, candidata: EstacionCandidata) -> Parada | None:
    for parada in plan.paradas:
        if parada.estacion.id == candidata.estacion.id:
            return parada
    return None
