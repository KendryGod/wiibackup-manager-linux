"""Recorrer una carpeta e identificar los juegos que hay adentro.

De archivos sueltos en el disco a una lista de `Game`: encontrar los
candidatos por extensión, leerles el header (o preguntarle a `wit`) y
armar el modelo. Es la mitad de "biblioteca" que de verdad escanea; lo
que después se hace con esos juegos vive en otros módulos.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from . import wit_wrapper
from .disc_header import (
    UNKNOWN_GAME_ID,
    DiscInfo,
    is_valid_game_id,
    read_ciso_disc_number,
    read_plain_iso_header,
    validate_game_id,
)
from .game_model import VALID_EXTENSIONS, Game


def _format_from_suffix(path: Path) -> str:
    return path.suffix.lstrip(".").upper() or "?"


def identify_file(path: Path, wit_binary: str = "wit") -> Optional[Game]:
    """Identifica un único archivo de juego (Wii o GameCube), probando
    primero el parseo directo (rápido, sin dependencias) y usando `wit`
    como respaldo para formatos envueltos (WBFS, CISO, WDF)."""
    suffix = path.suffix.lower()
    if suffix not in VALID_EXTENSIONS:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None

    info: Optional[DiscInfo] = None

    if suffix == ".iso":
        info = read_plain_iso_header(path)

    if info is None and wit_wrapper.is_available(wit_binary):
        try:
            info = wit_wrapper.identify(path, wit_binary)
        except wit_wrapper.WitNotFoundError:
            info = None

    # El número de disco de una ISO plana ya viene en `info` (se leyó junto
    # con el resto del header). Para CISO, `wit` no lo expone en ningún
    # comando parseable: se lee directo del archivo con el mismo criterio
    # de "0 si no se puede confiar" que usa `read_plain_iso_header`.
    disc_number = info.disc_number if info is not None else 0
    if suffix == ".ciso":
        disc_number = read_ciso_disc_number(path)

    # Punto único donde nace el game_id de un `Game`: si lo que devolvió el
    # header (o `wit`) no es un ID6 válido, el archivo se trata como no
    # identificado en vez de arrastrar un ID que después terminaría
    # formando parte de una ruta del filesystem (ver disc_header).
    if info is None or not is_valid_game_id(info.game_id):
        return Game(
            path=path,
            game_id=UNKNOWN_GAME_ID,
            title=info.title if info is not None and info.title else path.stem,
            fmt=_format_from_suffix(path),
            size_bytes=size,
            identified_by="unknown",
            console=info.console if info is not None else "wii",
            disc_number=disc_number,
        )

    return Game(
        path=path,
        game_id=validate_game_id(info.game_id),
        title=info.title,
        fmt=_format_from_suffix(path),
        size_bytes=size,
        identified_by=info.source,
        console=info.console,
        disc_number=disc_number,
    )


def find_game_files(root: Path, skipped: Optional[list] = None) -> list[Path]:
    """Todos los ISO/WBFS/CISO/WDF que haya debajo de `root`, ordenados.

    Camina con `os.walk` y no con `Path.rglob` justamente por las carpetas
    que no se pueden leer: rglob se las saltea en silencio (probado en
    Python 3.14: no levanta PermissionError, simplemente devuelve menos
    archivos), así que el usuario veía faltar juegos que sabía que estaban
    y no tenía forma de enterarse de por qué. `os.walk` avisa por
    `onerror`, y acá esas carpetas se saltean igual -una carpeta ilegible
    no puede frenar el escaneo de todo lo demás- pero se anotan en
    `skipped` para que quien llame lo pueda informar.

    Con `skipped=None` el comportamiento es el de siempre: saltear y
    seguir."""
    found: list[Path] = []

    def on_error(error: OSError) -> None:
        if skipped is None:
            return
        name = getattr(error, "filename", None)
        skipped.append(Path(name) if name else Path(root))

    for dirpath, _dirnames, filenames in os.walk(root, onerror=on_error):
        base = Path(dirpath)
        for name in filenames:
            path = base / name
            if path.suffix.lower() in VALID_EXTENSIONS and path.is_file():
                found.append(path)
    found.sort()
    return found


def scan_library(
    root: Path,
    wit_binary: str = "wit",
    progress_cb: Optional[Callable[[int, int], None]] = None,
    skipped_dirs: Optional[list] = None,
) -> list[Game]:
    """Escanea recursivamente `root` buscando ISO/WBFS/CISO/WDF.

    `skipped_dirs`, si se pasa, recibe las carpetas que no se pudieron
    leer (permisos, unidad que se desconectó a mitad del escaneo). Ver
    `find_game_files`."""
    if not root.exists():
        return []

    candidates = find_game_files(root, skipped_dirs)

    games: list[Game] = []
    total = len(candidates)
    for i, path in enumerate(candidates, start=1):
        game = identify_file(path, wit_binary)
        if game is not None:
            games.append(game)
        if progress_cb:
            progress_cb(i, total)

    games.sort(key=lambda g: g.title.lower())
    return games
