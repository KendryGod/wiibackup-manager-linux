"""Escaneo de la biblioteca y modelo de datos de un juego."""
from __future__ import annotations

import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import drives, wit_wrapper
from .disc_header import DiscInfo, read_plain_iso_header

VALID_EXTENSIONS = {".iso", ".wbfs", ".ciso", ".wdf"}

# Límite duro de FAT32 por archivo (en realidad 2^32 - 1 bytes; usamos
# 4 GiB parejo como aproximación) para decidir si un WBFS ya existente
# entra sin dividir. Nota: esto es distinto de
# `wit_wrapper.FAT32_SPLIT_SIZE_BYTES` (4 GB, más chico a propósito), que es
# el tamaño de partición que le pedimos a `wit` cuando sí hace falta
# dividir — no hace falta que coincidan, uno decide "¿hace falta dividir?"
# y el otro "¿dónde corta wit cada parte?".
_FAT32_SIZE_LIMIT_BYTES = 4 * 1024 ** 3

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def format_size(n: int) -> str:
    """GB con un decimal, o MB si es menos de 1 GB (evita mostrar '0.0 GB'
    para tamaños chicos)."""
    gb = n / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{n / (1024 ** 2):.1f} MB"


def format_eta(seconds: float) -> str:
    """Tiempo estimado restante en formato corto ('45s', '2m 15s', '1h 5m')."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


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


# Tamaño de bloque para la copia manual con progreso (_copy_with_progress).
# 4 MiB: bastante grande para no perder tiempo en overhead de syscalls en
# un archivo de varios GB, bastante chico para reportar progreso con
# granularidad razonable.
_COPY_CHUNK_BYTES = 4 * 1024 * 1024


def _copy_with_progress(src: Path, dest: Path, progress_cb: Callable[[int], None]) -> None:
    """Igual que `shutil.copy2(src, dest)` (copia contenido + metadata),
    pero reportando cuánto lleva copiado cada ~1s vía `progress_cb`, algo
    que `shutil.copy2` no ofrece."""
    written = 0
    last_report = time.monotonic()
    with open(src, "rb") as fsrc, open(dest, "wb") as fdst:
        while True:
            buf = fsrc.read(_COPY_CHUNK_BYTES)
            if not buf:
                break
            fdst.write(buf)
            written += len(buf)
            now = time.monotonic()
            if now - last_report >= 1.0:
                progress_cb(written)
                last_report = now
    progress_cb(written)
    shutil.copystat(src, dest)


def send_to_wbfs_drive(
    game: Game,
    drive_root: Path,
    wit_binary: str = "wit",
    bytes_progress_cb: Optional[Callable[[int], None]] = None,
) -> Path:
    """Copia `game` a la estructura estándar 'wbfs/<ID6>/<ID6>.wbfs' que
    reconocen los USB Loaders de Wii (USB Loader GX, CFG USB Loader, etc.)
    dentro de `drive_root`. Si el origen ya es WBFS y entra entero se copia
    tal cual; para cualquier otro formato (ISO/CISO/WDF), o si el destino
    puede necesitar dividir el archivo, se delega en `wit`, que es quien
    sabe empaquetar (y, si hace falta, partir) el WBFS correctamente.

    FAT32 tiene un límite duro de ~4GiB por archivo, y hay discos Wii
    dual-layer que lo superan: si el filesystem del destino no se puede
    determinar con confianza, se asume que hace falta dividir (ver
    `drives.needs_wbfs_split`).

    `bytes_progress_cb`, si se pasa, se llama periódicamente con los bytes
    escritos hasta el momento hacia `dest` (copia directa o vía `wit`, ver
    `wit_wrapper.convert`), para poder mostrar progreso real dentro de la
    copia/conversión de un solo juego grande."""
    dest_dir = Path(drive_root) / "wbfs" / game.game_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{game.game_id}.wbfs"

    split = drives.needs_wbfs_split(dest_dir)

    # Copia directa solo si ya es WBFS Y sabemos que entra entero sin
    # dividir (si hiciera falta dividir, una copia plana no puede hacerlo:
    # hay que pasar por `wit COPY --split`).
    if game.fmt.upper() == "WBFS" and not (split and game.size_bytes >= _FAT32_SIZE_LIMIT_BYTES):
        if bytes_progress_cb is not None:
            _copy_with_progress(game.path, dest, bytes_progress_cb)
        else:
            shutil.copy2(game.path, dest)
        return dest

    if not wit_wrapper.is_available(wit_binary):
        raise wit_wrapper.WitNotFoundError(wit_binary)

    result = wit_wrapper.convert(game.path, dest, "WBFS", wit_binary, split=split,
                                  bytes_progress_cb=bytes_progress_cb)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Error desconocido al convertir con wit")
    return dest
