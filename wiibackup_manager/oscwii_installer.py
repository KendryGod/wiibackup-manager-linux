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
   quedar adentro de esa carpeta permitida dentro de `dest_root`. Ningún
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
5. Extraer SOLO dentro de esas carpetas permitidas de `dest_root`. Cada
   ZIP toca una o más "unidades" -una subcarpeta de primer nivel dentro
   de una carpeta permitida, típicamente `apps/<NombreDeLaApp>`- y cada
   unidad se instala como bloque atómico, no archivo por archivo:

   a. Se extrae la unidad ENTERA a una carpeta de staging oculta,
      hermana de su carpeta final (mismo filesystem, para que el
      intercambio de abajo sea un `os.replace` simple y no una copia).
      Si algo falla durante la extracción, se borra la staging entera y
      la carpeta final -la versión anterior de la app, si había una- no
      se toca para nada.
   b. Con la extracción completa: si la unidad no existía antes, la
      staging pasa a ocupar su lugar directo. Si ya existía
      (actualización), se la aparta primero a un nombre oculto de
      respaldo, entra la staging en su lugar, y el respaldo se borra
      solo si el intercambio completo salió bien. Sin esto, una
      actualización que fallaba a mitad de camino podía dejar una
      mezcla de archivos de la versión vieja y la nueva -cada archivo
      individual escrito de forma válida, pero la app entera rota-;
      mismo patrón que `library_ops.DestinationGuard` (Sesión 2), llevado
      de archivos WBFS individuales a una carpeta completa. Ver
      `_stage_and_swap_unit`.

   Los pocos archivos sueltos que un ZIP puede traer directo dentro de
   una carpeta permitida sin ninguna subcarpeta (sin "unidad" que
   intercambiar) se siguen escribiendo uno por uno con el patrón atómico
   de siempre (`_extract_member`, mismo que `gametdb._store_cover` y
   `config.write_text_atomic`) -nunca ejecutando ni el binario
   (.dol/.elf) ni ningún script, solo copiando bytes.

Nada de esto usa GLib: igual que `gametdb.py`, este módulo es agnóstico de
la interfaz. `install_app` es una función sincrónica y bloqueante; quien
la llame desde la UI (Paso 3) es responsable de correrla en un hilo de
fondo y de reenviar cualquier actualización a GTK con `GLib.idle_add`,
como ya hace `queue_manager.TransferQueue` para las transferencias.
"""
from __future__ import annotations

import hashlib
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

from . import atomicfs, drives, formatting, library_ops, transfer_plan
from .atomicfs import atomic_write_target
from .oscwii_client import (HomebrewApp, UnsafeDownloadURL,
                            open_allowlisted, url_rejection_reason)

REQUEST_TIMEOUT = 20
DOWNLOAD_CHUNK_SIZE = 256 * 1024

# Los ZIP de OSC son chicos (el más pesado del catálogo probado, wiimc,
# ronda los 6 MB comprimidos / 11 MB descomprimidos). Estos topes son
# generosos para cualquier app real y actúan como freno ante una respuesta
# rota o maliciosa (Content-Length mentiroso, "zip bomb"), mismo espíritu
# que `gametdb.WIITDB_MAX_UNCOMPRESSED`.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_UNCOMPRESSED_TOTAL_BYTES = 512 * 1024 * 1024

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


class InstallStatus(Enum):
    OK = "ok"
    DOWNLOAD_ERROR = "download_error"
    HASH_MISMATCH = "hash_mismatch"
    BAD_ZIP = "bad_zip"
    UNSAFE_ZIP = "unsafe_zip"
    # La URL de descarga (la del catálogo, o el destino de una
    # redirección) no pasó la lista blanca de esquema/host. Distinto de
    # `DOWNLOAD_ERROR`: no es que la descarga haya fallado, es que ni
    # siquiera se intentó porque el catálogo apuntaba a otro lado.
    UNSAFE_URL = "unsafe_url"
    UNSAFE_DEST_ROOT = "unsafe_dest_root"
    NO_SPACE = "no_space"
    IO_ERROR = "io_error"
    CANCELLED = "cancelled"
    # La instalación falló Y ADEMÁS `_stage_and_swap_unit` no pudo
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
    # Rutas finales escritas bajo dest_root/apps, para loguear o listar.
    installed_paths: tuple = ()
    # Respaldos de la versión anterior que se instalaron bien pero no se
    # pudieron borrar después (ver `_stage_and_swap_unit`). La
    # instalación es un éxito igual -`status` sigue siendo OK- pero
    # quedaron carpetas ocultas ocupando espacio en la unidad, y eso hay
    # que decirlo.
    orphaned_backups: tuple = ()

    @property
    def ok(self) -> bool:
        return self.status.is_ok


class CancelRequested(Exception):
    """Interna: corta el flujo de `install_app` apenas se pide cancelar."""


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
    (False, motivo, "", ""); nunca lanza salvo `CancelRequested` y
    `UnsafeDownloadURL`, que se dejan propagar para que `install_app` las
    distinga de un error de red común."""
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    try:
        # `open_allowlisted` valida la URL antes de abrir el socket (o
        # sea, antes de tocar `dest_path`), valida cada redirección antes
        # de seguirla y valida también la URL final: nada de lo que se
        # escribe acá puede venir de un host que no esté en la lista.
        with open_allowlisted(app.zip_url, REQUEST_TIMEOUT,
                              "la URL de descarga del catálogo") as resp:
            if resp.status != 200:
                return False, f"status HTTP {resp.status}", "", ""

            total = resp.length or app.zip_compressed_size
            if total is not None and total > MAX_DOWNLOAD_BYTES:
                return False, "el archivo declarado supera el límite esperado", "", ""

            written = 0
            with dest_path.open("wb") as f:
                while True:
                    _check_cancel(cancel_event)
                    chunk = resp.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        return False, "la descarga supera el límite esperado", "", ""
                    f.write(chunk)
                    md5.update(chunk)
                    sha256.update(chunk)
                    _report(on_progress, "download",
                           (written / total) if total else None)
    except CancelRequested:
        raise
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}", "", ""
    except (urllib.error.URLError, OSError, TimeoutError) as e:
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
            return (f"el SHA-256 no coincide (esperado {app.zip_sha256}, "
                    f"descargado {computed_sha256})")
        return None
    if app.zip_md5:
        if computed_md5.lower() != app.zip_md5.strip().lower():
            return (f"el MD5 no coincide (esperado {app.zip_md5}, "
                    f"descargado {computed_md5})")
        return None
    return None


