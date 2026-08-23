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

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

WII_DISC_MAGIC = b"\x5d\x1c\x9e\xa3"
HEADER_TITLE_OFFSET = 0x20
HEADER_TITLE_MAX_LEN = 0x40
HEADER_MAGIC_OFFSET = 0x18
GAME_ID_LEN = 6

# Un Game ID de Wii/GameCube son exactamente 6 caracteres alfanuméricos
# ASCII en mayúscula (ej. "RMCE01"): 4 de código de juego + 2 de editor.
#
# Validar con esta forma exacta -y no con algo laxo como `isprintable()` o
# `str.isalnum()`- importa por seguridad, no solo por prolijidad: el
# game_id se lee del header del archivo (contenido que la app no controla)
# y después se usa para armar rutas del filesystem, p. ej.
# `wbfs/<ID6>/<ID6>.wbfs` en `library.send_to_wbfs_drive`. Un header
# manipulado con "../../" o cualquier cosa con "/" podría escaparse de la
# carpeta wbfs/ prevista y escribir en otro lado. `isprintable()` dejaba
# pasar "/", "\\" y "."; `isalnum()` deja pasar dígitos y letras Unicode
# (p. ej. árabes o de ancho completo), que tampoco son un ID6 real.
_GAME_ID_RE = re.compile(r"^[A-Z0-9]{6}$")

# El ID que usa la app cuando no pudo identificar el archivo. Un game_id
# que no pasa la validación se trata igual que un archivo no identificado.
UNKNOWN_GAME_ID = "??????"


def is_valid_game_id(game_id: str) -> bool:
    """True si `game_id` es un ID6 real (6 alfanuméricos ASCII). Acepta
    minúsculas: se comparan en mayúscula, igual que `validate_game_id`."""
    return bool(_GAME_ID_RE.fullmatch(game_id.upper())) if game_id else False


def validate_game_id(game_id: str) -> str:
    """Devuelve el game_id normalizado a mayúsculas, o levanta ValueError
    si no es un ID6 válido.

    Usar SIEMPRE esta función antes de meter un game_id en una ruta del
    filesystem (ver el comentario de `_GAME_ID_RE`)."""
    normalized = (game_id or "").upper()
    if not _GAME_ID_RE.fullmatch(normalized):
        raise ValueError(
            f"Game ID inválido: {game_id!r} (se esperaban 6 caracteres A-Z/0-9)"
        )
    return normalized


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
    # Si el ID no tiene la forma de un ID6 real, este archivo no se
    # considera identificado (lo maneja `library.identify_file`, que cae
    # a `wit` y después a "no identificado"): nunca se devuelve un
    # game_id que después terminaría formando parte de una ruta.
    if not is_valid_game_id(game_id):
        return None
    game_id = validate_game_id(game_id)

    title_raw = data[HEADER_TITLE_OFFSET:HEADER_TITLE_OFFSET + HEADER_TITLE_MAX_LEN]
    title = title_raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()

    return DiscInfo(game_id=game_id, title=title or game_id, source="iso")
