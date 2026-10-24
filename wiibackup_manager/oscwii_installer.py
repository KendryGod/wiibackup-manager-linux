"""Descarga y extracción segura de una app de Open Shop Channel.

Flujo de `install_app`, en orden, y por qué:

1. Descargar el ZIP a un directorio temporal (fuera del destino elegido:
   la USB/SD ya preparada por Modo Fábrica, o cualquier carpeta que el
   usuario confirme). Nada de esto toca el destino todavía. La descarga
   va por `oscwii_client.open_allowlisted`: antes de abrir siquiera el
   socket se exige que la URL sea https y apunte a un host de la lista
   blanca (`oscwii_client.ALLOWED_DOWNLOAD_HOSTS`), y cada redirección
   se valida igual antes de seguirla. La URL sale del catálogo de la
   API, que es contenido de red, y a ese contenido no se le delega desde
   qué dominio se baja lo que después se instala. La lista vive en
   `oscwii_client` -no acá- porque las URLs de ícono que baja ese mismo
   módulo salen del mismo catálogo y tienen que pasar por el mismo
   filtro: una sola lista, no dos que se desincronizan.
2. Verificar el hash contra el que traiga la API (`HomebrewApp.zip_sha256`
   / `zip_md5`), si trae alguno. Hoy la API v3 de OSC no expone ningún
   hash (ver el comentario de `oscwii_client`), así que este paso no hace
   nada en la práctica todavía, pero el código está listo para cuando
   aparezca.
3. Validar que el ZIP sea válido de punta a punta (`ZipFile.testzip`, que
   chequea el CRC de cada miembro): sin esto, un ZIP corrupto o truncado
   se extraería a medias y dejaría una app instalada a medio copiar.
4. Validar CADA ruta interna del ZIP antes de extraer nada: tiene que
   empezar con una carpeta de primer nivel conocida y segura
   (`_ALLOWED_TOP_LEVEL_DIRS`: "apps/" siempre, y "controllers/" para el
   caso real de Nintendont -ver el comentario de esa constante-), no
   puede traer ".." ni ser absoluta, y el destino resuelto tiene que
   quedar adentro de la raíz real de `dest_root` (ver `_is_safe_member` y
   `_clasificar`). Ningún
   archivo del ZIP puede ser un symlink (podría apuntar afuera y hacer
   que una entrada posterior, ya "adentro" según el nombre, en realidad
   escriba a través de él hacia otro lado). Si UNA sola entrada no pasa,
   se aborta todo: no se extrae nada, ni siquiera las entradas que sí
   eran seguras.
   Además, dos entradas tampoco pueden colisionar ENTRE SÍ vistas como
   las ve el destino real, que es FAT32/exFAT y no distingue mayúsculas:
   "apps/Foo/meta.xml" y "apps/Foo/META.XML" son dos archivos distintos
   acá y uno solo allá -el segundo pisaría al primero en silencio, así
   que lo instalado no sería lo que el catálogo dice-. Tampoco puede una
   ruta ser un archivo y otra necesitar ese mismo nombre como carpeta.
   Cualquiera de las dos cosas rechaza el ZIP entero
   (`_check_name_collisions`).
5. Extraer SOLO dentro de esas carpetas permitidas de `dest_root`, y la
   app entera como UNA transacción (`atomicfs.StagedSwap`), no unidad por
   unidad ni archivo por archivo. Un ZIP puede tocar varias "unidades" -una
   subcarpeta de primer nivel dentro de una carpeta permitida,
   típicamente `apps/<NombreDeLaApp>`- y archivos sueltos directo en una
   carpeta permitida (los perfiles de `controllers/` de Nintendont):

   a. Cada unidad se extrae ENTERA a una carpeta de staging oculta,
      hermana de su carpeta final, y cada suelto a un temporal oculto
      hermano de su archivo final (mismo filesystem, para que el
      intercambio de abajo sea un `os.replace` simple y no una copia).
      Si algo falla durante la extracción -o se cancela-, se borra todo
      lo preparado y el destino no se tocó.
   b. Con TODO extraído, cada ruta que ya existía se aparta a un nombre
      oculto de respaldo y lo nuevo entra en su lugar. Los respaldos se
      borran recién cuando entró todo. Si algo falla a mitad, o se
      cancela justo al final, vuelve la versión anterior entera. Sin
      esto, cancelar con la primera unidad ya intercambiada dejaba esa
      unidad nueva -con su respaldo ya borrado- y el resto viejo. Ver
      `_instalar_todo`.
   c. Lo que es del cliente adentro de la carpeta de una app
      (`PRESERVED_PATHS`: la configuración y las carátulas de USB Loader
      GX, por ejemplo) se COPIA de la versión instalada a la staging en
      el paso (a), antes de intercambiar nada: entra junto con lo nuevo, y
      la versión anterior queda entera hasta el final.
   d. Fuera de `apps/` y `controllers/` (las carpetas de datos de
      `CARPETAS_DE_DATOS`: `smw/`, `mame/`, `WiiFlow/plugins/`...) viven
      también las ROMs, las partidas y la configuración del usuario, así
      que ahí no se reemplaza ninguna carpeta: se FUSIONA
      (`_comparar_datos`). Lo que no existe entra (una carpeta nueva,
      entera); lo que ya está igual no se toca; un archivo que existe con
      otro contenido se conserva salvo que se pida reemplazarlo, y uno de
      0 bytes del paquete nunca lo reemplaza; nada que el paquete no
      traiga se borra. Va en la misma transacción que lo demás. La
      documentación suelta y la basura conocida (`Destino.OMITIDO`) no se
      copian.

   Nunca se ejecuta ni el binario (.dol/.elf) ni ningún script: se copian
   bytes nomás.

Nada de esto usa GLib: igual que `gametdb.py`, este módulo es agnóstico de
la interfaz. `install_app` es una función sincrónica y bloqueante; quien
la llame desde la UI (Paso 3) es responsable de correrla en un hilo de
fondo y de reenviar cualquier actualización a GTK con `GLib.idle_add`,
como ya hace `queue_manager.TransferQueue` para las transferencias.
"""
from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import stat
import tempfile
import threading
import urllib.error
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from . import atomicfs, drives, formatting, library_ops, transfer_plan, wii_formats
from .i18n import _
from .oscwii_client import (HomebrewApp, UnsafeDownloadURL,
                            open_allowlisted, safe_slug, url_rejection_reason)

REQUEST_TIMEOUT = 20
DOWNLOAD_CHUNK_SIZE = 256 * 1024

# Los ZIP de OSC son chicos (el más pesado del catálogo probado, wiimc,
# ronda los 6 MB comprimidos / 11 MB descomprimidos). Estos topes son
# generosos para cualquier app real y actúan como freno ante una respuesta
# rota o maliciosa (Content-Length mentiroso, "zip bomb"), mismo espíritu
# que `gametdb.WIITDB_MAX_UNCOMPRESSED`.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_UNCOMPRESSED_TOTAL_BYTES = 512 * 1024 * 1024

# Clúster que se asume si no se puede leer el del destino: el de una unidad
# que preparó Modo Fábrica (`drives.WII_USB_SECTORS_PER_CLUSTER` × 512).
DEFAULT_CLUSTER_BYTES = 32 * 1024

# Carpetas de primer nivel donde SÍ se puede extraer. "apps" es la
# convención general de Homebrew Channel (ver el comentario de
# `oscwii_client`, confirmada bajando ZIPs reales). "controllers" se
# agregó después de un caso real encontrado en el propio catálogo de
# OSC: el ZIP de Nintendont (subdirectories, según la API:
# ["/controllers", "/apps/Nintendont"]) trae perfiles de mandos HID en
# una carpeta al mismo nivel que "apps", no adentro. Sin esta carpeta en
# la lista, el ZIP de Nintendont se rechazaba ENTERO como "no seguro" y
# la app nunca se podía instalar de verdad. Cualquier otra carpeta de
# primer nivel que no esté acá se sigue rechazando: la protección
# anti zip-slip de abajo (`_is_safe_member`) no se relaja, solo se le
# suma un segundo destino válido conocido.
_ALLOWED_TOP_LEVEL_DIRS = ("apps", "controllers")


