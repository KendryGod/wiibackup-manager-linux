"""El modelo de dominio: qué es un juego y cómo se llama su archivo.

Solo el dato y lo que se deriva de él sin tocar el disco. Nada de acá
escanea, copia ni convierte -eso vive en `scanning`, `fileops` y
`library_ops`-, así que se puede importar desde cualquier capa sin
arrastrar `wit` ni las unidades.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .disc_header import is_valid_game_id, validate_game_id


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
    console: str = "wii"  # "wii" | "gc"
    disc_number: int = 0  # 0 = disco 1, 1 = disco 2, ... (ver disc_header)

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = _INVALID_FS_CHARS.sub("", name)
    name = name.strip().rstrip(".")
    return name or "untitled"


def standard_filename(game: Game) -> str:
    """Nombre estándar sugerido: 'Título [GAMEID].ext', como usa el
    WiiBackup Manager clásico y la mayoría de USB Loaders.

    Si el juego no está identificado (o su ID no es un ID6 válido) se
    omite el sufijo '[ID]' en vez de meter en el nombre del archivo algo
    que no es un ID: '??????' trae caracteres que FAT32 no acepta, y un ID
    arbitrario leído del header podría traer separadores de ruta."""
    safe_title = sanitize_filename(game.title)
    ext = game.path.suffix
    if not is_valid_game_id(game.game_id):
        return f"{safe_title}{ext}"
    return f"{safe_title} [{validate_game_id(game.game_id)}]{ext}"


def needs_rename(game: Game) -> bool:
    """True si el archivo no está ya con el nombre estándar."""
    return game.path.name != standard_filename(game)
