"""Cliente de la API pública de Open Shop Channel (https://oscwii.org).

Investigación del endpoint real
--------------------------------
oscwii.org no publica documentación de API navegable (`/docs/` y `/api`
devuelven 404 al pedirlos). El endpoint de abajo se confirmó en vivo,
pidiéndolo de verdad contra el servidor:

    GET https://hbb1.oscwii.org/api/v3/contents

Devuelve un único array JSON con TODO el repositorio (294 apps al momento
de escribir esto), sin paginado, sin parámetros y sin autenticación.
Cada elemento tiene esta forma (recortada a los campos que se usan acá;
confirmado bajando la respuesta real y dos ZIPs de muestra, "WiiDonut" y
"wiimc"):

    {
      "slug": "WiiDonut",
      "name": "Wii Donut",
      "author": "jornmann, Andy Sloane",
      "category": "demos",
      "description": {"short": "...", "long": "..."},
      "file_size": {"binary": 430656, "icon": 2404,
                    "zip_compressed": 226230, "zip_uncompressed": 433665},
      "release_date": 1644364800,
      "supported_platforms": ["wii", "vwii", "wii_mini"],
      "peripherals": ["Wii Remote"],
      "url": {"icon": "https://hbb1.oscwii.org/api/contents/WiiDonut/icon.png",
              "zip": "https://hbb1.oscwii.org/api/contents/WiiDonut/WiiDonut.zip"},
      "version": "1.0.0"
    }

ASUNCIÓN A REVISAR: en la muestra probada la API v3 NO expone ningún
campo de hash/checksum (ni md5 ni sha1 ni sha256), ni para el ZIP ni para
el binario. `HomebrewApp` igual trae `zip_sha256`/`zip_md5` (siempre
`None` hoy, bajo nombres de campo adivinados -"sha256"/"md5"- que nadie
confirmó porque la API no los tiene) para que `oscwii_installer` los use
el día que aparezcan, sin tener que tocar el resto del pipeline. Mientras
tanto la única verificación real de integridad es la validación de ZIP
que hace `oscwii_installer` (Paso 2), que es obligatoria siempre y no
depende de que esto se confirme.

También se confirmó bajando esos dos ZIPs reales que TODOS los archivos
de adentro empiezan con "apps/<slug>/" (p. ej. "apps/WiiDonut/boot.dol"):
es la convención estándar de Homebrew Channel para instalar en la
tarjeta/USB (SD:/apps/<carpeta>/boot.dol + meta.xml + icon.png).
`oscwii_installer` la usa como parte de la protección contra zip-slip.
"""
from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from . import config

OSC_API_BASE = "https://hbb1.oscwii.org"
OSC_CONTENTS_URL = f"{OSC_API_BASE}/api/v3/contents"
REQUEST_TIMEOUT = 15
CONTENTS_CACHE_FILENAME = "oscwii_contents.json"


def contents_cache_path() -> Path:
    return config.CACHE_DIR / CONTENTS_CACHE_FILENAME