class Destino(Enum):
    """Qué se hace con una entrada del ZIP (ver `_clasificar`)."""

    # En `apps/` o `controllers/`: la carpeta de la app se reemplaza entera.
    APP = "app"
    # En una carpeta de datos conocida, fuera de `apps/`.
    DATOS = "datos"
    # Documentación o basura conocida: no se copia.
    OMITIDO = "omitido"


# Carpetas fuera de `apps/` y `controllers/` donde una app de OSC deja sus
# datos, en minúsculas (FAT32 no distingue mayúsculas, y los ZIP de hatari y
# vbcgx escriben `WiiFlow/` y `wiiflow/`). Salen de leer los 299 ZIP del
# catálogo el 2026-10-24: 39 apps se rechazaban enteras por esto. Cada una
# es una carpeta con el nombre de la app, o una ruta fija que la app lee.
# Lo que no está acá sigue rechazando el paquete entero.
CARPETAS_DE_DATOS = (
    ("cavestory",),                 # cavestory
    ("fbzx-wii",),                  # fbzx-wii
    ("frodo",),                     # frodo
    ("gxgeo",),                     # gxgeo
    ("hatari",),                    # hatari
    ("mame",),                      # mame4wii
    ("mcbackup",),                  # not64: una partida para gcmm
    ("neocd",),                     # neocd
    ("not64",),                     # not64
    ("ploader",),                   # postloader, Settings-Editor-GUI
    ("smw",),                       # smw-wiigii
    ("tworld",),                    # tworld-wii
    ("uae",),                       # uae
    ("usb-loader",),                # usb-loader
    ("vbcgx",),                     # vbcgx
    ("wad",),                       # postloader: su forwarder (solo .wad)
    ("wii64",),                     # wii64
    ("wii7800",),                   # wii7800
    ("wiicolem",),                  # wiicolem
    ("wiiflow",),                   # wiiflow; plugins de hatari y vbcgx
    ("wiihandysdl",),               # wiihandysdl
    ("wiisxrx",),                   # WiiSXRX
    ("yabause",),                   # yabause
    ("data", "supersonicwii"),      # supersonicwii
    ("etc", "timidity"),            # openttd: la música (timidity.cfg)
    ("usr", "local", "lib", "timidity"),  # openttd: los instrumentos
)
# Cuántos niveles puede tener, como mucho, una ruta en una carpeta de
# datos (contando el archivo). La más honda del catálogo tiene 7
# (`smw/gfx/packs/Classic/tilesets/SMB1/<archivo>`). El Recovery Manager
# recorre cada carpeta de datos hasta acá para encontrar los restos de una
# instalación cortada (`recovery_service`), así que más hondo no se
# escribe: el paquete se rechaza.
MAX_COMPONENTES_DATOS = 8
# Archivos sueltos en la raíz que una app lee por ese nombre: el `boot.dol`
# de AnyTitleDeleterMOD abre "/database.txt" para mostrar los nombres de
# los títulos.
_ARCHIVOS_DE_DATOS_EN_LA_RAIZ = ("database.txt",)
# Lo que se omite sin rechazar el paquete: carpetas de primer nivel que no
# son de la app (la basura de macOS, la guía en HTML del Homebrew Browser,
# el código fuente de gcmm), los dos ejecutables de postloader para neek,
# que postloader no usa, y la documentación suelta en la raíz.
_CARPETAS_OMITIDAS = ("__macosx", "homebrew browser guide and help", "gcmm_1.5.2")
_ARCHIVOS_OMITIDOS_EN_LA_RAIZ = ("neek2obooter.dol", "neek2obooter.app",
                                 "readme", "license", "read.mii")
_DOCUMENTACION = (".txt", ".pdf", ".html", ".htm", ".md")


def _clasificar(nombre: str) -> Optional[Destino]:
    """Qué hacer con la entrada `nombre` de un ZIP, o None si no está en
    ninguna lista y el paquete se rechaza. Solo mira el nombre: que no
    tenga ".." ni sea absoluta, y que no se salga de la unidad, lo ve
    `_is_safe_member`."""
    partes = [p for p in nombre.replace("\\", "/").split("/") if p and p != "."]
    if not partes:
        return None
    if len(partes) >= 2 and partes[0] in _ALLOWED_TOP_LEVEL_DIRS:
        return Destino.APP
    bajas = tuple(p.lower() for p in partes)
    if len(bajas) == 1:
        if bajas[0] in _ARCHIVOS_DE_DATOS_EN_LA_RAIZ:
            return Destino.DATOS
        if bajas[0] in _ARCHIVOS_OMITIDOS_EN_LA_RAIZ or bajas[0].endswith(_DOCUMENTACION):
            return Destino.OMITIDO
        return None
    if bajas[0] in _CARPETAS_OMITIDAS:
        return Destino.OMITIDO
    for prefijo in CARPETAS_DE_DATOS:
        if (bajas[:len(prefijo)] == prefijo
                and len(prefijo) < len(bajas) <= MAX_COMPONENTES_DATOS):
            if prefijo == ("wad",) and not bajas[-1].endswith(".wad"):
                return None
            return Destino.DATOS
    return None


# Lo que `_validate_zip` deja pasar.
_SE_ACEPTAN = frozenset(Destino)

# Lo que es del cliente adentro de la carpeta de una app, por unidad del
# ZIP ("apps/<App>", en minúsculas). Reinstalar o actualizar reemplaza la
# carpeta entera (`_instalar_todo`), así que sin esta lista se perdía, por
# ejemplo, la configuración de USB Loader GX con sus carátulas. Lo de acá
# se copia de la versión instalada a la nueva ANTES del intercambio (ver
# `_copiar_del_cliente`). Solo entra algo con evidencia en el código de la
# app.
#
# USB Loader GX (wiidev/usbloadergx, release v4.0-r1283) guarda todo en su
# carpeta (`ConfigPath`, source/settings/CSettings.cpp):
# - GXGlobal.cfg: la configuración (CSettings::Load / Save).
# - GXGameSettings.cfg, GXGameStatistics.cfg y GXGameCategories.xml: los
#   ajustes por juego, las veces jugado y favoritos, y las categorías
#   (CGameSettings.cpp, CGameStatistics.cpp, CGameCategories.cpp).
# - Wiinnertag.xml (`WiinnertagPath`, network/Wiinnertag.cpp) y font.ttf,
#   la fuente propia (`Theme::LoadFont(ConfigPath)`, themes/CTheme.cpp).
# - GameList.csv y GameList.txt: la lista de juegos que exporta
#   (`save_gamelist`, prompts/gameinfo.cpp).
# - images/: las carátulas 3D, 2D, completas y de disco (`covers_path`,
#   `covers2d_path`, `coversFull_path`, `disc_path`).
# - theme/: los temas instalados (`theme_path`, themes/ThemeMenu.cpp).
# Afuera a propósito: cache/, cache_bnr/ y TitlesCache.bin (cachés que
# vuelve a armar solo), language/ y wiitdb.xml (los trae el ZIP y tienen
# que ser los de la versión nueva) y las capturas Screenshot_*.png (llevan
# la fecha en el nombre: no son una ruta fija).
#
# Nintendont no tiene nada así: guarda su configuración (nincfg.bin), las
# partidas (saves/) y los perfiles de mandos (controllers/) fuera de su
# carpeta, y adentro solo quedan titles.txt y gcn_md5.txt, que baja su
# propio actualizador (loader/source/update.c).
PRESERVED_PATHS = {
    "apps/usbloader_gx": (
        "GXGlobal.cfg", "GXGameSettings.cfg", "GXGameStatistics.cfg",
        "GXGameCategories.xml", "Wiinnertag.xml", "font.ttf",
        "GameList.csv", "GameList.txt", "images", "theme",
    ),
}


