"""Wrapper sobre Wiimms ISO Tools (`wit`).

`wit` es la herramienta estándar en Linux para trabajar con imágenes de
Wii/GameCube: lee ISO planas, WBFS (single-game y multi-game), CISO, WDF,
etc. y sabe convertir entre todos esos formatos y verificar integridad
(hashes por partición). En vez de reimplementar el parseo de esos formatos
binarios, esta app delega en `wit` para todo lo que no sea una ISO plana.

Repo / instalación: https://wit.wiimm.de/  (en Fedora: compilar desde
fuente o usar el binario estático que publican; no hay paquete oficial en
los repos de Fedora).
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import fsutil
from .disc_header import DiscInfo, is_valid_game_id, validate_game_id
from .i18n import _

# Algunas builds de `wit` colorean su salida con secuencias ANSI aunque la
# salida esté redirigida a una pipe (no es una terminal), así que no podemos
# confiar en que stdout venga "limpio" solo por capturarlo con subprocess.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class WitNotFoundError(RuntimeError):
    """`wit` no está instalado o no se encuentra en el PATH."""


class OperationCancelled(RuntimeError):
    """El usuario canceló la operación desde la interfaz. No es un error:
    quien llama lo distingue de un fallo real para no contarlo como tal."""


# Segundos que se le dan a `wit` para terminar por las buenas (SIGTERM)
# antes de matarlo a la fuerza (SIGKILL) cuando se lo da por colgado (ver
# `_terminate_process_group`). Cancelar no espera: SIGKILL directo.
_KILL_GRACE_SECONDS = 5.0


def _send_signal_group(proc: subprocess.Popen, sig) -> None:
    """Manda `sig` a `proc` y a todo su grupo de procesos.

    Los subprocesos de `wit` se lanzan con `start_new_session=True`, o sea
    en su propio grupo: señalar el grupo entero (`os.killpg`) y no solo el
    PID directo asegura que no quede ningún hijo de `wit` escribiendo en
    el destino después de cancelar. Si por algún motivo no se puede
    obtener el grupo, cae a señalar el proceso directamente."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid == os.getpgrp():
        # El proceso no tiene grupo propio: señalar "su" grupo sería
        # señalar a la app (un SIGSTOP la dejaría congelada).
        pgid = None

    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except OSError:
            pass  # el grupo ya no existe: probamos con el proceso solo
    try:
        proc.send_signal(sig)
    except OSError:
        pass


def _request_termination(proc: subprocess.Popen) -> None:
    """Mata `proc` (y su grupo) con SIGKILL y VUELVE EN EL ACTO.

    Esto lo llama el botón "Cancelar", o sea el hilo de GTK: cualquier
    espera acá congela la ventana entera.

    SIGKILL directo, sin pasar antes por SIGTERM. `wit` captura SIGTERM y
    lo toma como "terminá lo que estás copiando", no como "abortá":
    medido en una USB real, con un solo SIGTERM siguió escribiendo y
    renombró el temporal al nombre final, o sea que dio por buena una
    copia que el usuario había cancelado. Antes el SIGKILL llegaba 5
    segundos después desde un hilo suelto, y si ese hilo no llegaba a
    correr (la app se cerraba en esos 5 segundos) quedaba un `wit`
    huérfano completando la copia. Mandarlo acá, en el mismo llamado, no
    depende de ningún otro hilo."""
    if proc.poll() is not None:
        return
    _send_signal_group(proc, signal.SIGKILL)


# `prctl(PR_SET_PDEATHSIG, SIGKILL)`: que el kernel mate a `wit` si muere
# quien lo lanzó. Python no lo trae, así que va por ctypes; la función se
# busca en el proceso padre, ANTES del fork, para que el hijo solo tenga
# que llamarla.
_PR_SET_PDEATHSIG = 1
_prctl = None
_prctl_buscado = False


def _libc_prctl():
    global _prctl, _prctl_buscado
    if not _prctl_buscado:
        _prctl_buscado = True
        try:
            import ctypes
            funcion = ctypes.CDLL(None, use_errno=True).prctl
            funcion.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                                ctypes.c_ulong, ctypes.c_ulong)
            funcion.restype = ctypes.c_int
            _prctl = funcion
        except (OSError, AttributeError):
            _prctl = None
    return _prctl


