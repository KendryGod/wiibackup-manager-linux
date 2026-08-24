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

import io
import threading
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import zipfile
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
DEFAULT_COVER_REGION = "EN"
REQUEST_TIMEOUT = 5
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def cover_cache_path(game_id: str, region: str = "EN") -> Path:
    """Ruta del PNG cacheado para `game_id` en esa región.

    La región va en el nombre del archivo, no solo en la clave de
    deduplicación en memoria: guardándolo como "RMCE01.png" a secas, la
    primera carátula que se bajaba (digamos la EN) se quedaba con el
    nombre, y cuando el usuario cambiaba la región en Preferencias la
    caché contestaba que ya la tenía y nunca se bajaba la nueva. El
    selector de región no hacía nada después del primer uso.

    Levanta ValueError si el ID no es un ID6 válido: acá el ID se
    convierte en nombre de archivo dentro de la caché (y más abajo en
    parte de una URL), así que no puede venir crudo del header de un
    archivo. Ver `disc_header.validate_game_id`. La región se normaliza a
    letras y números por el mismo motivo."""
    safe_region = "".join(ch for ch in (region or DEFAULT_COVER_REGION)
                           if ch.isalnum()).upper() or DEFAULT_COVER_REGION
    return config.COVERS_DIR / f"{validate_game_id(game_id)}.{safe_region}.png"


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
    cache_path = cover_cache_path(game_id, region)
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
    cached = cover_cache_path(game_id, region)
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
# GameTDB publica el volcado como ZIP, no como XML suelto: la URL
# .../wiitdb.xml responde 404 (probado contra el servidor real), así que
# hasta ahora esta parte no traía nunca ningún dato y el panel de detalle
# mostraba siempre "no se encontró información adicional". El ZIP pesa
# ~8 MB y adentro trae un único miembro, wiitdb.xml, de ~32 MB.
WIITDB_URL = "https://www.gametdb.com/wiitdb.zip"
WIITDB_MEMBER = "wiitdb.xml"
WIITDB_DOWNLOAD_TIMEOUT = 60
# Tope de lo que se acepta descomprimir del ZIP. El volcado real ronda los
# 32 MB; 256 MB deja margen de sobra para que crezca sin que un ZIP
# malformado (o una "zip bomb") pueda llenar la caché del usuario.
WIITDB_MAX_UNCOMPRESSED = 256 * 1024 * 1024

# La región de las carátulas es también el idioma con el que se busca el
# título y la sinopsis en wiitdb.xml. "US" no es un idioma: es la región
# NTSC-U, cuyo idioma en la base es EN. El resto de las opciones que
# ofrece Preferencias ya son códigos de idioma válidos de la base.
_REGION_TO_LANGUAGE = {"US": "EN"}
DEFAULT_LANGUAGE = "EN"


def language_for_region(region: str) -> str:
    """Idioma de wiitdb.xml para una región de carátulas de Preferencias."""
    region = (region or DEFAULT_LANGUAGE).upper()
    return _REGION_TO_LANGUAGE.get(region, region)


_wiitdb_lock = threading.Lock()
_wiitdb_index: Optional[dict[str, ET.Element]] = None


# Vocabulario de accesorios de GameTDB (documentado en la cabecera del
# propio wiitdb.xml) traducido a nombres que se entiendan en la interfaz.
# Un tipo que no esté acá se muestra tal como vino: la lista de GameTDB
# crece (en el volcado actual ya aparecen "mii", "3dglasses" y "gameboy
# advance", que no figuran en su documentación), y mostrar el nombre crudo
# es preferible a tragarse el dato.
CONTROL_LABELS = {
    "wiimote": "Wii Remote",
    "nunchuk": "Nunchuk",
    "motionplus": "Wii MotionPlus",
    "classiccontroller": "Classic Controller",
    "gamecube": "Mando de GameCube",
    "wheel": "Wii Wheel",
    "zapper": "Wii Zapper",
    "balanceboard": "Wii Balance Board",
    "wiispeak": "Wii Speak",
    "microphone": "Micrófono",
    "guitar": "Guitarra",
    "drums": "Batería",
    "dancepad": "Alfombra de baile",
    "keyboard": "Teclado USB",
    "nintendods": "Nintendo DS",
    "udraw": "uDraw GameTablet",
    "mii": "Mii",
    "3dglasses": "Gafas 3D",
    "gameboy advance": "Game Boy Advance",
}


