"""Lectura del header de disco Wii/GameCube para archivos ISO planos (y,
para el número de disco nomás, también CISO).

Esto es un *fallback* que funciona sin dependencias externas: el formato
del header de un disco de Wii o GameCube está documentado, es el mismo
para las dos consolas salvo el magic word, y no cambia entre juegos:

    offset 0x00 .. 0x06   -> Game ID (6 bytes ASCII, ej. "RMCE01"/"GZ2E01")
    offset 0x06           -> número de disco (0 = disco 1, 1 = disco 2, ...)
    offset 0x07           -> versión del disco
    offset 0x18 .. 0x1C   -> magic word de disco Wii  = 0x5D1C9EA3
    offset 0x1C .. 0x20   -> magic word de disco GameCube = 0xC2339F3D
    offset 0x20 .. 0x60   -> título del juego (ASCII, termina en \\x00)

Para archivos .wbfs (que envuelven la ISO en un contenedor con su propio
header) NO se reimplementa el parseo aquí: es fácil equivocarse con el
tamaño de sector y el offset del primer disco, y un error ahí puede hacer
que la app muestre datos incorrectos sobre tus respaldos reales. Para
.wbfs y para conversión/verificación se usa Wiimms ISO Tools (`wit`),
ver wit_wrapper.py, que es el estándar de facto en Linux para esto.

Para .ciso sí se lee acá, pero SOLO el número de disco (ver
`read_ciso_disc_number` más abajo): identificar el juego sigue yendo por
`wit`, que ya sabe leer CISO de punta a punta."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .i18n import _

WII_DISC_MAGIC = b"\x5d\x1c\x9e\xa3"
GC_DISC_MAGIC = b"\xc2\x33\x9f\x3d"
HEADER_TITLE_OFFSET = 0x20
HEADER_TITLE_MAX_LEN = 0x40
HEADER_MAGIC_OFFSET = 0x18  # Wii; el de GameCube va 4 bytes después, ver abajo.
GC_HEADER_MAGIC_OFFSET = 0x1C
DISC_NUMBER_OFFSET = 0x06
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
            _("Game ID inválido: {game_id!r} (se esperaban 6 caracteres A-Z/0-9)")
            .format(game_id=game_id)
        )
    return normalized


@dataclass
class DiscInfo:
    game_id: str
    title: str
    source: str  # "iso" | "wit"
    console: str = "wii"  # "wii" | "gc"
    disc_number: int = 0  # 0 = disco 1, 1 = disco 2, ... (offset 0x06)


def read_plain_iso_header(path: Path) -> Optional[DiscInfo]:
    """Lee el header de una ISO de Wii o GameCube sin envolver. Devuelve
    None si el archivo no parece ser un disco válido de ninguna de las dos
    consolas (no tiene ninguno de los dos magic words)."""
    try:
        with open(path, "rb") as f:
            data = f.read(0x100)
    except OSError:
        return None

    if len(data) < 0x60:
        return None

    if data[HEADER_MAGIC_OFFSET:HEADER_MAGIC_OFFSET + 4] == WII_DISC_MAGIC:
        console = "wii"
    elif data[GC_HEADER_MAGIC_OFFSET:GC_HEADER_MAGIC_OFFSET + 4] == GC_DISC_MAGIC:
        console = "gc"
    else:
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

    # Nintendo nunca sacó un disco más allá del segundo: un valor más alto
    # acá es un header corrupto (o un archivo que no es lo que dice ser), y
    # confiarlo ciegamente terminaría armando un nombre de archivo
    # "discN.ext" sin sentido. Ante la duda, se trata como disco 1.
    disc_number = data[DISC_NUMBER_OFFSET] if data[DISC_NUMBER_OFFSET] < 8 else 0

    title_raw = data[HEADER_TITLE_OFFSET:HEADER_TITLE_OFFSET + HEADER_TITLE_MAX_LEN]
    title = title_raw.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()

    return DiscInfo(game_id=game_id, title=title or game_id, source="iso",
                     console=console, disc_number=disc_number)


# --- Número de disco para CISO ---
#
# CISO envuelve el disco en bloques: un header fijo de 0x8000 bytes con la
# firma "CISO", el tamaño de bloque (u32 LE) y un mapa de un byte por
# bloque (1 = bloque presente, 0 = ausente), y a continuación los bloques
# marcados como presentes, concatenados EN ORDEN DE ÍNDICE. El bloque 0
# -offset 0 del disco original, justo donde vive el header que se parsea
# arriba- está presente en cualquier volcado real: sin él no hay ni
# siquiera forma de identificar el disco. Por eso, si el mapa lo marca
# presente, sus datos son SIEMPRE los primeros del área de datos, pegados
# justo después del header de 0x8000 bytes, sin tener que sumarle el
# tamaño de ningún otro bloque.
#
# Confirmado contra un CISO sintético con estos offsets: `wit LIST
# --sections` sobre ese archivo leyó el Game ID y el tipo de disco
# correctos, lo que valida que el área de datos arranca donde dice el
# formato documentado.
#
# Esto se usa solo para el número de disco (multidisco de GameCube, ver
# `library.gc_dest_path`): identificar el juego sigue yendo por `wit`
# (`wit_wrapper.identify`), que sabe leer CISO de punta a punta y ya es el
# camino probado para eso.
CISO_MAGIC = b"CISO"
CISO_HEADER_SIZE = 0x8000
CISO_BLOCK_SIZE_OFFSET = 4
CISO_BLOCK_MAP_OFFSET = 8


def read_ciso_disc_number(path: Path) -> int:
    """Número de disco (0 = disco 1, 1 = disco 2, ...) leído directo de un
    .ciso, o 0 si el archivo no es un CISO reconocible, el bloque 0 no
    está presente, o cualquier otra cosa no cierra. 0 (disco 1) es el
    respaldo correcto: es lo que vale para la enorme mayoría de los
    juegos, que no son multidisco."""
    try:
        with open(path, "rb") as f:
            header = f.read(CISO_HEADER_SIZE)
            if len(header) < CISO_HEADER_SIZE or header[0:4] != CISO_MAGIC:
                return 0
            block_size = int.from_bytes(
                header[CISO_BLOCK_SIZE_OFFSET:CISO_BLOCK_SIZE_OFFSET + 4], "little")
            if block_size == 0:
                return 0
            if header[CISO_BLOCK_MAP_OFFSET] != 1:
                # El bloque 0 no está marcado presente: este volcado no
                # trae ni el header del disco, así que tampoco tiene
                # sentido que lo identifique read_plain_iso_header vía
                # `wit` haya funcionado. No hay nada confiable que leer.
                return 0
            f.seek(CISO_HEADER_SIZE + DISC_NUMBER_OFFSET)
            raw = f.read(1)
    except OSError:
        return 0
    if not raw:
        return 0
    value = raw[0]
    return value if value < 8 else 0
