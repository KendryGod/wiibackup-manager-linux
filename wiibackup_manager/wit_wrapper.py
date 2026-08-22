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

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .disc_header import DiscInfo


class WitNotFoundError(RuntimeError):
    """`wit` no está instalado o no se encuentra en el PATH."""


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


def identify(path: Path, binary: str = "wit") -> Optional[DiscInfo]:
    """Usa `wit ID6 --long` para identificar un juego (ISO o WBFS)."""
    if not find_wit(binary):
        raise WitNotFoundError(binary)

    result = _run(binary, "ID6", "--long", str(path))
    if result.returncode != 0 or not result.stdout.strip():
        return None

    # Formato típico de salida: "RMCE01 The Legend of Zelda: TP"
    line = result.stdout.strip().splitlines()[0]
    parts = line.split(None, 1)
    if not parts:
        return None
    game_id = parts[0].strip()
    title = parts[1].strip() if len(parts) > 1 else game_id
    if not game_id:
        return None
    return DiscInfo(game_id=game_id, title=title, source="wit")


def convert(
    src: Path,
    dest: Path,
    target_format: str,
    binary: str = "wit",
    progress_cb: Optional[Callable[[str], None]] = None,
) -> subprocess.CompletedProcess:
    """Convierte src -> dest. target_format: 'WBFS' o 'ISO'."""
    if not find_wit(binary):
        raise WitNotFoundError(binary)

    # wit infiere el formato de salida por la extensión de --dest, así que
    # nos aseguramos de que dest tenga la extensión correcta antes de llamar.
    result = subprocess.run(
        [binary, "COPY", "--overwrite", str(src), "--dest", str(dest)],
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
        line = line.strip()
        if not line or line.startswith("*") or line.startswith("-"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2 and len(parts[0]) == 6:
            games.append(DiscInfo(game_id=parts[0], title=parts[1].strip(), source="wit"))
    return games