# ------------------------------------------------- Validación del ZIP --
def _is_safe_member(name: str, dest_root: Path) -> bool:
    """True si la entrada `name` del ZIP puede extraerse sin escaparse de
    alguna de las carpetas de `_ALLOWED_TOP_LEVEL_DIRS` dentro de
    `dest_root`.

    Se combinan dos chequeos independientes a propósito: el del primer
    componente de la ruta (rápido, y documenta la regla: "solo estas
    carpetas de primer nivel") y el de la ruta resuelta (`resolve()` +
    contención), que es el que de verdad importa para la seguridad y
    cubre casos que el primero solo no ve (p. ej. componentes "."
    mezclados, o separadores repetidos)."""
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return False
    pure = PurePosixPath(normalized)
    if pure.is_absolute():
        return False
    if ".." in pure.parts:
        return False
    if not pure.parts or pure.parts[0] not in _ALLOWED_TOP_LEVEL_DIRS:
        return False

    try:
        target = (dest_root / normalized).resolve()
        allowed_root = (dest_root / pure.parts[0]).resolve()
    except OSError:
        return False
    return target == allowed_root or allowed_root in target.parents


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
       en el destino, y `_stage_and_swap_unit` instalaría una encima de
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
            return (f'"{archivo}" es un archivo y "{carpeta}" necesita ese '
                    f"mismo nombre como carpeta")
        if ruta_previa != ruta:
            return (f'"{ruta_previa}" y "{ruta}" son el mismo nombre en el '
                    f"destino (FAT32/exFAT no distingue mayúsculas)")
        if not es_carpeta:
            return f'"{ruta}" aparece dos veces en el ZIP'
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
        return None, InstallStatus.BAD_ZIP, f"el archivo descargado no es un ZIP válido ({e})"

    # Un solo punto de salida para el error: así, pase lo que pase adentro
    # del try, `zf` se cierra siempre que NO se vaya a devolver abierto (y
    # nunca se cierra en el único camino que sí lo devuelve).
    status: Optional[InstallStatus] = None
    error = ""
    try:
        bad_member = zf.testzip()
        if bad_member is not None:
            status = InstallStatus.BAD_ZIP
            error = f"el ZIP está corrupto o truncado (falló {bad_member})"
        else:
            total_uncompressed = 0
            for info in zf.infolist():
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_UNCOMPRESSED_TOTAL_BYTES:
                    status = InstallStatus.BAD_ZIP
                    error = "el contenido descomprimido supera el límite esperado"
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
                    error = f"el ZIP contiene un symlink ({info.filename}), rechazado"
                    break

                if not _is_safe_member(info.filename, dest_root):
                    status = InstallStatus.UNSAFE_ZIP
                    error = (f"entrada fuera de las carpetas permitidas "
                            f"({', '.join(_ALLOWED_TOP_LEVEL_DIRS)}) en el "
                            f"ZIP ({info.filename}), rechazado")
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
                    error = (f"el ZIP tiene entradas que colisionan en el "
                            f"destino: {motivo}; rechazado")
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        status = InstallStatus.BAD_ZIP
        error = f"el ZIP está corrupto o truncado ({e})"

    if status is not None:
        zf.close()
        return None, status, error
    return zf, None, ""