@dataclass(frozen=True)
class GameControl:
    """Un accesorio soportado por el juego, según GameTDB."""

    type: str
    required: bool

    @property
    def label(self) -> str:
        return CONTROL_LABELS.get(self.type, self.type)

    def describe(self) -> str:
        """Nombre para mostrar, aclarando si es opcional. Los obligatorios
        van sin aclaración: son el caso normal y ponerles "(obligatorio)"
        a todos convertiría la lista en ruido."""
        return self.label if self.required else f"{self.label} (opcional)"


@dataclass
class GameExtraInfo:
    genre: Optional[str] = None
    players: Optional[str] = None
    release_date: Optional[str] = None
    publisher: Optional[str] = None
    developer: Optional[str] = None
    # Título en el idioma configurado por el usuario y título original
    # (EN) de GameTDB. Se guardan los dos porque el título que la app ya
    # muestra sale del header del disco (o de la base de `wit`), y puede
    # coincidir con cualquiera de los dos: quien los muestra decide cuál
    # aporta algo y cuál sería un duplicado. Ver `title_to_show_next_to`.
    localized_title: Optional[str] = None
    original_title: Optional[str] = None
    # Sinopsis en el idioma configurado, cayendo al inglés si ese idioma no
    # la tiene (la trae el 80% de los juegos en EN y el 51% en ES).
    synopsis: Optional[str] = None
    # Accesorios soportados, en el orden en que los lista GameTDB (que
    # arranca por el mando principal). Tupla y no lista: este dataclass se
    # comparte entre hilos, y una tupla no se puede modificar sin querer
    # desde la interfaz.
    controls: tuple = ()

    def is_empty(self) -> bool:
        return not any((self.genre, self.players, self.release_date,
                         self.publisher, self.developer,
                         self.localized_title, self.original_title,
                         self.synopsis, self.controls))

    def title_to_show_next_to(self, displayed_title: str):
        """Devuelve (etiqueta, título) con el título de GameTDB que vale la
        pena mostrar al lado de `displayed_title`, o None si no aporta
        nada.

        La comparación es laxa a propósito (sin mayúsculas, sin espacios
        ni puntuación): muchos headers de disco traen el título en
        mayúsculas o con la puntuación cambiada ("SUPER SMASH BROS BRAWL"
        contra "Super Smash Bros. Brawl"), y repetir eso como si fuera un
        dato nuevo es ruido, no información.

        Se prioriza el título original (EN) porque es el que el usuario
        pidió ver; si ese ya es el que se está mostrando, se ofrece el
        localizado, que en ~1 de cada 6 juegos de la base difiere del
        inglés y sí es información nueva."""
        shown = _normalize_title(displayed_title)
        if self.original_title and _normalize_title(self.original_title) != shown:
            return ("Título original", self.original_title)
        if self.localized_title and _normalize_title(self.localized_title) != shown:
            return ("Título traducido", self.localized_title)
        return None


def _normalize_title(title: str) -> str:
    """Título reducido a letras y números en minúscula, para comparar dos
    títulos sin que los separe solo la puntuación o las mayúsculas."""
    return "".join(ch for ch in (title or "").lower() if ch.isalnum())


def wiitdb_cache_path() -> Path:
    return config.CACHE_DIR / "wiitdb.xml"


def _xml_is_well_formed(data: bytes) -> bool:
    """True si `data` es XML parseable de punta a punta.

    Se valida por partes con un parser incremental en vez de con
    `ET.fromstring`: el volcado son 30+ MB y armar el árbol entero solo
    para tirarlo duplicaría el pico de memoria justo cuando después hay
    que armar el índice de verdad. Los elementos ya cerrados se vacían a
    medida que aparecen, así que la validación no acumula el documento."""
    parser = ET.XMLPullParser(events=("end",))
    try:
        for start in range(0, len(data), 1024 * 1024):
            parser.feed(data[start:start + 1024 * 1024])
            for _event, element in parser.read_events():
                element.clear()
        parser.close()
    except ET.ParseError:
        return False
    return True


