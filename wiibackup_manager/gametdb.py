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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import config
from .disc_header import is_valid_game_id, validate_game_id

COVER_URL_TEMPLATE = "https://art.gametdb.com/wii/cover/{region}/{game_id}.png"
# GameTDB no siempre sube la carátula bajo la región "EN": muchos títulos
# NTSC-U (p.ej. SMNE01, New Super Mario Bros. Wii) sólo existen bajo "US".
# Probamos la región pedida y después esta lista de respaldo, en orden.
COVER_FALLBACK_REGIONS = ["US", "EN", "DE", "FR", "JA", "KO"]
REQUEST_TIMEOUT = 5
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def cover_cache_path(game_id: str) -> Path:
    """Ruta del PNG cacheado para `game_id`. Levanta ValueError si el ID no
    es un ID6 válido: acá el ID se convierte en nombre de archivo dentro de
    la caché (y más abajo en parte de una URL), así que no puede venir
    crudo del header de un archivo. Ver `disc_header.validate_game_id`."""
    return config.COVERS_DIR / f"{validate_game_id(game_id)}.png"


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

    Devuelve None si no se pudo obtener de ninguna región, incluido el caso
    de un juego sin identificar ("??????") o con un ID que no es un ID6
    válido: para esos no hay carátula que pedir y su ID no puede usarse ni
    como nombre de archivo de caché ni dentro de la URL.
    """
    if not is_valid_game_id(game_id):
        return None
    game_id = validate_game_id(game_id)
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


# --- Descarga de carátulas en segundo plano (pool compartido) ---
#
# Todas las vistas que muestran carátulas (Biblioteca, Transferir, panel de
# detalle) pasan por acá. Antes cada una tenía su propia estrategia: la
# Biblioteca usaba un pool acotado y Transferir lanzaba un
# `threading.Thread` por fila, así que con 300 juegos podía disparar 300
# descargas simultáneas contra GameTDB (saturando la conexión y haciendo
# que el servidor rechace pedidos), mientras la Biblioteca hacía 6.
#
# Con un solo pool compartido el límite es global de verdad: no importa
# cuántas vistas pidan carátulas al mismo tiempo, nunca hay más de
# `_COVER_DOWNLOAD_WORKERS` descargas en vuelo. Y una carátula lenta o
# colgada ocupa como mucho un worker; el resto sigue.
_COVER_DOWNLOAD_WORKERS = 6
_cover_executor = ThreadPoolExecutor(
    max_workers=_COVER_DOWNLOAD_WORKERS, thread_name_prefix="cover-dl"
)

# Descargas en vuelo, por (game_id, región). Un rescan reconstruye todas
# las filas y vuelve a pedir las mismas carátulas: sin esto, cada rescan
# encolaría de nuevo descargas que ya están corriendo. En vez de eso, el
# pedido nuevo se cuelga del que ya está en curso y recibe el mismo
# resultado cuando termina.
_inflight: dict = {}
_inflight_lock = threading.Lock()

CoverCallback = Callable[[Optional[Path]], None]


def fetch_cover_async(game_id: str, region: str = "EN",
                      on_done: Optional[CoverCallback] = None) -> None:
    """Pide la carátula de `game_id` y llama a `on_done(path_o_None)` al
    terminar.

    Ojo: `on_done` se llama desde un hilo del pool (o desde el hilo que
    llama, si la carátula ya estaba en caché), así que quien toque widgets
    de GTK adentro tiene que reenviarlo con `GLib.idle_add`.

    Dos atajos evitan trabajo al pedo: un juego sin identificar no tiene
    carátula que pedir, y una carátula ya cacheada se resuelve en el acto
    sin ocupar un worker (importante en un rescan, donde se repiden todas
    las carátulas de la biblioteca de una)."""
    if on_done is None:
        return

    if not is_valid_game_id(game_id):
        on_done(None)
        return

    game_id = validate_game_id(game_id)
    cached = cover_cache_path(game_id)
    if _is_valid_cached_cover(cached):
        on_done(cached)
        return

    key = (game_id, region)
    with _inflight_lock:
        waiting = _inflight.get(key)
        if waiting is not None:
            # Ya hay una descarga en curso para esta carátula: colgarse de
            # ella en vez de encolar otra igual.
            waiting.append(on_done)
            return
        _inflight[key] = [on_done]

    _cover_executor.submit(_run_cover_job, key)


def _run_cover_job(key: tuple) -> None:
    game_id, region = key
    try:
        path = get_cover_path(game_id, region)
    except Exception:
        path = None
    with _inflight_lock:
        callbacks = _inflight.pop(key, [])
    for cb in callbacks:
        try:
            cb(path)
        except Exception:
            # Un callback que falla (p. ej. una fila que ya no existe) no
            # puede llevarse puestos a los demás ni al worker del pool.
            pass


def covers_in_flight() -> int:
    """Cuántas carátulas distintas se están descargando ahora mismo."""
    with _inflight_lock:
        return len(_inflight)


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
