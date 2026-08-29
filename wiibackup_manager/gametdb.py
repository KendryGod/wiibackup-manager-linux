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
import os
import sys
import threading
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import config
from .disc_header import is_valid_game_id, validate_game_id
from .atomicfs import atomic_write_target
from .fsutil import PNG_MAGIC
from .i18n import _
from .inflight import InflightRegistry

# Una plantilla por consola, aunque hoy las dos apunten al mismo lugar:
# se probó en serio contra el servidor (pedidos reales, no documentación)
# que GameTDB NO separa las carátulas por consola bajo art.gametdb.com —
# tanto Wii como GameCube se sirven bajo /wii/cover/{region}/{id6}.png. La
# ruta /gc/cover/... que este diccionario usaba antes daba 404 SIEMPRE
# (confirmado con GZ2E01 y GMSE01, dos juegos de GameCube distintos), así
# que ninguna carátula de GameCube se llegaba a descargar. Se deja la
# entrada "gc" como clave separada -en vez de borrar el dict y usar un
# solo template- por dos motivos: si GameTDB alguna vez separa de verdad
# por consola alcanza con cambiar un valor acá, y `console` sigue siendo
# la clave de `cover_cache_path` para no mezclar en una sola carpeta la
# caché de las dos consolas.
COVER_URL_TEMPLATES = {
    "wii": "https://art.gametdb.com/wii/cover/{region}/{game_id}.png",
    "gc": "https://art.gametdb.com/wii/cover/{region}/{game_id}.png",
}
DEFAULT_CONSOLE = "wii"
# GameTDB no siempre sube la carátula bajo la región "EN": muchos títulos
# NTSC-U (p.ej. SMNE01, New Super Mario Bros. Wii) sólo existen bajo "US".
# Probamos la región pedida y después esta lista de respaldo, en orden.
COVER_FALLBACK_REGIONS = ["US", "EN", "DE", "FR", "JA", "KO"]
DEFAULT_COVER_REGION = "EN"
REQUEST_TIMEOUT = 5


def cover_cache_path(game_id: str, region: str = "EN", console: str = DEFAULT_CONSOLE) -> Path:
    """Ruta del PNG cacheado para `game_id` en esa región y consola.

    La región va en el nombre del archivo, no solo en la clave de
    deduplicación en memoria: guardándolo como "RMCE01.png" a secas, la
    primera carátula que se bajaba (digamos la EN) se quedaba con el
    nombre, y cuando el usuario cambiaba la región en Preferencias la
    caché contestaba que ya la tenía y nunca se bajaba la nueva. El
    selector de región no hacía nada después del primer uso.

    La consola NO se agrega al nombre cuando es "wii" (el respaldo), a
    propósito: es la consola con la que esta app trabajó siempre, y
    agregarle un sufijo a todos esos nombres invalidaría de un saque la
    caché de disco de cualquiera que ya la tuviera poblada. Para GameCube
    sí hace falta: un ID6 es, en teoría, un espacio de nombres por
    consola, así que sin el sufijo una carátula de GameCube podría
    convivir (mal) con una de Wii que casualmente comparta ID.

    Levanta ValueError si el ID no es un ID6 válido: acá el ID se
    convierte en nombre de archivo dentro de la caché (y más abajo en
    parte de una URL), así que no puede venir crudo del header de un
    archivo. Ver `disc_header.validate_game_id`. La región se normaliza a
    letras y números por el mismo motivo."""
    safe_region = "".join(ch for ch in (region or DEFAULT_COVER_REGION)
                           if ch.isalnum()).upper() or DEFAULT_COVER_REGION
    suffix = "" if console == DEFAULT_CONSOLE else f".{console}"
    return config.COVERS_DIR / f"{validate_game_id(game_id)}.{safe_region}{suffix}.png"


# Último bloque de todo PNG bien formado (longitud 0 + "IEND" + su CRC,
# que es fijo). Si el archivo no termina con esto, está cortado.
_PNG_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"


