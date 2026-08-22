"""Lectura del header de disco Wii para archivos ISO planos.

Esto es un *fallback* que funciona sin dependencias externas: el formato
del header de una ISO de Wii está documentado y no cambia entre juegos:

    offset 0x00 .. 0x06   -> Game ID (6 bytes ASCII, ej. "RMCE01")
    offset 0x06           -> número de disco
    offset 0x07           -> versión del disco
    offset 0x18 .. 0x1C   -> magic word de disco Wii = 0x5D1C9EA3
    offset 0x20 .. 0x60   -> título del juego (ASCII, termina en \\x00)

Para archivos .wbfs (que envuelven la ISO en un contenedor con su propio
header) NO se reimplementa el parseo aquí: es fácil equivocarse con el
tamaño de sector y el offset del primer disco, y un error ahí puede hacer
que la app muestre datos incorrectos sobre tus respaldos reales. Para
.wbfs y para conversión/verificación se usa Wiimms ISO Tools (`wit`),
ver wit_wrapper.py, que es el estándar de facto en Linux para esto.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

WII_DISC_MAGIC = b"\x5d\x1c\x9e\xa3"
HEADER_TITLE_OFFSET = 0x20
HEADER_TITLE_MAX_LEN = 0x40
HEADER_MAGIC_OFFSET = 0x18
GAME_ID_LEN = 6


@dataclass
class DiscInfo:
    game_id: str
    title: str
    source: str  # "iso" | "wit"


def read_plain_iso_header(path: Path) -> Optional[DiscInfo]:
    """Lee el header de una ISO de Wii sin envolver. Devuelve None si el
    archivo no parece ser una ISO de Wii válida."""
    try:
        with open(path, "rb") as f:
            data = f.read(0x100)
    except OSError:
        return None

    if len(data) < 0x60:
        return None

    if data[HEADER_MAGIC_OFFSET:HEADER_MAGIC_OFFSET + 4] != WII_DISC_MAGIC:
        return None

    game_id_raw = data[0:GAME_ID_LEN]
    try:
        game_id = game_id_raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not game_id.isprintable():
        return None

    title_raw = data[HEADER_TITLE_OFFSET:HEADER_TITLE_OFFSET + HEADER_TITLE_MAX_LEN]
    title = title_raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()

    return DiscInfo(game_id=game_id, title=title or game_id, source="iso")
