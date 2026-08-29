"""Cola de transferencias: una tarea por juego, procesadas de a una.

Por qué una cola y no un lote
-----------------------------
Hasta acá, "transferir" era UNA operación en bloque: un hilo recorría la
selección entera, dibujaba todo en una sola barra de progreso y tenía un
único botón "Cancelar" que mataba el conjunto. Eso arrastraba tres
problemas que no se arreglan con más comentarios:

1. **Cancelar era todo o nada.** El usuario que veía que el juego 3 de 40
   estaba tardando muchísimo solo podía abortar los 40.
2. **No se podía agregar trabajo.** Elegir otro juego mientras copiaba
   significaba esperar a que terminara el lote y arrancar otro.
3. **El progreso mentía por diseño.** Una barra para N juegos de tamaños
   distintos es un promedio ponderado que no dice nada de lo que está
   pasando *ahora* con el archivo que se está escribiendo.

Acá cada juego es un `TransferJob` independiente, con su estado, su
progreso y su propio token de cancelación. La interfaz muestra una fila
por tarea y puede matar una sola sin tocar las demás.

Por qué se procesan de a una
----------------------------
El destino típico de esta app es un disco USB externo o una tarjeta SD.
Copiar dos ISOs a la vez sobre ese medio no va al doble de velocidad: va
más lento que en serie, porque el cabezal (o el controlador de la SD) se
pasa el tiempo saltando entre dos archivos que crecen en zonas distintas,
y encima deja los dos archivos fragmentados. La paralelización de esta
refactorización está donde sí paga -medir tamaños, que es leer headers y
esperar I/O: ver `library.plan_transfer_fast`- y no donde perjudica.

Por qué todo sale por `GLib.idle_add`
-------------------------------------
El hilo de la cola nunca toca un widget. Cada cambio de estado de una
tarea se avisa con un callback que se despacha al hilo principal de GTK
con `GLib.idle_add`; GTK4 no es thread-safe y tocarlo desde un hilo de
fondo no falla siempre, falla *a veces*, que es peor. La regla vive acá
adentro (`_emit`) y no en cada llamador, así que no hay forma de olvidarse
de aplicarla desde la interfaz.
"""
from __future__ import annotations

import itertools
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from gi.repository import GLib

from . import drives, library, oplog, wit_wrapper
from .i18n import _
from .library import Game, TransferItem
from .operations import OperationBusy, OperationKind, OperationOutcome


class JobStatus(Enum):
    """Estado de una tarea de la cola.

    Igual que `OperationKind`, el VALOR queda en español (es el idioma del
    código fuente y el msgid del catálogo) y la traducción se aplica al
    mostrarlo, con `.label`."""

    PENDING = "Pendiente"
    RUNNING = "Copiando"
    DONE = "Completado"
    SKIPPED = "Ya estaba en el destino"
    ERROR = "Error"
    CANCELLED = "Cancelado"

    @property
    def label(self) -> str:
        return _(self.value)

    @property
    def is_final(self) -> bool:
        """Si la tarea ya no va a cambiar más de estado. Lo usa la interfaz
        para saber qué filas se pueden limpiar y la cola para rechazar una
        cancelación que llega tarde."""
        return self in _FINAL_STATUSES


_FINAL_STATUSES = frozenset(
    {JobStatus.DONE, JobStatus.SKIPPED, JobStatus.ERROR, JobStatus.CANCELLED}
)


