"""Coordinación central de las operaciones largas de la app.

Antes cada acción (escanear, importar, convertir, verificar, transferir,
eliminar) lanzaba su propio hilo sin que nadie supiera qué más estaba
corriendo. Eso dejaba pasar combinaciones que corrompen datos o muestran
información falsa:

- eliminar o renombrar un juego mientras `wit` lo está convirtiendo;
- dos escaneos en paralelo, donde el que termina último pisa al otro y la
  lista queda mostrando el resultado más viejo;
- escanear mientras se copia/convierte dentro de la biblioteca, con lo que
  el escaneo llega a ver archivos a medio escribir y los identifica mal.

Este módulo es a propósito independiente de GTK: no importa nada de la
interfaz, se puede probar sin levantar una ventana, y la interfaz se
entera de los cambios registrando un listener.

No es un candado global: dos operaciones que no se pisan (verificar un
juego mientras se transfiere otro) siguen pudiendo correr juntas, y las
descargas de carátulas ni pasan por acá. Lo que se bloquea son las
combinaciones peligrosas, definidas en `_find_conflict`.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional


class OperationKind(Enum):
    """Tipo de operación y si modifica los archivos que toca.

    `mutates` es lo que decide si dos operaciones sobre el mismo archivo se
    pueden solapar: dos lecturas (verificar y transferir) conviven sin
    problema; en cuanto una escribe, no."""

    SCANNING = ("Escaneando la biblioteca", False)
    IMPORTING = ("Agregando juegos", True)
    CONVERTING = ("Convirtiendo", True)
    VERIFYING = ("Verificando", False)
    TRANSFERRING = ("Enviando a la unidad", False)
    DELETING = ("Eliminando", True)
    RENAMING = ("Renombrando", True)

    def __init__(self, label: str, mutates: bool):
        self.label = label
        self.mutates = mutates


# Operaciones de las que solo puede haber UNA a la vez de su mismo tipo,
# aunque toquen archivos distintos: dos escaneos en paralelo se pisan el
# resultado entre sí, y las demás comparten la barra de progreso de la
# ventana, así que dos iguales mezclan el progreso.
_EXCLUSIVE_KINDS = frozenset(
    {OperationKind.SCANNING, OperationKind.IMPORTING,
     OperationKind.TRANSFERRING, OperationKind.CONVERTING}
)

# Operaciones que además no conviven ENTRE SÍ, no solo con otra igual.
# Todas actualizan la MISMA barra de progreso de la ventana principal:
# corriendo dos a la vez el progreso salta de una a la otra y la primera
# que termina esconde la barra mientras la otra sigue trabajando.
#
# El escaneo queda afuera a propósito: no es una acción que el usuario
# pida (corre solo, después de cada operación y cuando la unidad de la
# biblioteca aparece), la regla 2 de abajo ya lo separa de todo lo que
# escribe archivos, y bloquearlo acá haría que un escaneo automático
# quedara postergado detrás de cada transferencia de la pestaña
# Transferir, que ni siquiera usa esta barra sino la suya.
_SHARED_PROGRESS_KINDS = frozenset(
    {OperationKind.IMPORTING, OperationKind.TRANSFERRING,
     OperationKind.CONVERTING}
)


@dataclass(frozen=True)
class Operation:
    """Una operación en curso. `paths` son los archivos concretos que toca
    (vacío = no se puede acotar a archivos puntuales, como el escaneo)."""

    id: int
    kind: OperationKind
    paths: frozenset

    @property
    def label(self) -> str:
        return self.kind.label


@dataclass(frozen=True)
class OperationOutcome:
    """Cómo terminó una operación, para el historial.

    `target` es sobre qué se operó (el título del juego, o un resumen como
    "12 juegos" en las operaciones en lote) y `detail` el motivo del error
    o el desglose, vacío si no hay nada que aclarar."""

    status: str
    target: str
    detail: str = ""


class OperationBusy(RuntimeError):
    """No se puede arrancar la operación pedida porque hay otra en curso
    que la bloquea. `blocker` es esa otra operación."""

    def __init__(self, blocker: Operation, detail: str):
        super().__init__(detail)
        self.blocker = blocker
        self.detail = detail


def _normalize(paths: Iterable) -> frozenset:
    """Rutas absolutas y sin symlinks para poder compararlas entre
    operaciones: 'juego.iso' y './juego.iso' son el mismo archivo."""
    normalized = set()
    for p in paths:
        path = Path(p)
        try:
            normalized.add(path.resolve())
        except OSError:
            normalized.add(path.absolute())
    return frozenset(normalized)


class OperationManager:
    """Registro de las operaciones en curso. Seguro entre hilos: la
    interfaz consulta desde el hilo de GTK y los workers terminan sus
    operaciones desde hilos de fondo.

    Si se le pasa un `log` (un `oplog.OperationLog`), cada operación que
    termina informando su resultado queda registrada en el historial. El
    enganche va acá y no en cada worker porque este es el único lugar por
    el que pasan todas las operaciones largas de la app, así que ninguna
    se puede olvidar de registrarse."""

    def __init__(self, log=None) -> None:
        self._lock = threading.RLock()
        self._active: dict = {}
        self._next_id = 1
        self._listeners: list = []
        self._log = log

    # ------------------------------------------------------------ Estado --
    def active_operations(self) -> list:
        with self._lock:
            return list(self._active.values())

    def is_busy(self) -> bool:
        with self._lock:
            return bool(self._active)

    def busy_label(self) -> Optional[str]:
        """Texto corto de lo que está corriendo, para tooltips y avisos.
        None si no hay nada en curso."""
        with self._lock:
            ops = list(self._active.values())
        if not ops:
            return None
        labels = []
        for op in ops:
            if op.label not in labels:
                labels.append(op.label)
        return " · ".join(labels)

    def is_path_busy(self, path) -> bool:
        """True si algún archivo pasado está tomado por una operación."""
        target = _normalize([path])
        with self._lock:
            return any(op.paths & target for op in self._active.values())

    # --------------------------------------------------------- Conflictos --
    def _find_conflict(self, kind: OperationKind, paths: frozenset):
        """Devuelve (operación_que_bloquea, motivo) o None si se puede
        arrancar. Las reglas, en orden:

        1. De las operaciones "exclusivas" (escanear, importar, transferir,
           convertir) puede haber solo una a la vez de cada tipo.
        1b. Las que comparten la barra de progreso de la ventana
           (importar, transferir, convertir) tampoco conviven entre sí.
        2. Escanear no convive con nada que escriba archivos: el escaneo
           llegaría a ver archivos a medio escribir y los identificaría mal.
        3. Dos operaciones que tocan el mismo archivo solo conviven si
           ninguna de las dos lo modifica."""
        for op in self._active.values():
            if kind in _EXCLUSIVE_KINDS and op.kind is kind:
                return op, f"ya hay una operación de este tipo en curso ({op.label})"

            if kind in _SHARED_PROGRESS_KINDS and op.kind in _SHARED_PROGRESS_KINDS:
                return op, (
                    "hay otra operación larga en curso y las dos comparten la "
                    f"misma barra de progreso ({op.label})"
                )

            if ((kind is OperationKind.SCANNING and op.kind.mutates)
                    or (op.kind is OperationKind.SCANNING and kind.mutates)):
                return op, (
                    "no se puede escanear la biblioteca mientras se escriben "
                    f"archivos en ella ({op.label})"
                )

            shared = op.paths & paths
            if shared and (kind.mutates or op.kind.mutates):
                name = sorted(shared)[0].name
                return op, f"'{name}' ya está en uso por otra operación ({op.label})"

        return None

    def conflict_for(self, kind: OperationKind, paths: Iterable = ()):
        """Como `_find_conflict` pero pública y con las rutas sin
        normalizar: devuelve la operación que bloquea, o None."""
        normalized = _normalize(paths)
        with self._lock:
            found = self._find_conflict(kind, normalized)
        return found[0] if found else None

    def check(self, kind: OperationKind, paths: Iterable = ()) -> None:
        """Levanta `OperationBusy` si `kind` sobre `paths` no puede
        arrancar ahora. Sirve para revalidar justo antes de tocar el disco
        en un flujo con diálogo de por medio, donde entre que el usuario
        confirma y se ejecuta pudo arrancar otra cosa."""
        normalized = _normalize(paths)
        with self._lock:
            found = self._find_conflict(kind, normalized)
        if found is not None:
            raise OperationBusy(found[0], found[1])

    # ------------------------------------------------------ Ciclo de vida --
    def start(self, kind: OperationKind, paths: Iterable = ()) -> Operation:
        """Registra una operación nueva y la devuelve. Levanta
        `OperationBusy` si choca con algo en curso."""
        normalized = _normalize(paths)
        with self._lock:
            found = self._find_conflict(kind, normalized)
            if found is not None:
                raise OperationBusy(found[0], found[1])
            op = Operation(id=self._next_id, kind=kind, paths=normalized)
            self._next_id += 1
            self._active[op.id] = op
        self._notify()
        return op

    def finish(self, op: Optional[Operation], result: Optional["OperationOutcome"] = None) -> None:
        """Marca la operación como terminada. Tolera `None` y llamadas
        repetidas para que el `finally` de un worker no tenga que
        preocuparse por si la operación llegó a arrancar.

        `result`, si se pasa, es lo que se guarda en el historial. Solo se
        registra en la primera llamada (la que efectivamente saca la
        operación de la lista de activas): un worker que llama a `finish`
        en un `finally` y otra vez al terminar no puede dejar la operación
        anotada dos veces.

        El escaneo de la biblioteca no se registra aunque informe
        resultado: no es una acción que el usuario pida (corre solo al
        arrancar y después de cada operación) y llenaría el historial de
        entradas que no dicen nada."""
        if op is None:
            return
        with self._lock:
            existed = self._active.pop(op.id, None) is not None
        if not existed:
            return
        if (result is not None and self._log is not None
                and op.kind is not OperationKind.SCANNING):
            self._log.record(op.label, result.target, result.status, result.detail)
        self._notify()

    # --------------------------------------------------------- Listeners --
    def add_listener(self, callback: Callable[[], None]) -> None:
        """Registra un callback que se llama cada vez que arranca o termina
        una operación. Ojo: se llama desde el hilo que arrancó/terminó la
        operación, que puede no ser el de GTK; quien actualice widgets tiene
        que reenviarlo con `GLib.idle_add`."""
        with self._lock:
            self._listeners.append(callback)

    def _notify(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            cb()