def _decodes_as_image(path: Path) -> bool:
    """True si el archivo se puede decodificar entero como imagen.

    Es la validación fuerte, y por eso se usa al GUARDAR una carátula
    recién bajada y no al leerla de la caché: decodificar 300 PNG en cada
    escaneo sería trabajo al pedo. Se usa GdkPixbuf, que es el mismo
    decodificador que después va a tener que mostrar la imagen: si él la
    puede abrir, la fila la va a poder mostrar."""
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        GdkPixbuf.Pixbuf.new_from_file(str(path))
        return True
    except Exception:
        return False


def _is_valid_cached_cover(path: Path) -> bool:
    """True si el archivo cacheado es un PNG completo.

    Una descarga interrumpida a medias (proceso matado, conexión cortada)
    puede dejar un archivo de 0 bytes, o -peor- uno con la cabecera PNG
    correcta y el contenido cortado, que pasaba la validación vieja y
    quedaba cacheado como bueno para siempre.

    Se mira la cabecera Y el bloque final IEND, que es lo que distingue a
    un PNG entero de uno truncado, y cuesta dos lecturas de 8 bytes."""
    try:
        if path.stat().st_size < len(PNG_MAGIC) + len(_PNG_IEND):
            return False
        with path.open("rb") as f:
            if f.read(len(PNG_MAGIC)) != PNG_MAGIC:
                return False
            f.seek(-len(_PNG_IEND), os.SEEK_END)
            return f.read(len(_PNG_IEND)) == _PNG_IEND
    except OSError:
        return False


class _CoverRejected(Exception):
    """Interna: la imagen que se bajó no se pudo decodificar.

    Existe para poder salir del bloque de `atomicfs.atomic_write_target`
    sin que el temporal se mueva a la caché: ese helper reemplaza el
    destino cuando el bloque termina bien y lo descarta cuando sale por una
    excepción, así que "esta imagen no sirve" tiene que viajar como
    excepción y no como un `return`."""


def _store_cover(cache_path: Path, data: bytes) -> bool:
    """Guarda la carátula en la caché, de forma atómica y validada.

    Se escribe primero a un temporal, se comprueba que la imagen se pueda
    decodificar ENTERA y recién entonces se la mueve al nombre definitivo
    (ver `atomicfs.atomic_write_target`). Así la caché nunca tiene un
    archivo a medias: o está la carátula completa o no está, y el
    próximo intento la vuelve a pedir.

    Devuelve False si la imagen no sirve (descarga cortada, el servidor
    devolvió cualquier cosa), sin dejar nada en la caché."""
    try:
        with atomic_write_target(cache_path) as tmp:
            tmp.write_bytes(data)
            if not _decodes_as_image(tmp):
                raise _CoverRejected()
    except (_CoverRejected, OSError):
        return False
    return True


def _log_cover_fetch_failed(game_id: str, console: str,
                            errors: list[tuple[str, str]]) -> None:
    """Avisa por stderr cuando NINGUNA región consiguió la carátula por algo
    que no sea "esta región no la tiene" (404, el caso normal y silencioso:
    la mayoría de los juegos solo están en una o dos regiones).

    Sin esto, un problema real -URL rota, sin red, GameTDB caído- se ve
    exactamente igual que un juego sin carátula: es lo que pasó con
    `COVER_URL_TEMPLATES["gc"]` apuntando a una ruta que daba 404 siempre,
    y no había forma de distinguirlo desde ningún lado hasta que alguien
    probó la URL a mano."""
    detail = "; ".join(f"{region}: {reason}" for region, reason in errors)
    print(f"[wiibackup-manager] no se pudo bajar la carátula de {game_id} "
          f"(consola={console}): {detail}", file=sys.stderr)


