"""Wrapper sobre `f3` (Fight Flash Fraud): confirmar que una memoria tiene
de verdad la capacidad que dice tener.

El problema que resuelve es concreto y viejo: hay pendrives y tarjetas SD
-y las hay a montones en cualquier venta online barata- cuyo controlador
MIENTE. Dicen 128 GB, el sistema operativo les cree porque la capacidad la
reporta el propio dispositivo, y en realidad tienen 8 GB de memoria física.
Escribir más de eso no da error: los datos nuevos pisan a los viejos o se
van a la nada. El cliente se entera meses después, cuando abre las fotos y
están todas rotas.

Nada de eso se detecta mirando el dispositivo: la única forma de saberlo es
llenarlo de datos conocidos y volver a leerlos. Eso es exactamente lo que
hace `f3`, la herramienta estándar en Linux para esto:

- `f3write <punto de montaje>` llena TODO el espacio libre con archivos
  `1.h2w`, `2.h2w`, ... de 1 GiB cada uno, con un contenido que se puede
  regenerar y verificar después.
- `f3read <punto de montaje>` los vuelve a leer y reporta cuántos sectores
  volvieron bien, cuántos corruptos, cuántos cambiados y cuántos
  sobrescritos. Si la memoria miente, la diferencia salta acá.

Repo / instalación: https://github.com/AltraMayor/f3  (en Fedora:
`sudo dnf install f3`; en Debian/Ubuntu: `sudo apt install f3`).

Por qué f3write/f3read y no f3probe
-----------------------------------
`f3probe` hace lo mismo en minutos en vez de horas, pero trabaja sobre el
dispositivo de bloque (`/dev/sdX`), necesita root y es DESTRUCTIVO: la
memoria queda inservible aunque el resultado sea "está bien". Para el uso
de esta app -verificar la memoria de un cliente antes de cargarle nada- eso
es al revés de lo que se necesita: `f3write`/`f3read` trabajan sobre el
punto de montaje, no necesitan privilegios, y lo único que dejan son sus
propios archivos de prueba, que se borran al terminar
(`cleanup_test_files`).

Lo que sí hay que decirle al usuario, y la interfaz lo dice, es que la
prueba llena el espacio LIBRE: en una memoria con datos adentro solo se
verifica lo que quedaba libre, así que conviene hacerla vacía.

Cómo se lee el progreso
-----------------------
`f3` reporta avance por su salida estándar, reescribiendo la misma línea
con `\\r` (típico de una herramienta de terminal):

    Creating file 1.h2w ... 24.53% -- 10.28 MB/s -- 45:32
    Creating file 1.h2w ... OK!

Por eso `_run_streaming` corta la salida tanto en `\\n` como en `\\r` y va
entregando líneas a medida que llegan, en vez de esperar a que el proceso
termine: una verificación de un disco de 1 TB puede tardar la mayor parte
de un día, y una barra de progreso que no se mueve hasta el final no sirve
de nada.

A propósito no se le pasa `--show-progress`: no todas las versiones de f3
lo aceptan, y sin porcentajes el progreso igual avanza -de a un archivo de
1 GiB por vez, que es granularidad de sobra para una barra. Lo que se
parsea es lo que TODA versión imprime: los `Creating file N.h2w` /
`Validating file N.h2w` y el resumen final.
"""
from __future__ import annotations

import math
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .i18n import _

F3WRITE_BINARY = "f3write"
F3READ_BINARY = "f3read"

# Cada archivo de prueba de f3write mide 1 GiB (salvo el último, que es lo
# que quede). Es lo que permite estimar cuántos archivos va a crear y, con
# eso, cuánto falta.
H2W_FILE_BYTES = 1024 ** 3

# Los archivos que crea f3write en el punto de montaje: "1.h2w", "2.h2w"...
_H2W_NAME_RE = re.compile(r"^\d+\.h2w$")

# f3 mide en sectores de 512 bytes en sus resúmenes ("62309848 sectors").
_SECTOR_BYTES = 512

PHASE_WRITE = "write"
PHASE_READ = "read"

# Segundos sin UNA sola línea de salida antes de dar el proceso por
# colgado. No hay límite absoluto a propósito: verificar un disco externo
# de 2 TB a 30 MB/s son casi 20 horas por pasada, y son 20 horas legítimas.
# Lo que no es legítimo es no imprimir nada: f3 escribe una línea por
# archivo de 1 GiB como mínimo, que a 1 MB/s -una SD de las peores- son
# ~17 minutos. Media hora deja margen de sobra y sigue detectando un medio
# que se colgó de verdad.
F3_INACTIVITY_TIMEOUT = 1800.0