def _download_wiitdb(force: bool = False) -> Optional[Path]:
    """Descarga el volcado de GameTDB y deja wiitdb.xml en la caché.

    Lo que se baja es un ZIP con un único miembro (wiitdb.xml), así que
    hay un paso de descompresión antes de guardar. Devuelve la ruta del
    XML ya listo, o None si algo falló (sin red, servidor caído, ZIP
    corrupto): en ese caso la app sigue andando sin metadata extendida.

    Con `force=True` se ignora (y se borra) lo que haya en la caché y se
    baja de nuevo. Lo usa `_load_wiitdb_index` cuando el XML cacheado
    resulta ilegible: sin eso, una caché corrupta -de una descarga
    interrumpida por una versión vieja de la app, o de un disco que se
    llenó a mitad de escritura- dejaba el índice vacío para siempre y ni
    la sinopsis ni los controles volvían a aparecer nunca más, sin
    ninguna forma de arreglarlo desde la interfaz."""
    path = wiitdb_cache_path()
    if path.exists():
        if not force:
            return path
        try:
            path.unlink()
        except OSError:
            return None

    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(WIITDB_URL, headers={"User-Agent": "wiibackup-manager-linux"})
        with urllib.request.urlopen(req, timeout=WIITDB_DOWNLOAD_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = resp.read()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Se lee el miembro por nombre y nunca se usa `extract`/
            # `extractall`: esos escriben usando la ruta que viene adentro
            # del ZIP, que es contenido remoto y podría traer "../" para
            # escribir fuera de la carpeta de caché.
            info = zf.getinfo(WIITDB_MEMBER)
            if info.file_size > WIITDB_MAX_UNCOMPRESSED:
                return None
            xml_bytes = zf.read(WIITDB_MEMBER)
    except (zipfile.BadZipFile, KeyError, OSError, EOFError):
        return None

    # Validar ANTES de dejarlo como caché buena: `zf.read` ya verifica el
    # CRC del miembro, pero eso no dice nada de una descarga que quedó
    # cortada de una versión anterior de la app ni de un XML que el
    # servidor haya publicado mal. Un archivo que no parsea guardado como
    # caché válida es exactamente lo que dejaba el índice vacío para
    # siempre.
    if not _xml_is_well_formed(xml_bytes):
        return None

    # Escribir a un temporal y mover: si el proceso se corta a mitad de la
    # escritura (30+ MB), no queremos dejar un wiitdb.xml truncado que
    # después ET.parse() no pueda leer y quede cacheado como "ya existe"
    # para siempre.
    tmp_path = path.with_suffix(".xml.tmp")
    try:
        tmp_path.write_bytes(xml_bytes)
        tmp_path.replace(path)
    except OSError:
        # Si la escritura o el movimiento fallan (disco lleno, permisos,
        # /home montado de otra forma), el temporal de 30 MB quedaba
        # ocupando lugar en la caché para siempre: nadie más lo mira, y el
        # próximo intento escribe sobre él pero solo si llega hasta acá.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return path


def _build_index(path: Path) -> Optional[dict[str, ET.Element]]:
    """Índice por Game ID del XML de `path`, o None si el archivo no se
    puede parsear.

    None y "diccionario vacío" son casos distintos a propósito: el
    primero significa "esta caché no sirve, bajala de nuevo" y el segundo
    "el XML se leyó bien pero no tenía juegos adentro"."""
    try:
        tree = ET.parse(path)
    except (ET.ParseError, OSError):
        return None

    index: dict[str, ET.Element] = {}
    for game_el in tree.getroot().iter("game"):
        id_el = game_el.find("id")
        if id_el is not None and id_el.text:
            index[id_el.text.strip()] = game_el
    return index


def _load_wiitdb_index() -> dict[str, ET.Element]:
    """Índice de wiitdb.xml, bajando el volcado si hace falta.

    El primer intento usa lo que haya en la caché. Si ese archivo no
    parsea (descarga interrumpida, disco lleno, archivo pisado), se lo
    descarta y se baja de nuevo una única vez: reintentar en un bucle no
    tendría sentido, porque si el segundo archivo tampoco parsea el
    problema no es la caché. Sin este reintento, una caché rota se
    quedaba pegada para siempre y la metadata extendida no volvía nunca."""
    for attempt in (1, 2):
        path = _download_wiitdb(force=attempt == 2)
        if path is None:
            return {}
        index = _build_index(path)
        if index is not None:
            return index
    return {}


def _ensure_wiitdb_index() -> dict[str, ET.Element]:
    """Descarga (si hace falta) y parsea wiitdb.xml una sola vez por
    ejecución; llamadas siguientes reusan el índice ya armado en memoria."""
    global _wiitdb_index
    with _wiitdb_lock:
        if _wiitdb_index is not None:
            return _wiitdb_index
        _wiitdb_index = _load_wiitdb_index()
        return _wiitdb_index


def _locale_texts(game_el: ET.Element, tag: str) -> dict:
    """Textos de un <game> por idioma: {"EN": "...", "ES": "...", ...}.

    En wiitdb.xml cada idioma es un <locale lang="XX"> con <title> y
    <synopsis> adentro. Los idiomas que traen la etiqueta vacía (los hay,
    sobre todo <synopsis /> en KO) no entran al diccionario, para que el
    respaldo al inglés se active en vez de dejar el campo en blanco."""
    texts = {}
    for locale_el in game_el.findall("locale"):
        lang = (locale_el.get("lang") or "").upper()
        el = locale_el.find(tag)
        if lang and el is not None and el.text and el.text.strip():
            texts[lang] = el.text.strip()
    return texts


def _clean_synopsis(text: Optional[str]) -> Optional[str]:
    """Normaliza los saltos de línea de una sinopsis de GameTDB.

    Los textos vienen con párrafos separados por líneas que tienen un
    espacio suelto (" \n \n"), que al mostrarlos tal cual dejan huecos
    irregulares. Se limpian los espacios al final de cada línea y se
    colapsan las corridas de líneas vacías a una sola."""
    if not text:
        return None
    lines = [line.strip() for line in text.splitlines()]
    paragraphs = []
    for line in lines:
        if line:
            paragraphs.append(line)
        elif paragraphs and paragraphs[-1] != "":
            paragraphs.append("")
    cleaned = "\n".join(paragraphs).strip()
    return cleaned or None


def _parse_controls(game_el: ET.Element) -> tuple:
    """Accesorios soportados, sin repetidos y respetando el orden de
    GameTDB. Un <control> sin `type` se ignora (no hay nada que mostrar);
    `required` se toma como verdadero salvo que diga explícitamente
    "false", para no marcar como opcional algo que en realidad no sabemos.
    """
    input_el = game_el.find(".//input")
    if input_el is None:
        return ()
    controls = []
    seen = set()
    for control_el in input_el.findall("control"):
        ctype = (control_el.get("type") or "").strip().lower()
        if not ctype or ctype in seen:
            continue
        seen.add(ctype)
        controls.append(GameControl(type=ctype,
                                     required=control_el.get("required") != "false"))
    return tuple(controls)


def get_game_extra_info(game_id: str,
                        language: str = DEFAULT_LANGUAGE) -> Optional[GameExtraInfo]:
    """Busca género, cantidad de jugadores, fecha de lanzamiento, publisher,
    developer y los títulos (original y en `language`) para `game_id` en
    wiitdb.xml. Devuelve None si el juego no está en la base descargada (o
    no se pudo descargar), o si está pero ninguno de estos campos viene
    informado para él."""
    index = _ensure_wiitdb_index()
    game_el = index.get(game_id)
    if game_el is None:
        return None

    def text_of(tag: str) -> Optional[str]:
        child = game_el.find(f".//{tag}")
        if child is not None and child.text and child.text.strip():
            return child.text.strip()
        return None

    lang = language_for_region(language)

    titles = _locale_texts(game_el, "title")
    # El título "original" es el inglés: es el que GameTDB usa como
    # canónico y del que salen las traducciones del resto de los idiomas.
    original_title = titles.get(DEFAULT_LANGUAGE)
    localized_title = titles.get(lang)

    # La sinopsis sí cae al inglés si el idioma configurado no la tiene:
    # a diferencia del título (donde mostrar el inglés cuando ya se ve el
    # inglés no aporta nada), acá el respaldo es la diferencia entre leer
    # de qué se trata el juego o no leer nada.
    synopses = _locale_texts(game_el, "synopsis")
    synopsis = _clean_synopsis(synopses.get(lang) or synopses.get(DEFAULT_LANGUAGE))

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
        localized_title=localized_title,
        original_title=original_title,
        synopsis=synopsis,
        controls=_parse_controls(game_el),
    )
    return None if info.is_empty() else info


