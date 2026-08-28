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
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

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
# antes de matarlo a la fuerza (SIGKILL) al cancelar.
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


def _escalate_to_kill(proc: subprocess.Popen) -> None:
    """Espera la gracia del SIGTERM y, si el proceso sigue vivo, SIGKILL.

    Sondea con `poll()` en vez de `wait()` a propósito: el hilo que lanzó
    el proceso ya está esperándolo, y dos `wait()` sobre el mismo Popen
    desde hilos distintos es justo la clase de carrera que no hace falta
    tener."""
    deadline = time.monotonic() + _KILL_GRACE_SECONDS
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    if proc.poll() is None:
        _send_signal_group(proc, signal.SIGKILL)


def _request_termination(proc: subprocess.Popen) -> None:
    """Pide que `proc` termine y VUELVE EN EL ACTO.

    Esto lo llama el botón "Cancelar", o sea el hilo de GTK: cualquier
    espera acá congela la ventana entera. Antes se mandaba SIGTERM y se
    esperaba hasta 5 segundos, y otros 5 después del SIGKILL: hasta 10
    segundos con la interfaz trabada, sin repintar ni responder clicks,
    justo cuando el usuario acaba de pedir que algo se detenga.

    El SIGTERM se manda de inmediato (es lo que corta la escritura) y la
    escalada a SIGKILL queda a cargo de un hilo suelto, que es trabajo de
    fondo y a nadie le importa cuánto tarde."""
    if proc.poll() is not None:
        return
    _send_signal_group(proc, signal.SIGTERM)
    threading.Thread(target=_escalate_to_kill, args=(proc,), daemon=True,
                      name="wit-kill").start()


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Versión bloqueante de lo de arriba, para los hilos de fondo que ya
    estaban esperando al proceso (por ejemplo cuando `wit` se cuelga y
    salta el timeout). Acá sí se puede esperar: no hay ninguna ventana del
    otro lado."""
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
# en library.py). Verificado además con una copia real: un WBFS real de
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


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


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
        returncode=1,
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


def _run_with_progress(
    args: list[str],
    dest: Path,
    bytes_progress_cb: Callable[[int], None],
    cancel: Optional[CancellationToken] = None,
    inactivity_timeout: Optional[float] = WIT_INACTIVITY_TIMEOUT,
    absolute_timeout: Optional[float] = WIT_ABSOLUTE_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Como `subprocess.run`, pero con `Popen` en vez de `.run()` para
    poder sondear cada 1s cuánto lleva escrito hacia `dest`
    (`estimate_bytes_written`) mientras el proceso sigue corriendo, en vez
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
    `inactivity_timeout` se reinicia cada vez que el destino crece, así
    que una transferencia lenta pero sana nunca se corta sola (ver el
    comentario de `WIT_INACTIVITY_TIMEOUT`); `absolute_timeout` queda
    detrás como última red de seguridad."""
    # Lo que ya existía antes de arrancar: si hay que limpiar por una
    # cancelación, se borra solo lo que agregó ESTA operación.
    outputs_before = output_files(dest)
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        proc = subprocess.Popen(args, stdout=out_f, stderr=err_f, start_new_session=True)
        # Si la cancelación llegó entre el chequeo previo y el Popen,
        # `attach` lo mata en el acto y devuelve False.
        running = cancel.attach(proc) if cancel is not None else True
        start = time.monotonic()
        # Último avance real: arranca en lo que ya había escrito (el
        # destino puede existir de antes) y se actualiza solo cuando crece.
        last_bytes = estimate_bytes_written(dest)
        last_progress_at = start
        timeout_reason: Optional[str] = None
        try:
            while running:
                try:
                    proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    written = estimate_bytes_written(dest)
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
                        _terminate_process_group(proc)
                        break
            if not running:
                proc.wait()
        except BaseException:
            _terminate_process_group(proc)
            cleanup_new_output_files(dest, outputs_before)
            raise
        finally:
            if cancel is not None:
                cancel.detach(proc)

        cancelled = cancel is not None and cancel.cancelled
        if cancelled or timeout_reason is not None:
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
    """"wii" o "gc" según el primer carácter del Game ID.

    Nintendo reservó 'G' como primer carácter de ID4 para GameCube (ej.
    "GZ2E01", Twilight Princess GC); los discos de Wii arrancan con otras
    letras ('R', 'S', 'W', ...). `wit LIST` no expone la consola como
    columna propia (se confirmó corriendo `wit LIST --long`: solo trae
    ID6/MiB/Región/Título), así que para lo que identifica `wit` -formatos
    envueltos como WBFS/CISO/WDF- esta es la señal disponible. Para ISO
    plana no hace falta: `disc_header.read_plain_iso_header` ya lee el
    magic word real del disco, que es la fuente de verdad."""
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


def identify(path: Path, binary: str = "wit") -> Optional[DiscInfo]:
    """Usa `wit LIST --long` para identificar un juego (ISO o WBFS).

    Sin --long, `wit LIST` cambia de formato (a veces omite las columnas
    MiB/Región) según detecte o no una terminal, lo que corre el título de
    lugar. Con --long el formato de 4 columnas (ID6, MiB, Región, Título)
    es estable tanto en terminal como redirigido a una pipe."""
    if not find_wit(binary):
        raise WitNotFoundError(binary)

    result = _run(binary, "LIST", "--long", str(path))
    if result.returncode != 0 or not result.stdout.strip():
        return None

    found = _find_id6_line(result.stdout)
    if found is None:
        return None
    game_id, title = found
    return DiscInfo(game_id=game_id, title=title, source="wit",
                     console=console_for_id(game_id))


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
) -> subprocess.CompletedProcess:
    """Convierte src -> dest. target_format: 'WBFS' o 'ISO'.

    `bytes_progress_cb`, si se pasa, se llama aproximadamente cada 1s
    (desde este mismo hilo, bloqueante) con una estimación de cuántos
    bytes lleva escritos `wit` hacia `dest` (ver `estimate_bytes_written`),
    para poder mostrar progreso real dentro de la conversión de un solo
    archivo grande y no solo saltar de 0% a 100% al terminar. No hay forma
    confiable de leer el progreso real de `wit` (no expone una opción de
    progreso parseable en `wit HELP COPY`), así que esto es una
    estimación por tamaño de archivo, no un progreso exacto reportado por
    la herramienta.

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
    `library.DestinationGuard`, o directamente salteando el archivo). Era
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
    queda como última red de seguridad."""
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
    args += [str(src), "--dest", str(dest)]

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

    proc = subprocess.Popen(
        [binary, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
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


def verify(
    path: Path, binary: str = "wit", timeout: Optional[float] = DEFAULT_WIT_TIMEOUT,
    cancel: Optional[CancellationToken] = None,
) -> tuple[bool, str]:
    """Verifica la integridad de una imagen con `wit VERIFY`.

    `cancel`, si se pasa, permite matar el `wit` en curso desde el hilo de
    GTK: en ese caso levanta `OperationCancelled` en vez de devolver un
    resultado."""
    if not find_wit(binary):
        raise WitNotFoundError(binary)
    result = _run_cancellable(binary, "VERIFY", "--long", str(path),
                               timeout=timeout, cancel=cancel)
    ok = result.returncode == 0
    output = (result.stdout + result.stderr).strip()
    return ok, output


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
    La salida trae una línea por juego con bloques y MiB; se suman los
    MiB, que cubre también el caso de un WBFS multi-juego."""
    if not find_wit(binary):
        return None
    result = _run(binary, "ISOSIZE", "--long", str(path))
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