# Gracia entre SIGTERM y SIGKILL al cancelar, igual que en `wit_wrapper`.
_KILL_GRACE_SECONDS = 5.0


class F3NotFoundError(RuntimeError):
    """`f3write`/`f3read` no están instalados o no se encuentran en el
    PATH."""


@dataclass(frozen=True)
class CheckProgress:
    """Un avance de la verificación, tal como se lo pasa `progress_cb` a la
    interfaz. `fraction` va de 0 a 1 sobre las DOS pasadas juntas (escribir
    y leer), que es lo que le importa a la barra: la mitad de abajo es la
    escritura y la de arriba la lectura."""

    phase: str
    fraction: float
    speed: str = ""
    eta: str = ""


@dataclass(frozen=True)
class CheckResult:
    """El veredicto de la verificación.

    `ok` es lo único que decide si la memoria pasa la prueba, y es
    deliberadamente estricto: TODO lo que se escribió volvió igual
    (`lost_bytes == 0`) y se llegó a escribir algo. Cualquier otra cosa
    -sectores corruptos, cambiados, sobrescritos, un f3 que terminó con
    error, una cancelación- deja `ok` en False, y la interfaz solo ofrece
    formatear cuando `ok` es True."""

    ok: bool
    cancelled: bool = False
    # Espacio libre que había al empezar: lo que la memoria DICE tener
    # disponible, y por lo tanto lo que se le puso a prueba.
    announced_bytes: int = 0
    ok_bytes: int = 0
    lost_bytes: int = 0
    corrupted_bytes: int = 0
    changed_bytes: int = 0
    overwritten_bytes: int = 0
    write_speed: str = ""
    read_speed: str = ""
    # Motivo cuando la verificación no se pudo completar (f3 falló, no está
    # instalado, se canceló). Vacío cuando terminó y hay veredicto.
    error: str = ""

    @property
    def ok_gb(self) -> float:
        return self.ok_bytes / (1024 ** 3)

    @property
    def announced_gb(self) -> float:
        return self.announced_bytes / (1024 ** 3)

    @property
    def lost_gb(self) -> float:
        return self.lost_bytes / (1024 ** 3)


# ------------------------------------------------------------ Binarios --
def find_binary(name: str) -> Optional[str]:
    return shutil.which(name)


def missing_binaries() -> list[str]:
    """Cuáles de los dos comandos que hacen falta no están instalados.
    Lista vacía = se puede verificar."""
    return [name for name in (F3WRITE_BINARY, F3READ_BINARY)
            if find_binary(name) is None]


def is_available() -> bool:
    return not missing_binaries()


# ------------------------------------------------- Proceso con progreso --
def _send_signal_group(proc: subprocess.Popen, sig) -> None:
    """Manda `sig` al grupo entero del proceso (se lanza con
    `start_new_session=True`), no solo al PID: mismo criterio que
    `wit_wrapper._send_signal_group`, para que no quede un f3 escribiendo
    en la memoria del cliente después de cancelar."""
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, sig)
            return
        except OSError:
            pass
    try:
        proc.send_signal(sig)
    except OSError:
        pass


def _terminate(proc: subprocess.Popen) -> None:
    """SIGTERM y, si sigue vivo tras la gracia, SIGKILL. Bloquea, así que
    se llama solo desde el hilo de fondo -el botón "Cancelar" no llama acá
    sino a `CancellationToken.cancel()`, que no espera nada."""
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


def _iter_output_lines(proc: subprocess.Popen):
    """Va entregando la salida de `proc` línea por línea, cortando tanto en
    `\\n` como en `\\r`.

    f3 reescribe la línea de progreso con `\\r` sin saltar de línea, así que
    leer con `for linea in proc.stdout` (que corta solo en `\\n`) no
    entregaría NADA hasta que el archivo de 1 GiB en curso terminara. Se lee
    de a bloques chicos y se corta a mano.

    stderr viene redirigido a stdout por quien llama, así que un mensaje de
    error de f3 también pasa por acá y termina en el `error` del resultado.

    Se lee en binario con `read1`, que devuelve apenas hay algo, y se
    decodifica acá: `read(n)` de un stream de texto se queda esperando a
    juntar n caracteres, o sea que la línea de progreso que f3 acaba de
    escribir no llegaría hasta que hubiera varias más -justo lo contrario
    de lo que se busca."""
    buffer = ""
    while True:
        crudo = proc.stdout.read1(4096)
        if not crudo:
            break
        buffer += crudo.decode("utf-8", "replace")
        buffer = buffer.replace("\r\n", "\n")
        while True:
            corte = min((i for i in (buffer.find("\n"), buffer.find("\r"))
                         if i >= 0), default=-1)
            if corte < 0:
                break
            yield buffer[:corte]
            buffer = buffer[corte + 1:]
    if buffer:
        yield buffer


