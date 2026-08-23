"""Integración con GameTDB (https://www.gametdb.com).

Dos usos:
1. Descargar la carátula (cover) de un juego a partir de su Game ID de 6
   caracteres, cacheándola en disco para no volver a pedirla.
2. (Opcional) Resolver el título completo de un juego cuando el header del
   disco no trae un nombre útil.

No requiere API key: las carátulas se sirven como imágenes estáticas en
art.gametdb.com y son de uso libre para proyectos como este.
"""
from __future__ import annotations

import threading
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import config

COVER_URL_TEMPLATE = "https://art.gametdb.com/wii/cover/{region}/{game_id}.png"
# GameTDB no siempre sube la carátula bajo la región "EN": muchos títulos
# NTSC-U (p.ej. SMNE01, New Super Mario Bros. Wii) sólo existen bajo "US".
# Probamos la región pedida y después esta lista de respaldo, en orden.
COVER_FALLBACK_REGIONS = ["US", "EN", "DE", "FR", "JA", "KO"]
REQUEST_TIMEOUT = 5
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def cover_cache_path(game_id: str) -> Path:
    return config.COVERS_DIR / f"{game_id}.png"


def _is_valid_cached_cover(path: Path) -> bool:
    """True si el archivo cacheado es un PNG con contenido real.

    Una descarga interrumpida a medias (proceso matado, conexión cortada)
    puede dejar un archivo de 0 bytes o con datos truncados/no-PNG. Sin esta
    validación, get_cover_path lo trataría como "ya existe" para siempre y
    jamás reintentaría la descarga.
    """
    try:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as f:
            return f.read(8) == PNG_MAGIC
    except OSError:
        return False


def get_cover_path(game_id: str, region: str = "EN", force: bool = False) -> Optional[Path]:
    """Devuelve la ruta local de la carátula, descargándola si hace falta.

    Devuelve None si no se pudo obtener de ninguna región.
    """
    cache_path = cover_cache_path(game_id)
    if cache_path.exists():
        if not force and _is_valid_cached_cover(cache_path):
            return cache_path
        # Cache corrupta (0 bytes / no-PNG) o se pidió forzar: la borramos
        # para que el intento de descarga de abajo no la deje pisada a
        # medias si también falla.
        try:
            cache_path.unlink()
        except OSError:
            pass

    config.COVERS_DIR.mkdir(parents=True, exist_ok=True)

    regions_to_try = [region] + [r for r in COVER_FALLBACK_REGIONS if r != region]

    for r in regions_to_try:
        url = COVER_URL_TEMPLATE.format(region=r, game_id=game_id)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wiibackup-manager-linux"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 200:
                    data = resp.read()
                    if data.startswith(PNG_MAGIC):
                        cache_path.write_bytes(data)
                        return cache_path
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
            continue

    return None


# --- Metadata extendida (género, jugadores, fecha, publisher, developer) ---
#
# A diferencia de las carátulas (una URL simple por Game ID), GameTDB no
# expone esos campos en un endpoint por juego: solo los publica dentro del
# volcado completo "wiitdb.xml" (documentado en gametdb.com/wiitdb.xml, el
# formato "datafile"/"game" que también consumen scrapers como los de
# EmulationStation/RetroPie para Wii). Por eso acá se descarga y cachea ese
# XML completo una sola vez y se arma un índice por Game ID en memoria, en
# vez de pedir algo por juego. El parseo es tolerante (busca las etiquetas
# en cualquier profundidad dentro de <game>): si GameTDB no trae alguno de
# estos campos para un juego puntual, o el XML no pudo descargarse, se
# devuelve None para ese campo (o para todo) en vez de inventar un valor.
WIITDB_URL = "https://www.gametdb.com/wiitdb.xml"
WIITDB_DOWNLOAD_TIMEOUT = 30

_wiitdb_lock = threading.Lock()
_wiitdb_index: Optional[dict[str, ET.Element]] = None


@dataclass
class GameExtraInfo:
    genre: Optional[str] = None
    players: Optional[str] = None
    release_date: Optional[str] = None
    publisher: Optional[str] = None
    developer: Optional[str] = None

    def is_empty(self) -> bool:
        return not any((self.genre, self.players, self.release_date,
                         self.publisher, self.developer))


def wiitdb_cache_path() -> Path:
    return config.CACHE_DIR / "wiitdb.xml"


def _download_wiitdb() -> Optional[Path]:
    path = wiitdb_cache_path()
    if path.exists():
        return path

    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(WIITDB_URL, headers={"User-Agent": "wiibackup-manager-linux"})
        with urllib.request.urlopen(req, timeout=WIITDB_DOWNLOAD_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return None

    # Escribir a un temporal y mover: si el proceso se corta a mitad de la
    # descarga (20+ MB), no queremos dejar un wiitdb.xml truncado que
    # después ET.parse() no pueda leer y quede cacheado como "ya existe"
    # para siempre.
    tmp_path = path.with_suffix(".xml.tmp")
    try:
        tmp_path.write_bytes(data)
        tmp_path.replace(path)
    except OSError:
        return None
    return path


def _build_index(path: Path) -> dict[str, ET.Element]:
    index: dict[str, ET.Element] = {}
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return index

    for game_el in tree.getroot().iter("game"):
        id_el = game_el.find("id")
        if id_el is not None and id_el.text:
            index[id_el.text.strip()] = game_el
    return index


def _ensure_wiitdb_index() -> dict[str, ET.Element]:
    """Descarga (si hace falta) y parsea wiitdb.xml una sola vez por
    ejecución; llamadas siguientes reusan el índice ya armado en memoria."""
    global _wiitdb_index
    with _wiitdb_lock:
        if _wiitdb_index is not None:
            return _wiitdb_index
        path = _download_wiitdb()
        _wiitdb_index = _build_index(path) if path is not None else {}
        return _wiitdb_index


def get_game_extra_info(game_id: str) -> Optional[GameExtraInfo]:
    """Busca género, cantidad de jugadores, fecha de lanzamiento, publisher
    y developer para `game_id` en wiitdb.xml. Devuelve None si el juego no
    está en la base descargada (o no se pudo descargar), o si está pero
    ninguno de estos campos viene informado para él."""
    index = _ensure_wiitdb_index()
    game_el = index.get(game_id)
    if game_el is None:
        return None

    def text_of(tag: str) -> Optional[str]:
        child = game_el.find(f".//{tag}")
        if child is not None and child.text and child.text.strip():
            return child.text.strip()
        return None

    players = None
    input_el = game_el.find(".//input")
    if input_el is not None:
        players = input_el.get("players")

    release_date = None
    date_el = game_el.find(".//date")
    if date_el is not None:
        parts = [date_el.get("year"), date_el.get("month"), date_el.get("day")]
        parts = [p for p in parts if p]
        if parts:
            release_date = "-".join(parts)

    info = GameExtraInfo(
        genre=text_of("genre"),
        players=players,
        release_date=release_date,
        publisher=text_of("publisher"),
        developer=text_of("developer"),
    )
    return None if info.is_empty() else info