@dataclass(frozen=True)
class HomebrewApp:
    """Una app del repositorio de Open Shop Channel, con solo los campos
    que esta app usa (la respuesta real trae varios más: contributors,
    flags, package_type, datos de "shop"/WAD, etc., que no hacen falta
    para listar/descargar/instalar y por eso no se guardan)."""

    slug: str
    name: str
    author: str = ""
    category: str = ""
    short_description: str = ""
    long_description: str = ""
    version: str = ""
    release_date: Optional[int] = None  # epoch segundos, si la API lo trae
    icon_url: str = ""
    zip_url: str = ""
    zip_compressed_size: Optional[int] = None
    zip_uncompressed_size: Optional[int] = None
    supported_platforms: tuple = ()
    peripherals: tuple = ()
    # Ver el comentario del módulo: la API no los provee hoy (siempre
    # quedan en None). `oscwii_installer.verify_download` ya sabe usarlos
    # si algún día aparecen.
    zip_sha256: Optional[str] = None
    zip_md5: Optional[str] = None

    @classmethod
    def from_api_dict(cls, d: dict) -> Optional["HomebrewApp"]:
        """Arma una `HomebrewApp` a partir de un elemento de la respuesta
        (o del caché en disco, que guarda la misma forma). Devuelve None
        si le falta algo imprescindible (slug, name o una URL de ZIP) o si
        `d` no es ni siquiera un diccionario: un elemento así no sirve
        para nada acá y se descarta en vez de reventar el resto de la
        lista, igual que hace `config.Settings.load` con cada campo."""
        if not isinstance(d, dict):
            return None

        slug = d.get("slug")
        name = d.get("name")
        if not isinstance(slug, str) or not slug.strip():
            return None
        if not isinstance(name, str) or not name.strip():
            return None

        url = d.get("url") if isinstance(d.get("url"), dict) else {}
        zip_url = url.get("zip")
        if not isinstance(zip_url, str) or not zip_url.strip():
            # Sin URL de descarga esta app no se puede instalar: no vale
            # la pena mostrarla en la tienda.
            return None

        desc = d.get("description") if isinstance(d.get("description"), dict) else {}
        file_size = d.get("file_size") if isinstance(d.get("file_size"), dict) else {}

        def _str(v) -> str:
            return v if isinstance(v, str) else ""

        def _int_or_none(v) -> Optional[int]:
            # bool es subclase de int en Python; explícitamente no cuenta
            # como tamaño/fecha válidos (mismo motivo que en config.py).
            return v if isinstance(v, int) and not isinstance(v, bool) else None

        def _str_tuple(v) -> tuple:
            if not isinstance(v, list):
                return ()
            return tuple(x for x in v if isinstance(x, str))

        return cls(
            slug=slug.strip(),
            name=name.strip(),
            author=_str(d.get("author")),
            category=_str(d.get("category")),
            short_description=_str(desc.get("short")),
            long_description=_str(desc.get("long")),
            version=_str(d.get("version")),
            release_date=_int_or_none(d.get("release_date")),
            icon_url=_str(url.get("icon")),
            zip_url=zip_url.strip(),
            zip_compressed_size=_int_or_none(file_size.get("zip_compressed")),
            zip_uncompressed_size=_int_or_none(file_size.get("zip_uncompressed")),
            supported_platforms=_str_tuple(d.get("supported_platforms")),
            peripherals=_str_tuple(d.get("peripherals")),
            zip_sha256=_str(d.get("sha256")) or None,
            zip_md5=_str(d.get("md5")) or None,
        )

    def to_cache_dict(self) -> dict:
        """Forma en la que esta app se guarda en el caché de disco: los
        campos ya normalizados por `from_api_dict`, no el JSON crudo de
        la API. Así el caché no depende de qué otros campos traía la
        respuesta ese día, y `from_api_dict` lo puede releer sin cambios."""
        return {
            "slug": self.slug,
            "name": self.name,
            "author": self.author,
            "category": self.category,
            "description": {"short": self.short_description,
                            "long": self.long_description},
            "version": self.version,
            "release_date": self.release_date,
            "url": {"icon": self.icon_url, "zip": self.zip_url},
            "file_size": {"zip_compressed": self.zip_compressed_size,
                          "zip_uncompressed": self.zip_uncompressed_size},
            "supported_platforms": list(self.supported_platforms),
            "peripherals": list(self.peripherals),
            "sha256": self.zip_sha256,
            "md5": self.zip_md5,
        }


class FetchStatus(Enum):
    """Cómo se obtuvo la lista devuelta por `list_apps`."""

    OK = "ok"                    # recién bajada de la API
    STALE_CACHE = "cache"        # la API falló; es el último catálogo guardado
    ERROR = "error"              # no hay lista que mostrar (ni API ni caché)


@dataclass(frozen=True)
class AppListResult:
    status: FetchStatus
    apps: tuple  # tuple[HomebrewApp, ...]
    # Motivo del fallo cuando status no es OK (para loguear o mostrar en la
    # interfaz); irrelevante y vacío cuando status es OK.
    error: str = ""


def _parse_apps(raw: bytes) -> tuple:
    """Parsea `raw` como la respuesta de la API (o el caché, que guarda la
    misma forma). Devuelve (apps, motivo_de_error): `apps` es None si
    `raw` no es JSON válido, no es una lista, o es una lista no vacía
    donde NINGÚN elemento tiene el formato esperado (probable cambio de
    esquema del lado del servidor, no vale la pena aceptarlo a medias).

    Una lista vacía SÍ se acepta como resultado válido (catálogo vacío de
    verdad): se distingue de "una lista con basura adentro" mirando el
    JSON crudo, no la cantidad de apps que sobrevivieron al parseo."""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as e:
        return None, f"JSON inválido ({e})"

    if not isinstance(parsed, list):
        return None, "la respuesta no es una lista JSON"

    if not parsed:
        return (), None

    apps = tuple(app for item in parsed
                 if (app := HomebrewApp.from_api_dict(item)) is not None)
    if not apps:
        return None, "ningún elemento de la respuesta tiene el formato esperado"
    return apps, None