def _run_streaming(args: list[str], on_line: Callable[[str], None], *,
                   cancel=None,
                   inactivity_timeout: Optional[float] = F3_INACTIVITY_TIMEOUT,
                   ) -> tuple[int, str]:
    """Corre `args` entregando cada línea de salida a `on_line` a medida
    que llega. Devuelve (returncode, motivo_de_corte).

    El motivo de corte no vacío significa que el proceso se mató desde acá
    (cuelgue detectado por inactividad), y viene con el texto que se le
    muestra al usuario.

    Un solo hilo lee la salida mientras el principal vigila el reloj de
    inactividad: leer siempre es lo que evita que f3 se trabe al llenarse
    el buffer del pipe, que es justo el riesgo de capturar con PIPE y no
    consumir."""
    # Binario (sin `text=True`) a propósito: ver `_iter_output_lines`.
    proc = subprocess.Popen(args, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, start_new_session=True)
    running = cancel.attach(proc) if cancel is not None else True

    ultimo = {"t": time.monotonic()}
    motivo = ""

    def _leer():
        try:
            for linea in _iter_output_lines(proc):
                ultimo["t"] = time.monotonic()
                on_line(linea)
        except (OSError, ValueError):
            # El pipe se cerró de golpe porque el proceso se mató: no es
            # un error propio, el returncode ya cuenta la historia.
            pass

    lector = threading.Thread(target=_leer, daemon=True, name="f3-reader")
    lector.start()
    try:
        while running:
            try:
                proc.wait(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if (inactivity_timeout is not None
                        and time.monotonic() - ultimo["t"] >= inactivity_timeout):
                    motivo = _(
                        "`f3` no informó nada durante {minutes:.0f} minutos: "
                        "se lo dio por colgado y se cortó la verificación. "
                        "La memoria puede estar fallando."
                    ).format(minutes=inactivity_timeout / 60)
                    _terminate(proc)
                    break
        if not running:
            proc.wait()
    except BaseException:
        _terminate(proc)
        raise
    finally:
        if cancel is not None:
            cancel.detach(proc)
        lector.join(timeout=5.0)
        if proc.stdout is not None:
            proc.stdout.close()
    return proc.returncode, motivo


# -------------------------------------------------------- Parseo de f3 --
_CREATING_RE = re.compile(r"Creating file (\d+)\.h2w")
_VALIDATING_RE = re.compile(r"Validating file (\d+)\.h2w")
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)%")
_SPEED_RE = re.compile(r"([\d.]+ [KMGT]?B/s)")
_ETA_RE = re.compile(r"--\s+(\d+:\d{2}(?::\d{2})?)\s*$")
_SECTORS_RE = re.compile(r"\((\d+) sectors?\)")
_AVG_WRITE_RE = re.compile(r"Average writing speed:\s*(.+?)\s*$")
_AVG_READ_RE = re.compile(r"Average reading speed:\s*(.+?)\s*$")


def _sectors_to_bytes(linea: str) -> Optional[int]:
    """Los bytes que reporta una línea de resumen de f3read, sacados del
    conteo de SECTORES y no del tamaño humano de al lado: "29.71 GB" está
    redondeado a dos decimales, "(62309848 sectors)" es exacto."""
    match = _SECTORS_RE.search(linea)
    if match is None:
        return None
    return int(match.group(1)) * _SECTOR_BYTES


