"""Descarga y extracción segura de una app de Open Shop Channel.

Flujo de `install_app`, en orden, y por qué:

1. Descargar el ZIP a un directorio temporal (fuera del destino elegido:
   la USB/SD ya preparada por Modo Fábrica, o cualquier carpeta que el
   usuario confirme). Nada de esto toca el destino todavía.
2. Verificar el hash contra el que traiga la API (`HomebrewApp.zip_sha256`
   / `zip_md5`), si trae alguno. Hoy la API v3 de OSC no expone ningún
   hash (ver el comentario de `oscwii_client`), así que este paso no hace
   nada en la práctica todavía, pero el código está listo para cuando
   aparezca.
3. Validar que el ZIP sea válido de punta a punta (`ZipFile.testzip`, que
   chequea el CRC de cada miembro): sin esto, un ZIP corrupto o truncado
   se extraería a medias y dejaría una app instalada a medio copiar.
4. Validar CADA ruta interna del ZIP antes de extraer nada: tiene que
   empezar con "apps/" (la convención real de Homebrew Channel, confirmada
   bajando ZIPs reales - ver `oscwii_client`), no puede traer ".." ni ser
   absoluta, y el destino resuelto tiene que quedar adentro de
   `dest_root/apps`. Ningún archivo del ZIP puede ser un symlink (podría
   apuntar afuera de `apps/` y hacer que una entrada posterior, ya
   "adentro" según el nombre, en realidad escriba a través de él hacia
   otro lado). Si UNA sola entrada no pasa, se aborta todo: no se extrae
   nada, ni siquiera las entradas que sí eran seguras.
5. Extraer SOLO a `dest_root/apps/`, copiando bytes (nunca ejecutando ni
   el binario -.dol/.elf- ni ningún script) a un archivo temporal por
   entrada y renombrándolo al final (mismo patrón atómico que
   `gametdb._store_cover` y `config.write_text_atomic`).

Nada de esto usa GLib: igual que `gametdb.py`, este módulo es agnóstico de
la interfaz. `install_app` es una función sincrónica y bloqueante; quien
la llame desde la UI (Paso 3) es responsable de correrla en un hilo de
fondo y de reenviar cualquier actualización a GTK con `GLib.idle_add`,
como ya hace `queue_manager.TransferQueue` para las transferencias.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from . import library
from .oscwii_client import HomebrewApp

REQUEST_TIMEOUT = 20
DOWNLOAD_CHUNK_SIZE = 256 * 1024

# Los ZIP de OSC son chicos (el más pesado del catálogo probado, wiimc,
# ronda los 6 MB comprimidos / 11 MB descomprimidos). Estos topes son
# generosos para cualquier app real y actúan como freno ante una respuesta
# rota o maliciosa (Content-Length mentiroso, "zip bomb"), mismo espíritu
# que `gametdb.WIITDB_MAX_UNCOMPRESSED`.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024
MAX_UNCOMPRESSED_TOTAL_BYTES = 512 * 1024 * 1024

# Carpeta que debe contener, sí o sí, cada entrada del ZIP (ver el
# comentario del módulo y el de `oscwii_client`).
_REQUIRED_PREFIX = "apps/"


class InstallStatus(Enum):
    OK = "ok"
    DOWNLOAD_ERROR = "download_error"
    HASH_MISMATCH = "hash_mismatch"
    BAD_ZIP = "bad_zip"
    UNSAFE_ZIP = "unsafe_zip"
    NO_SPACE = "no_space"
    IO_ERROR = "io_error"
    CANCELLED = "cancelled"

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
    (False, motivo, "", ""); nunca lanza salvo `CancelRequested`, que se
    deja propagar para que `install_app` la distinga de un error real."""
    md5 = hashlib.md5()
    sha256 = hashlib.sha256()
    try:
        req = urllib.request.Request(
            app.zip_url, headers={"User-Agent": "wiibackup-manager-linux"})
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
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
def _is_safe_member(name: str, apps_dir: Path, dest_root: Path) -> bool:
    """True si la entrada `name` del ZIP puede extraerse sin escaparse de
    `dest_root/apps`.

    Se combinan dos chequeos independientes a propósito: el del prefijo
    de texto (rápido, y documenta la regla: "solo /apps/") y el de la ruta
    resuelta (`resolve()` + contención), que es el que de verdad importa
    para la seguridad y cubre casos que el primero solo no ve (p. ej.
    componentes "." mezclados, o separadores repetidos)."""
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return False
    if not normalized.startswith(_REQUIRED_PREFIX):
        return False
    pure = PurePosixPath(normalized)
    if pure.is_absolute():
        return False
    if ".." in pure.parts:
        return False

    try:
        target = (dest_root / normalized).resolve()
        apps_resolved = apps_dir.resolve()
    except OSError:
        return False
    return target == apps_resolved or apps_resolved in target.parents


