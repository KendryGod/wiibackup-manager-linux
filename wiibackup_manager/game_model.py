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
from .wit_wrapper import CARACTERES_QUE_WIT_CAMBIA


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


def _titulo_que_wit_respeta(titulo: str) -> str:
    """`titulo` (ya pasado por `sanitize_filename`) sin lo que `wit` le
    cambiaría al nombre de un destino (ver
    `wit_wrapper.wit_respeta_el_nombre`): esos caracteres pasan a ser un
    espacio y los espacios quedan de a uno y sin los de los extremos.

    `sanitize_filename` saca la "/" o los ":" pero deja los espacios que
    los rodeaban: "Ocarina of Time / Master Quest" quedaba con dos
    espacios, y `wit` escribe ese nombre con uno solo. No va adentro de
    `sanitize_filename` porque esa también arma la carpeta de un juego de
    GameCube en la unidad, que tiene que seguir llamándose como en las
    unidades ya preparadas."""
    limpio = "".join(" " if (c in CARACTERES_QUE_WIT_CAMBIA or ord(c) < 0x20 or c == "\x7f")
                     else c for c in titulo)
    return " ".join(limpio.split()) or "untitled"


def title_is_usable(title: str) -> bool:
    """True si `title` sirve para armar un nombre de archivo: no vacío,
    sin caracteres de reemplazo (U+FFFD, lo que deja un título que no se
    pudo decodificar) y sin caracteres no imprimibles."""
    return (bool(title.strip()) and "\ufffd" not in title
            and title.isprintable())


def standard_filename(game: Game) -> str:
    """Nombre estándar sugerido: 'Título [GAMEID].ext', como usa el
    WiiBackup Manager clásico y la mayoría de USB Loaders.

    Si el juego no está identificado (o su ID no es un ID6 válido) se
    omite el sufijo '[ID]' en vez de meter en el nombre del archivo algo
    que no es un ID: '??????' trae caracteres que FAT32 no acepta, y un ID
    arbitrario leído del header podría traer separadores de ruta.

    El título va sin nada que `wit` le cambiaría al nombre (ver
    `_titulo_que_wit_respeta`): convertir el juego después tiene que
    poder escribir el archivo con este mismo nombre."""
    safe_title = _titulo_que_wit_respeta(sanitize_filename(game.title))
    ext = game.path.suffix
    if not is_valid_game_id(game.game_id):
        return f"{safe_title}{ext}"
    return f"{safe_title} [{validate_game_id(game.game_id)}]{ext}"


def same_filename(a: str, b: str) -> bool:
    """True si `a` y `b` son el mismo nombre salvo la forma de
    normalización Unicode (NFC/NFD).

    `sanitize_filename` arma los nombres en NFKD ("é" como "e" + tilde
    combinante), y un archivo con la "é" compuesta (NFC) se ve idéntico:
    cambiar uno por el otro no es renombrar nada. Una diferencia de
    compatibilidad (una letra de ancho completo, que NFKD convierte) sí
    cuenta: NFC no la toca."""
    return unicodedata.normalize("NFC", a) == unicodedata.normalize("NFC", b)


def needs_rename(game: Game) -> bool:
    """True si el archivo no está ya con el nombre estándar (ignorando la
    forma de normalización, ver `same_filename`). Un juego cuyo título no
    sirve para un nombre de archivo (`title_is_usable`) no se renombra."""
    if not title_is_usable(game.title):
        return False
    return not same_filename(game.path.name, standard_filename(game))