class InstallStatus(Enum):
    OK = "ok"
    DOWNLOAD_ERROR = "download_error"
    # La conexión se cortó antes de que llegara todo el ZIP (ver
    # `_download_zip`). Aparte de `DOWNLOAD_ERROR` porque lo que hay que
    # hacer es claro y distinto: volver a intentar, nada más.
    INCOMPLETE_DOWNLOAD = "incomplete_download"
    HASH_MISMATCH = "hash_mismatch"
    BAD_ZIP = "bad_zip"
    # El ZIP está sano, pero un `boot.dol`, `boot.elf` o `.wad` de adentro
    # no tiene la estructura de su formato (ver `wii_formats`): un "404: Not
    # Found" con el nombre del ejecutable, por ejemplo.
    BAD_CONTENT = "bad_content"
    UNSAFE_ZIP = "unsafe_zip"
    # La URL de descarga (la del catálogo, o el destino de una
    # redirección) no pasó la lista blanca de esquema/host. Distinto de
    # `DOWNLOAD_ERROR`: no es que la descarga haya fallado, es que ni
    # siquiera se intentó porque el catálogo apuntaba a otro lado.
    UNSAFE_URL = "unsafe_url"
    UNSAFE_DEST_ROOT = "unsafe_dest_root"
    NO_SPACE = "no_space"
    IO_ERROR = "io_error"
    # Se desconectó la unidad a mitad de la instalación. Distinto de
    # `IO_ERROR` porque no hay nada que revisar del lado del destino
    # ni del paquete: hay que volver a enchufar y repetir.
    DEVICE_DISCONNECTED = "device_disconnected"
    CANCELLED = "cancelled"
    # La instalación falló Y ADEMÁS `_instalar_todo` no pudo
    # devolver la versión anterior a su lugar (ver
    # `library_ops.RollbackFailedError`): distinto de `IO_ERROR` porque acá
    # la app puede haber quedado directamente inexistente, no solo con
    # la instalación nueva sin aplicar.
    ROLLBACK_FAILED = "rollback_failed"

    @property
    def is_ok(self) -> bool:
        return self is InstallStatus.OK


@dataclass(frozen=True)
class InstallResult:
    status: InstallStatus
    app_slug: str
    error: str = ""
    # Rutas finales escritas, para loguear o listar.
    installed_paths: tuple = ()
    # Respaldos de la versión anterior que se instalaron bien pero no se
    # pudieron borrar después (ver `_instalar_todo`). La
    # instalación es un éxito igual -`status` sigue siendo OK- pero
    # quedaron carpetas ocultas ocupando espacio en la unidad, y eso hay
    # que decirlo.
    orphaned_backups: tuple = ()
    # Lo del cliente que estaba en la versión anterior y pasó entero a la
    # nueva (ver `PRESERVED_PATHS`): rutas finales, una por cada cosa de
    # la lista que existía.
    preserved_paths: tuple = ()
    # Fuera de `apps/` y `controllers/` (ver `_comparar_datos`): lo que se
    # escribió, lo del usuario que ya estaba y se dejó como estaba, y los
    # nombres del ZIP que no se copian (`Destino.OMITIDO`).
    datos_escritos: tuple = ()
    datos_conservados: tuple = ()
    omitidos: tuple = ()
    # `datos_escritos` resumido: (carpeta de datos, cantidad), para el
    # historial.
    carpetas_de_datos: tuple = ()

    @property
    def ok(self) -> bool:
        return self.status.is_ok


class CancelRequested(Exception):
    """Interna: corta el flujo de `install_app` apenas se pide cancelar."""


class DescargaIncompleta(Exception):
    """Interna: llegó menos de lo que el servidor anunció, o la conexión se
    cortó a mitad de un envío `chunked`. `install_app` la devuelve como
    `INCOMPLETE_DOWNLOAD`; es una excepción y no el `(False, motivo, ...)`
    de `_download_zip` para que no se confunda con un error de descarga
    cualquiera."""

    def __init__(self, recibidos: int, esperados: Optional[int]):
        super().__init__(recibidos, esperados)
        self.recibidos = recibidos
        self.esperados = esperados

    def mensaje(self) -> str:
        if self.esperados is None:
            return _("Descarga incompleta: la conexión se cortó después de "
                     "{got}. Reintentá.").format(
                         got=formatting.format_size(self.recibidos))
        return _("Descarga incompleta: llegaron {got} de {total}. Reintentá.").format(
            got=formatting.format_size(self.recibidos),
            total=formatting.format_size(self.esperados))


@dataclass
class InstallProgress:
    """Un evento de progreso. `phase` es "download" o "extract"; `fraction`
    va de 0.0 a 1.0 (o None si todavía no se sabe el total, p. ej. sin
    Content-Length y sin tamaño declarado por la API)."""

    phase: str
    fraction: Optional[float]


ProgressCallback = Callable[[InstallProgress], None]


def _check_cancel(cancel_event: Optional[threading.Event]) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise CancelRequested()


def _report(on_progress: Optional[ProgressCallback], phase: str,
           fraction: Optional[float]) -> None:
    if on_progress is not None:
        on_progress(InstallProgress(phase=phase, fraction=fraction))


# ----------------------------------------------------------- Descarga --
def _download_zip(app: HomebrewApp, dest_path: Path,
                  cancel_event: Optional[threading.Event],
                  on_progress: Optional[ProgressCallback]) -> tuple:
    """Descarga `app.zip_url` a `dest_path`, calculando md5 y sha256 al
    vuelo (una sola pasada por los bytes, sirve para lo que sea que la API
    haya provisto). Devuelve (True, "", md5_hex, sha256_hex) o
    (False, motivo, "", ""); nunca lanza salvo `CancelRequested`,
    `UnsafeDownloadURL` y `DescargaIncompleta`, que se dejan propagar para
    que `install_app` las distinga de un error de red común.

    Una conexión cortada no siempre avisa. Medido con Python 3.14.8: si el
    servidor anunció un `Content-Length`, `resp.read(n)` devuelve lo que
    llegó y después `b""`, sin ninguna excepción, así que lo recibido se
    compara acá contra ese largo. Con `chunked` sí levanta
    `http.client.IncompleteRead`, que no es un `OSError`: antes caía en el
    `except Exception` de `install_app` como si hubiera fallado la unidad."""
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    written = 0
    try:
        # `open_allowlisted` valida la URL antes de abrir el socket (o
        # sea, antes de tocar `dest_path`), valida cada redirección antes
        # de seguirla y valida también la URL final: nada de lo que se
        # escribe acá puede venir de un host que no esté en la lista.
        with open_allowlisted(app.zip_url, REQUEST_TIMEOUT,
                              _("la URL de descarga del catálogo")) as resp:
            if resp.status != 200:
                return False, _("status HTTP {status}").format(status=resp.status), "", ""

            # El `Content-Length`, tomado ANTES de leer: `http.client` lo
            # va descontando a medida que llegan bytes. El tamaño del
            # catálogo no sirve para esto (puede ser de una versión vieja
            # del ZIP); solo para la barra cuando el servidor no anuncia nada.
            esperado = resp.length
            total = esperado or app.zip_compressed_size
            if total is not None and total > MAX_DOWNLOAD_BYTES:
                return False, _("el archivo declarado supera el límite esperado"), "", ""

            with dest_path.open("wb") as f:
                while True:
                    _check_cancel(cancel_event)
                    chunk = resp.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        return False, _("la descarga supera el límite esperado"), "", ""
                    f.write(chunk)
                    md5.update(chunk)
                    sha256.update(chunk)
                    _report(on_progress, "download",
                           (written / total) if total else None)
            if esperado is not None and written < esperado:
                raise DescargaIncompleta(written, esperado)
    except CancelRequested:
        raise
    except http.client.IncompleteRead:
        raise DescargaIncompleta(written, None) from None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", "", ""
    except (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException) as e:
        # `HTTPException` tampoco es un `OSError` (una respuesta que no es
        # HTTP, por ejemplo): es la red, no la unidad.
        return False, str(e), "", ""

    return True, "", md5.hexdigest(), sha256.hexdigest()


# --------------------------------------------------------- Verificación --
def _verify_hash(app: HomebrewApp, computed_md5: str, computed_sha256: str) -> Optional[str]:
    """Compara el hash calculado contra el que haya provisto la API.
    Devuelve None si coincide (o si la API no dio ningún hash: no hay nada
    que comparar), o el mensaje de error si NO coincide.

    Hoy `app.zip_sha256`/`zip_md5` siempre son None (ver `oscwii_client`),
    así que esta función no hace nada en la práctica. Queda implementada
    -y llamada siempre desde `install_app`- para que activarse el día que
    la API sume un campo de hash sea cambiar `HomebrewApp.from_api_dict`,
    no reescribir el pipeline de instalación."""
    if app.zip_sha256:
        if computed_sha256.lower() != app.zip_sha256.strip().lower():
            return _("el SHA-256 no coincide (esperado {expected}, descargado "
                     "{found})").format(expected=app.zip_sha256, found=computed_sha256)
        return None
    if app.zip_md5:
        if computed_md5.lower() != app.zip_md5.strip().lower():
            return _("el MD5 no coincide (esperado {expected}, descargado "
                     "{found})").format(expected=app.zip_md5, found=computed_md5)
        return None
    return None


