"""Coordinación central de las operaciones largas de la app.

Antes cada acción (escanear, importar, convertir, verificar, transferir,
eliminar) lanzaba su propio hilo sin que nadie supiera qué más estaba
corriendo. Eso dejaba pasar combinaciones que corrompen datos o muestran
información falsa:

- eliminar o renombrar un juego mientras `wit` lo está convirtiendo;
- dos escaneos en paralelo, donde el que termina último pisa al otro y la
  lista queda mostrando el resultado más viejo;
- escanear mientras se copia/convierte dentro de la biblioteca, con lo que
  el escaneo llega a ver archivos a medio escribir y los identifica mal;
- expulsar el USB mientras se le está copiando un juego encima;
- formatear un USB (Modo Fábrica) mientras se le está copiando un juego o
  instalando homebrew encima -o al revés: arrancar una copia sobre un
  disco que Modo Fábrica está formateando en ese momento. Acá es donde
  `resources` no puede ser el punto de montaje nomás: Modo Fábrica lo
  desmonta como parte de formatear, así que declara el disco físico
  entero (`drives.physical_disk_for_path` / `BlockDevice.path`) y
  Transferencias/Homebrew declaran ESE MISMO recurso además del punto de
  montaje, para que ambos lados choquen entre sí sin importar cuál
  arrancó primero.

Cada operación declara TRES cosas, y de ahí salen todos los conflictos:

- `read_paths`: los archivos que va a leer;
- `write_paths`: los archivos que va a escribir, borrar o renombrar;
- `resources`: los "lugares" que ocupa mientras dura -la carpeta de la
  biblioteca durante un escaneo, el punto de montaje del USB durante una
  transferencia-. Un recurso ocupado no se puede desmontar ni recibir
  escrituras de otra operación.

Esa última categoría es la que faltaba: la transferencia registraba los
archivos de ORIGEN (los de la biblioteca) y nadie sabía que el DESTINO
-un pendrive de un cliente- estaba en pleno uso, así que el botón
"Expulsar unidad" lo desmontaba a mitad de la escritura.

Este módulo es a propósito independiente de GTK: no importa nada de la
interfaz, se puede probar sin levantar una ventana, y la interfaz se
entera de los cambios registrando un listener.

No es un candado global: dos operaciones que no se pisan (verificar o
borrar un juego mientras se convierte o se transfiere OTRO) siguen
pudiendo correr juntas, y las descargas de carátulas ni pasan por acá.
Lo que se bloquea son las combinaciones peligrosas y las que se pelean
por la barra de progreso, definidas en `_find_conflict`.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Optional

from .i18n import _


class OperationKind(Enum):
    """Tipo de operación, con el texto que se le muestra al usuario.

    Ya no lleva una bandera `mutates`: qué se lee y qué se escribe lo
    declara cada operación al arrancar (`read_paths` / `write_paths`), que
    es información concreta y no una propiedad del tipo. La transferencia
    era el ejemplo de por qué: "no modifica la biblioteca" es cierto, pero
    escribe -y mucho- en la unidad de destino."""

    SCANNING = "Escaneando la biblioteca"
    IMPORTING = "Agregando juegos"
    CONVERTING = "Convirtiendo"
    VERIFYING = "Verificando"
    TRANSFERRING = "Enviando a la unidad"
    DELETING = "Eliminando"
    RENAMING = "Renombrando"
    INSTALLING_HOMEBREW = "Instalando homebrew"
    CHECKING_MEMORY = "Verificando la memoria"
    FORMATTING = "Formateando la unidad"

    @property
    def label(self) -> str:
        """El texto para mostrar, traducido al idioma del sistema.

        El VALOR del enum queda siempre en español: es lo que `finish`
        guarda en el historial y lo que después se traduce al mostrarlo
        (ver oplog.LogEntry.operation). Si acá se guardara el texto ya
        traducido, el historial quedaría con entradas en el idioma que
        estuviera puesto en cada momento."""
        return _(self.value)


# Operaciones de las que solo puede haber UNA a la vez de su mismo tipo,
# aunque toquen archivos distintos: dos escaneos en paralelo se pisan el
# resultado entre sí, y las demás comparten la barra de progreso de la
# ventana, así que dos iguales mezclan el progreso.
_EXCLUSIVE_KINDS = frozenset(
    {OperationKind.SCANNING, OperationKind.IMPORTING,
     OperationKind.TRANSFERRING, OperationKind.CONVERTING}
)

# Tipos que SIEMPRE ocupan la barra de progreso, arranquen desde donde
# arranquen. Dos de estas a la vez hacen que el progreso salte de una a la
# otra y que la primera en terminar esconda la barra mientras la otra
# sigue trabajando, así que no conviven entre sí.
#
# Verificar y eliminar NO están acá porque depende de cómo se las use: en
# lote muestran progreso (van por `_run_batch`) y sobre un solo juego no
# (verificar un archivo no reporta avance, y borrarlo es instantáneo). Esa
# diferencia la marca quien arranca la operación, con
# `uses_progress_bar=True`, en vez de castigar al caso suelto: verificar o
# borrar un juego mientras se convierte otro distinto es algo que se hace
# todo el tiempo preparando varias unidades seguidas, y no se pisa con
# nada -- si el archivo es el mismo, ahí lo frena la regla 3, que es la
# que corresponde.
#
# El escaneo también dibuja en esa barra pero queda afuera a propósito: no
# es una acción que el usuario pida (corre solo, después de cada operación
# y cuando la unidad de la biblioteca aparece), la regla 2 de abajo ya lo
# separa de todo lo que escribe archivos, y bloquearlo acá haría que un
# escaneo automático quedara postergado detrás de cada transferencia de la
# pestaña Transferir, que ni siquiera usa esta barra sino la suya.
_SHARED_PROGRESS_KINDS = frozenset(
    {OperationKind.IMPORTING, OperationKind.TRANSFERRING,
     OperationKind.CONVERTING}
)


def _uses_progress_bar(kind: OperationKind, declared: bool) -> bool:
    """Si esta operación ocupa la barra de progreso: por ser de un tipo
    que siempre la usa, o porque quien la arranca lo declaró (el caso de
    los lotes de verificar y eliminar)."""
    return declared or kind in _SHARED_PROGRESS_KINDS


@dataclass(frozen=True)
class Operation:
    """Una operación en curso y qué está usando mientras dura."""

    id: int
    kind: OperationKind
    # Archivos concretos que lee y que escribe (o borra, o renombra).
    read_paths: frozenset = frozenset()
    write_paths: frozenset = frozenset()
    # Lugares ocupados: la carpeta de la biblioteca mientras se la escanea,
    # el punto de montaje del USB mientras se le copia. Ver el docstring
    # del módulo.
    resources: frozenset = frozenset()
    # Si esta operación está ocupando la barra de progreso de la ventana.
    # Ver `_uses_progress_bar` y la regla 1b de `_find_conflict`.
    uses_progress_bar: bool = False

    @property
    def label(self) -> str:
        return self.kind.label

    @property
    def paths(self) -> frozenset:
        """Todos los archivos que toca, sin distinguir cómo."""
        return self.read_paths | self.write_paths


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


def _is_within(child: Path, parent: Path) -> bool:
    """True si `child` está dentro del árbol de `parent` (o es `parent`)."""
    try:
        return child == parent or child.is_relative_to(parent)
    except (OSError, ValueError):
        return False


def _touching(paths: frozenset, resources: frozenset) -> Optional[Path]:
    """Primera ruta de `paths` que cae dentro de alguno de `resources`.

    Los recursos son pocos (uno o dos: una carpeta, un punto de montaje),
    así que este recorrido es barato aunque `paths` traiga cientos de
    archivos de una selección grande."""
    for resource in resources:
        for path in paths:
            if _is_within(path, resource):
                return path
    return None


def _resources_overlap(a: frozenset, b: frozenset) -> Optional[Path]:
    """Primer recurso compartido entre dos operaciones: el mismo lugar, o
    uno adentro del otro (la biblioteca guardada dentro del USB que se
    está por escribir, por ejemplo)."""
    for ra in a:
        for rb in b:
            if _is_within(ra, rb) or _is_within(rb, ra):
                return ra
    return None


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
        """True si ese archivo está tomado por alguna operación."""
        target = _normalize([path])
        with self._lock:
            return any(op.paths & target for op in self._active.values())

    def is_resource_busy(self, path) -> Optional[Operation]:
        """La operación que está ocupando ese lugar, o None.

        Un lugar está ocupado si alguna operación lo declaró como recurso
        (o declaró uno que lo contiene, o uno adentro), o si está
        escribiendo un archivo ahí adentro. Con esto el botón "Expulsar
        unidad" puede negarse mientras se le está copiando algo a ese
        pendrive, que era la forma más fácil de corromper el disco de un
        cliente."""
        target = _normalize([path])
        with self._lock:
            for op in self._active.values():
                if _resources_overlap(target, op.resources) is not None:
                    return op
                if _touching(op.write_paths, target) is not None:
                    return op
        return None

    # --------------------------------------------------------- Conflictos --
    def _find_conflict(self, kind: OperationKind, read_paths: frozenset,
                        write_paths: frozenset, resources: frozenset,
                        uses_progress_bar: bool = False):
        """Devuelve (operación_que_bloquea, motivo) o None si se puede
        arrancar. Las reglas, en orden:

        1. De las operaciones "exclusivas" (escanear, importar, transferir,
           convertir) puede haber solo una a la vez de cada tipo.
        1b. Dos operaciones que ocupan la barra de progreso no conviven
           (ver `_uses_progress_bar`). Verificar o borrar un juego suelto
           no ocupa la barra, así que no entra por acá.
        2. Dos operaciones no pueden ocupar el mismo lugar: la misma
           unidad, la misma carpeta, o una dentro de la otra.
        3. Nadie escribe archivos dentro de un lugar que otra operación
           está ocupando (ni al revés). Acá es donde el escaneo de la
           biblioteca queda separado de todo lo que escriba EN la
           biblioteca, sin bloquear lo que escribe en otro lado.
        4. Dos operaciones que tocan el mismo archivo solo conviven si
           ninguna de las dos lo escribe."""
        wants_bar = _uses_progress_bar(kind, uses_progress_bar)
        all_paths = read_paths | write_paths
        for op in self._active.values():
            if kind in _EXCLUSIVE_KINDS and op.kind is kind:
                return op, _("ya hay una operación de este tipo en curso ({op})").format(op=op.label)

            if wants_bar and op.uses_progress_bar:
                return op, _(
                    "hay otra operación larga en curso y las dos comparten la "
                    "misma barra de progreso ({op})"
                ).format(op=op.label)

            shared_resource = _resources_overlap(resources, op.resources)
            if shared_resource is not None:
                return op, _(
                    "'{name}' ya está en uso por otra operación ({op})"
                ).format(name=shared_resource.name or shared_resource, op=op.label)

            invading = (_touching(write_paths, op.resources)
                        or _touching(op.write_paths, resources))
            if invading is not None:
                return op, _(
                    "otra operación está usando esa ubicación ({op}): "
                    "no se puede escribir '{name}' mientras tanto"
                ).format(op=op.label, name=invading.name)

            shared = (op.write_paths & all_paths) | (write_paths & op.paths)
            if shared:
                name = sorted(shared)[0].name
                return op, _(
                    "'{name}' ya está en uso por otra operación ({op})"
                ).format(name=name, op=op.label)

        return None

    def conflict_for(self, kind: OperationKind, read=(), write=(),
                      resources=(), uses_progress_bar: bool = False):
        """Como `_find_conflict` pero pública y con las rutas sin
        normalizar: devuelve la operación que bloquea, o None."""
        found = self.conflicts_for([(kind, kind)], read, write, resources,
                                    uses_progress_bar)
        return found[kind]

    def conflicts_for(self, specs, read=(), write=(), resources=(),
                       uses_progress_bar: bool = False) -> dict:
        """Conflictos de varias operaciones que tocarían las MISMAS rutas,
        resolviéndolas una sola vez.

        `specs` es una lista de `(clave, tipo)` o de `(clave, tipo, rol)`,
        donde el rol ("read" o "write") dice si ese tipo escribiría esas
        rutas o solo las leería; por defecto se usan `read`/`write` tal
        como vienen. Devuelve {clave: operación_que_bloquea_o_None}.

        Existe por el costo de `_normalize`, que resuelve cada ruta contra
        el filesystem: la barra de selección pregunta por los cuatro
        botones en cada click de una casilla, y con una biblioteca grande
        seleccionada entera resolver las rutas cuatro veces en vez de una
        se nota como un tironeo en la interfaz."""
        base_read = _normalize(read)
        base_write = _normalize(write)
        norm_resources = _normalize(resources)
        todos = base_read | base_write

        result = {}
        with self._lock:
            for spec in specs:
                if len(spec) == 3:
                    key, kind, role = spec
                    r = frozenset() if role == "write" else todos
                    w = todos if role == "write" else frozenset()
                else:
                    key, kind = spec
                    r, w = base_read, base_write
                found = self._find_conflict(kind, r, w, norm_resources,
                                             uses_progress_bar)
                result[key] = found[0] if found is not None else None
        return result

    def check(self, kind: OperationKind, read=(), write=(), resources=(),
               uses_progress_bar: bool = False) -> None:
        """Levanta `OperationBusy` si la operación no puede arrancar ahora.
        Sirve para revalidar justo antes de tocar el disco en un flujo con
        diálogo de por medio, donde entre que el usuario confirma y se
        ejecuta pudo arrancar otra cosa.

        Los argumentos tienen que ser los mismos que en el `start` que
        venga después: si no, se chequea una cosa y se arranca otra."""
        with self._lock:
            found = self._find_conflict(kind, _normalize(read), _normalize(write),
                                         _normalize(resources), uses_progress_bar)
        if found is not None:
            raise OperationBusy(found[0], found[1])

    # ------------------------------------------------------ Ciclo de vida --
    def start(self, kind: OperationKind, read=(), write=(), resources=(),
               uses_progress_bar: bool = False) -> Operation:
        """Registra una operación nueva y la devuelve. Levanta
        `OperationBusy` si choca con algo en curso.

        - `read`: archivos que va a leer.
        - `write`: archivos que va a escribir, borrar o renombrar.
        - `resources`: lugares que ocupa mientras dura (la carpeta de la
          biblioteca, el punto de montaje del destino). Lo que declare acá
          es lo que va a impedir que se expulse la unidad o que otra
          operación escriba ahí adentro.
        - `uses_progress_bar=True` lo pasan las operaciones que muestran
          progreso en la ventana principal y que no son de un tipo que
          siempre lo haga: en la práctica, los lotes de verificar y
          eliminar. Ver `_uses_progress_bar`."""
        norm_read = _normalize(read)
        norm_write = _normalize(write)
        norm_resources = _normalize(resources)
        with self._lock:
            found = self._find_conflict(kind, norm_read, norm_write,
                                         norm_resources, uses_progress_bar)
            if found is not None:
                raise OperationBusy(found[0], found[1])
            op = Operation(id=self._next_id, kind=kind,
                           read_paths=norm_read, write_paths=norm_write,
                           resources=norm_resources,
                           uses_progress_bar=_uses_progress_bar(kind, uses_progress_bar))
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
            # `op.kind.value`, no `op.label`: al historial va el nombre sin
            # traducir y la traducción se aplica al mostrarlo.
            self._log.record(op.kind.value, result.target, result.status,
                             result.detail)
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