def _fetch_remote() -> tuple:
    """Baja y parsea la lista fresca desde la API. Devuelve
    (apps, motivo_de_error); nunca lanza (timeout, error HTTP y JSON
    malformado quedan todos capturados acá, no en quien llama)."""
    try:
        req = urllib.request.Request(
            OSC_CONTENTS_URL, headers={"User-Agent": "wiibackup-manager-linux"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                return None, f"status HTTP {resp.status}"
            raw = resp.read()
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return None, str(e)

    return _parse_apps(raw)


def _load_cache() -> tuple:
    """Lee el último catálogo guardado. Devuelve (apps, motivo_de_error);
    (None, motivo) si no hay caché o está corrupto."""
    path = contents_cache_path()
    if not path.exists():
        return None, "no hay ninguna lista guardada de una sesión anterior"
    try:
        raw = path.read_bytes()
    except OSError as e:
        return None, str(e)
    return _parse_apps(raw)


def _store_cache(apps: tuple) -> None:
    """Guarda `apps` como el último catálogo bueno conocido, de forma
    atómica (`config.write_text_atomic`, la misma que usa el resto de la
    app). Que esto falle (disco lleno, permisos) no es grave -la próxima
    consulta exitosa lo vuelve a intentar- así que no se propaga."""
    try:
        payload = json.dumps([app.to_cache_dict() for app in apps])
        config.write_text_atomic(contents_cache_path(), payload)
    except OSError:
        pass


def list_apps() -> AppListResult:
    """Lista de apps del repositorio de Open Shop Channel.

    Primero intenta la API en vivo (`_fetch_remote`). Si contesta con algo
    que parsea -aunque sea un catálogo vacío- esa lista se guarda como el
    último catálogo bueno conocido y se devuelve con estado OK.

    Si la API falla por cualquier motivo (sin red, timeout, HTTP != 200,
    JSON malformado, o una respuesta que ya no tiene la forma esperada),
    se cae al último catálogo guardado en disco, con estado STALE_CACHE:
    así la Homebrew Store puede mostrar "última lista conocida (sin
    conexión)" en vez de quedar vacía. Si tampoco hay caché utilizable
    (primer uso sin red, o el caché está corrupto), el estado es ERROR y
    `apps` queda vacío.

    Nunca lanza: cualquier excepción no prevista explícitamente también
    se convierte en ERROR en vez de escaparse hacia quien llama (que,
    en la interfaz, sería la UI)."""
    try:
        apps, err = _fetch_remote()
    except Exception as e:  # noqa: BLE001 - red de seguridad final
        apps, err = None, str(e)

    if apps is not None:
        _store_cache(apps)
        return AppListResult(status=FetchStatus.OK, apps=apps)

    try:
        cached, cache_err = _load_cache()
    except Exception as e:  # noqa: BLE001
        cached, cache_err = None, str(e)

    if cached is not None:
        return AppListResult(status=FetchStatus.STALE_CACHE, apps=cached, error=err)

    return AppListResult(status=FetchStatus.ERROR, apps=(),
                         error=err or cache_err or "motivo desconocido")


# --- Versión en segundo plano, para no bloquear la UI ---
#
# Mismo patrón que `gametdb.fetch_cover_async`/`fetch_extra_info_async`: un
# solo worker (acá alcanza y sobra, es un único pedido HTTP) y los pedidos
# que llegan mientras uno ya está en vuelo se cuelgan de ese en vez de
# disparar otro. `on_done` corre en el hilo del pool: quien toque widgets
# de GTK adentro tiene que reenviarlo con `GLib.idle_add` (eso lo hace la
# UI del Paso 3, no este módulo).
_list_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oscwii-list")
_list_inflight: list = []
_list_lock = threading.Lock()

AppListCallback = Callable[[AppListResult], None]


def fetch_apps_async(on_done: Optional[AppListCallback] = None) -> None:
    """Pide la lista de apps y llama a `on_done(resultado)` al terminar."""
    if on_done is None:
        return
    with _list_lock:
        _list_inflight.append(on_done)
        if len(_list_inflight) > 1:
            return
    _list_executor.submit(_run_list_job)


def _run_list_job() -> None:
    try:
        result = list_apps()
    except Exception as e:  # noqa: BLE001
        result = AppListResult(status=FetchStatus.ERROR, apps=(), error=str(e))
    with _list_lock:
        callbacks = list(_list_inflight)
        _list_inflight.clear()
    for cb in callbacks:
        try:
            cb(result)
        except Exception:
            # Un callback que falla (p. ej. una vista que ya se cerró) no
            # puede llevarse puestos a los demás.
            pass


# --- Íconos de cada app (Paso 3: la tienda los muestra en cada tarjeta) --
#
# Confirmado bajando tres íconos reales (WiiDonut, wiimc, radiow): los tres
# son PNG de 128x48 -el tamaño estándar del banner de Homebrew Channel-, así
# que la vista puede reservarles ese espacio de entrada sin "saltar" cuando
# termina de cargar cada uno, igual que hace `game_row.build_cover_widget`
# con las carátulas de GameTDB.
ICONS_CACHE_DIRNAME = "oscwii_icons"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def icons_cache_dir() -> Path:
    return config.CACHE_DIR / ICONS_CACHE_DIRNAME


def icon_cache_path(slug: str) -> Path:
    """Ruta del ícono cacheado de `slug`.

    `slug` viene de la API (contenido de red) y termina siendo parte de un
    nombre de archivo: se sanea igual que `gametdb.cover_cache_path` sanea
    la región, aunque los slugs reales de OSC ya sean alfanuméricos con
    guiones -no hay que confiar ciegamente en que nunca traigan "/" o "..".
    """
    safe = "".join(ch for ch in slug if ch.isalnum() or ch in "-_") or "app"
    return icons_cache_dir() / f"{safe}.png"


def _looks_like_png(data: bytes) -> bool:
    return data.startswith(PNG_MAGIC)


def get_icon_path(app: HomebrewApp, force: bool = False) -> Optional[Path]:
    """Ícono cacheado de `app`, descargándolo si hace falta.

    Devuelve None si la app no trae `icon_url`, o si la descarga/validación
    falla por cualquier motivo (sin red, HTTP != 200, la respuesta no
    empieza con la firma PNG): en ese caso la tarjeta se queda con el
    ícono de placeholder, nunca con un hueco roto ni con una excepción sin
    capturar. No hace falta una validación tan fuerte como
    `gametdb._decodes_as_image` (que decodifica el PNG entero): un ícono
    mal cacheado acá es cosmético, no arriesga instalar nada corrupto -eso
    lo cubre `oscwii_installer`, con el ZIP."""
    if not app.icon_url:
        return None

    cache_path = icon_cache_path(app.slug)
    if cache_path.exists() and not force:
        return cache_path

    icons_cache_dir().mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(
            app.icon_url, headers={"User-Agent": "wiibackup-manager-linux"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = resp.read()
    except urllib.error.HTTPError:
        return None
    except (urllib.error.URLError, OSError, TimeoutError):
        return None

    if not _looks_like_png(data):
        return None

    tmp = cache_path.with_name(f".{cache_path.name}.parcial-{os.getpid()}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, cache_path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    return cache_path


# Mismo patrón que `fetch_cover_async` de gametdb.py: un pool compartido
# (6 descargas simultáneas como mucho, mismo número que ahí) y los pedidos
# duplicados -inevitables apenas se abre la tienda, con potencialmente
# cientos de tarjetas pidiendo su ícono a la vez- se cuelgan del que ya
# está en vuelo en vez de disparar una descarga por tarjeta.
_ICON_DOWNLOAD_WORKERS = 6
_icon_executor = ThreadPoolExecutor(
    max_workers=_ICON_DOWNLOAD_WORKERS, thread_name_prefix="oscwii-icon")
_icon_inflight: dict = {}
_icon_lock = threading.Lock()

IconCallback = Callable[[Optional[Path]], None]


def fetch_icon_async(app: HomebrewApp, on_done: Optional[IconCallback] = None) -> None:
    """Pide el ícono de `app` y llama a `on_done(path_o_None)` al terminar.

    `on_done` corre en un hilo del pool (o en el que llama, si el ícono ya
    estaba cacheado): quien toque widgets de GTK adentro tiene que
    reenviarlo con `GLib.idle_add`, igual que en gametdb.py."""
    if on_done is None:
        return

    if not app.icon_url:
        on_done(None)
        return

    cached = icon_cache_path(app.slug)
    if cached.exists():
        on_done(cached)
        return

    with _icon_lock:
        waiting = _icon_inflight.get(app.slug)
        if waiting is not None:
            waiting.append(on_done)
            return
        _icon_inflight[app.slug] = [on_done]

    _icon_executor.submit(_run_icon_job, app)


def _run_icon_job(app: HomebrewApp) -> None:
    try:
        path = get_icon_path(app)
    except Exception:
        path = None
    with _icon_lock:
        callbacks = _icon_inflight.pop(app.slug, [])
    for cb in callbacks:
        try:
            cb(path)
        except Exception:
            pass