# ------------------------------------------------- Validación del ZIP --
def _is_safe_member(name: str, dest_root: Path) -> bool:
    """True si la entrada `name` del ZIP está en una de las listas de
    `_clasificar` que se aceptan y, extraída, queda dentro de `dest_root`.

    Se combinan dos chequeos independientes a propósito: el del nombre
    (rápido, y documenta la regla: "solo estas carpetas") y el de la ruta
    resuelta (`resolve()` + contención), que es el que de verdad importa
    para la seguridad y cubre casos que el primero solo no ve (p. ej.
    componentes "." mezclados, separadores repetidos o un enlace simbólico
    que ya esté en el destino).

    La contención es contra la raíz REAL del destino, no contra la carpeta
    de primer nivel resuelta: si `apps` es un enlace hacia afuera, esa
    carpeta resuelta también queda afuera y la ruta "estaba adentro"."""
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return False
    pure = PurePosixPath(normalized)
    if pure.is_absolute():
        return False
    if ".." in pure.parts:
        return False
    if _clasificar(normalized) not in _SE_ACEPTAN:
        return False

    try:
        target = (dest_root / normalized).resolve()
        raiz = Path(dest_root).resolve()
    except OSError:
        return False
    return raiz in target.parents


def _normalized_member_path(name: str) -> str:
    """Ruta de una entrada del ZIP en la forma canónica que se usa para
    compararla con las demás: separadores "/" (los ZIP de Windows pueden
    traer "\\"), sin componentes vacíos ni ".", y sin la "/" final que
    marca a las carpetas. Devuelve "" si no queda nada."""
    partes = [p for p in name.replace("\\", "/").split("/") if p and p != "."]
    return "/".join(partes)


def _check_name_collisions(infos: list) -> Optional[str]:
    """Busca dos entradas del ZIP que terminarían siendo LA MISMA cosa en
    el destino. Devuelve el motivo del rechazo, o None si no hay ninguna.

    Dos casos, y los dos rechazan el ZIP entero (no solo la entrada
    repetida: si el paquete es ambiguo, no hay forma de saber cuál de las
    dos versiones era la que el catálogo decía instalar):

    1. Colisión de nombres sin distinguir mayúsculas. El destino real es
       una SD/USB con FAT32 o exFAT, que no las distingue:
       "apps/Foo/meta.xml" y "apps/Foo/META.XML" son dos entradas
       distintas para `zipfile` -y para Linux- pero un solo archivo allá,
       y la segunda pisaría a la primera sin que nada lo avise. Lo mismo
       vale entre carpetas ("apps/Foo" y "apps/foo" son una sola unidad
       en el destino, y `_instalar_todo` instalaría una encima de
       la otra). La comparación usa `casefold()`, que es el plegado de
       mayúsculas pensado para comparar, no `lower()`.
    2. Conflicto archivo/carpeta: una entrada es el archivo "apps/foo" y
       otra necesita que "apps/foo" sea una carpeta (p. ej. la entrada
       "apps/foo/bar"). No es un choque de nombres sino de estructura, y
       en el destino tampoco puede existir de las dos formas a la vez.

    También cuenta como colisión que la MISMA ruta aparezca dos veces
    como archivo: un ZIP puede traer entradas duplicadas, y quien lo lea
    después se queda con una u otra según la implementación -otra vez,
    contenido ambiguo-. Los duplicados de carpeta no son ambiguos (una
    carpeta no tiene contenido propio) y se aceptan: son inevitables,
    porque toda ruta implica a sus carpetas padre."""
    # clave normalizada -> (ruta tal como quedó normalizada, es_carpeta)
    vistos: dict = {}

    def _registrar(ruta: str, es_carpeta: bool) -> Optional[str]:
        clave = ruta.casefold()
        previa = vistos.get(clave)
        if previa is None:
            vistos[clave] = (ruta, es_carpeta)
            return None
        ruta_previa, previa_es_carpeta = previa
        if previa_es_carpeta != es_carpeta:
            archivo = ruta_previa if not previa_es_carpeta else ruta
            carpeta = ruta if not previa_es_carpeta else ruta_previa
            return _('"{file}" es un archivo y "{folder}" necesita ese mismo '
                     'nombre como carpeta').format(file=archivo, folder=carpeta)
        if ruta_previa != ruta:
            return _('"{first}" y "{second}" son el mismo nombre en el destino '
                     '(FAT32/exFAT no distingue mayúsculas)').format(
                         first=ruta_previa, second=ruta)
        if not es_carpeta:
            return _('"{path}" aparece dos veces en el ZIP').format(path=ruta)
        return None

    for info in infos:
        ruta = _normalized_member_path(info.filename)
        if not ruta:
            continue
        partes = ruta.split("/")
        # Cada carpeta padre de la entrada, aunque el ZIP no traiga una
        # entrada propia para ella: es lo que hay que crear en el destino
        # para poder escribir esta ruta.
        for i in range(1, len(partes)):
            motivo = _registrar("/".join(partes[:i]), True)
            if motivo is not None:
                return motivo
        motivo = _registrar(ruta, info.is_dir())
        if motivo is not None:
            return motivo
    return None


def _archivo_danado(zf: zipfile.ZipFile) -> Optional[str]:
    """El motivo por el que un `boot.dol`, `boot.elf` o `.wad` del ZIP no
    sirve (ver `wii_formats`), o None si todos están bien formados.

    `testzip` ya leyó cada miembro entero y comprobó su CRC, así que acá
    se lee solo el principio de cada uno (`wii_formats.CABEZA`) y el
    tamaño del directorio central es el de verdad."""
    for info in zf.infolist():
        if info.is_dir() or not wii_formats.hay_que_validar(info.filename):
            continue
        with zf.open(info) as f:
            cabeza = f.read(wii_formats.CABEZA)
        motivo = wii_formats.problema(info.filename, cabeza, info.file_size)
        if motivo is not None:
            return _("{name} está dañado: {reason}").format(name=info.filename,
                                                            reason=motivo)
    return None


