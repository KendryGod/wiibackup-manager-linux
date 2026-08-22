"""Escaneo de la biblioteca y modelo de datos de un juego."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import wit_wrapper
from .disc_header import DiscInfo, read_plain_iso_header

VALID_EXTENSIONS = {".iso", ".wbfs", ".ciso", ".wdf"}

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class Game:
    path: Path
    game_id: str
    title: str
    fmt: str  # "ISO" | "WBFS" | "CISO" | "WDF" | "?"
    size_bytes: int
    identified_by: str  # "iso" | "wit" | "unknown"

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = _INVALID_FS_CHARS.sub("", name)
    name = name.strip().rstrip(".")
    return name or "untitled"


def _format_from_suffix(path: Path) -> str:
    return path.suffix.lstrip(".").upper() or "?"


def identify_file(path: Path, wit_binary: str = "wit") -> Optional[Game]:
    """Identifica un único archivo de juego, probando primero el parseo
    directo (rápido, sin dependencias) y usando `wit` como respaldo para
    formatos envueltos (WBFS, CISO, WDF)."""
    if path.suffix.lower() not in VALID_EXTENSIONS:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None

    info: Optional[DiscInfo] = None

    if path.suffix.lower() == ".iso":
        info = read_plain_iso_header(path)

    if info is None and wit_wrapper.is_available(wit_binary):
        try:
            info = wit_wrapper.identify(path, wit_binary)
        except wit_wrapper.WitNotFoundError:
            info = None

    if info is None:
        return Game(
            path=path,
            game_id="??????",
            title=path.stem,
            fmt=_format_from_suffix(path),
            size_bytes=size,
            identified_by="unknown",
        )

    return Game(
        path=path,
        game_id=info.game_id,
        title=info.title,
        fmt=_format_from_suffix(path),
        size_bytes=size,
        identified_by=info.source,
    )


def scan_library(
    root: Path,
    wit_binary: str = "wit",
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> list[Game]:
    """Escanea recursivamente `root` buscando ISO/WBFS/CISO/WDF."""
    if not root.exists():
        return []

    candidates = [
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in VALID_EXTENSIONS
    ]

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


def standard_filename(game: Game) -> str:
    """Nombre estándar sugerido: 'Título [GAMEID].ext', como usa el
    WiiBackup Manager clásico y la mayoría de USB Loaders."""
    safe_title = sanitize_filename(game.title)
    ext = game.path.suffix
    return f"{safe_title} [{game.game_id}]{ext}"


def rename_to_standard(game: Game, dry_run: bool = False) -> Path:
    """Renombra el archivo del juego a la convención 'Título [ID].ext'
    dentro de la misma carpeta. Devuelve la nueva ruta."""
    new_name = standard_filename(game)
    new_path = game.path.with_name(new_name)
    if new_path == game.path:
        return game.path
    if new_path.exists():
        raise FileExistsError(f"Ya existe un archivo en {new_path}")
    if not dry_run:
        game.path.rename(new_path)
        game.path = new_path
    return new_path
