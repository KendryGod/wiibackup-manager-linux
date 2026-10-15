"""Packs de taller: listas con nombre de juegos que se cargan juntos
("Pack Mario", "Pack niños"), para marcarlos de una vez en Transferir.

Se guardan en config.json (`config.Settings.packs`) como
{nombre: [ID6, ...]}: por ID6 y no por archivo, así el mismo pack sirve
aunque la biblioteca se reorganice, se renombre o se convierta (un ISO
pasado a WBFS sigue siendo RMCE01). Nada de este módulo toca el disco.
"""
from __future__ import annotations

from typing import Optional

from .disc_header import is_valid_game_id

# Lo que puede tener de malo un nombre nuevo (ver `check_name`).
EMPTY = "empty"
DUPLICATE = "duplicate"

# Hasta dónde se muestra un nombre en el menú: uno de 300 caracteres
# (editado a mano en config.json) estiraba el menú hasta salirse de la
# pantalla. Se recorta solo al mostrarlo; el pack sigue llamándose igual.
_SHORT_NAME = 40


def clean_name(name) -> str:
    """`name` como se guarda y se compara: sin caracteres no imprimibles
    (un tab, un salto de línea, un NUL de un JSON editado a mano) y con los
    espacios colapsados. "" si no queda nada o no es texto."""
    if not isinstance(name, str):
        return ""
    visibles = "".join(ch if ch.isprintable() else " " for ch in name)
    return " ".join(visibles.split())


def short_name(name: str) -> str:
    if len(name) <= _SHORT_NAME:
        return name
    return name[:_SHORT_NAME - 1] + "…"


def _key(name: str) -> str:
    return clean_name(name).casefold()


def find(packs: dict, name: str) -> Optional[str]:
    """La clave de `packs` que es `name` sin distinguir mayúsculas ni
    espacios de más, o None."""
    buscado = _key(name)
    return next((k for k in packs if _key(k) == buscado), None)


def check_name(packs: dict, name, renaming: Optional[str] = None) -> tuple:
    """(nombre limpio, problema), con problema EMPTY, DUPLICATE o None.

    Repetido es que otro pack ya se llame así sin distinguir mayúsculas:
    "pack mario" y "Pack Mario" en el mismo menú no se distinguen, y
    guardar uno no puede pisar al otro. `renaming` es el pack que se está
    renombrando, que no cuenta: cambiarle solo las mayúsculas vale."""
    limpio = clean_name(name)
    if not limpio:
        return limpio, EMPTY
    otro = find(packs, limpio)
    if otro is not None and otro != renaming:
        return limpio, DUPLICATE
    return limpio, None


def clean_ids(raw) -> list:
    """Los ID6 válidos de `raw`, en mayúsculas, sin repetir y en orden. Lo
    que no es texto o no es un ID6 se descarta."""
    ids = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, str):
            continue
        game_id = item.strip().upper()
        if is_valid_game_id(game_id) and game_id not in ids:
            ids.append(game_id)
    return ids


def clean_packs(raw) -> dict:
    """Deja solo los packs que tengan forma de tal.

    La validación por tipo de `Settings.load` asegura que `packs` es un
    objeto, pero no dice nada de lo que hay adentro: un config.json
    editado a mano puede traer un pack que no es una lista, IDs que no son
    texto o nombres vacíos. Se descarta lo que no sirve y se conserva el
    resto, como hace `clean_presets` con los destinos. Si dos nombres son
    el mismo (ver `check_name`), queda el primero."""
    packs = {}
    for name, ids in (raw.items() if isinstance(raw, dict) else ()):
        nombre = clean_name(name)
        if not nombre or not isinstance(ids, list) or find(packs, nombre) is not None:
            continue
        packs[nombre] = clean_ids(ids)
    return packs


def renamed(packs: dict, old: str, new: str) -> dict:
    """`packs` con `old` llamado `new`, en el mismo lugar del orden."""
    return {(new if k == old else k): v for k, v in packs.items()}


def ids_from_selection(games) -> list:
    """Los ID6 de `games` para guardarlos como pack: sin los que no están
    identificados ("??????") y sin repetir (los dos discos de un juego de
    GameCube, o el mismo juego en ISO y en WBFS, son un solo ID)."""
    return clean_ids([g.game_id for g in games])


def match(ids, games) -> tuple:
    """(juegos de `games` a marcar, IDs de `ids` que no están).

    Uno por disco: los dos discos de un juego de GameCube se marcan los
    dos, pero si el mismo juego está dos veces en la biblioteca (en ISO y
    en WBFS) se marca el primero, para no copiarlo dos veces."""
    pedidos = set(clean_ids(list(ids)))
    elegidos, vistos = [], set()
    for game in games:
        game_id = (game.game_id or "").upper()
        clave = (game_id, game.disc_number)
        if game_id in pedidos and clave not in vistos:
            vistos.add(clave)
            elegidos.append(game)
    encontrados = {game_id for game_id, _disco in vistos}
    faltan = [game_id for game_id in clean_ids(list(ids)) if game_id not in encontrados]
    return elegidos, faltan
