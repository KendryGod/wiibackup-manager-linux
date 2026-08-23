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


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


def find_wit(binary_name: str = "wit") -> Optional[str]:
    return shutil.which(binary_name)


def is_available(binary_name: str = "wit") -> bool:
    return find_wit(binary_name) is not None


def _run(binary: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [binary, *args],
        capture_output=True,
        text=True,
        check=False,
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
) -> subprocess.CompletedProcess:
    """Convierte src -> dest. target_format: 'WBFS' o 'ISO'.

    `split=True` agrega `--split` (división en partes de ~4GiB, el tamaño
    por defecto de `wit`), necesario para destinos en FAT32, que no admite
    archivos más grandes y con el que hay discos Wii dual-layer que no
    entran enteros. `wit` solo genera varias partes cuando el resultado
    realmente supera ese límite, así que pasar `split=True` "por las
    dudas" en un filesystem que sí soporta archivos grandes no tiene
    costo: el archivo sale igual, entero."""
    if not find_wit(binary):
        raise WitNotFoundError(binary)

    # wit infiere el formato de salida por la extensión de --dest, así que
    # nos aseguramos de que dest tenga la extensión correcta antes de llamar.
    args = [binary, "COPY", "--overwrite"]
    if split:
        args.append("--split")
    args += [str(src), "--dest", str(dest)]

    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    if progress_cb:
        progress_cb(result.stdout)
    return result


def verify(path: Path, binary: str = "wit") -> tuple[bool, str]:
    """Verifica la integridad de una imagen con `wit VERIFY`."""
    if not find_wit(binary):
        raise WitNotFoundError(binary)
    result = _run(binary, "VERIFY", "--long", str(path))
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