# --- Metadata extendida en segundo plano ---
#
# Un solo worker y no un pool: lo caro es armar el índice (bajar 8 MB,
# descomprimir 32 MB de XML y parsearlos), y eso pasa UNA vez por
# ejecución detrás de `_wiitdb_lock`. Con varios workers, todos menos uno
# se quedarían igual esperando ese lock; después de armado, cada consulta
# es una búsqueda en un dict y no necesita paralelismo. Lo importante es
# que nada de esto ocurra en el hilo de GTK: una biblioteca de 300 juegos
# pide metadata para las 300 filas y no puede congelar la ventana.
_metadata_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wiitdb")

ExtraInfoCallback = Callable[[Optional[GameExtraInfo]], None]

# Consultas de metadata en vuelo y resultados ya resueltos, por
# (game_id, idioma). Mismo patrón que el `_inflight` de las carátulas, y
# por el mismo motivo: la Biblioteca, Transferir y el panel de detalle
# piden lo mismo, y cada rescan vuelve a pedir la biblioteca entera. Sin
# esto, 300 juegos generaban 300 tareas encoladas contra UN solo worker,
# cada una reteniendo su callback (y con él la fila que lo creó) hasta que
# le tocara el turno.
#
# La caché de resultados es barata y vale la pena: lo que se guarda es una
# referencia al GameExtraInfo que ya está en memoria dentro del índice de
# wiitdb.xml, no una copia, y evita rehacer la búsqueda y el parseo de
# controles/sinopsis en cada rescan.
_extra_inflight: dict = {}
_extra_cache: dict = {}
_extra_lock = threading.Lock()


