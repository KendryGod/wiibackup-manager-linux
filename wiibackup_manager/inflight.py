"""Coalescing de pedidos idénticos en vuelo.

El problema, que aparece en todas las vistas que cargan datos de red: la
interfaz pide LO MISMO muchas veces al mismo tiempo. Un rescan reconstruye
las 300 filas de la biblioteca y cada una vuelve a pedir su carátula;
abrir la Homebrew Store dispara el ícono de cada tarjeta; la Biblioteca,
Transferir y el panel de detalle piden la metadata del mismo juego. Sin
coordinación, eso son cientos de descargas iguales en paralelo contra el
mismo servidor.

La solución -que gametdb.py y oscwii_client.py habían escrito cuatro veces
entre los dos- es esta: el primer pedido de una clave dispara el trabajo
real, y todos los que llegan mientras ese sigue en curso se cuelgan de él
y reciben el mismo resultado cuando termina. Opcionalmente el resultado
queda recordado, así los pedidos POSTERIORES tampoco rehacen el trabajo.

Este módulo no sabe nada de carátulas, íconos ni catálogos: recibe una
clave y una función que produce un valor. Las reglas de negocio de cada
usuario (qué es una clave, cuándo vale la pena recordar un resultado)
quedan en su módulo, que es donde se pueden leer y probar.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from typing import Any, Callable, Optional


class Claim(Enum):
    """Qué le tocó a quien acaba de anotarse con `InflightRegistry.join`."""

    CLAIMED = "claimed"        # es el primero: le toca correr el trabajo
    WAITING = "waiting"        # ya hay un pedido igual en curso: nada que hacer
    REMEMBERED = "remembered"  # ya había resultado: hay que entregarlo en el acto


def _no_result(_exc: BaseException) -> None:
    """Fallback por defecto: un trabajo que falla se resuelve como None."""
    return None


class InflightRegistry:
    """Pedidos en curso por clave, con su propio pool de hilos.

    Uso normal, todo en una llamada:

        _cover_jobs = InflightRegistry(6, "cover-dl")
        _cover_jobs.submit(key, lambda: bajar(key), on_done)

    `on_done` corre en un hilo del pool (o en el hilo que llama, si el
    resultado ya estaba recordado), así que quien toque widgets de GTK
    adentro tiene que reenviarlo con `GLib.idle_add`.

    Dos garantías que importan y que son la razón de que esto viva en un
    solo lugar:

    - **Los callbacks se llaman SIEMPRE fuera del lock.** Un callback
      puede volver a llamar a `submit` (una fila que se redibuja y vuelve
      a pedir), y hacerlo con el lock tomado sería un deadlock.
    - **Un callback que falla no se lleva puestos a los demás** ni al
      worker del pool: se ignora y se sigue con el resto. Es el caso
      normal de una fila que ya no existe cuando llega su resultado.
    """

    def __init__(self, workers: int, thread_name_prefix: str,
                 *, remember_results: bool = False):
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=thread_name_prefix)
        self._waiting: dict = {}
        # None (y no un dict vacío) cuando no se recuerdan resultados: así
        # "este registry no cachea" y "cachea pero todavía no tiene nada"
        # son estados distintos y no hay que preguntarle a un flag aparte.
        self._results: Optional[dict] = {} if remember_results else None
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- API --
    def submit(self, key, work: Callable[[], Any],
               on_done: Callable[[Any], None], *,
               on_error: Callable[[BaseException], Any] = _no_result,
               remember_when: Optional[Callable[[Any], bool]] = None) -> None:
        """Pide `work()` para `key` y llama a `on_done(resultado)`.

        Si ya hay un resultado recordado para `key`, `on_done` se llama en
        el acto y en el hilo que llama, sin ocupar un worker. Si ya hay un
        pedido igual en curso, `on_done` se cuelga de ese. Si no, `work`
        se encola en el pool.

        `work` corre en un hilo del pool; si levanta cualquier excepción,
        el resultado pasa a ser `on_error(excepcion)` -por defecto None- y
        los callbacks se llaman igual: un fallo no puede dejar a nadie
        esperando para siempre.

        `remember_when(valor)` decide si el resultado se recuerda, y solo
        se consulta en un registry creado con `remember_results=True`."""
        action, remembered = self.join(key, on_done)
        if action is Claim.REMEMBERED:
            on_done(remembered)
            return
        if action is Claim.WAITING:
            return
        self._executor.submit(self.run_now, key, work,
                              on_error=on_error, remember_when=remember_when)

    def join(self, key, on_done: Callable[[Any], None]) -> tuple:
        """Anota `on_done` bajo `key` y dice qué le toca hacer a quien
        llama. Devuelve `(Claim, valor_recordado)`.

        Es la mitad "decidir" de `submit`, separada para que quien tenga
        que hacer algo distinto con cada caso -o para poder probar el
        registro sin lanzar hilos- pueda hacerlo. El valor recordado solo
        viene con `Claim.REMEMBERED`; en los otros dos casos es None.

        Ojo: si devuelve `Claim.CLAIMED`, la clave YA quedó reservada y
        quien llama se compromete a resolverla (`run_now`), pase lo que
        pase. Abandonarla deja a sus callbacks esperando para siempre."""
        with self._lock:
            if self._results is not None and key in self._results:
                return Claim.REMEMBERED, self._results[key]
            waiting = self._waiting.get(key)
            if waiting is not None:
                waiting.append(on_done)
                return Claim.WAITING, None
            self._waiting[key] = [on_done]
            return Claim.CLAIMED, None

    def run_now(self, key, work: Callable[[], Any], *,
                on_error: Callable[[BaseException], Any] = _no_result,
                remember_when: Optional[Callable[[Any], bool]] = None) -> None:
        """Corre `work()`, libera `key` y entrega el resultado a todos los
        que estaban esperándolo. Sincrónico: es lo que `submit` manda al
        pool, expuesto aparte para poder probarlo sin hilos."""
        try:
            value = work()
        except Exception as e:  # noqa: BLE001 - el fallo va como resultado
            value = on_error(e)

        # `remember_when` se evalúa ANTES de tomar el lock, a propósito:
        # puede consultar otro estado protegido por su propio lock (el
        # índice de wiitdb, sin ir más lejos), y llamarlo acá adentro
        # impondría un orden entre los dos locks sin ninguna necesidad.
        remember = self._results is not None and (
            remember_when is None or remember_when(value))

        with self._lock:
            callbacks = self._waiting.pop(key, [])
            if remember:
                self._results[key] = value

        for cb in callbacks:
            try:
                cb(value)
            except Exception:
                # Una fila que ya no existe cuando llega su resultado no
                # puede llevarse puestos a los demás que esperan lo mismo.
                pass

    def in_flight(self) -> int:
        """Cuántas claves distintas se están resolviendo ahora mismo."""
        with self._lock:
            return len(self._waiting)

    def forget(self) -> None:
        """Vacía los pedidos en curso y los resultados recordados.

        Para las pruebas: estos registries son globales al proceso, así
        que sin esto el estado de un test se filtraría al siguiente."""
        with self._lock:
            self._waiting.clear()
            if self._results is not None:
                self._results.clear()