def get_cover_path(game_id: str, region: str = "EN", force: bool = False,
                   console: str = DEFAULT_CONSOLE) -> Optional[Path]:
    """Devuelve la ruta local de la carátula, descargándola si hace falta.

    `console` selecciona la plantilla de `COVER_URL_TEMPLATES` (hoy la misma
    URL para "wii" y "gc", ver el comentario ahí) y, sobre todo, en qué
    archivo de caché queda guardada (ver `cover_cache_path`): lo que importa
    de verdad es no mezclar en un mismo nombre de caché la carátula de un
    juego de Wii y la de uno de GameCube que casualmente compartan ID6.

    Devuelve None si no se pudo obtener de ninguna región, incluido el caso
    de un juego sin identificar ("??????") o con un ID que no es un ID6
    válido: para esos no hay carátula que pedir y su ID no puede usarse ni
    como nombre de archivo de caché ni dentro de la URL.
    """
    if not is_valid_game_id(game_id):
        return None
    game_id = validate_game_id(game_id)
    cache_path = cover_cache_path(game_id, region, console)
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
    url_template = COVER_URL_TEMPLATES.get(console, COVER_URL_TEMPLATES[DEFAULT_CONSOLE])

    # Solo se acumulan acá los fallos que NO son un 404 (ver
    # `_log_cover_fetch_failed`): un 404 significa "GameTDB no tiene la
    # carátula en esta región", que es el resultado normal para la mayoría
    # de las regiones de fallback y no algo que valga la pena avisar.
    errors: list[tuple[str, str]] = []

    for r in regions_to_try:
        url = url_template.format(region=r, game_id=game_id)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wiibackup-manager-linux"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status != 200:
                    errors.append((r, f"status HTTP {resp.status}"))
                    continue
                data = resp.read()
                if not data.startswith(PNG_MAGIC):
                    errors.append((r, "la respuesta no es un PNG"))
                    continue
                if _store_cover(cache_path, data):
                    return cache_path
                errors.append((r, "el PNG descargado no se pudo decodificar/guardar"))
        except urllib.error.HTTPError as e:
            if e.code != 404:
                errors.append((r, f"HTTP {e.code}"))
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            errors.append((r, str(e)))

    if errors:
        _log_cover_fetch_failed(game_id, console, errors)
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
#
# La clave del registry es (game_id, región, consola): un rescan
# reconstruye todas las filas y vuelve a pedir las mismas carátulas, y sin
# esto cada rescan encolaría de nuevo descargas que ya están corriendo.
# Ver `inflight.InflightRegistry`, compartido con la metadata de acá abajo
# y con la lista/íconos de `oscwii_client`.
_COVER_DOWNLOAD_WORKERS = 6
_cover_jobs = InflightRegistry(_COVER_DOWNLOAD_WORKERS, "cover-dl")

CoverCallback = Callable[[Optional[Path]], None]