def _validate_zip(zip_path: Path, dest_root: Path) -> tuple:
    """Abre y valida el ZIP entero SIN extraer nada. Devuelve
    (zipfile.ZipFile, None, "") si todo salió bien -el ZipFile queda
    abierto y a cargo de quien llama cerrarlo- o (None, InstallStatus,
    motivo) ante cualquier problema. El status va tipado (no se infiere
    del texto del mensaje) para que `install_app` no tenga que adivinar
    si un fallo fue de integridad (`BAD_ZIP`) o de seguridad
    (`UNSAFE_ZIP`) parseando el string."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        return None, InstallStatus.BAD_ZIP, _(
            "el archivo descargado no es un ZIP válido ({error})").format(error=e)

    # Un solo punto de salida para el error: así, pase lo que pase adentro
    # del try, `zf` se cierra siempre que NO se vaya a devolver abierto (y
    # nunca se cierra en el único camino que sí lo devuelve).
    status: Optional[InstallStatus] = None
    error = ""
    try:
        bad_member = zf.testzip()
        if bad_member is not None:
            status = InstallStatus.BAD_ZIP
            error = _("el ZIP está corrupto o truncado (falló {member})").format(
                member=bad_member)
        else:
            total_uncompressed = 0
            for info in zf.infolist():
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_UNCOMPRESSED_TOTAL_BYTES:
                    status = InstallStatus.BAD_ZIP
                    error = _("el contenido descomprimido supera el límite esperado")
                    break

                if info.is_dir():
                    continue

                # El modo unix va en los 16 bits altos de external_attr;
                # si el bit S_IFLNK está prendido, esta entrada es un
                # symlink. Un symlink dentro de "apps/" que apunte afuera
                # (p. ej. a "/" o a "../../..") haría que una entrada
                # SIGUIENTE, con un nombre que pasa la validación de ruta,
                # en realidad escriba a través de él hacia otro lado. Más
                # simple y seguro rechazar el ZIP entero apenas aparece uno.
                mode = info.external_attr >> 16
                if mode and stat.S_ISLNK(mode):
                    status = InstallStatus.UNSAFE_ZIP
                    error = _("el ZIP contiene un symlink ({name}), rechazado").format(
                        name=info.filename)
                    break

                if not _is_safe_member(info.filename, dest_root):
                    status = InstallStatus.UNSAFE_ZIP
                    error = _("el ZIP escribe en {name}, que no está entre "
                              "las carpetas que la Store puede usar; "
                              "rechazado").format(name=info.filename)
                    break

            # Recién con todas las entradas validadas de a una tiene
            # sentido mirarlas entre sí: dos rutas que por separado son
            # seguras pueden ser la misma cosa en el destino. Se hace
            # sobre `infolist()` completo -carpetas incluidas- porque el
            # conflicto puede estar justo entre un archivo y una carpeta.
            if status is None:
                motivo = _check_name_collisions(zf.infolist())
                if motivo is not None:
                    status = InstallStatus.UNSAFE_ZIP
                    error = _("el ZIP tiene entradas que colisionan en el "
                              "destino: {reason}; rechazado").format(reason=motivo)

            # Lo último, con el paquete ya validado como ZIP: que los
            # ejecutables y los WADs sean lo que dicen ser.
            if status is None:
                motivo = _archivo_danado(zf)
                if motivo is not None:
                    status = InstallStatus.BAD_CONTENT
                    error = motivo
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        status = InstallStatus.BAD_ZIP
        error = _("el ZIP está corrupto o truncado ({error})").format(error=e)

    if status is not None:
        zf.close()
        return None, status, error
    return zf, None, ""


# -------------------------------------------------------------- Extracción --
@dataclass(frozen=True)
class PlanDeInstalacion:
    """Qué se hace con cada archivo de un ZIP ya validado (ver
    `_planificar`). Todo lo que se escribe entra en la MISMA transacción
    (`_instalar_todo`)."""

    # Subcarpetas de primer nivel dentro de `apps/` o `controllers/` (ej.
    # "apps/WiiDonut"): cada una se reemplaza entera. Clave -> miembros.
    unidades: dict
    # Archivos directo en `apps/` o `controllers/`, sin subcarpeta (ej.
    # "controllers/perfil.ini"): se reemplazan de a uno.
    sueltos: tuple
    # Archivos en una carpeta de datos fuera de `apps/` (`Destino.DATOS`).
    datos: tuple
    # Nombres de lo que no se copia (`Destino.OMITIDO`).
    omitidos: tuple


def _planificar(members: list) -> PlanDeInstalacion:
    """Reparte los miembros del ZIP (ya validados, sin carpetas) según
    `_clasificar`."""
    unidades: dict = {}
    sueltos: list = []
    datos: list = []
    omitidos: list = []
    for info in members:
        destino = _clasificar(info.filename)
        if destino is Destino.OMITIDO:
            omitidos.append(info.filename)
        elif destino is Destino.DATOS:
            datos.append(info)
        else:
            parts = PurePosixPath(info.filename).parts
            if len(parts) >= 3:
                clave = f"{parts[0]}/{parts[1]}"
                unidades.setdefault(clave, []).append(info)
            else:
                sueltos.append(info)
    return PlanDeInstalacion(unidades, tuple(sueltos), tuple(datos), tuple(omitidos))


class _NombresEnLaUnidad:
    """La ruta final de cada archivo de datos, con el nombre que cada
    carpeta YA tiene en la unidad: el ZIP de vbcgx dice `wiiflow/` y la
    unidad puede tener `WiiFlow/`. En FAT32 es la misma carpeta; en una
    carpeta de ext4 elegida a mano se crearían dos. (Dos grafías dentro
    del mismo ZIP no llegan hasta acá: `_check_name_collisions` rechaza el
    paquete.)"""

    def __init__(self, raiz: Path) -> None:
        self._raiz = raiz
        self._listados: dict = {}

    def _listado(self, carpeta: Path) -> dict:
        if carpeta not in self._listados:
            try:
                nombres = sorted(os.listdir(carpeta))
            except OSError:
                nombres = []
            por_clave: dict = {}
            for nombre in nombres:
                por_clave.setdefault(nombre.lower(), []).append(nombre)
            self._listados[carpeta] = por_clave
        return self._listados[carpeta]

    def ruta(self, nombre_en_el_zip: str) -> Path:
        actual = self._raiz
        for parte in _normalized_member_path(nombre_en_el_zip).split("/"):
            existentes = self._listado(actual).get(parte.lower(), [])
            elegido = parte if parte in existentes or not existentes else existentes[0]
            actual = actual / elegido
        return actual


@dataclass(frozen=True)
class DatosDelPaquete:
    """Lo que el paquete trae fuera de `apps/`, comparado con lo que ya
    hay en la unidad (ver `_comparar_datos`). Rutas finales."""

    # (miembro, ruta) que no existen: entran.
    nuevos: tuple
    # (miembro, ruta) de archivos que existen con otro contenido: se
    # conservan, salvo que se pida reemplazarlos.
    distintos: tuple
    # Rutas que ya están con el mismo contenido: no se tocan.
    iguales: tuple
    # Rutas del usuario que NUNCA se reemplazan: el paquete las trae vacías
    # (un `wiicolem.conf` de 0 bytes vaciaría la configuración), en la
    # unidad son una carpeta o un enlace, o el paquete necesita una carpeta
    # donde el usuario tiene un archivo.
    intocables: tuple


def _mismo_contenido(zf: zipfile.ZipFile, info: zipfile.ZipInfo, ruta: Path) -> bool:
    with zf.open(info) as a, open(ruta, "rb") as b:
        while True:
            bloque = a.read(DOWNLOAD_CHUNK_SIZE)
            if bloque != b.read(len(bloque)):
                return False
            if not bloque:
                return True


def _comparar_datos(zf: zipfile.ZipFile, datos: tuple, dest_root: Path) -> DatosDelPaquete:
    """Compara cada archivo de datos del paquete con la unidad. Solo lee."""
    nombres = _NombresEnLaUnidad(dest_root)
    es_carpeta: dict = {}

    def _estorba(ruta: Path) -> bool:
        # Alguna carpeta del camino existe y no es una carpeta de verdad
        # (un archivo, o un enlace simbólico, que no se sigue).
        for carpeta in reversed(ruta.relative_to(dest_root).parents[:-1]):
            carpeta = dest_root / carpeta
            if carpeta not in es_carpeta:
                try:
                    es_carpeta[carpeta] = stat.S_ISDIR(os.lstat(carpeta).st_mode)
                except FileNotFoundError:
                    es_carpeta[carpeta] = None
            if es_carpeta[carpeta] is None:
                return False
            if not es_carpeta[carpeta]:
                return True
        return False

    nuevos, distintos, iguales, intocables = [], [], [], []
    for info in datos:
        ruta = nombres.ruta(info.filename)
        if _estorba(ruta):
            intocables.append(ruta)
            continue
        try:
            info_disco = os.lstat(ruta)
        except FileNotFoundError:
            nuevos.append((info, ruta))
            continue
        if not stat.S_ISREG(info_disco.st_mode):
            intocables.append(ruta)
        elif (info_disco.st_size == info.file_size
              and _mismo_contenido(zf, info, ruta)):
            iguales.append(ruta)
        elif info.file_size == 0:
            intocables.append(ruta)
        else:
            distintos.append((info, ruta))
    return DatosDelPaquete(tuple(nuevos), tuple(distintos), tuple(iguales),
                           tuple(intocables))


def _carpeta_para_mostrar(ruta: Path, dest_root: Path) -> str:
    """La carpeta de datos de `ruta` como se muestra: con el nombre que
    tiene en la unidad y "/" al final (`smw/`, `usr/local/lib/timidity/`),
    o el nombre del archivo si está suelto en la raíz (`database.txt`)."""
    partes = ruta.relative_to(dest_root).parts
    if len(partes) == 1:
        return partes[0]
    bajas = tuple(p.lower() for p in partes)
    largo = next((len(prefijo) for prefijo in CARPETAS_DE_DATOS
                  if bajas[:len(prefijo)] == prefijo), 1)
    return "/".join(partes[:largo]) + "/"


def _por_carpeta(rutas, dest_root: Path) -> tuple:
    """(carpeta, cantidad de archivos) de `rutas`, en el orden en que
    aparece cada carpeta."""
    cuenta: dict = {}
    for ruta in rutas:
        carpeta = _carpeta_para_mostrar(ruta, dest_root)
        cuenta[carpeta] = cuenta.get(carpeta, 0) + 1
    return tuple(cuenta.items())


@dataclass(frozen=True)
class AvisoDeInstalacion:
    """Lo que la Store muestra antes de escribir cuando el paquete escribe
    fuera de `apps/` y `controllers/`, omite algo o choca con algo del
    usuario (ver `install_app`). Rutas relativas a la raíz de la unidad."""

    # Lo de `apps/` y `controllers/`: carpetas que se reemplazan enteras
    # ("apps/smw-wiigii/") y archivos sueltos.
    reemplaza: tuple
    # (carpeta de datos, cuántos archivos nuevos entran): ("smw/", 1298).
    carpetas: tuple
    # Archivos del usuario con otro contenido: se elige si se reemplazan.
    distintos: tuple
    # Archivos del usuario que se conservan siempre (`DatosDelPaquete.intocables`).
    intocables: tuple
    # Lo que no se copia.
    omitidos: tuple


def _aviso(plan: PlanDeInstalacion, datos: DatosDelPaquete,
           dest_root: Path) -> Optional[AvisoDeInstalacion]:
    """El aviso para el usuario, o None si no hace falta: todo va a `apps/`
    o `controllers/`, o lo de afuera ya está igual y no se omite nada."""
    if not (datos.nuevos or datos.distintos or datos.intocables or plan.omitidos):
        return None

    def _rel(ruta: Path) -> str:
        return ruta.relative_to(dest_root).as_posix()

    return AvisoDeInstalacion(
        reemplaza=(tuple(f"{clave}/" for clave in plan.unidades)
                   + tuple(_normalized_member_path(i.filename) for i in plan.sueltos)),
        carpetas=_por_carpeta((r for _i, r in datos.nuevos), dest_root),
        distintos=tuple(_rel(r) for _i, r in datos.distintos),
        intocables=tuple(_rel(r) for r in datos.intocables),
        omitidos=plan.omitidos)


def _agrupar_nuevos(nuevos: tuple, dest_root: Path) -> tuple[dict, list]:
    """Reparte los archivos nuevos en carpetas nuevas -la más alta del
    camino que no existe, que entra entera con un solo cambio de nombre: la
    primera instalación de cavestory son 4728 archivos- y archivos nuevos
    en una carpeta que ya existe."""
    existe: dict = {}
    carpetas: dict = {}
    archivos: list = []
    for info, ruta in nuevos:
        tope = None
        for carpeta in reversed(ruta.relative_to(dest_root).parents[:-1]):
            carpeta = dest_root / carpeta
            if carpeta not in existe:
                existe[carpeta] = os.path.lexists(carpeta)
            if not existe[carpeta]:
                tope = carpeta
                break
        if tope is None:
            archivos.append((info, ruta))
        else:
            carpetas.setdefault(tope, []).append((info, ruta.relative_to(tope)))
    return carpetas, archivos


# Marcas de los nombres ocultos que arma `atomicfs.hidden_sibling`: la
# staging donde se extrae la unidad y el respaldo de la versión anterior
# quedan como `.<NombreDeLaApp>.wbm-staging-<pid>` y
# `.<NombreDeLaApp>.wbm-respaldo-<pid>`. El prefijo "wbm-" los distingue
# de cualquier cosa que deje otro programa en la misma SD/USB.
#
# Públicas por el mismo motivo que `library_ops.MARCA_RESPALDO`: `recovery_service`
# reconoce con ellas los restos de una instalación que se cortó a mitad, y
# tiene que leer exactamente la misma marca que escribió el instalador.
MARCA_STAGING = "wbm-staging"
MARCA_RESPALDO = "wbm-respaldo"


def _copiar_miembro(zf: zipfile.ZipFile, info: zipfile.ZipInfo, destino: Path) -> None:
    """Copia los bytes de una entrada ya validada a `destino`. Nunca usa
    `ZipFile.extract`: eso resolvería la ruta interna por su cuenta, y acá
    ya se decidió a mano dónde cae cada archivo. No cambia permisos de
    ejecución ni corre nada."""
    with zf.open(info) as src, destino.open("wb") as dst:
        shutil.copyfileobj(src, dst)


@dataclass(frozen=True)
class _DelCliente:
    """Una cosa de `PRESERVED_PATHS` que el cliente tiene en la versión
    instalada: de qué unidad es, su nombre en la lista, dónde está, cuántos
    archivos copia (para la barra) y cuánto ocupa la copia en el destino."""

    unit_key: str
    nombre: str
    origen: Path
    archivos: int
    ocupa: int


def _medir(ruta: Path, cluster: int) -> Optional[tuple]:
    """(archivos, bytes que ocupa la copia) de `ruta`: cada archivo
    redondeado al clúster, como `_space_needed`, y cada carpeta un clúster.
    None si no está o no es un archivo ni una carpeta (un enlace simbólico
    no se sigue ni se copia). Tampoco está si la carpeta de la app es en
    realidad un archivo: ahí no hay nada adentro que conservar."""
    try:
        info = os.lstat(ruta)
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISREG(info.st_mode):
        return 1, -(-info.st_size // cluster) * cluster
    if not stat.S_ISDIR(info.st_mode):
        return None
    archivos, ocupa = 0, cluster
    with os.scandir(ruta) as entradas:
        nombres = [e.name for e in entradas]
    for nombre in nombres:
        medido = _medir(ruta / nombre, cluster)
        if medido is not None:
            archivos += medido[0]
            ocupa += medido[1]
    return archivos, ocupa


def _plan_del_cliente(unidades: dict, dest_root: Path, cluster: int) -> list:
    """Lo de `PRESERVED_PATHS` que el cliente tiene en la versión instalada
    de cada unidad del ZIP. Solo lee. Levanta OSError si la unidad no se
    puede leer: mejor no instalar que perder lo del cliente."""
    plan = []
    for unit_key in unidades:
        for nombre in PRESERVED_PATHS.get(unit_key.lower(), ()):
            origen = dest_root / unit_key / nombre
            medido = _medir(origen, cluster)
            if medido is not None:
                plan.append(_DelCliente(unit_key, nombre, origen, *medido))
    return plan


def _copiar_del_cliente(origen: Path, destino: Path,
                        cancel_event: Optional[threading.Event],
                        on_step: Callable[[], None]) -> None:
    """Copia `origen` -algo del cliente en la versión instalada- a
    `destino`, adentro de la carpeta de staging. Gana lo del cliente: lo
    que haya puesto el ZIP con ese nombre se reemplaza, y en una carpeta lo
    del cliente se suma a lo del ZIP. `origen` solo se lee: si algo falla o
    se cancela, la versión anterior queda entera. Un enlace simbólico (o
    algo que no es archivo ni carpeta) no se sigue ni se copia."""
    info = os.lstat(origen)
    if stat.S_ISDIR(info.st_mode):
        if os.path.lexists(destino) and not os.path.isdir(destino):
            os.unlink(destino)
        destino.mkdir(exist_ok=True)
        with os.scandir(origen) as entradas:
            nombres = sorted(e.name for e in entradas)
        for nombre in nombres:
            _copiar_del_cliente(origen / nombre, destino / nombre, cancel_event, on_step)
    elif stat.S_ISREG(info.st_mode):
        _check_cancel(cancel_event)
        if os.path.isdir(destino) and not os.path.islink(destino):
            shutil.rmtree(destino)
        with open(origen, "rb") as src, open(destino, "wb") as dst:
            shutil.copyfileobj(src, dst)
        on_step()


def _instalar_todo(zf: zipfile.ZipFile, unidades: dict, sueltos: list,
                   dest_root: Path, cancel_event: Optional[threading.Event],
                   on_step: Callable[[], None],
                   del_cliente: tuple = (),
                   carpetas_de_datos: Optional[dict] = None,
                   archivos_de_datos: tuple = ()) -> tuple[list, list, list]:
    """Instala la app entera como una transacción: todo o nada, nunca una
    mezcla de archivos viejos y nuevos, ni una unidad nueva con otra vieja.

    Primero se extrae TODO a nombres ocultos al lado de su destino (cada
    unidad a una carpeta de staging, cada suelto a un temporal); después se
    intercambia todo junto, y los respaldos de la versión anterior se
    borran recién cuando entró todo (`atomicfs.StagedSwap`). Si la
    extracción falla o se cancela, se borra lo preparado y el destino no
    se tocó; si falla el intercambio, o se cancela justo al final, vuelve
    la versión anterior entera.

    `del_cliente` (ver `_plan_del_cliente`) es lo que el cliente tiene en
    la versión instalada y tiene que pasar a la nueva: se copia a la
    staging de su unidad después de extraer el ZIP y antes de intercambiar
    nada, así que entra en la misma transacción y la versión anterior no se
    toca hasta el final.

    `carpetas_de_datos` y `archivos_de_datos` (ver `_agrupar_nuevos`) son
    lo de fuera de `apps/`, que se FUSIONA con lo que hay: una carpeta que
    no existía entra entera; un archivo entra solo, al lado de lo que el
    usuario ya tenga. Lo nuevo se prepara `exclusivo`: si mientras tanto
    apareció algo con ese nombre, no se aparta (después se borraría con los
    respaldos), se deshace todo. `archivos_de_datos` lleva (miembro, ruta,
    reemplaza): solo un archivo que el usuario eligió reemplazar se aparta.

    Devuelve (rutas finales escritas, respaldos que no se pudieron borrar,
    rutas finales de lo que se conservó del cliente). Lo segundo casi
    siempre viene vacío: si ESE borrado falla, la app quedó bien instalada
    pero su versión anterior sigue ocupando lugar en una carpeta oculta
    que el usuario no va a encontrar solo, así que se reporta en vez de
    ignorarse (mismo criterio que `library_ops.DestinationGuard._discard`).

    Si deshacer también falla levanta `library_ops.RollbackFailedError`,
    la misma excepción que `DestinationGuard` para el caso equivalente con
    archivos WBFS: la estructura es distinta, pero el problema es el
    mismo."""
    tx = atomicfs.StagedSwap(staging_marca=MARCA_STAGING, backup_marca=MARCA_RESPALDO,
                             durable=True)
    escritos: list = []
    conservados: list = []
    try:
        for unit_key, unit_members in unidades.items():
            _check_cancel(cancel_event)
            final_dir = dest_root / unit_key
            staging = tx.stage_dir(final_dir)
            for info in unit_members:
                _check_cancel(cancel_event)
                relativo = Path(*PurePosixPath(info.filename).parts[2:])
                destino = staging / relativo
                destino.parent.mkdir(parents=True, exist_ok=True)
                _copiar_miembro(zf, info, destino)
                escritos.append(final_dir / relativo)
                on_step()
            # Después del ZIP, para que con el mismo nombre gane lo del
            # cliente.
            for item in del_cliente:
                if item.unit_key == unit_key:
                    _copiar_del_cliente(item.origen, staging / item.nombre,
                                        cancel_event, on_step)
                    conservados.append(final_dir / item.nombre)
        for info in sueltos:
            _check_cancel(cancel_event)
            final = dest_root / info.filename
            _copiar_miembro(zf, info, tx.stage_file(final))
            escritos.append(final)
            on_step()
        for carpeta, contenido in (carpetas_de_datos or {}).items():
            _check_cancel(cancel_event)
            staging = tx.stage_dir(carpeta, exclusivo=True)
            for info, relativo in contenido:
                _check_cancel(cancel_event)
                destino = staging / relativo
                destino.parent.mkdir(parents=True, exist_ok=True)
                _copiar_miembro(zf, info, destino)
                escritos.append(carpeta / relativo)
                on_step()
        for info, final, reemplaza in archivos_de_datos:
            _check_cancel(cancel_event)
            _copiar_miembro(zf, info, tx.stage_file(final, exclusivo=not reemplaza))
            escritos.append(final)
            on_step()
    except BaseException:
        tx.abort()
        raise
    try:
        # Última oportunidad de cancelar: todo ya está en su lugar y los
        # respaldos todavía existen, así que se puede volver atrás entero.
        huerfanos = tx.commit(confirm=lambda: _check_cancel(cancel_event))
    except atomicfs.SwapRollbackFailed as e:
        raise library_ops.RollbackFailedError(
            e.pending, original_error=e.original_error) from e.original_error
    return escritos, huerfanos, conservados


# -------------------------------------------------------------------- API --
def _cluster_size(path: Path) -> int:
    """Lo que ocupa como mínimo un archivo en el sistema de archivos de
    `path` (el clúster, en FAT32), o `DEFAULT_CLUSTER_BYTES` si no se puede
    leer."""
    try:
        st = os.statvfs(path)
    except OSError:
        return DEFAULT_CLUSTER_BYTES
    return st.f_frsize or st.f_bsize or DEFAULT_CLUSTER_BYTES


def _space_needed(members: list, cluster: int) -> int:
    """Lo que van a ocupar `members` en el destino: cada archivo redondeado
    al clúster. Sumar los tamaños exactos daba de menos con muchos archivos
    chicos -en FAT32 cada uno ocupa al menos 32 KB-, y la instalación
    fallaba a mitad con el disco lleno después de descargar."""
    return sum(-(-info.file_size // cluster) * cluster for info in members)


def _estado_de_io(exc, dest_root, era_montaje: bool = False) -> InstallStatus:
    """`IO_ERROR`, salvo que lo que pasó sea que ya no está la unidad.

    Instalar homebrew escribe muchos archivos chicos en la SD del cliente,
    así que es de los lugares donde más fácil se saca la tarjeta a mitad
    de camino. "Error al escribir en el destino" manda a revisar permisos
    o espacio; el problema era el cable."""
    if drives.device_is_gone(mount_point=dest_root if era_montaje else None,
                             known_dir=dest_root, exc=exc):
        return InstallStatus.DEVICE_DISCONNECTED
    return InstallStatus.IO_ERROR


def _detalle_de_io(exc, dest_root, era_montaje: bool = False) -> str:
    if _estado_de_io(exc, dest_root, era_montaje) is InstallStatus.DEVICE_DISCONNECTED:
        return drives.disconnected_message()
    return str(exc)


def _destino_no_escribible(dest_root: Path) -> Optional[OSError]:
    """El error que daría escribir en `dest_root`, o None si es una
    carpeta donde se puede escribir. No crea nada."""
    if not dest_root.is_dir():
        return NotADirectoryError(
            _("{path} no existe o no es una carpeta").format(path=dest_root))
    if not os.access(dest_root, os.W_OK | os.X_OK):
        return PermissionError(
            _("no hay permiso para escribir en {path}").format(path=dest_root))
    return None


def install_app(app: HomebrewApp, dest_root: Path, *,
                cancel_event: Optional[threading.Event] = None,
                on_progress: Optional[ProgressCallback] = None,
                reemplazar: bool = False,
                confirmar: Optional[Callable[[AvisoDeInstalacion], Optional[bool]]] = None,
                ) -> InstallResult:
    """Descarga, verifica y extrae `app` a `dest_root/`, respetando solo
    las carpetas de primer nivel de `_ALLOWED_TOP_LEVEL_DIRS` (hoy "apps"
    y "controllers"; ver el comentario de esa constante).

    `dest_root` es la raíz de la unidad de destino (la USB/SD ya
    preparada, o cualquier carpeta que el usuario haya confirmado): esta
    función rechaza `dest_root` de entrada si es una ruta crítica del
    sistema operativo (`drives.is_critical_system_path`, mismo criterio
    que el BLINDAJE 4 de Modo Fábrica) o si no es una carpeta escribible
    -para fallar rápido, antes de gastar tiempo en la descarga, y sin
    crear nada: lo que no llega a instalarse no deja rastro en la unidad-,
    y nunca escribe fuera de las carpetas permitidas (ver el comentario
    del módulo).

    Nunca lanza: cualquier fallo -de red, de integridad, de espacio, de
    E/S- vuelve como un `InstallResult` con el `status` que corresponda,
    nunca como una excepción sin capturar. `cancel_event`, si se pasa, se
    revisa entre cada bloque descargado y entre cada archivo extraído."""
    # Foto de si `dest_root` es un punto de montaje, ANTES de tocar nada:
    # si después dejó de serlo, es que sacaron la unidad. Solo vale si lo
    # era al empezar -ver `drives.device_is_gone`-, porque el destino bien
    # puede ser una carpeta adentro de la SD y no su raíz.
    era_montaje = drives.is_mount_point(dest_root)
    try:
        dest_root = Path(dest_root)

        # Defensa en profundidad: hoy la UI (Homebrew Store) solo deja
        # elegir unidades removibles o una carpeta confirmada a mano, así
        # que este caso no debería poder darse en la práctica. Pero esta
        # función no tiene por qué confiar en eso -mismo espíritu que
        # `drives.check_no_critical_mounts` para Modo Fábrica- por si el
        # día de mañana se llama desde otro lugar sin ese filtro.
        if drives.is_critical_system_path(dest_root):
            return InstallResult(
                InstallStatus.UNSAFE_DEST_ROOT, app.slug,
                _("{path} es una ruta crítica del sistema operativo, se rechaza "
                  "como destino de instalación").format(path=dest_root))

        # Antes de crear nada en el destino: si la URL que trae el
        # catálogo no está en la lista blanca, esta instalación no va a
        # pasar de acá, así que no tiene por qué dejar rastro en la
        # unidad del usuario. `_download_zip` vuelve a chequearlo por su
        # cuenta -es la que abre el socket- y además valida cada
        # redirección.
        url_err = url_rejection_reason(app.zip_url)
        if url_err is not None:
            return InstallResult(
                InstallStatus.UNSAFE_URL, app.slug,
                _("la URL de descarga de la app fue rechazada: {reason} "
                  "({url})").format(reason=url_err, url=app.zip_url))

        # Fallar rápido si no se va a poder escribir, pero sin crear nada:
        # `apps/` se creaba acá y quedaba vacía en la unidad si después la
        # descarga fallaba o el paquete venía roto. Las carpetas las crea
        # la transacción cuando ya hay algo que poner (`_instalar_todo`).
        problema = _destino_no_escribible(dest_root)
        if problema is not None:
            return InstallResult(_estado_de_io(problema, dest_root, era_montaje), app.slug,
                                 _detalle_de_io(problema, dest_root, era_montaje))

        with tempfile.TemporaryDirectory(prefix="wiibackup-manager-oscwii-") as tmp_dir:
            # El slug es contenido de red: sin sanear, "../x" sacaba el ZIP
            # del directorio temporal (ver `oscwii_client.safe_slug`).
            tmp_zip = Path(tmp_dir) / f"{safe_slug(app.slug)}.zip"

            ok, err, computed_md5, computed_sha256 = _download_zip(
                app, tmp_zip, cancel_event, on_progress)
            if not ok:
                return InstallResult(InstallStatus.DOWNLOAD_ERROR, app.slug, err)

            hash_err = _verify_hash(app, computed_md5, computed_sha256)
            if hash_err:
                return InstallResult(InstallStatus.HASH_MISMATCH, app.slug, hash_err)

            _check_cancel(cancel_event)

            zf, zip_status, zip_err = _validate_zip(tmp_zip, dest_root)
            if zf is None:
                return InstallResult(zip_status, app.slug, zip_err)

            try:
                members = [i for i in zf.infolist() if not i.is_dir()]
                plan = _planificar(members)
                unidades, sueltos = plan.unidades, list(plan.sueltos)
                datos = _comparar_datos(zf, plan.datos, dest_root)
                # Con el paquete ya validado y antes de tocar la unidad: qué
                # va a escribir fuera de `apps/`, qué choca con lo del
                # usuario y qué no se copia. None es "no instalar"; si no,
                # si se reemplazan los archivos distintos del usuario.
                aviso = _aviso(plan, datos, dest_root)
                if confirmar is not None and aviso is not None:
                    respuesta = confirmar(aviso)
                    _check_cancel(cancel_event)
                    if respuesta is None:
                        raise CancelRequested()
                    reemplazar = bool(respuesta)
                # Fuera de `apps/` lo del usuario se conserva: un archivo
                # distinto solo se reemplaza si se pidió, y uno vacío del
                # paquete nunca (ver `DatosDelPaquete.intocables`).
                reemplazados = datos.distintos if reemplazar else ()
                conservados_del_usuario = datos.intocables + (
                    () if reemplazar else tuple(r for _i, r in datos.distintos))
                carpetas_de_datos, nuevos_sueltos = _agrupar_nuevos(datos.nuevos, dest_root)
                archivos_de_datos = (tuple((i, r, False) for i, r in nuevos_sueltos)
                                     + tuple((i, r, True) for i, r in reemplazados))
                a_escribir = ([i for m in unidades.values() for i in m] + sueltos
                              + [i for i, _r in datos.nuevos] + [i for i, _r in reemplazados])

                cluster = _cluster_size(dest_root)
                # Lo del cliente se copia mientras la versión anterior sigue
                # en su lugar: ocupa lugar dos veces hasta el final, y hay
                # que contarlo.
                del_cliente = _plan_del_cliente(unidades, dest_root, cluster)

                needed = (_space_needed(a_escribir, cluster)
                          + sum(item.ocupa for item in del_cliente))
                free = transfer_plan.free_space(dest_root)
                if free is not None and needed > free:
                    return InstallResult(
                        InstallStatus.NO_SPACE, app.slug,
                        _("necesita {need} y quedan {free} en el destino").format(
                            need=formatting.format_size(needed),
                            free=formatting.format_size(free)))

                total = len(a_escribir) + sum(item.archivos for item in del_cliente)
                hechos = [0]

                def _paso() -> None:
                    hechos[0] += 1
                    _report(on_progress, "extract",
                           min(hechos[0] / total, 1.0) if total else 1.0)

                written, huerfanos, conservados = _instalar_todo(
                    zf, unidades, sueltos, dest_root, cancel_event, _paso,
                    tuple(del_cliente), carpetas_de_datos, archivos_de_datos)
            finally:
                zf.close()

        datos_escritos = tuple(
            [c / rel for c, contenido in carpetas_de_datos.items() for _i, rel in contenido]
            + [r for _i, r, _x in archivos_de_datos])
        return InstallResult(InstallStatus.OK, app.slug,
                             installed_paths=tuple(written),
                             orphaned_backups=tuple(huerfanos),
                             preserved_paths=tuple(conservados),
                             datos_escritos=datos_escritos,
                             datos_conservados=conservados_del_usuario,
                             carpetas_de_datos=_por_carpeta(datos_escritos, dest_root),
                             omitidos=plan.omitidos)

    except CancelRequested:
        return InstallResult(InstallStatus.CANCELLED, app.slug,
                             _("instalación cancelada"))
    except DescargaIncompleta as e:
        # Sale de `_download_zip`, antes de abrir el ZIP: en la unidad no se
        # escribió nada de la app.
        return InstallResult(InstallStatus.INCOMPLETE_DOWNLOAD, app.slug, e.mensaje())
    except UnsafeDownloadURL as e:
        # Sale de `_download_zip` (o de una redirección rechazada a mitad
        # de la descarga): la app no se instaló y nada del destino se
        # tocó, pero el motivo no es un fallo de red común y se reporta
        # distinto.
        return InstallResult(InstallStatus.UNSAFE_URL, app.slug, str(e))
    except library_ops.RollbackFailedError as e:
        # Caso grave: la instalación falló Y ADEMÁS no se pudo devolver
        # la versión anterior a su lugar (ver `_instalar_todo`).
        # `user_message` distingue esto de un `IO_ERROR` común -acá la
        # app puede haber quedado directamente inexistente, y el
        # respaldo que sigue existiendo (`e.pending`) es la única forma
        # de rescatarla a mano.
        return InstallResult(InstallStatus.ROLLBACK_FAILED, app.slug,
                             e.user_message())
    except OSError as e:
        return InstallResult(_estado_de_io(e, dest_root, era_montaje), app.slug,
                             _detalle_de_io(e, dest_root, era_montaje))
    except Exception as e:  # noqa: BLE001 - red de seguridad final
        return InstallResult(_estado_de_io(e, dest_root, era_montaje), app.slug,
                             _detalle_de_io(e, dest_root, era_montaje))