class _OutputParser:
    """Estado del parseo de las dos pasadas de f3.

    Vive en una clase y no en variables sueltas porque `on_line` se llama
    desde el hilo lector: todo lo que se acumula entre líneas (en qué
    archivo va, qué reportó el resumen) tiene que quedar en algún lado que
    sobreviva a la llamada."""

    def __init__(self, total_files: int,
                 progress_cb: Optional[Callable[[CheckProgress], None]]):
        self.total_files = max(total_files, 1)
        self.progress_cb = progress_cb
        self.phase = PHASE_WRITE
        self.write_speed = ""
        self.read_speed = ""
        self.ok_bytes = 0
        self.lost_bytes = 0
        self.corrupted_bytes = 0
        self.changed_bytes = 0
        self.overwritten_bytes = 0
        # Últimas líneas con pinta de error, para el mensaje cuando f3
        # termina con returncode != 0.
        self.last_lines: list[str] = []

    # -- progreso --
    def _emit(self, indice_archivo: int, porcentaje: float, linea: str) -> None:
        if self.progress_cb is None:
            return
        # Dentro de la pasada: archivos ya terminados + lo que va del actual.
        dentro = min((indice_archivo - 1 + porcentaje) / self.total_files, 1.0)
        # Las dos pasadas pesan la mitad cada una: escribir es la primera
        # mitad de la barra, leer la segunda.
        fraccion = dentro / 2 if self.phase == PHASE_WRITE else 0.5 + dentro / 2
        velocidad = _SPEED_RE.search(linea)
        eta = _ETA_RE.search(linea)
        self.progress_cb(CheckProgress(
            phase=self.phase,
            fraction=max(0.0, min(fraccion, 1.0)),
            speed=velocidad.group(1) if velocidad else "",
            eta=eta.group(1) if eta else "",
        ))

    def feed(self, linea: str) -> None:
        texto = linea.strip()
        if not texto:
            return
        if len(self.last_lines) >= 5:
            self.last_lines.pop(0)
        self.last_lines.append(texto)

        creando = _CREATING_RE.search(texto)
        if creando is not None:
            self.phase = PHASE_WRITE
            indice = int(creando.group(1))
            porcentaje = self._percent(texto)
            self._emit(indice, porcentaje, texto)
            return

        validando = _VALIDATING_RE.search(texto)
        if validando is not None:
            self.phase = PHASE_READ
            indice = int(validando.group(1))
            self._emit(indice, self._percent(texto), texto)
            return

        self._feed_summary(texto)

    @staticmethod
    def _percent(texto: str) -> float:
        """Cuánto va del archivo en curso. "OK!" al final de la línea es f3
        diciendo que ese archivo terminó; si no hay ni porcentaje ni OK
        (versiones sin progreso parcial), se cuenta como recién empezado y
        el avance lo marca el próximo archivo."""
        if texto.endswith("OK!"):
            return 1.0
        match = _PERCENT_RE.search(texto)
        return float(match.group(1)) / 100 if match else 0.0

    # -- resumen de f3read --
    def _feed_summary(self, texto: str) -> None:
        avg_w = _AVG_WRITE_RE.search(texto)
        if avg_w is not None:
            self.write_speed = avg_w.group(1)
            return
        avg_r = _AVG_READ_RE.search(texto)
        if avg_r is not None:
            self.read_speed = avg_r.group(1)
            return

        bytes_ = _sectors_to_bytes(texto)
        if bytes_ is None:
            return
        if texto.startswith("Data OK:"):
            self.ok_bytes = bytes_
        elif texto.startswith("Data LOST:"):
            self.lost_bytes = bytes_
        elif texto.startswith("Corrupted:"):
            self.corrupted_bytes = bytes_
        elif texto.startswith("Slightly changed:"):
            self.changed_bytes = bytes_
        elif texto.startswith("Overwritten:"):
            self.overwritten_bytes = bytes_


# ------------------------------------------------ Archivos de la prueba --
def test_files(mount_point) -> list[Path]:
    """Los archivos de prueba de f3 (`1.h2w`, `2.h2w`, ...) que hay AHORA
    en `mount_point`. Lista vacía si no se puede leer la carpeta."""
    try:
        return sorted(p for p in Path(mount_point).iterdir()
                      if p.is_file() and _H2W_NAME_RE.match(p.name))
    except OSError:
        return []


def cleanup_test_files(mount_point, keep: Optional[set] = None) -> int:
    """Borra los archivos de prueba de f3 que quedaron en `mount_point` y
    devuelve cuántos borró.

    `keep` son los que ya estaban ANTES de empezar (una corrida anterior
    que quedó a medias, por ejemplo): no los toca, para no borrar nada que
    esta operación no haya creado. Los errores se ignoran a propósito -si
    un archivo no se puede borrar, la verificación igual terminó y el
    usuario puede formatear o borrarlos a mano; hacer fallar la operación
    entera por eso sería peor."""
    conservar = keep or set()
    borrados = 0
    for archivo in test_files(mount_point):
        if archivo in conservar:
            continue
        try:
            archivo.unlink()
            borrados += 1
        except OSError:
            pass
    return borrados


