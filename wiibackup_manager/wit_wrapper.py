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

import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .disc_header import DiscInfo

# Algunas builds de `wit` colorean su salida con secuencias ANSI aunque la
# salida esté redirigida a una pipe (no es una terminal), así que no podemos
# confiar en que stdout venga "limpio" solo por capturarlo con subprocess.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


class WitNotFoundError(RuntimeError):
    """`wit` no está instalado o no se encuentra en el PATH."""


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

# Tiempo máximo (segundos) que esperamos a `wit` antes de darlo por
# colgado. Generoso a propósito: copiar o verificar varios GB sobre una SD
# lenta por USB puede legítimamente tardar minutos y no queremos abortar
# una transferencia real que sigue progresando. Es una red de seguridad
# para el caso "wit dejó de progresar del todo" (visto en la práctica:
# proceso en estado D, ~0% CPU acumulado, el archivo destino dejó de
# crecer por completo), no un límite de velocidad.
DEFAULT_WIT_TIMEOUT = 1800.0


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def find_wit(binary_name: str = "wit") -> Optional[str]:
    return shutil.which(binary_name)


def is_available(binary_name: str = "wit") -> bool:
    return find_wit(binary_name) is not None


def _run(
    binary: str, *args: str, timeout: Optional[float] = DEFAULT_WIT_TIMEOUT
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            [binary, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return _timeout_result([binary, *args], exc, timeout)


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
        + f"\n`wit` no respondió en {timeout:.0f}s: se lo dio por colgado y se canceló la operación.",
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
    for f in _wbfs_temp_files(dest):
        try:
            total += f.stat().st_size
        except OSError:
            pass
    try:
        if dest.exists():
            total += dest.stat().st_size
    except OSError:
        pass
    stem = dest.with_suffix("")
    part_num = 1
    while True:
        part = stem.with_suffix(f".wbf{part_num}")
        try:
            if not part.exists():
                break
            total += part.stat().st_size
        except OSError:
            break
        part_num += 1
    return total


def _run_with_progress(
    args: list[str],
    dest: Path,
    timeout: Optional[float],
    bytes_progress_cb: Callable[[int], None],
) -> subprocess.CompletedProcess:
    """Como `subprocess.run`, pero con `Popen` en vez de `.run()` para
    poder sondear cada 1s cuánto lleva escrito hacia `dest`
    (`estimate_bytes_written`) mientras el proceso sigue corriendo, en vez
    de bloquear sin ninguna señal intermedia hasta que termina.

    stdout/stderr van a archivos temporales (no a `PIPE`): si se
    capturaran con `PIPE` y nadie los lee mientras este bucle sondea,
    `wit` podría bloquearse al llenar el buffer del pipe del kernel (típ.
    64KiB) si llega a tirar mucha salida; con archivos no hay ese techo."""
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        proc = subprocess.Popen(args, stdout=out_f, stderr=err_f)
        start = time.monotonic()
        try:
            while True:
                try:
                    proc.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    bytes_progress_cb(estimate_bytes_written(dest))
                    if timeout is not None and (time.monotonic() - start) >= timeout:
                        proc.kill()
                        proc.wait()
                        out_f.seek(0)
                        err_f.seek(0)
                        stderr = err_f.read().decode("utf-8", "replace")
                        stdout = out_f.read().decode("utf-8", "replace")
                        return subprocess.CompletedProcess(
                            args=args,
                            returncode=1,
                            stdout=stdout,
                            stderr=stderr
                            + f"\n`wit` no respondió en {timeout:.0f}s: se lo dio por "
                              "colgado y se canceló la operación.",
                        )
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        out_f.seek(0)
        err_f.seek(0)
        return subprocess.CompletedProcess(
            args=args,
            returncode=proc.returncode,
            stdout=out_f.read().decode("utf-8", "replace"),
            stderr=err_f.read().decode("utf-8", "replace"),
        )


def _find_id6_line(output: str) -> Optional[tuple[str, str]]:
    """Busca, entre las líneas de salida de `wit LIST`, la fila de datos de
    un disco y devuelve (game_id, title).

    No podemos asumir que esa fila esté en un índice fijo: `wit LIST`
    antepone líneas de encabezado y separadores (p. ej. "ID6  MiB Reg. …",
    "----…") que varían de una build a otra. En cambio, reconocemos la fila
    de datos por su forma: empieza con un ID6 real (6 caracteres
    alfanuméricos), seguido de tamaño y región, y el resto de la línea es
    el título del juego.
    """
    for raw_line in output.splitlines():
        line = _strip_ansi(raw_line).strip()
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        game_id = parts[0]
        if len(game_id) != 6 or not game_id.isalnum():
            continue
        title = parts[3].strip()
        if not title:
            continue
        return game_id, title
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
    return DiscInfo(game_id=game_id, title=title, source="wit")


def convert(
    src: Path,
    dest: Path,
    target_format: str,
    binary: str = "wit",
    progress_cb: Optional[Callable[[str], None]] = None,
    split: bool = False,
    timeout: Optional[float] = DEFAULT_WIT_TIMEOUT,
    bytes_progress_cb: Optional[Callable[[int], None]] = None,
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
    entero."""
    if not find_wit(binary):
        raise WitNotFoundError(binary)

    # wit infiere el formato de salida por la extensión de --dest, así que
    # nos aseguramos de que dest tenga la extensión correcta antes de llamar.
    args = [binary, "COPY", "--overwrite"]
    if split:
        args += ["--split-size", _SPLIT_SIZE_ARG]
    args += [str(src), "--dest", str(dest)]

    if bytes_progress_cb is not None:
        result = _run_with_progress(args, dest, timeout, bytes_progress_cb)
    else:
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            result = _timeout_result(args, exc, timeout)
    if progress_cb:
        progress_cb(result.stdout)
    return result


def verify(
    path: Path, binary: str = "wit", timeout: Optional[float] = DEFAULT_WIT_TIMEOUT
) -> tuple[bool, str]:
    """Verifica la integridad de una imagen con `wit VERIFY`."""
    if not find_wit(binary):
        raise WitNotFoundError(binary)
    result = _run(binary, "VERIFY", "--long", str(path), timeout=timeout)
    ok = result.returncode == 0
    output = (result.stdout + result.stderr).strip()
    return ok, output


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
        if len(game_id) != 6 or not game_id.isalnum():
            continue
        title = parts[3].strip()
        if not title:
            continue
        games.append(DiscInfo(game_id=game_id, title=title, source="wit"))
    return games