# -------------------------------------------------------------- Extracción --
def _extract_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo,
                    dest_root: Path) -> Path:
    """Copia UNA entrada ya validada a su destino final, de forma atómica
    (`atomicfs.atomic_write_target`, el mismo helper que usa
    `gametdb._store_cover`). Nunca usa `ZipFile.extract`: eso resolvería la
    ruta interna por su cuenta, y acá ya se decidió a mano exactamente
    dónde tiene que caer cada archivo. Copia bytes nomás -nunca cambia
    permisos de ejecución, nunca corre nada- así que el resultado es
    siempre un archivo de datos, jamás un programa en marcha.

    Si la copia se corta a mitad (unidad desconectada, disco lleno), el
    temporal se borra antes de propagar el error: mientras esta función
    armaba el temporal a mano se olvidaba de eso, y una instalación
    fallida dejaba un `.boot.dol.parcial-<sufijo>` tirado en la unidad del
    usuario para siempre."""
    target = dest_root / info.filename
    with atomic_write_target(target, mkparents=True) as tmp:
        with zf.open(info) as src, tmp.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    return target


# ---------------------------------------------- Unidades (carpeta atómica) --
def _group_members(members: list) -> tuple[dict, list]:
    """Separa los miembros del ZIP (ya validados, sin carpetas) en:

    - unidades: subcarpetas de primer nivel dentro de una carpeta
      permitida (ej. "apps/WiiDonut", o "controllers/Nintendont" si el
      ZIP trae esa estructura) que se instalan como bloque atómico con
      `_stage_and_swap_unit`.
    - sueltos: archivos que caen directo dentro de una carpeta permitida
      SIN ninguna subcarpeta (ej. "controllers/perfil.ini"). Para un
      archivo suelto no hay una "carpeta completa" que intercambiar -ya
      es atómico con `_extract_member` de siempre- así que agruparlo
      igual no compraría ninguna protección de más, solo complejidad."""
    unidades: dict = {}
    sueltos: list = []
    for info in members:
        parts = PurePosixPath(info.filename).parts
        if len(parts) >= 3:
            clave = f"{parts[0]}/{parts[1]}"
            unidades.setdefault(clave, []).append(info)
        else:
            sueltos.append(info)
    return unidades, sueltos


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


def _stage_and_swap_unit(zf: zipfile.ZipFile, unit_members: list,
                         final_dir: Path,
                         cancel_event: Optional[threading.Event],
                         on_step: Callable[[], None]) -> tuple[list, list]:
    """Instala UNA unidad -una subcarpeta de primer nivel del ZIP, ej.
    "apps/WiiDonut"- como bloque atómico: todo o nada, nunca una mezcla
    de archivos viejos y nuevos.

    El mecanismo (staging hermana, intercambio, respaldo de la versión
    anterior y su restauración si el intercambio falla) es
    `atomicfs.staged_directory`, el mismo que documenta paso por paso
    cómo y en qué orden pasa cada cosa. Lo que queda acá es lo propio de
    instalar una app: qué se escribe en la staging (los miembros del ZIP
    de esta unidad, sin sus dos primeros componentes de ruta), dónde se
    revisa la cancelación, y qué significa un fallo de restauración para
    el usuario -`library_ops.RollbackFailedError`, la misma excepción que usa
    `DestinationGuard` para el caso equivalente con archivos WBFS.

    Devuelve (rutas finales escritas, respaldos que no se pudieron
    borrar). Lo segundo casi siempre viene vacío: es el respaldo de la
    versión anterior, que se borra cuando el intercambio ya salió bien.
    Si ESE borrado falla, la app quedó instalada correctamente pero su
    versión anterior sigue ocupando lugar en una carpeta oculta que el
    usuario no va a encontrar solo, así que se reporta en vez de
    ignorarse (mismo criterio que `library_ops.DestinationGuard._discard`)."""
    relativos: list[PurePosixPath] = []
    try:
        with atomicfs.staged_directory(final_dir,
                                       staging_marca=MARCA_STAGING,
                                       backup_marca=MARCA_RESPALDO) as staging:
            for info in unit_members:
                _check_cancel(cancel_event)
                relativo = PurePosixPath(*PurePosixPath(info.filename).parts[2:])
                destino = staging.path / Path(*relativo.parts)
                destino.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, destino.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                relativos.append(relativo)
                on_step()
    except atomicfs.SwapRollbackFailed as e:
        # La primitiva reporta QUE no se pudo restaurar; qué significa eso
        # para el usuario lo decide este módulo, y acá significa lo mismo
        # que en `library_ops.DestinationGuard`: la estructura es distinta -un
        # solo par carpeta-original/carpeta-respaldo contra varios pares
        # de archivos WBFS- pero el problema es el mismo, y vale la misma
        # excepción. `from e.original_error` conserva la cadena original.
        raise library_ops.RollbackFailedError(
            e.pending, original_error=e.original_error) from e.original_error

    return ([final_dir / Path(*r.parts) for r in relativos],
            list(staging.orphaned_backups))