def fetch_extra_info_async(game_id: str, language: str = DEFAULT_LANGUAGE,
                           on_done: Optional[ExtraInfoCallback] = None) -> None:
    """Pide la metadata de `game_id` y llama a `on_done(info_o_None)` al
    terminar.

    Igual que `fetch_cover_async`: `on_done` corre en un hilo de fondo (o
    en el hilo que llama, si ya estaba resuelto), así que quien toque
    widgets de GTK adentro tiene que reenviarlo con `GLib.idle_add`. Un
    juego sin identificar se resuelve como None en el acto, sin ocupar el
    worker.

    Dos pedidos iguales no encolan dos tareas: el segundo se cuelga del
    primero, y si el dato ya se resolvió antes se contesta en el acto."""
    if on_done is None:
        return

    if not is_valid_game_id(game_id):
        on_done(None)
        return

    game_id = validate_game_id(game_id)
    key = (game_id, language)

    with _extra_lock:
        if key in _extra_cache:
            cached = _extra_cache[key]
            resolver = True
        else:
            resolver = False
            waiting = _extra_inflight.get(key)
            if waiting is not None:
                # Ya hay una consulta igual en curso: colgarse de ella.
                waiting.append(on_done)
                return
            _extra_inflight[key] = [on_done]
    if resolver:
        on_done(cached)
        return

    _metadata_executor.submit(_run_extra_info_job, key)


def _run_extra_info_job(key: tuple) -> None:
    game_id, language = key
    try:
        info = get_game_extra_info(game_id, language)
    except Exception:
        info = None
    with _extra_lock:
        callbacks = _extra_inflight.pop(key, [])
        _extra_cache[key] = info
    for cb in callbacks:
        try:
            cb(info)
        except Exception:
            # Un callback que falla (p. ej. una fila que ya no existe) no
            # puede llevarse puestos a los demás ni al worker.
            pass


def extra_info_in_flight() -> int:
    """Cuántas consultas de metadata distintas están en curso."""
    with _extra_lock:
        return len(_extra_inflight)