# ------------------------------------------------------------ Operación --
def check_memory(mount_point, *,
                 progress_cb: Optional[Callable[[CheckProgress], None]] = None,
                 cancel=None,
                 stream=_run_streaming,
                 cleanup: bool = True) -> CheckResult:
    """Verifica que la memoria montada en `mount_point` tenga de verdad el
    espacio que dice tener: `f3write` la llena de archivos de prueba,
    `f3read` los vuelve a leer y compara.

    Pensada para correr en un hilo de fondo: no toca GTK. `progress_cb` se
    llama muchísimas veces (varias por segundo) desde el hilo lector, así
    que quien la use tiene que reenviar a GTK con `GLib.idle_add`, igual
    que hace el resto de la app.

    `cancel` es un `wit_wrapper.CancellationToken`: se comparte el mismo
    tipo a propósito, es el mismo problema (matar el proceso y su grupo
    desde el hilo de GTK sin esperar nada) y no hay motivo para tener dos.

    Al terminar borra los archivos de prueba que creó -y también si se
    cancela o si falla a mitad de camino: dejar 60 GB de `.h2w` en la
    memoria de un cliente sería peor que no haber verificado nada. Los que
    ya estaban antes de empezar no se tocan.

    Levanta `F3NotFoundError` si falta alguno de los dos comandos. En
    cualquier otro caso devuelve un `CheckResult`: los fallos se informan
    ahí (`ok=False` + `error`), no con excepciones, porque "la memoria no
    pasó la prueba" es un resultado, no un error del programa."""
    faltan = missing_binaries()
    if faltan:
        raise F3NotFoundError(
            _("Falta instalar {binaries}. Es parte del paquete `f3` "
              "(en Fedora: sudo dnf install f3).").format(
                  binaries=", ".join(f"`{b}`" for b in faltan)))

    punto = Path(mount_point)
    try:
        libre = shutil.disk_usage(punto).free
    except OSError as e:
        return CheckResult(ok=False, error=str(e))

    previos = set(test_files(punto))
    total_archivos = max(math.ceil(libre / H2W_FILE_BYTES), 1)
    parser = _OutputParser(total_archivos, progress_cb)

    def _cancelado() -> bool:
        return cancel is not None and cancel.cancelled

    try:
        rc_write, motivo_write = stream(
            [F3WRITE_BINARY, str(punto)], parser.feed, cancel=cancel)
        if _cancelado():
            return CheckResult(ok=False, cancelled=True, announced_bytes=libre)
        if rc_write != 0 or motivo_write:
            return CheckResult(
                ok=False, announced_bytes=libre,
                error=motivo_write or _(
                    "`f3write` terminó con error: {detail}").format(
                        detail=_detalle(parser)))

        parser.phase = PHASE_READ
        rc_read, motivo_read = stream(
            [F3READ_BINARY, str(punto)], parser.feed, cancel=cancel)
        if _cancelado():
            return CheckResult(ok=False, cancelled=True, announced_bytes=libre)
        if rc_read != 0 or motivo_read:
            return CheckResult(
                ok=False, announced_bytes=libre,
                error=motivo_read or _(
                    "`f3read` terminó con error: {detail}").format(
                        detail=_detalle(parser)))
    finally:
        if cleanup:
            cleanup_test_files(punto, keep=previos)

    return CheckResult(
        ok=parser.lost_bytes == 0 and parser.ok_bytes > 0,
        announced_bytes=libre,
        ok_bytes=parser.ok_bytes,
        lost_bytes=parser.lost_bytes,
        corrupted_bytes=parser.corrupted_bytes,
        changed_bytes=parser.changed_bytes,
        overwritten_bytes=parser.overwritten_bytes,
        write_speed=parser.write_speed,
        read_speed=parser.read_speed,
    )


def _detalle(parser: _OutputParser) -> str:
    """Las últimas líneas que imprimió f3, que es donde está el motivo real
    del fallo (permisos, medio de solo lectura, error de E/S)."""
    return " / ".join(parser.last_lines[-2:]) or _("sin detalle")