# -------------------------------------------------------------------- API --
def install_app(app: HomebrewApp, dest_root: Path, *,
                cancel_event: Optional[threading.Event] = None,
                on_progress: Optional[ProgressCallback] = None) -> InstallResult:
    """Descarga, verifica y extrae `app` a `dest_root/`, respetando solo
    las carpetas de primer nivel de `_ALLOWED_TOP_LEVEL_DIRS` (hoy "apps"
    y "controllers"; ver el comentario de esa constante).

    `dest_root` es la raíz de la unidad de destino (la USB/SD ya
    preparada, o cualquier carpeta que el usuario haya confirmado): esta
    función rechaza `dest_root` de entrada si es una ruta crítica del
    sistema operativo (`drives.is_critical_system_path`, mismo criterio
    que el BLINDAJE 4 de Modo Fábrica), crea `dest_root/apps` si no
    existe -para fallar rápido si el destino no es escribible, antes de
    gastar tiempo en la descarga-, y nunca escribe fuera de las carpetas
    permitidas (ver el comentario del módulo).

    Nunca lanza: cualquier fallo -de red, de integridad, de espacio, de
    E/S- vuelve como un `InstallResult` con el `status` que corresponda,
    nunca como una excepción sin capturar. `cancel_event`, si se pasa, se
    revisa entre cada bloque descargado y entre cada archivo extraído."""
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
                f"{dest_root} es una ruta crítica del sistema operativo, "
                "se rechaza como destino de instalación")

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
                f"la URL de descarga de la app fue rechazada: {url_err} "
                f"({app.zip_url})")

        try:
            (dest_root / "apps").mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return InstallResult(InstallStatus.IO_ERROR, app.slug, str(e))

        with tempfile.TemporaryDirectory(prefix="wiibackup-manager-oscwii-") as tmp_dir:
            tmp_zip = Path(tmp_dir) / f"{app.slug}.zip"

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

                needed = sum(i.file_size for i in members)
                free = transfer_plan.free_space(dest_root)
                if free is not None and needed > free:
                    return InstallResult(
                        InstallStatus.NO_SPACE, app.slug,
                        f"necesita {formatting.format_size(needed)} y quedan "
                        f"{formatting.format_size(free)} en el destino")

                unidades, sueltos = _group_members(members)
                total = len(members)
                hechos = [0]

                def _paso() -> None:
                    hechos[0] += 1
                    _report(on_progress, "extract",
                           (hechos[0] / total) if total else 1.0)

                written: list = []
                huerfanos: list = []
                for unit_key, unit_members in unidades.items():
                    _check_cancel(cancel_event)
                    final_dir = dest_root / unit_key
                    escritos, respaldos = _stage_and_swap_unit(
                        zf, unit_members, final_dir, cancel_event, _paso)
                    written.extend(escritos)
                    huerfanos.extend(respaldos)

                for info in sueltos:
                    _check_cancel(cancel_event)
                    written.append(_extract_member(zf, info, dest_root))
                    _paso()
            finally:
                zf.close()

        return InstallResult(InstallStatus.OK, app.slug,
                             installed_paths=tuple(written),
                             orphaned_backups=tuple(huerfanos))

    except CancelRequested:
        return InstallResult(InstallStatus.CANCELLED, app.slug,
                             "instalación cancelada")
    except UnsafeDownloadURL as e:
        # Sale de `_download_zip` (o de una redirección rechazada a mitad
        # de la descarga): la app no se instaló y nada del destino se
        # tocó, pero el motivo no es un fallo de red común y se reporta
        # distinto.
        return InstallResult(InstallStatus.UNSAFE_URL, app.slug, str(e))
    except library_ops.RollbackFailedError as e:
        # Caso grave: la instalación falló Y ADEMÁS no se pudo devolver
        # la versión anterior a su lugar (ver `_stage_and_swap_unit`).
        # `user_message` distingue esto de un `IO_ERROR` común -acá la
        # app puede haber quedado directamente inexistente, y el
        # respaldo que sigue existiendo (`e.pending`) es la única forma
        # de rescatarla a mano.
        return InstallResult(InstallStatus.ROLLBACK_FAILED, app.slug,
                             e.user_message())
    except OSError as e:
        return InstallResult(InstallStatus.IO_ERROR, app.slug, str(e))
    except Exception as e:  # noqa: BLE001 - red de seguridad final
        return InstallResult(InstallStatus.IO_ERROR, app.slug, str(e))