def _validate_zip(zip_path: Path, apps_dir: Path,
                  dest_root: Path) -> tuple:
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

                if not _is_safe_member(info.filename, apps_dir, dest_root):
                    status = InstallStatus.UNSAFE_ZIP
                    error = (f"entrada fuera de /apps/ en el ZIP "
                            f"({info.filename}), rechazado")
                    break
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
    (temporal + `os.replace`, igual que `gametdb._store_cover`). Nunca usa
    `ZipFile.extract`: eso resolvería la ruta interna por su cuenta, y acá
    ya se decidió a mano exactamente dónde tiene que caer cada archivo.
    Copia bytes nomás -nunca cambia permisos de ejecución, nunca corre
    nada- así que el resultado es siempre un archivo de datos, jamás un
    programa en marcha."""
    target = dest_root / info.filename
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.parcial-{os.getpid()}")
    with zf.open(info) as src, tmp.open("wb") as dst:
        shutil.copyfileobj(src, dst)
    os.replace(tmp, target)
    return target


# -------------------------------------------------------------------- API --
def install_app(app: HomebrewApp, dest_root: Path, *,
                cancel_event: Optional[threading.Event] = None,
                on_progress: Optional[ProgressCallback] = None) -> InstallResult:
    """Descarga, verifica y extrae `app` a `dest_root/apps/`.

    `dest_root` es la raíz de la unidad de destino (la USB/SD ya
    preparada, o cualquier carpeta que el usuario haya confirmado): esta
    función crea `dest_root/apps` si no existe, pero nunca escribe fuera
    de ahí (ver el comentario del módulo).

    Nunca lanza: cualquier fallo -de red, de integridad, de espacio, de
    E/S- vuelve como un `InstallResult` con el `status` que corresponda,
    nunca como una excepción sin capturar. `cancel_event`, si se pasa, se
    revisa entre cada bloque descargado y entre cada archivo extraído."""
    try:
        dest_root = Path(dest_root)
        apps_dir = dest_root / "apps"
        try:
            apps_dir.mkdir(parents=True, exist_ok=True)
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

            zf, zip_status, zip_err = _validate_zip(tmp_zip, apps_dir, dest_root)
            if zf is None:
                return InstallResult(zip_status, app.slug, zip_err)

            try:
                members = [i for i in zf.infolist() if not i.is_dir()]

                needed = sum(i.file_size for i in members)
                free = library.free_space(dest_root)
                if free is not None and needed > free:
                    return InstallResult(
                        InstallStatus.NO_SPACE, app.slug,
                        f"necesita {library.format_size(needed)} y quedan "
                        f"{library.format_size(free)} en el destino")

                written: list = []
                for idx, info in enumerate(members):
                    _check_cancel(cancel_event)
                    written.append(_extract_member(zf, info, dest_root))
                    _report(on_progress, "extract", (idx + 1) / len(members)
                           if members else 1.0)
            finally:
                zf.close()

        return InstallResult(InstallStatus.OK, app.slug,
                             installed_paths=tuple(written))

    except CancelRequested:
        return InstallResult(InstallStatus.CANCELLED, app.slug,
                             "instalación cancelada")
    except OSError as e:
        return InstallResult(InstallStatus.IO_ERROR, app.slug, str(e))
    except Exception as e:  # noqa: BLE001 - red de seguridad final
        return InstallResult(InstallStatus.IO_ERROR, app.slug, str(e))