@dataclass
class TransferJob:
    """Un juego esperando (o siendo) copiado a una unidad.

    Los campos mutables (`status`, `progress`, `speed_text`, `error_msg`)
    los escribe SIEMPRE el hilo de la cola, a través de
    `TransferQueue._update`, y los lee el hilo de GTK desde el callback.
    No hace falta un lock para leerlos: son asignaciones de atributos
    sueltos, atómicas en CPython, y el callback llega después de la
    escritura porque lo dispara la propia escritura. Lo que sí importa es
    que la interfaz **no los escriba**: la fuente de la verdad del estado
    es la cola, y una fila que se pinte a sí misma "cancelado" sin que la
    cola se entere queda mintiendo."""

    id: int
    game: Game
    dest_root: Path
    status: JobStatus = JobStatus.PENDING
    # 0.0 a 1.0. Se queda en 0.99 hasta que la copia termina de verdad:
    # `wit` sigue cerrando y renombrando un instante después de escribir el
    # último byte, y una barra clavada en 100% "terminando…" se lee como
    # que la app se colgó.
    progress: float = 0.0
    # Texto ya armado para el subtítulo ("12.4 MB/s · ~3m 20s restantes").
    # Se arma acá y no en el widget porque depende de datos que solo tiene
    # la cola (bytes escritos, cuándo arrancó) y así la fila es tonta.
    speed_text: str = ""
    error_msg: str = ""
    cancel_token: wit_wrapper.CancellationToken = field(
        default_factory=wit_wrapper.CancellationToken)

    # ---- Datos de trabajo (no los toca la interfaz) ----
    wit_binary: str = "wit"
    overwrite: bool = False
    # "Optimizar espacio (Scrubbing)" de Ajustes -ver
    # `config.Settings.scrub_update`-, congelado en la tarea al encolar
    # para que cambiar el switch a mitad de una tanda no le cambie las
    # reglas a una tarea que ya está copiando.
    scrub_update: bool = True
    # Cuánto se espera que ocupe en el destino. Si viene de
    # `library.plan_transfer_fast` ya está medido; si no, lo mide la cola
    # justo antes de copiar (ver `_ensure_output_bytes`).
    output_bytes: int = 0
    bytes_done: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def title(self) -> str:
        return self.game.title

    @property
    def is_final(self) -> bool:
        return self.status.is_final

    @property
    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        end = self.finished_at or time.monotonic()
        return max(0.0, end - self.started_at)


@dataclass(frozen=True)
class QueueSummary:
    """Resultado de una tanda: lo que pasó desde que la cola arrancó hasta
    que se quedó sin trabajo. Es lo que la interfaz usa para el toast
    final, en vez de tener que contar filas ella misma."""

    done: int = 0
    skipped: int = 0
    errors: int = 0
    cancelled: int = 0

    @property
    def total(self) -> int:
        return self.done + self.skipped + self.errors + self.cancelled


# Cada cuánto, como mucho, se le avisa a la interfaz del avance de la
# tarea en curso. `wit` reporta bytes escritos muy seguido y sin freno; sin
# esto, una copia grande mete miles de `GLib.idle_add` por segundo y la
# ventana se pone a repintar filas en vez de responder al usuario. 100 ms
# son 10 refrescos por segundo: más fluido de lo que el ojo distingue en
# una barra de progreso.
_PROGRESS_INTERVAL = 0.1

# Cuánto espera la cola antes de volver a intentar arrancar una tarea que
# choca con otra operación (una conversión, una importación). No es un
# error: la cola es justamente el lugar donde tiene sentido esperar.
_BUSY_RETRY_SECONDS = 0.5


def _format_speed(bytes_per_second: float) -> str:
    """Velocidad legible.

    No se usa `library.format_size` para esto: esa función corta en MB (por
    debajo de 1 GB muestra megas), y una copia lenta de verdad -una SD vieja
    o un USB 1.1 a 60 KB/s- se vería como "0.0 MB/s", que se lee igual que
    "no está avanzando". Justo ahí es donde el usuario más necesita ver que
    algo se mueve."""
    if bytes_per_second < 1024 ** 2:
        return f"{bytes_per_second / 1024:.0f} KB/s"
    return f"{bytes_per_second / 1024 ** 2:.1f} MB/s"


def _glib_dispatch(func: Callable, *args) -> None:
    """Corre `func(*args)` en el hilo principal de GTK.

    `GLib.idle_add` espera que el callback devuelva si quiere volver a ser
    llamado; devolver `None` funciona por accidente (se lee como falso)
    pero deja el contrato a merced de un detalle de PyGObject. El envoltorio
    devuelve `GLib.SOURCE_REMOVE` explícito: se ejecuta una vez y se va."""
    GLib.idle_add(_run_once, func, args)