def fetch_cover_async(game_id: str, region: str = "EN",
                      on_done: Optional[CoverCallback] = None,
                      console: str = DEFAULT_CONSOLE) -> None:
    """Pide la carátula de `game_id` y llama a `on_done(path_o_None)` al
    terminar.

    `console` ("wii" o "gc") decide de qué carpeta de GameTDB se pide (ver
    `get_cover_path`); quien llama lo saca de `game.console`.

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
    cached = cover_cache_path(game_id, region, console)
    if _is_valid_cached_cover(cached):
        on_done(cached)
        return

    _cover_jobs.submit(
        (game_id, region, console),
        lambda: get_cover_path(game_id, region, console=console),
        on_done)


def covers_in_flight() -> int:
    """Cuántas carátulas distintas se están descargando ahora mismo."""
    return _cover_jobs.in_flight()


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
    "wiimote": _("Wii Remote"),
    "nunchuk": _("Nunchuk"),
    "motionplus": _("Wii MotionPlus"),
    "classiccontroller": _("Classic Controller"),
    "gamecube": _("Mando de GameCube"),
    "wheel": _("Wii Wheel"),
    "zapper": _("Wii Zapper"),
    "balanceboard": _("Wii Balance Board"),
    "wiispeak": _("Wii Speak"),
    "microphone": _("Micrófono"),
    "guitar": _("Guitarra"),
    "drums": _("Batería"),
    "dancepad": _("Alfombra de baile"),
    "keyboard": _("Teclado USB"),
    "nintendods": _("Nintendo DS"),
    "udraw": _("uDraw GameTablet"),
    "mii": _("Mii"),
    "3dglasses": _("Gafas 3D"),
    "gameboy advance": _("Game Boy Advance"),
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
            return (_("Título original"), self.original_title)
        if self.localized_title and _normalize_title(self.localized_title) != shown:
            return (_("Título traducido"), self.localized_title)
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


# Cuándo falló el último intento de armar el índice, y cuánto esperar
# antes de reintentar. Sin esto, un arranque sin internet dejaba la app
# entera sin metadata hasta cerrarla y volver a abrirla: el índice vacío
# se memorizaba como resultado definitivo.
_wiitdb_failed_at = 0.0
_WIITDB_RETRY_SECONDS = 60.0


def _ensure_wiitdb_index() -> dict[str, ET.Element]:
    """Descarga (si hace falta) y parsea wiitdb.xml una sola vez por
    ejecución; llamadas siguientes reusan el índice ya armado en memoria.

    Un intento que sale vacío (sin internet, servidor caído) NO se guarda
    como resultado final: se reintenta pasado `_WIITDB_RETRY_SECONDS`, así
    que si la conexión vuelve mientras la app está abierta, la metadata
    aparece sola. El tiempo de espera está para no golpear al servidor una
    vez por cada juego de la biblioteca."""
    global _wiitdb_index, _wiitdb_failed_at
    with _wiitdb_lock:
        if _wiitdb_index:
            return _wiitdb_index
        if (_wiitdb_index is not None
                and time.monotonic() - _wiitdb_failed_at < _WIITDB_RETRY_SECONDS):
            # Falló hace poco: no insistir todavía.
            return _wiitdb_index
        _wiitdb_index = _load_wiitdb_index()
        if not _wiitdb_index:
            _wiitdb_failed_at = time.monotonic()
        return _wiitdb_index


def wiitdb_index_available() -> bool:
    """True si el índice está armado y tiene juegos, o sea si una consulta
    que devuelve None significa de verdad "GameTDB no lo tiene"."""
    with _wiitdb_lock:
        return bool(_wiitdb_index)


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
ExtraInfoCallback = Callable[[Optional[GameExtraInfo]], None]

# Consultas de metadata en vuelo y resultados ya resueltos, por
# (game_id, idioma). El mismo registry que usan las carátulas de acá
# arriba, y por el mismo motivo: la Biblioteca, Transferir y el panel de
# detalle piden lo mismo, y cada rescan vuelve a pedir la biblioteca
# entera. Sin esto, 300 juegos generaban 300 tareas encoladas contra UN
# solo worker, cada una reteniendo su callback (y con él la fila que lo
# creó) hasta que le tocara el turno.
#
# `remember_results=True`: acá, a diferencia de las carátulas, el
# resultado se guarda en memoria. Es barato -lo que se guarda es una
# referencia al GameExtraInfo que ya está adentro del índice de
# wiitdb.xml, no una copia- y evita rehacer la búsqueda y el parseo de
# controles/sinopsis en cada rescan.
_extra_jobs = InflightRegistry(1, "wiitdb", remember_results=True)


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
    _extra_jobs.submit(
        (game_id, language),
        lambda: get_game_extra_info(game_id, language),
        on_done,
        remember_when=_extra_result_is_final)


def _extra_result_is_final(info: Optional[GameExtraInfo]) -> bool:
    """Si vale la pena recordar `info` como respuesta definitiva.

    Un None puede significar dos cosas muy distintas: "GameTDB no tiene
    este juego" (definitivo, vale recordarlo) o "no se pudo bajar el
    volcado" (temporal: sin internet, servidor caído). Recordar el segundo
    como definitivo dejaba la biblioteca sin metadata para siempre aunque
    volviera la conexión."""
    return info is not None or wiitdb_index_available()


def extra_info_in_flight() -> int:
    """Cuántas consultas de metadata distintas están en curso."""
    return _extra_jobs.in_flight()