def _pdeathsig_preexec() -> Optional[Callable[[], None]]:
    """El `preexec_fn` que deja a `wit` atado a la vida de quien lo lanza,
    o None si el sistema no tiene `prctl` (no es Linux).

    Ojo con el "quien": para el kernel, el padre de esta señal es el HILO
    que hizo el fork, no el proceso. Acá eso es lo que se quiere, porque
    `wit` siempre se lanza desde el hilo que después se queda esperándolo
    (`_run_with_progress` y `_run_cancellable` no vuelven hasta que `wit`
    termina): ese hilo no puede terminar antes que `wit` salvo que la app
    entera se esté cayendo, que es justo el caso que esto cubre. Lanzarlo
    desde un hilo que NO lo espera lo mataría en cuanto ese hilo termine.

    Si el proceso padre ya murió entre el fork y el `prctl` (la señal no
    llegaría nunca), el hijo sale sin ejecutar `wit`."""
    funcion = _libc_prctl()
    if funcion is None:
        return None
    padre = os.getpid()

    def _en_el_hijo() -> None:
        funcion(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
        if os.getppid() != padre:
            os._exit(1)

    return _en_el_hijo


def _popen_wit(args: list[str], **kwargs) -> subprocess.Popen:
    """Lanza `wit` en su propio grupo de procesos (para poder señalarlo
    entero, ver `_send_signal_group`) y atado a la vida del hilo que lo
    lanza (ver `_pdeathsig_preexec`): si la app muere de golpe, `wit` no
    sigue escribiendo en la unidad -ni queda detenido por
    `_WritebackLimiter`- sin nadie que lo espere."""
    return subprocess.Popen(args, start_new_session=True,
                            preexec_fn=_pdeathsig_preexec(), **kwargs)


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL al grupo y espera, bloqueante, a que termine: es cómo se
    corta un `wit` que ESCRIBE (copiar, convertir) por un timeout o una
    excepción, igual que al cancelar (`_request_termination`).

    Sin SIGTERM por delante: `wit` lo toma como "terminá el trabajo en
    curso" (medido: termina la copia, renombra el temporal al nombre
    final y sale con 110), así que la gracia eran 5 segundos más
    escribiendo en la unidad después de haber decidido cortar. La espera
    tiene tope: después del SIGKILL el kernel puede tardar en soltarlo
    mientras baja lo poco que el limitador dejó pendiente, y quien llama
    igual sigue."""
    if proc.poll() is not None:
        return
    _send_signal_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM, la gracia de `_KILL_GRACE_SECONDS` y después SIGKILL, todo
    bloqueante: es para los hilos de fondo que ya estaban esperando al
    proceso, cuando una operación de SOLO LECTURA (VERIFY, LIST, ISOSIZE)
    se cuelga y salta el timeout. Ahí terminar por las buenas no cuesta
    nada. Lo que escribe se corta con `_kill_process_group`."""
    if proc.poll() is not None:
        return
    _send_signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=_KILL_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    _send_signal_group(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass


class CancellationToken:
    """Puente entre el botón "Cancelar" (hilo de GTK) y la operación de
    `wit` que está corriendo en el hilo de fondo.

    Antes la cancelación era solo una bandera que el worker miraba ENTRE
    juegos: si `wit` llevaba 20 minutos copiando un archivo grande, el
    botón no hacía nada hasta que ese archivo terminara. Este token
    además guarda el proceso en curso y lo mata (a él y a su grupo) apenas
    se cancela.

    Es seguro usarlo desde dos hilos: todo el estado va bajo un lock."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._proc: Optional[subprocess.Popen] = None

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        """Marca la operación como cancelada y le pide al proceso en curso
        que termine. Se llama desde el hilo de GTK, así que no espera nada:
        ver `_request_termination`."""
        with self._lock:
            self._cancelled = True
            proc = self._proc
        if proc is not None:
            _request_termination(proc)

    def attach(self, proc: subprocess.Popen) -> bool:
        """Registra el proceso recién lanzado. Devuelve False (y lo mata en
        el acto) si la cancelación llegó justo antes de lanzarlo, para que
        no quede un `wit` huérfano corriendo por esa ventana de carrera."""
        with self._lock:
            self._proc = proc
            cancelled = self._cancelled
        if cancelled:
            # `attach` corre en el hilo de fondo, pero no hay motivo para
            # esperar acá tampoco: quien llama va a recoger el proceso.
            _request_termination(proc)
            return False
        return True

    def detach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if self._proc is proc:
                self._proc = None


# Tamaño de partición al dividir un WBFS para que quepa en FAT32.
#
# Confirmado corriendo `wit HELP COPY` (wit v3.05a r8638): `-z --split` sin
# tamaño explícito ya usa por defecto 4 GB **decimal** (4_000_000_000 bytes),
# no 4 GiB como se asumía antes en este proyecto (ver comentario corregido
# en library_ops.py). Verificado además con una copia real: un WBFS real de
# 7.1GB copiado con `wit COPY --overwrite --split` contra un FAT32 real
# (loop device formateado con `mkfs.vfat -F32`) dividió limpio en partes de
# ~4.0GB + ~3.1GB, sin colgarse.
#
# Lo pasamos explícito con `--split-size` (en vez de confiar en el default
# implícito de `--split`) por dos motivos: (1) no depende de que el default
# de `wit` siga siendo el mismo en otra versión/build, y (2) `--split-size`
# interpreta un número sin sufijo de unidad como GiB (no bytes ni GB), así
# que hace falta el sufijo 'c' (=1 byte) para no toparse con esa ambigüedad.
#
# 4_000_000_000 deja ~295 MB de margen bajo el límite duro real de FAT32
# (2**32 - 1 = 4_294_967_295 bytes).
FAT32_SPLIT_SIZE_BYTES = 4_000_000_000
_SPLIT_SIZE_ARG = f"{FAT32_SPLIT_SIZE_BYTES}c"

# Tiempo máximo (segundos) que esperamos a `wit` en las operaciones que NO
# escriben un destino que podamos medir (LIST/identificar/VERIFY): ahí no
# hay forma de distinguir "lento" de "colgado", así que queda un límite
# absoluto, generoso, como única red de seguridad.
DEFAULT_WIT_TIMEOUT = 1800.0

# Con qué returncode vuelve una corrida que se pasó del timeout. No es un
# código que devuelva `wit`: lo pone `_timeout_result` para que quien
# llama pueda distinguir "no terminó a tiempo" de "terminó y dijo que
# está mal", que son dos cosas muy distintas cuando lo que se está
# preguntando es si un archivo quedó bien copiado. El 124 es el que usa
# `timeout(1)` para lo mismo.
TIMEOUT_RETURNCODE = 124

# Para copiar/convertir sí podemos medir el progreso real (cuánto creció
# el archivo temporal, ver `estimate_bytes_written`), así que el límite es
# POR INACTIVIDAD, no absoluto: se reinicia cada vez que el destino crece.
#
# Un límite absoluto castigaba a la transferencia lenta pero sana, que es
# el caso normal de esta app: 7 GB a 5 MB/s sobre USB 2.0 o una SD lenta
# son ~25 minutos, y con un tope absoluto de 30 min se cortaba sola una
# copia que venía progresando perfecto. Lo que sí es señal real de cuelgue
# (visto en la práctica: proceso en estado D, ~0% CPU, el archivo destino
# dejó de crecer del todo) es que no avance NADA durante un buen rato.
#
# 10 minutos sin escribir un solo byte: bien por encima de cualquier pausa
# legítima (flush grande, medio con errores reintentando) y muy por debajo
# de lo que tardaría alguien en darse cuenta solo.
WIT_INACTIVITY_TIMEOUT = 600.0

# Red de seguridad final por si algo "progresa" para siempre sin terminar
# nunca (p. ej. un destino que crece de a poquito por un bug del medio).
# 4 horas: mucho más que la transferencia legítima más lenta imaginable
# (una unidad a 1 MB/s copiando un dual-layer de 8 GB serían ~2h15m).
WIT_ABSOLUTE_TIMEOUT = 4 * 60 * 60.0

# `wit ISOSIZE` es la excepción entre las operaciones sin progreso
# medible: no recorre el archivo, solo lee la estructura del disco
# (medido con juegos reales de 350 MB y de 7.3 GB: 0.02 s los dos). Con el
# timeout general de 30 minutos, un archivo corrupto o un medio que
# reintenta sin fin retenía la cola de transferencias todo ese rato antes
# de marcarse como error -y la cola pregunta el tamaño ANTES de cada
# copia, así que el resto de la tanda esperaba también.
#
# 90 segundos son ~4500 veces lo que tarda de verdad: sobra para un USB
# lento o con sectores que reintentan, y falla rápido cuando el archivo no
# tiene arreglo. Que se agote no rompe nada: `iso_size_bytes` devuelve
# None y quien llama estima el tamaño por otro lado.
ISOSIZE_TIMEOUT = 90.0


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def _log_command(args: list[str]) -> None:
    """Deja en stderr el comando `wit` exacto que se lanza, copiable tal
    cual a una terminal. Es lo primero que hace falta para diagnosticar
    una transferencia lenta o rara: qué flags se pasaron de verdad, y no
    los que uno cree que se pasaron."""
    print(f"[wiibackup-manager] wit: {shlex.join(args)}", file=sys.stderr)


def find_wit(binary_name: str = "wit") -> Optional[str]:
    return shutil.which(binary_name)


def is_available(binary_name: str = "wit") -> bool:
    return find_wit(binary_name) is not None


def _run(
    binary: str, *args: str, timeout: Optional[float] = DEFAULT_WIT_TIMEOUT
) -> subprocess.CompletedProcess:
    """Corre `wit` y espera el resultado. Ver `_run_cancellable`, que es la
    misma implementación: acá simplemente no hay token de cancelación."""
    return _run_cancellable(binary, *args, timeout=timeout, cancel=None)


def _timeout_result(
    args: list[str], exc: subprocess.TimeoutExpired, timeout: Optional[float]
) -> subprocess.CompletedProcess:
    """Convierte un `TimeoutExpired` en un resultado con returncode != 0 en
    vez de dejar que la excepción se propague: así todos los call sites que
    ya revisan `result.returncode` (o `verify()`'s `ok`) muestran el error
    en el toast como cualquier otro fallo de `wit`, en vez de quedarse
    colgados en silencio en el hilo de fondo que no tiene manejo de
    excepciones alrededor de la llamada."""
    return subprocess.CompletedProcess(
        args=args,
        returncode=TIMEOUT_RETURNCODE,
        stdout=exc.stdout or "",
        stderr=(exc.stderr or "")
        + "\n" + _("`wit` no respondió en {seconds:.0f}s: se lo dio por colgado "
                     "y se canceló la operación.").format(seconds=timeout),
    )


def _wbfs_temp_files(dest: Path):
    """Los archivos temporales que `wit COPY` usa mientras escribe `dest`.

    Confirmado por observación directa (copia real de un WBFS de 7.1GB
    contra un FAT32 real): mientras la copia está en curso, `wit` no
    escribe en `dest` ni en sus partes finales (`dest.wbf1`, `dest.wbf2`,
    ...) — escribe en archivos ocultos `.{nombre}.{random}.tmp` (primera
    parte) y `.{nombre}.{random}.tmp.1`, `.tmp.2`, ... (partes
    siguientes si divide) en el mismo directorio, y recién al terminar
    los renombra de golpe a los nombres finales. Por eso monitorear el
    tamaño de `dest` directamente no sirve para estimar progreso: se
    queda en 0 (no existe) hasta el instante final del rename."""
    try:
        return [f for f in dest.parent.glob(f".{dest.name}.*") if f.is_file()]
    except OSError:
        return []


def output_files(dest: Path) -> set:
    """Todos los archivos que una operación hacia `dest` puede estar
    escribiendo en este momento: los temporales de `wit` (ver
    `_wbfs_temp_files`), el archivo final y sus partes `.wbf1`, `.wbf2`,
    ... si se dividió."""
    files = set(_wbfs_temp_files(dest))
    try:
        if dest.exists():
            files.add(dest)
    except OSError:
        pass
    stem = dest.with_suffix("")
    part_num = 1
    while True:
        part = stem.with_suffix(f".wbf{part_num}")
        try:
            if not part.exists():
                break
        except OSError:
            break
        files.add(part)
        part_num += 1
    return files


def cleanup_new_output_files(dest: Path, before: set) -> None:
    """Borra lo que ESTA operación dejó a medio escribir hacia `dest`.

    Se compara contra el conjunto de archivos que ya existían antes de
    arrancar y solo se borran los nuevos: un `glob()` amplio podría
    llevarse por delante los temporales de otra operación en curso sobre
    el mismo destino, o el archivo que el usuario ya tenía ahí.

    Hay que mirar el archivo final y sus partes, no solo los temporales:
    al recibir SIGTERM, `wit` alcanza a veces a renombrar su temporal al
    nombre definitivo antes de salir (confirmado mandándole SIGTERM a una
    copia real a mitad de camino), y ese archivo parcial pasaría por un
    respaldo bueno en el próximo escaneo. Si el destino YA existía antes,
    no se toca: puede ser el archivo original del usuario, que `wit` deja
    intacto hasta el rename final."""
    for f in output_files(dest) - before:
        try:
            f.unlink()
        except OSError:
            pass


def estimate_bytes_written(dest: Path) -> int:
    """Estimación best-effort de cuánto lleva escrito hacia `dest` (copia
    directa o `wit COPY`, dividido o no), sumando tanto sus archivos
    temporales (mientras la operación está en curso, ver
    `_wbfs_temp_files`) como los archivos finales ya renombrados (`dest`
    mismo y sus partes `.wbf1`, `.wbf2`, ... si ya se dividió). No hace
    falta distinguir un caso del otro: en un momento dado solo uno de los
    dos existe, salvo un instante muy breve durante el rename, donde
    sumar ambos no rompe nada."""
    total = 0
    for f in output_files(dest):
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def _process_bytes_written(pid: int) -> Optional[int]:
    """Cuántos bytes le pasó el proceso `pid` a `write()` hasta ahora
    (`wchar` de /proc/<pid>/io), o None si no se pudo leer.

    Es mejor medida de avance que el tamaño del destino: con la reserva
    de espacio de `wit` (`--prealloc`, activa salvo en FAT32) el archivo
    temporal nace con su tamaño final, y una barra basada en `st_size`
    saltaba al 99% en el primer segundo y se quedaba ahí toda la copia."""
    try:
        with open(f"/proc/{pid}/io", encoding="ascii") as f:
            for linea in f:
                clave, _sep, valor = linea.partition(":")
                if clave == "wchar":
                    return int(valor)
    except (OSError, ValueError):
        pass
    return None


# ------------------------------------------- Caché sin bajar acotada --
# Cuánto se deja a `wit` adelantarse a la unidad, en segundos de copia a la
# velocidad que la unidad viene mostrando. Es, casi exacto, lo que tarda
# en morir un `wit` cancelado: el kernel no lo deja salir hasta bajar lo
# que tenga en la caché de sus archivos (medido en una USB FAT32 de ~14
# MB/s: con ~1 GB en caché, 86 s; con la caché acotada a 16 MB, 0.9 s).
WRITEBACK_TARGET_SECONDS = 1.0
# Piso y techo de esa ventana. El piso evita esperas diminutas al
# arrancar (todavía sin velocidad medida) o en una unidad lentísima; el
# techo, que un disco rápido junte cientos de MB en la RAM.
WRITEBACK_MIN_WINDOW = 4 * 1024 * 1024
WRITEBACK_MAX_WINDOW = 256 * 1024 * 1024
# Cada cuánto se mira si `wit` se pasó de la ventana. `wit` escribe en la
# caché a velocidad de RAM, así que lo que se pasa entre dos miradas es
# este intervalo por esa velocidad: con 10 ms se medía ~12 MB de más.
_WRITEBACK_GATE_INTERVAL = 0.002
# Cuánto se espera a los hilos del limitador al cortar una copia (cancelar,
# fallo, timeout). Los dos son daemon y sus archivos están abiertos solo
# para leer: dejarlos terminar solos no arriesga nada.
_LIMITER_JOIN_ON_ABORT = 0.2


def _is_wit_temp(path: Path, dest: Path) -> bool:
    """`path` tiene la forma de un temporal de `wit` para `dest`
    (`.{nombre}.{al azar}.tmp`, `.tmp.1`, ...; ver `_wbfs_temp_files`).
    El respaldo de `DestinationGuard` (`.{nombre}.respaldo-<pid>`) cae en
    el mismo glob, pero no tiene esta forma."""
    return re.fullmatch(rf"\.{re.escape(dest.name)}\.[^.]+\.tmp(\.\d+)?",
                        path.name) is not None


class _WritebackLimiter:
    """Que `wit` no deje más de ~1 s de copia en la caché sin bajar.

    `wit` escribe en la caché del kernel a velocidad de RAM y la unidad la
    baja a su ritmo: en una USB lenta se juntaba ~1 GB, y como un proceso
    no termina de morir hasta que se baja lo que tiene pendiente, cancelar
    tardaba minuto y medio aunque el SIGKILL saliera en el acto. Este
    limitador hace dos cosas, cada una en su hilo:

    - baja lo escrito (`sync_file_range` sobre los temporales de ESTA
      operación) y mide a qué velocidad lo acepta la unidad;
    - si `wit` se adelanta más de la ventana (~1 s a esa velocidad, ver
      `WRITEBACK_TARGET_SECONDS`), lo detiene con SIGSTOP hasta que lo
      pendiente baje a la mitad, y lo reanuda con SIGCONT.

    La copia no se hace más lenta: la unidad sigue siendo el cuello de
    botella, y el tiempo que `wit` pasa detenido es el que antes pasaba
    bloqueado esperando a la unidad (medido: 273 s hasta el 50% con la
    ventana, 275-278 s sin ella).

    Solo toca los temporales nuevos de `wit` (`_is_wit_temp` y fuera de
    `protected`, la foto de lo que ya había): nunca el respaldo de
    `DestinationGuard` ni un archivo de otra operación. Los abre solo para
    leer, así que cerrarlos no dispara ninguna escritura.

    `stop()` reanuda a `wit` si quedó detenido, y lo mismo pasa si
    cualquiera de los dos hilos falla: nunca queda un `wit` detenido por
    culpa de esto. `sync` y `clock` se pueden reemplazar en las pruebas."""

    def __init__(self, proc: subprocess.Popen, dest: Path, protected: set,
                 sync: Optional[Callable[[int, int, int, int], None]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 target_seconds: float = WRITEBACK_TARGET_SECONDS,
                 min_window: int = WRITEBACK_MIN_WINDOW,
                 max_window: int = WRITEBACK_MAX_WINDOW,
                 interval: float = _WRITEBACK_GATE_INTERVAL):
        self.proc = proc
        self.dest = Path(dest)
        self.protected = set(protected)
        self._sync = sync or fsutil.sync_range
        self._clock = clock
        self.target_seconds = target_seconds
        self.min_window = min_window
        self.max_window = max_window
        self.interval = interval
        self.rate: Optional[float] = None   # bytes/s que acepta la unidad
        self.flushed = 0                    # de lo escrito, ya bajado
        self.max_pending = 0
        self.pauses = 0
        self._paused = False
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    # -- medidas -------------------------------------------------------
    @property
    def window(self) -> int:
        if self.rate is None:
            return self.min_window
        return int(min(max(self.rate * self.target_seconds, self.min_window),
                       self.max_window))

    def temp_files(self) -> list[Path]:
        return sorted((f for f in _wbfs_temp_files(self.dest)
                       if f not in self.protected and _is_wit_temp(f, self.dest)),
                      key=lambda f: f.name)

    def written(self) -> int:
        """Lo que `wit` lleva escrito: `wchar` si el kernel lo cuenta (con
        la reserva de espacio el temporal nace con su tamaño final, y el
        tamaño no dice nada), si no la suma de los temporales."""
        escrito = _process_bytes_written(self.proc.pid)
        if escrito is not None:
            return escrito
        total = 0
        for f in self.temp_files():
            try:
                total += f.stat().st_size
            except OSError:
                pass
        return total

    @property
    def paused(self) -> bool:
        return self._paused

    # -- ciclo de vida -------------------------------------------------
    def start(self) -> None:
        for destino, nombre in ((self._gate, "wit-writeback-gate"),
                                (self._syncer, "wit-writeback-sync")):
            hilo = threading.Thread(target=destino, name=nombre, daemon=True)
            self._threads.append(hilo)
            hilo.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Deja de limitar y reanuda a `wit` si estaba detenido. Se puede
        llamar más de una vez. No espera más de `timeout` a los hilos: el
        que baja datos puede estar esperando a la unidad, y para entonces
        ya no puede detener a nadie."""
        self._stop.set()
        self._resume()
        for hilo in self._threads:
            if hilo is not threading.current_thread():
                hilo.join(timeout)

    # -- pausa ---------------------------------------------------------
    def _pause(self) -> None:
        with self._lock:
            if self._paused or self._stop.is_set() or self.proc.returncode is not None:
                return
            _send_signal_group(self.proc, signal.SIGSTOP)
            self._paused = True
            self.pauses += 1

    def _resume(self) -> None:
        with self._lock:
            if not self._paused:
                return
            self._paused = False
            if self.proc.returncode is None:
                _send_signal_group(self.proc, signal.SIGCONT)

    def _gate(self) -> None:
        # Lo más que `wit` llegó a escribir entre dos miradas. Se lo
        # detiene cuando la PRÓXIMA mirada ya llegaría tarde, no cuando ya
        # se pasó de la ventana, y se lo reanuda solo si entra otra ráfaga
        # entera. Decae de a poco, pero solo mientras `wit` corre: detenido
        # no escribe nada, y olvidarla ahí era soltarlo justo antes de que
        # vuelva a escribir a velocidad de RAM.
        rafaga = 0.0
        anterior = self.written()
        try:
            while not self._stop.is_set():
                escrito = self.written()
                delta = escrito - anterior
                anterior = escrito
                if delta > rafaga:
                    rafaga = delta
                elif not self._paused:
                    rafaga *= 0.99
                pendiente = escrito - self.flushed
                self.max_pending = max(self.max_pending, pendiente)
                ventana = self.window
                if self._paused:
                    # Si la ráfaga sola ya no entra en la ventana, se lo
                    # suelta igual cuando casi no queda nada pendiente:
                    # si no, no volvería a escribir nunca.
                    if (pendiente + rafaga <= ventana * 3 // 4
                            or pendiente <= ventana // 8):
                        self._resume()
                elif pendiente + rafaga >= ventana:
                    self._pause()
                self._stop.wait(self.interval)
        finally:
            self._stop.set()
            self._resume()

    # -- bajada --------------------------------------------------------
    def _syncer(self) -> None:
        fds: dict = {}
        # Lo escrito al TERMINAR la bajada anterior: esa bajada pudo
        # llevarse también lo que se escribió mientras duraba, pero nada
        # posterior. Es la base para medir la velocidad por lo bajo.
        escrito_al_terminar = 0
        try:
            while not self._stop.is_set():
                marca = self.written()
                pendiente = marca - self.flushed
                # Con `wit` detenido se baja lo que haya, por poco que sea:
                # es lo único que lo puede soltar.
                minimo = 1 if self._paused else max(self.window // 4, 1)
                if pendiente < minimo:
                    self._stop.wait(self.interval * 5)
                    continue
                inicio = self._clock()
                bajados = 0
                for f in self.temp_files():
                    fd = fds.get(f)
                    if fd is None:
                        try:
                            fd = fds[f] = os.open(f, os.O_RDONLY)
                        except FileNotFoundError:
                            continue   # `wit` ya lo renombró
                    self._sync(fd, 0, 0, fsutil._SYNC_AND_WAIT)
                    bajados += 1
                    try:
                        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    except (OSError, AttributeError):
                        pass
                duracion = self._clock() - inicio
                # Seguro bajó, en esta vuelta, lo escrito entre el final
                # de la anterior y `marca`; quizás más, nunca menos. Medida
                # así, la velocidad queda por debajo de la real y la
                # ventana del lado seguro: contar `pendiente` entero la
                # inflaba (una USB de 5 MB/s llegó a medirse a 30 MB/s).
                seguro = marca - escrito_al_terminar
                if bajados and duracion > 0 and seguro >= 256 * 1024:
                    medida = seguro / duracion
                    self.rate = (medida if self.rate is None
                                 else 0.7 * self.rate + 0.3 * medida)
                self.flushed = marca
                escrito_al_terminar = self.written()
        except OSError:
            # La unidad no acepta lo que se le pide (desconectada, error
            # de E/S): no hay nada que acotar. `wit` va a chocar con el
            # mismo error por su cuenta y lo informa quien lo espera.
            pass
        finally:
            self._stop.set()
            self._resume()
            for fd in fds.values():
                os.close(fd)


def _run_with_progress(
    args: list[str],
    dest: Path,
    bytes_progress_cb: Callable[[int], None],
    cancel: Optional[CancellationToken] = None,
    inactivity_timeout: Optional[float] = WIT_INACTIVITY_TIMEOUT,
    absolute_timeout: Optional[float] = WIT_ABSOLUTE_TIMEOUT,
    cleanup_on_abort: bool = True,
) -> subprocess.CompletedProcess:
    """Como `subprocess.run`, pero con `Popen` en vez de `.run()` para
    poder sondear cada 1s cuánto lleva escrito `wit`
    (`_process_bytes_written`, o `estimate_bytes_written` si el kernel no
    lo cuenta) mientras el proceso sigue corriendo, en vez
    de bloquear sin ninguna señal intermedia hasta que termina.

    stdout/stderr van a archivos temporales (no a `PIPE`): si se
    capturaran con `PIPE` y nadie los lee mientras este bucle sondea,
    `wit` podría bloquearse al llenar el buffer del pipe del kernel (típ.
    64KiB) si llega a tirar mucha salida; con archivos no hay ese techo.

    `start_new_session=True` pone a `wit` en su propio grupo de procesos
    para que `cancel.cancel()` (desde el botón "Cancelar", en el hilo de
    GTK) pueda matarlo de verdad en el acto, en vez de que la cancelación
    recién surta efecto cuando el archivo grande en curso termine solo.

    El mismo sondeo que reporta progreso sirve para detectar un cuelgue:
    `inactivity_timeout` se reinicia cada vez que `wit` escribe algo, así
    que una transferencia lenta pero sana nunca se corta sola (ver el
    comentario de `WIT_INACTIVITY_TIMEOUT`); `absolute_timeout` queda
    detrás como última red de seguridad.

    Con `cleanup_on_abort=False`, lo que `wit` deja a medio escribir al
    cancelar, fallar o vencer el timeout NO se borra acá: queda para quien
    llama (`library_ops.DestinationGuard`, que primero devuelve el
    original a su lugar y recién después barre)."""
    # Lo que ya existía antes de arrancar: si hay que limpiar por una
    # cancelación, se borra solo lo que agregó ESTA operación.
    outputs_before = output_files(dest)
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        proc = _popen_wit(args, stdout=out_f, stderr=err_f)
        # Si la cancelación llegó entre el chequeo previo y el Popen,
        # `attach` lo mata en el acto y devuelve False.
        running = cancel.attach(proc) if cancel is not None else True
        # Que `wit` no se adelante a la unidad más de ~1 s: es lo que hace
        # que cancelar corte en el acto (ver `_WritebackLimiter`). Solo
        # sobre los temporales nuevos: `outputs_before` incluye el
        # respaldo que haya apartado `DestinationGuard`.
        limiter = _WritebackLimiter(proc, dest, outputs_before)
        if running:
            limiter.start()

        def medir() -> int:
            # Lo que escribió `wit`, si el kernel lo cuenta; si no, el
            # tamaño del destino (ver `_process_bytes_written`).
            escrito = _process_bytes_written(proc.pid)
            return escrito if escrito is not None else estimate_bytes_written(dest)

        start = time.monotonic()
        # Último avance real: arranca en lo que ya había escrito (el
        # destino puede existir de antes) y se actualiza solo cuando crece.
        last_bytes = medir()
        last_progress_at = start
        timeout_reason: Optional[str] = None
        try:
            while running:
                try:
                    proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    written = medir()
                    bytes_progress_cb(written)
                    now = time.monotonic()
                    if written > last_bytes:
                        # Sigue escribiendo: el reloj del cuelgue vuelve a cero
                        # por lento que vaya.
                        last_bytes = written
                        last_progress_at = now
                    if (inactivity_timeout is not None
                            and (now - last_progress_at) >= inactivity_timeout):
                        timeout_reason = _(
                            "`wit` no escribió un solo byte en {minutes:.0f} "
                            "minutos: se lo dio por colgado y se canceló la "
                            "operación."
                        ).format(minutes=inactivity_timeout / 60)
                    elif (absolute_timeout is not None
                            and (now - start) >= absolute_timeout):
                        timeout_reason = _(
                            "`wit` lleva más de {hours:.0f} horas sin terminar: "
                            "se canceló la operación."
                        ).format(hours=absolute_timeout / 3600)
                    if timeout_reason is not None:
                        limiter.stop(timeout=_LIMITER_JOIN_ON_ABORT)
                        _kill_process_group(proc)
                        break
            if not running:
                proc.wait()
        except BaseException:
            limiter.stop(timeout=_LIMITER_JOIN_ON_ABORT)
            _kill_process_group(proc)
            if cleanup_on_abort:
                cleanup_new_output_files(dest, outputs_before)
            raise
        finally:
            # Pase lo que pase, `wit` no queda detenido. Si se canceló no
            # se espera a los hilos del limitador más que un instante: el
            # que baja datos puede estar esperando a la unidad, ya no
            # detiene a nadie, y cada segundo acá es un segundo más con el
            # original del cliente apartado (ver `DestinationGuard`).
            abortado = ((cancel is not None and cancel.cancelled)
                        or timeout_reason is not None)
            limiter.stop(timeout=_LIMITER_JOIN_ON_ABORT if abortado else 2.0)
            if cancel is not None:
                cancel.detach(proc)

        cancelled = cancel is not None and cancel.cancelled
        if (cancelled or timeout_reason is not None) and cleanup_on_abort:
            # El proceso murió a mitad de una escritura: los temporales que
            # dejó no los va a renombrar ni limpiar nadie.
            cleanup_new_output_files(dest, outputs_before)

        out_f.seek(0)
        err_f.seek(0)
        stdout = out_f.read().decode("utf-8", "replace")
        stderr = err_f.read().decode("utf-8", "replace")
        if timeout_reason is not None:
            stderr += "\n" + timeout_reason
        return subprocess.CompletedProcess(
            args=args,
            returncode=1 if (timeout_reason is not None or cancelled) else proc.returncode,
            stdout=stdout,
            stderr=stderr,
        )


def console_for_id(game_id: str) -> str:
    """"wii" o "gc" según el primer carácter del Game ID. Es SOLO un
    respaldo, y no es confiable.

    La mayoría de los GameCube arrancan con 'G' (ej. "GZ2E01"), pero no
    todos: discos especiales y de promoción usan otras letras (ej.
    "D43E01", Ocarina of Time / Master Quest), y esta función los da por
    Wii. La fuente de verdad es el disco: el magic word para ISO plana
    (`disc_header.read_plain_iso_header`) y `disctype=` de
    `wit LIST --sections` para lo demás (`_parse_list_sections`). Esto
    queda para cuando `wit` no trae esa línea, y para
    `list_wbfs_container`."""
    return "gc" if game_id[:1].upper() == "G" else "wii"


def _find_id6_line(output: str) -> Optional[tuple[str, str]]:
    """Busca, entre las líneas de salida de `wit LIST`, la fila de datos de
    un disco y devuelve (game_id, title).

    No podemos asumir que esa fila esté en un índice fijo: `wit LIST`
    antepone líneas de encabezado y separadores (p. ej. "ID6  MiB Reg. …",
    "----…") que varían de una build a otra. En cambio, reconocemos la fila
    de datos por su forma: empieza con un ID6 real (6 caracteres A-Z/0-9,
    ver `disc_header.is_valid_game_id`), seguido de tamaño y región, y el
    resto de la línea es el título del juego.
    """
    for raw_line in output.splitlines():
        line = _strip_ansi(raw_line).strip()
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        game_id = parts[0]
        # `is_valid_game_id` en vez de `isalnum()`: este ID termina
        # formando parte de rutas del filesystem, ver disc_header.
        if not is_valid_game_id(game_id):
            continue
        title = parts[3].strip()
        if not title:
            continue
        return validate_game_id(game_id), title
    return None


def _first_disc_fields(output: str) -> dict[str, str]:
    """Los pares `clave=valor` del primer `[disc-N]` de
    `wit LIST --sections` (vacío si no hay ninguno)."""
    campos: dict[str, str] = {}
    en_disco = False
    for raw_line in output.splitlines():
        line = _strip_ansi(raw_line).strip()
        if line.startswith("["):
            if en_disco:
                break  # ya se leyó el primer disco entero
            en_disco = line.startswith("[disc-")
            continue
        if en_disco and "=" in line:
            clave, valor = line.split("=", 1)
            campos[clave.strip()] = valor.strip()
    return campos


def _console_from_disctype(disctype: str) -> Optional[str]:
    """"gc"/"wii" según el `disctype=` de `wit` ("1 GameCube" / "2 Wii"),
    o None si no dice ninguna de las dos."""
    disctype = disctype.lower()
    if "gamecube" in disctype:
        return "gc"
    if "wii" in disctype:
        return "wii"
    return None


def _parse_list_sections(output: str) -> Optional[tuple[str, str, str]]:
    """Del primer `[disc-N]` de `wit LIST --sections`, devuelve
    (game_id, title, console), o None si no hay un disco con ID6 válido.

    La consola sale de `disctype=` ("1 GameCube" / "2 Wii"), que es lo que
    `wit` leyó del disco de verdad. Comprobado contra `wit` v3.05a con un
    GameCube metido en un WBFS (`disctype=1 GameCube`) y un Wii
    (`disctype=2 Wii`). Solo si falta esa línea se cae a `console_for_id`."""
    campos = _first_disc_fields(output)
    game_id = campos.get("id", "")
    # `is_valid_game_id`: este ID termina formando parte de rutas del
    # filesystem, ver disc_header.
    if not is_valid_game_id(game_id):
        return None
    game_id = validate_game_id(game_id)
    title = campos.get("title") or campos.get("name") or game_id

    console = (_console_from_disctype(campos.get("disctype", ""))
               or console_for_id(game_id))
    return game_id, title, console


def disc_console(path: Path, binary: str = "wit") -> Optional[str]:
    """"gc"/"wii" según el `disctype=` que `wit` lee de `path`, o None si
    no se pudo saber (sin `wit`, `wit` falló, o no trae esa línea).

    A diferencia de `identify`, NO cae al prefijo del ID: lo usa la cola
    como segundo control antes de `wit VERIFY` (que con un GameCube dentro
    de un WBFS sale con 0 sin revisar nada), y ahí una respuesta adivinada
    no sirve. None quiere decir "no sé", no "es Wii"."""
    if not find_wit(binary):
        return None
    result = _run(binary, "LIST", "--sections", str(path))
    if result.returncode != 0:
        return None
    return _console_from_disctype(
        _first_disc_fields(result.stdout).get("disctype", ""))


def identify(path: Path, binary: str = "wit") -> Optional[DiscInfo]:
    """Usa `wit LIST --sections` para identificar un juego (WBFS, CISO,
    WDF, o una ISO que el parseo directo no reconoció).

    `--sections` y no `--long`: el formato `clave=valor` no depende de si
    hay terminal, y es el único que trae el tipo de disco (`disctype=`).
    Con `--long` la consola había que adivinarla por el primer carácter
    del ID, y eso clasificaba como Wii a GameCube cuyo ID no empieza con
    'G' -p. ej. D43E01, Ocarina of Time / Master Quest-, que terminaban
    copiados como WBFS en `wbfs/` y "verificados" por un `wit VERIFY` que
    no revisó nada (ver `verify_result`)."""
    if not find_wit(binary):
        raise WitNotFoundError(binary)

    result = _run(binary, "LIST", "--sections", str(path))
    if result.returncode != 0 or not result.stdout.strip():
        return None

    found = _parse_list_sections(result.stdout)
    if found is None:
        return None
    game_id, title, console = found
    return DiscInfo(game_id=game_id, title=title, source="wit", console=console)


def convert(
    src: Path,
    dest: Path,
    target_format: str,
    binary: str = "wit",
    progress_cb: Optional[Callable[[str], None]] = None,
    split: bool = False,
    bytes_progress_cb: Optional[Callable[[int], None]] = None,
    cancel: Optional[CancellationToken] = None,
    inactivity_timeout: Optional[float] = WIT_INACTIVITY_TIMEOUT,
    absolute_timeout: Optional[float] = WIT_ABSOLUTE_TIMEOUT,
    overwrite: bool = False,
    scrub_update: bool = True,
    prealloc: bool = True,
    cleanup_on_abort: bool = True,
) -> subprocess.CompletedProcess:
    """Convierte src -> dest. target_format: 'WBFS' o 'ISO'.

    `bytes_progress_cb`, si se pasa, se llama aproximadamente cada 1s
    (desde este mismo hilo, bloqueante) con cuántos bytes lleva escritos
    `wit` (ver `_process_bytes_written`),
    para poder mostrar progreso real dentro de la conversión de un solo
    archivo grande y no solo saltar de 0% a 100% al terminar. No hay forma
    confiable de leer el progreso real de `wit` (no expone una opción de
    progreso parseable en `wit HELP COPY`), así que esto es lo que el
    kernel le contó escribir, no un progreso exacto reportado por la
    herramienta.

    `split=True` agrega `--split-size` con `FAT32_SPLIT_SIZE_BYTES`
    (división en partes de 4GB, ver comentario junto a esa constante),
    necesario para destinos en FAT32, que no admite archivos más grandes y
    con el que hay discos Wii dual-layer que no entran enteros. `wit` solo
    genera varias partes cuando el resultado realmente supera ese límite,
    así que pasar `split=True` "por las dudas" en un filesystem que sí
    soporta archivos grandes no tiene costo: el archivo sale igual,
    entero.

    `overwrite=True` le pasa `--overwrite` a `wit`, o sea que si el destino
    ya existe lo reemplaza sin vuelta atrás. El default es False a
    propósito: antes `--overwrite` iba SIEMPRE, de forma incondicional, y
    eso funcionaba solo porque todos los que llaman hoy se ocupan del
    destino existente por su cuenta (apartándolo con un
    `library_ops.DestinationGuard`, o directamente salteando el archivo). Era
    una trampa esperando a que alguien llamara a `convert()` sin ese
    cuidado y perdiera un juego sin enterarse; con el default en False, el
    que quiera pisar tiene que decirlo.

    `cancel`, si se pasa, permite matar el `wit` en curso desde otro hilo
    (el botón "Cancelar" de la interfaz): en ese caso se levanta
    `OperationCancelled` y se limpian los temporales que quedaron a medio
    escribir, en vez de devolver un resultado con error.

    Se da por colgado a `wit` cuando pasa `inactivity_timeout` sin que el
    destino crezca ni un byte, no por tardar mucho: una copia lenta pero
    sana sigue adelante (ver `WIT_INACTIVITY_TIMEOUT`). `absolute_timeout`
    queda como última red de seguridad.

    `scrub_update=True` (default) agrega `--psel=-UPDATE`: descarta la
    partición de actualización del disco de origen, que USB Loader
    GX/Nintendont no usan para nada y que en algunos juegos pesa varios
    cientos de MB. Es la opción "Optimizar espacio (Scrubbing)" de
    Ajustes -ver `config.Settings.scrub_update`-; con `scrub_update=False`
    el WBFS resultante queda idéntico al disco original, actualizable
    desde el propio juego.

    `prealloc=False` agrega `--prealloc=OFF`. Por defecto `wit` reserva
    el archivo destino entero antes de copiar (`posix_fallocate`), y en
    FAT32 eso no reserva nada: escribe ceros. Medido en una USB real: 1 GB
    de reserva tardó 74 s (14.7 MB/s), o sea que cada byte se escribía dos
    veces. Quien llama decide según el filesystem del destino (ver
    `drives.is_fat_filesystem`); sin la reserva `wit` ya no falla temprano
    por falta de espacio, así que el espacio libre lo tiene que mirar
    quien llama ANTES de copiar.

    `cleanup_on_abort=False` deja los temporales de una copia cortada
    para quien llama (ver `_run_with_progress`)."""
    if not find_wit(binary):
        raise WitNotFoundError(binary)

    if cancel is not None and cancel.cancelled:
        raise OperationCancelled(_("Operación cancelada antes de arrancar `wit`."))

    # wit infiere el formato de salida por la extensión de --dest, así que
    # nos aseguramos de que dest tenga la extensión correcta antes de llamar.
    args = [binary, "COPY"]
    if overwrite:
        args.append("--overwrite")
    if split:
        args += ["--split-size", _SPLIT_SIZE_ARG]
    if scrub_update:
        # `--psel` (selector de particiones) y NO `--rm`: wit acepta
        # abreviaturas de opciones largas y expande `--rm` a `--rm-files`,
        # un filtro de ARCHIVOS que exige reglas con prefijo +/-/: y corta
        # con "ERROR #108 ... => UPDATE" antes de copiar nada. Con una sola
        # regla DENY, `--psel` habilita todas las demás particiones (datos
        # y canales). Va pegado con `=` para que el "-" de la regla no se
        # lea como otra opción.
        args.append("--psel=-UPDATE")
    if not prealloc:
        args.append("--prealloc=OFF")
    args += [str(src), "--dest", str(dest)]
    _log_command(args)

    # UN SOLO camino de ejecución, haya o no callbacks. Antes, sin progreso
    # ni cancelación se caía en un `subprocess.run` con timeout, que al
    # vencer:
    #
    # - le manda la señal SOLO al hijo directo. `wit` corre con
    #   `start_new_session=True`, o sea en su propio grupo, así que
    #   cualquier nieto quedaba vivo escribiendo en el destino después de
    #   que la app ya había dado la operación por terminada (comprobado con
    #   un proceso de prueba que deja un nieto escribiendo: sobrevivía);
    # - no limpiaba los temporales a medio escribir. Una conversión real de
    #   7.1 GB cortada por timeout dejaba un `.salida.iso.XXXX.tmp` de
    #   8.1 GB huérfano ocupando el disco;
    # - solo podía aplicar el límite absoluto, no el de inactividad.
    #
    # `_run_with_progress` ya resuelve las tres cosas, y el sondeo del
    # destino que necesita para medir inactividad no depende de que el que
    # llama quiera progreso: cuando no lo quiere, recibe un callback no-op.
    result = _run_with_progress(
        args, dest, bytes_progress_cb or (lambda _n: None), cancel,
        inactivity_timeout=inactivity_timeout,
        absolute_timeout=absolute_timeout,
        cleanup_on_abort=cleanup_on_abort,
    )

    if cancel is not None and cancel.cancelled:
        raise OperationCancelled("Transferencia cancelada por el usuario.")

    if progress_cb:
        progress_cb(result.stdout)
    return result


def _run_cancellable(
    binary: str, *args: str, timeout: Optional[float] = DEFAULT_WIT_TIMEOUT,
    cancel: Optional[CancellationToken] = None,
) -> subprocess.CompletedProcess:
    """Corre `wit` esperando el resultado, con dos garantías que
    `subprocess.run` no da:

    - si se pasa un `cancel`, el proceso queda registrado en el token para
      poder matarlo desde el botón "Cancelar" (subprocess.run no deja
      llegar al proceso mientras corre, así que cancelar un lote de
      verificación tenía que esperar a que `wit` terminara con el juego
      en curso: en un dual-layer, varios minutos);
    - si salta el timeout, se mata el GRUPO de procesos entero y no solo
      el proceso directo. `wit` se lanza con `start_new_session=True`, o
      sea en su propio grupo, y `subprocess.run` solo le manda la señal al
      hijo directo: cualquier nieto quedaba vivo, posiblemente escribiendo
      todavía en el destino."""
    if cancel is not None and cancel.cancelled:
        raise OperationCancelled(_("Operación cancelada antes de arrancar `wit`."))

    proc = _popen_wit(
        [binary, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if cancel is not None and not cancel.attach(proc):
        # La cancelación llegó entre el chequeo y el Popen: `attach` ya lo
        # mató, solo queda recogerlo para no dejar un zombi.
        proc.wait()
        raise OperationCancelled(_("Operación cancelada por el usuario."))
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # Mata al proceso Y a su grupo, y recién después recoge lo que
        # haya alcanzado a escribir.
        _terminate_process_group(proc)
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
        exc.stdout = exc.stdout or stdout
        exc.stderr = exc.stderr or stderr
        return _timeout_result([binary, *args], exc, timeout)
    finally:
        if cancel is not None:
            cancel.detach(proc)

    if cancel is not None and cancel.cancelled:
        raise OperationCancelled(_("Operación cancelada por el usuario."))
    return subprocess.CompletedProcess(
        args=[binary, *args], returncode=proc.returncode,
        stdout=stdout, stderr=stderr,
    )


@dataclass(frozen=True)
class VerifyResult:
    """Cómo salió un `wit VERIFY`, con el detalle que hace falta para
    decidir qué decirle al usuario.

    `ok` a secas no alcanza cuando lo que se está preguntando es si un
    archivo recién copiado quedó bien: "`wit` dice que la imagen está
    mal" y "`wit` no llegó a terminar de mirarla" son dos respuestas
    distintas, y tratarlas igual sería acusar de corrupto a un archivo
    que quizás está perfecto. Por eso `timed_out` viene aparte y no
    escondido adentro de `ok=False`."""

    ok: bool
    timed_out: bool
    output: str


def verify_result(
    path: Path, binary: str = "wit", timeout: Optional[float] = DEFAULT_WIT_TIMEOUT,
    cancel: Optional[CancellationToken] = None,
) -> VerifyResult:
    """Verifica la integridad de una imagen con `wit VERIFY`.

    OJO -comprobado contra `wit` v3.05a, no deducido-: **VERIFY es solo
    para imágenes de Wii**. Con una de GameCube contesta
    `ERROR #30 [WRONG FILE TYPE] ... Wii ISO image expected` y sale con
    returncode 4, o sea que se vería igual que "este archivo está
    corrupto". Y con un GameCube metido en un WBFS es PEOR: contesta
    `+OK` con returncode 0 al instante, sin haber leído nada (no hay
    particiones cifradas con hashes que revisar). Quien llame tiene que
    filtrar GameCube ANTES; acá no se adivina la consola a partir de la
    ruta.

    Para un WBFS dividido alcanza con pasar la PRIMERA parte (`.wbfs`):
    `wit` sigue solo la cadena `.wbf1`, `.wbf2`… Comprobado partiendo un
    WBFS válido a mano: con la continuación al lado da returncode 0, y
    con el mismo contenido pero sin ella sale con returncode 47
    (`READ FILE FAILED`). O sea que verificar la primera parte NO es un
    falso positivo: si falta una pieza, se entera.

    `cancel`, si se pasa, permite matar el `wit` en curso desde el hilo de
    GTK: en ese caso levanta `OperationCancelled` en vez de devolver un
    resultado."""
    if not find_wit(binary):
        raise WitNotFoundError(binary)
    _log_command([binary, "VERIFY", "--long", str(path)])
    result = _run_cancellable(binary, "VERIFY", "--long", str(path),
                               timeout=timeout, cancel=cancel)
    return VerifyResult(
        ok=result.returncode == 0,
        timed_out=result.returncode == TIMEOUT_RETURNCODE,
        output=(result.stdout + result.stderr).strip(),
    )


def verify(
    path: Path, binary: str = "wit", timeout: Optional[float] = DEFAULT_WIT_TIMEOUT,
    cancel: Optional[CancellationToken] = None,
) -> tuple[bool, str]:
    """`verify_result` para quien solo necesita el sí/no y el texto.

    Es lo que usa la Biblioteca para "Verificar integridad", donde un
    timeout y una imagen mala se muestran igual (un toast con el motivo).
    La cola de transferencias usa `verify_result`, que sí los distingue."""
    result = verify_result(path, binary, timeout=timeout, cancel=cancel)
    return result.ok, result.output


_ISOSIZE_LINE_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+\S")


def iso_size_bytes(path: Path, binary: str = "wit") -> Optional[int]:
    """Cuánto ocupa el juego de `path` como datos reales de disco, o None
    si no se pudo averiguar.

    Es la respuesta a "¿cuánto va a pesar esto una vez pasado a WBFS?",
    que NO se puede deducir del tamaño del archivo cuando el origen es
    CISO o WDF: esos formatos guardan el disco de forma compacta, así que
    su tamaño en disco puede ser bastante menor que el WBFS resultante.

    `wit ISOSIZE --long` lee la estructura del disco (no el archivo
    entero): medido con juegos reales de 350 MB y 7.3 GB, tarda 0.02s.
    Por eso corre con `ISOSIZE_TIMEOUT` y no con el timeout general (ver
    el comentario de esa constante).
    La salida trae una línea por juego con bloques y MiB; se suman los
    MiB, que cubre también el caso de un WBFS multi-juego."""
    if not find_wit(binary):
        return None
    result = _run(binary, "ISOSIZE", "--long", str(path),
                  timeout=ISOSIZE_TIMEOUT)
    if result.returncode != 0:
        return None
    total_mib = 0
    encontrado = False
    for line in result.stdout.splitlines():
        m = _ISOSIZE_LINE_RE.match(_strip_ansi(line))
        if m:
            total_mib += int(m.group(2))
            encontrado = True
    if not encontrado:
        return None
    return total_mib * 1024 * 1024


def list_wbfs_container(path: Path, binary: str = "wit") -> list[DiscInfo]:
    """Lista todos los juegos dentro de un contenedor WBFS multi-juego."""
    if not find_wit(binary):
        raise WitNotFoundError(binary)
    result = _run(binary, "LIST", "--long", str(path))
    games: list[DiscInfo] = []
    if result.returncode != 0:
        return games
    for line in result.stdout.splitlines():
        line = _strip_ansi(line).strip()
        if not line or line.startswith("*") or line.startswith("-"):
            continue
        # Mismo patrón que _find_id6_line/identify(): con --long la fila de
        # datos tiene 4 columnas (ID6, MiB, Región, Título); split(None, 1)
        # mezclaba MiB y Región dentro del título.
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        game_id = parts[0]
        if not is_valid_game_id(game_id):
            continue
        title = parts[3].strip()
        if not title:
            continue
        games.append(DiscInfo(game_id=validate_game_id(game_id), title=title, source="wit",
                              console=console_for_id(game_id)))
    return games