def _run_once(func: Callable, args: tuple) -> bool:
    func(*args)
    return GLib.SOURCE_REMOVE


class TransferQueue:
    """Cola de transferencias con un hilo de fondo que la vacía de a una.

    Uso desde la interfaz::

        cola = TransferQueue(ops, on_job_changed=self._on_job_changed,
                             on_queue_idle=self._on_queue_idle)
        jobs = cola.add_jobs(items, dest_root, wit_binary="wit")
        # ...
        cola.cancel_job(job.id)

    `on_job_changed(job)` se llama para CADA cambio de estado (encolado,
    arranque, avance, final) ya en el hilo de GTK. `on_queue_idle(summary)`
    se llama una sola vez, cuando la cola se quedó sin trabajo."""

    def __init__(self, ops=None,
                 on_job_changed: Optional[Callable[[TransferJob], None]] = None,
                 on_queue_idle: Optional[Callable[[QueueSummary], None]] = None,
                 dispatch: Callable = _glib_dispatch) -> None:
        # `ops` es el `OperationManager` compartido con la ventana: es lo
        # que hace que "Expulsar unidad" se niegue mientras esta cola
        # escribe, y que no arranque una copia sobre un archivo que se está
        # convirtiendo. Sin él la cola funciona igual, pero a ciegas.
        if ops is None:
            from .operations import OperationManager
            ops = OperationManager()
        self.ops = ops
        self._on_job_changed = on_job_changed
        self._on_queue_idle = on_queue_idle
        # Inyectable para poder probar la cola sin un bucle de GTK
        # corriendo. En la app real es siempre `_glib_dispatch`.
        self._dispatch = dispatch

        self._lock = threading.Lock()
        # `Condition` sobre el mismo lock: el hilo de fondo duerme acá
        # cuando no hay trabajo, en vez de despertarse a preguntar cada
        # tantos milisegundos. Lo despiertan `add_jobs`, `cancel_job` y
        # `shutdown`.
        self._wake = threading.Condition(self._lock)
        self._pending: deque[TransferJob] = deque()
        self._jobs: dict[int, TransferJob] = {}
        self._order: list[int] = []
        self._active: Optional[TransferJob] = None
        self._ids = itertools.count(1)
        self._worker: Optional[threading.Thread] = None
        self._stopping = False
        self._tally = {"done": 0, "skipped": 0, "errors": 0, "cancelled": 0}

    # ------------------------------------------------------------ Encolar --
    def add_jobs(self, items, dest_root, wit_binary: str = "wit",
                 overwrite: bool = False, scrub_update: bool = True) -> list[TransferJob]:
        """Encola juegos hacia `dest_root` y devuelve las tareas creadas.

        `items` puede ser una lista de `Game` o de `library.TransferItem`.
        Lo segundo es lo que conviene: el `TransferItem` ya trae medido
        cuánto va a ocupar el juego en el destino (lo calculó
        `plan_transfer_fast` en paralelo), así que la cola no tiene que
        parar a preguntarle a `wit` antes de cada copia. Con `Game` pelado
        funciona igual, midiendo sobre la marcha.

        Vuelve enseguida: lo único que hace es apuntar el trabajo y
        despertar al hilo de fondo. Se puede llamar con la cola andando,
        que es justamente la gracia -las tareas nuevas se suman al final
        de la fila."""
        dest_root = Path(dest_root)
        nuevos: list[TransferJob] = []
        for item in items:
            if isinstance(item, TransferItem):
                game, output_bytes = item.game, item.output_bytes
            else:
                game, output_bytes = item, 0
            job = TransferJob(
                id=next(self._ids), game=game, dest_root=dest_root,
                wit_binary=wit_binary, overwrite=overwrite,
                output_bytes=output_bytes, scrub_update=scrub_update,
            )
            nuevos.append(job)

        if not nuevos:
            return []

        with self._wake:
            for job in nuevos:
                self._jobs[job.id] = job
                self._order.append(job.id)
                self._pending.append(job)
            self._ensure_worker_locked()
            self._wake.notify_all()

        # El aviso va DESPUÉS de soltar el lock: el callback termina
        # ejecutándose en el hilo de GTK, y no hay motivo para que el hilo
        # que encola siga reteniendo la cola mientras tanto.
        for job in nuevos:
            self._emit(job)
        return nuevos

    def _ensure_worker_locked(self) -> None:
        """Arranca el hilo de fondo la primera vez que hay algo que hacer.

        Perezoso a propósito: una `TransferQueue` que se construye al abrir
        la app y a la que nunca se le encola nada no deja un hilo dando
        vueltas. `daemon=True` porque la cola no debe impedir que la app
        cierre; el trabajo a medias lo corta `shutdown`, que sí mata el
        `wit` en curso."""
        if self._stopping:
            return
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(target=self._run, name="transfer-queue",
                                        daemon=True)
        self._worker.start()

    # ---------------------------------------------------------- Consultas --
    @property
    def jobs(self) -> list[TransferJob]:
        """Todas las tareas conocidas, en el orden en que se encolaron."""
        with self._lock:
            return [self._jobs[i] for i in self._order if i in self._jobs]

    @property
    def active_job(self) -> Optional[TransferJob]:
        with self._lock:
            return self._active

    @property
    def is_busy(self) -> bool:
        """Si queda trabajo por hacer (en curso o esperando)."""
        with self._lock:
            return self._active is not None or bool(self._pending)

    def pending_resources(self) -> set:
        """Unidades hacia las que la cola todavía tiene trabajo.

        Es la respuesta a "¿puedo expulsar este disco?". El
        `OperationManager` solo conoce la tarea que se está copiando *en
        este instante*; entre una tarea y la siguiente hay un pestañeo en
        el que no hay ninguna operación declarada, y expulsar la unidad ahí
        dejaría a la próxima tarea escribiendo sobre un punto de montaje
        que ya no existe. Esto cubre ese hueco."""
        with self._lock:
            activos = [job for job in self._jobs.values() if not job.is_final]
            return {job.dest_root for job in activos}

    def is_writing_to(self, path) -> bool:
        """Si la cola tiene trabajo pendiente hacia `path` (o hacia algo
        adentro de `path`)."""
        try:
            objetivo = Path(path).resolve()
        except OSError:
            objetivo = Path(path)
        for root in self.pending_resources():
            try:
                root = root.resolve()
            except OSError:
                pass
            if root == objetivo or objetivo in root.parents:
                return True
        return False

    # -------------------------------------------------------- Cancelación --
    def cancel_job(self, job_id) -> bool:
        """Cancela UNA tarea. Devuelve False si ya había terminado.

        Dos casos bien distintos:

        - **Pendiente**: se saca de la fila y queda "Cancelado" al
          instante. Nunca llegó a tocar el disco.
        - **En curso**: se marca el token, que además de frenar la cola
          mata el `wit` que esté corriendo (ver
          `wit_wrapper.CancellationToken`). No se espera acá a que muera:
          esto lo llama el hilo de GTK desde un botón, y bloquearlo sería
          congelar la ventana justo cuando el usuario pide que algo pare.

        Acepta el id o el propio `TransferJob`, para que el botón de la
        fila pueda pasar lo que tenga a mano."""
        if isinstance(job_id, TransferJob):
            job_id = job_id.id
        with self._wake:
            job = self._jobs.get(job_id)
            if job is None or job.is_final:
                return False
            pendiente = job.status is JobStatus.PENDING
            if pendiente:
                try:
                    self._pending.remove(job)
                except ValueError:
                    # Ya lo levantó el hilo de fondo entre medio: entonces
                    # no es "pendiente" sino "recién arrancado", y alcanza
                    # con el token de abajo.
                    pendiente = False
            job.cancel_token.cancel()
            if pendiente:
                job.status = JobStatus.CANCELLED
                job.speed_text = ""
                job.finished_at = time.monotonic()
                self._tally["cancelled"] += 1
            self._wake.notify_all()
        self._emit(job)
        return True

    def cancel_all(self) -> int:
        """Cancela todo lo que quede vivo. Devuelve cuántas tareas frenó."""
        with self._lock:
            ids = [job.id for job in self._jobs.values() if not job.is_final]
        return sum(1 for job_id in ids if self.cancel_job(job_id))

    def clear_finished(self) -> list[TransferJob]:
        """Olvida las tareas terminadas y las devuelve, para que la interfaz
        sepa qué filas sacar. No toca lo que está en curso ni lo que
        espera."""
        with self._lock:
            terminadas = [self._jobs[i] for i in self._order
                          if i in self._jobs and self._jobs[i].is_final]
            for job in terminadas:
                del self._jobs[job.id]
            self._order = [i for i in self._order if i in self._jobs]
        return terminadas

    def shutdown(self, wait: float = 0.0) -> None:
        """Corta todo: cancela lo vivo y le pide al hilo que termine.

        Se llama al cerrar la app. El hilo es daemon, así que no cerrar la
        cola no cuelga el proceso, pero sí dejaría un `wit` escribiendo
        sobre una unidad que el usuario está por desenchufar."""
        self.cancel_all()
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
            worker = self._worker
        if wait and worker is not None:
            worker.join(timeout=wait)

    # ---------------------------------------------------- Estado / avisos --
    def _update(self, job: TransferJob, **cambios) -> None:
        """Único lugar donde se le escribe a un `TransferJob`, y único lugar
        desde donde sale un aviso a la interfaz. Que sea uno solo es lo que
        garantiza la regla del módulo: no hay cambio de estado sin
        `GLib.idle_add`."""
        with self._lock:
            for campo, valor in cambios.items():
                setattr(job, campo, valor)
        self._emit(job)

    def _emit(self, job: TransferJob) -> None:
        if self._on_job_changed is not None:
            self._dispatch(self._on_job_changed, job)

    def _emit_idle(self, summary: QueueSummary) -> None:
        if self._on_queue_idle is not None:
            self._dispatch(self._on_queue_idle, summary)

    # ------------------------------------------------------- Hilo de fondo --
    def _run(self) -> None:
        """Bucle del hilo: sacar una tarea, hacerla, repetir. De a una."""
        while True:
            # Se quedó sin trabajo: cerrar la tanda y avisar ANTES de
            # dormir. El aviso sale con el lock ya soltado -- un callback
            # que vuelva a llamar a la cola (encolar de nuevo, limpiar
            # terminadas) no tiene por qué encontrarse con el lock tomado.
            resumen = None
            with self._wake:
                if not self._pending and not self._stopping:
                    resumen = self._take_summary_locked()
            if resumen is not None:
                self._emit_idle(resumen)

            with self._wake:
                while not self._pending and not self._stopping:
                    self._wake.wait()
                if self._stopping:
                    return
                job = self._pending.popleft()
                self._active = job
            try:
                self._process(job)
            except Exception as e:  # noqa: BLE001
                # Red de seguridad: una excepción que se escape de
                # `_process` mataría el hilo y dejaría la cola muda para
                # siempre, con las tareas siguientes "Pendiente" eternas.
                # Se marca la tarea y se sigue con la que viene.
                self._finish_job(job, JobStatus.ERROR, str(e),
                                 oplog.STATUS_ERROR)
            finally:
                with self._lock:
                    self._active = None

    def _take_summary_locked(self) -> Optional[QueueSummary]:
        """Cierra la tanda actual: devuelve el conteo y lo pone en cero.
        None si no hubo nada que contar (para no disparar un toast cada vez
        que el hilo pasa por acá)."""
        if not any(self._tally.values()):
            return None
        resumen = QueueSummary(done=self._tally["done"],
                               skipped=self._tally["skipped"],
                               errors=self._tally["errors"],
                               cancelled=self._tally["cancelled"])
        for clave in self._tally:
            self._tally[clave] = 0
        return resumen

    def _process(self, job: TransferJob) -> None:
        if job.cancel_token.cancelled:
            self._finish_job(job, JobStatus.CANCELLED, "", None)
            return

        # El destino se resuelve antes de declarar nada: un Game ID inválido
        # no es una copia que falle, es una copia que no se puede ni
        # plantear (y `game_dest_path` lo rechaza justamente para que ese
        # ID no termine siendo un componente de ruta). `game_dest_path`
        # elige sola la estructura que corresponda (WBFS de Wii o carpeta
        # de Nintendont para GameCube, ver library.py).
        try:
            dest = library.game_dest_path(job.game, job.dest_root)
        except ValueError as e:
            self._finish_job(job, JobStatus.ERROR, str(e), oplog.STATUS_ERROR)
            return

        op = self._acquire_operation(job, dest)
        if op is None:
            # Cancelado mientras esperaba su turno.
            self._finish_job(job, JobStatus.CANCELLED, "", None)
            return

        try:
            self._copy(job, op)
        finally:
            # `finish` tolera que ya se haya llamado, así que las salidas
            # de `_copy` que ya cerraron la operación con su resultado no
            # anotan nada dos veces. Este `finally` está para el camino que
            # nadie previó: sin él, una excepción rara dejaría la unidad
            # declarada como ocupada para siempre y "Expulsar" gris hasta
            # reiniciar la app.
            self.ops.finish(op)

    def _acquire_operation(self, job: TransferJob, dest: Path):
        """Declara la copia en el `OperationManager`, esperando si hace
        falta.

        Que el gestor diga "ocupado" no es un error acá: la cola existe
        justamente para esperar. Si hay una conversión andando en la
        Biblioteca, la tarea se queda en "Pendiente" diciendo por qué, y
        arranca sola cuando la otra termina. La alternativa -fallar la
        tarea- obligaría al usuario a estar mirando para reintentar.

        Devuelve None si el usuario canceló mientras esperaba."""
        aviso_dado = False
        while True:
            if job.cancel_token.cancelled:
                return None
            try:
                return self.ops.start(
                    OperationKind.TRANSFERRING,
                    read=[job.game.path],
                    write=[dest],
                    resources=drives.resources_for_mount_point(job.dest_root),
                )
            except OperationBusy as e:
                if not aviso_dado:
                    # Una sola vez: el motivo no cambia mientras se espera,
                    # y repetirlo cada medio segundo sería repintar la fila
                    # para nada.
                    self._update(job, speed_text=_("En espera: {op}").format(
                        op=e.blocker.label))
                    aviso_dado = True
                with self._wake:
                    # Espera interrumpible: si el usuario cancela la tarea,
                    # `cancel_job` despierta el hilo y no hay que aguantar
                    # el intervalo entero.
                    self._wake.wait(_BUSY_RETRY_SECONDS)
                    if self._stopping:
                        return None

    def _copy(self, job: TransferJob, op) -> None:
        self._update(job, status=JobStatus.RUNNING, progress=0.0,
                     started_at=time.monotonic(),
                     speed_text=_("Preparando…"))

        if not self._ensure_output_bytes(job, op):
            return

        # El espacio libre se mira ACÁ y no al encolar: entre que el usuario
        # armó la cola y le toca el turno a esta tarea se escribieron todas
        # las anteriores, así que el número de hace un minuto no sirve.
        libres = library.free_space(job.dest_root)
        if libres is not None and job.output_bytes > libres:
            self._finish_job(
                job, JobStatus.ERROR,
                _("No entra en el destino: necesita {need} y quedan {free}.")
                .format(need=library.format_size(job.output_bytes),
                        free=library.format_size(libres)),
                oplog.STATUS_ERROR)
            return

        ultimo = [0.0]

        def on_bytes(escritos: int) -> None:
            ahora = time.monotonic()
            if ahora - ultimo[0] < _PROGRESS_INTERVAL:
                return
            ultimo[0] = ahora
            self._report_progress(job, escritos, ahora)

        try:
            library.send_to_wbfs_drive(
                job.game, job.dest_root, job.wit_binary,
                bytes_progress_cb=on_bytes,
                overwrite=job.overwrite,
                cancel=job.cancel_token,
                scrub_update=job.scrub_update,
            )
        except wit_wrapper.OperationCancelled:
            self._finish_job(job, JobStatus.CANCELLED, "", oplog.STATUS_CANCELLED,
                             op=op)
            return
        except library.DestinationExistsError:
            # Ni éxito ni error: el juego ya está en la unidad y no se pidió
            # pisarlo. Tiene su propio estado porque contarlo como error
            # haría que una cola de 40 juegos ya copiados se viera roja
            # entera, y contarlo como copiado sería mentir sobre lo que se
            # escribió.
            self._finish_job(job, JobStatus.SKIPPED, "", oplog.STATUS_OK, op=op)
            return
        except wit_wrapper.WitNotFoundError:
            self._finish_job(
                job, JobStatus.ERROR,
                _("No se encontró `wit` ({binary}). Instalalo o configurá la "
                  "ruta en Preferencias.").format(binary=job.wit_binary),
                oplog.STATUS_ERROR, op=op)
            return
        except Exception as e:  # noqa: BLE001
            if job.cancel_token.cancelled:
                # El fallo es consecuencia de haber matado a `wit` al
                # cancelar, no un problema de la copia.
                self._finish_job(job, JobStatus.CANCELLED, "",
                                 oplog.STATUS_CANCELLED, op=op)
            else:
                self._finish_job(job, JobStatus.ERROR, str(e),
                                 oplog.STATUS_ERROR, op=op)
            return

        self._finish_job(job, JobStatus.DONE, "", oplog.STATUS_OK, op=op)

    def _ensure_output_bytes(self, job: TransferJob, op=None) -> bool:
        """Se asegura de saber cuánto va a ocupar el juego en el destino.

        Si la tarea vino de un `TransferItem` ya está medido y esto no
        cuesta nada. Si vino de un `Game` pelado hay que preguntarle a
        `wit`, que es rápido pero puede fallar; el respaldo es el mismo que
        usa `library.plan_transfer_fast`, el tamaño del archivo de origen.
        Nunca cero: es el denominador de la barra de progreso.

        Devuelve False si la tarea se canceló mientras se medía."""
        if job.output_bytes <= 0:
            self._update(job, speed_text=_("Calculando tamaño…"))
            try:
                medido = library.estimate_transfer_size(job.game, job.wit_binary)
            except Exception:
                medido = 0
            job.output_bytes = medido or job.game.size_bytes
        if job.cancel_token.cancelled:
            self._finish_job(job, JobStatus.CANCELLED, "", oplog.STATUS_CANCELLED,
                             op=op)
            return False
        return True

    def _report_progress(self, job: TransferJob, escritos: int, ahora: float) -> None:
        total = job.output_bytes or 1
        # Tope en 0.99: ver el comentario de `TransferJob.progress`.
        fraccion = min(escritos / total, 0.99)
        transcurrido = ahora - (job.started_at or ahora)
        texto = ""
        if transcurrido > 1 and escritos > 0:
            velocidad = escritos / transcurrido
            texto = _format_speed(velocidad)
            if velocidad > 0:
                faltan = max(total - escritos, 0)
                texto += _(" · ~{eta} restantes").format(
                    eta=library.format_eta(faltan / velocidad))
        self._update(job, progress=fraccion, bytes_done=escritos,
                     speed_text=texto)

    def _finish_job(self, job: TransferJob, status: JobStatus, error_msg: str,
                    log_status: Optional[str], op=None) -> None:
        """Cierra una tarea: fija su estado final, lo anota en el historial
        y avisa. Un solo camino de salida para que ninguna rama se olvide
        de cerrar la operación o de contar el resultado."""
        if op is not None and log_status is not None:
            self.ops.finish(op, OperationOutcome(
                status=log_status, target=job.game.title,
                detail=(error_msg or status.value)))
        elif op is not None:
            self.ops.finish(op)

        with self._lock:
            if status is JobStatus.DONE:
                self._tally["done"] += 1
            elif status is JobStatus.SKIPPED:
                self._tally["skipped"] += 1
            elif status is JobStatus.ERROR:
                self._tally["errors"] += 1
            elif status is JobStatus.CANCELLED:
                self._tally["cancelled"] += 1

        self._update(job, status=status, error_msg=error_msg,
                     progress=1.0 if status is JobStatus.DONE else job.progress,
                     speed_text="", finished_at=time.monotonic())
