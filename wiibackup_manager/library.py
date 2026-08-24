"""Escaneo de la biblioteca y modelo de datos de un juego."""
from __future__ import annotations

import csv
import io
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import drives, wit_wrapper
from .disc_header import (
    UNKNOWN_GAME_ID,
    DiscInfo,
    is_valid_game_id,
    read_plain_iso_header,
    validate_game_id,
)

VALID_EXTENSIONS = {".iso", ".wbfs", ".ciso", ".wdf"}

# Límite duro de FAT32 por archivo (en realidad 2^32 - 1 bytes; usamos
# 4 GiB parejo como aproximación) para decidir si un WBFS ya existente
# entra sin dividir. Nota: esto es distinto de
# `wit_wrapper.FAT32_SPLIT_SIZE_BYTES` (4 GB, más chico a propósito), que es
# el tamaño de partición que le pedimos a `wit` cuando sí hace falta
# dividir — no hace falta que coincidan, uno decide "¿hace falta dividir?"
# y el otro "¿dónde corta wit cada parte?".
_FAT32_SIZE_LIMIT_BYTES = 4 * 1024 ** 3

_INVALID_FS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def format_size(n: int) -> str:
    """GB con un decimal, o MB si es menos de 1 GB (evita mostrar '0.0 GB'
    para tamaños chicos)."""
    gb = n / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{n / (1024 ** 2):.1f} MB"


def format_eta(seconds: float) -> str:
    """Tiempo estimado restante en formato corto ('45s', '2m 15s', '1h 5m')."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


@dataclass
class Game:
    path: Path
    game_id: str
    title: str
    fmt: str  # "ISO" | "WBFS" | "CISO" | "WDF" | "?"
    size_bytes: int
    identified_by: str  # "iso" | "wit" | "unknown"

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 * 1024)


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = _INVALID_FS_CHARS.sub("", name)
    name = name.strip().rstrip(".")
    return name or "untitled"


def _format_from_suffix(path: Path) -> str:
    return path.suffix.lstrip(".").upper() or "?"


def identify_file(path: Path, wit_binary: str = "wit") -> Optional[Game]:
    """Identifica un único archivo de juego, probando primero el parseo
    directo (rápido, sin dependencias) y usando `wit` como respaldo para
    formatos envueltos (WBFS, CISO, WDF)."""
    if path.suffix.lower() not in VALID_EXTENSIONS:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size == 0:
        return None

    info: Optional[DiscInfo] = None

    if path.suffix.lower() == ".iso":
        info = read_plain_iso_header(path)

    if info is None and wit_wrapper.is_available(wit_binary):
        try:
            info = wit_wrapper.identify(path, wit_binary)
        except wit_wrapper.WitNotFoundError:
            info = None

    # Punto único donde nace el game_id de un `Game`: si lo que devolvió el
    # header (o `wit`) no es un ID6 válido, el archivo se trata como no
    # identificado en vez de arrastrar un ID que después terminaría
    # formando parte de una ruta del filesystem (ver disc_header).
    if info is None or not is_valid_game_id(info.game_id):
        return Game(
            path=path,
            game_id=UNKNOWN_GAME_ID,
            title=info.title if info is not None and info.title else path.stem,
            fmt=_format_from_suffix(path),
            size_bytes=size,
            identified_by="unknown",
        )

    return Game(
        path=path,
        game_id=validate_game_id(info.game_id),
        title=info.title,
        fmt=_format_from_suffix(path),
        size_bytes=size,
        identified_by=info.source,
    )


def find_game_files(root: Path, skipped: Optional[list] = None) -> list[Path]:
    """Todos los ISO/WBFS/CISO/WDF que haya debajo de `root`, ordenados.

    Camina con `os.walk` y no con `Path.rglob` justamente por las carpetas
    que no se pueden leer: rglob se las saltea en silencio (probado en
    Python 3.14: no levanta PermissionError, simplemente devuelve menos
    archivos), así que el usuario veía faltar juegos que sabía que estaban
    y no tenía forma de enterarse de por qué. `os.walk` avisa por
    `onerror`, y acá esas carpetas se saltean igual -una carpeta ilegible
    no puede frenar el escaneo de todo lo demás- pero se anotan en
    `skipped` para que quien llame lo pueda informar.

    Con `skipped=None` el comportamiento es el de siempre: saltear y
    seguir."""
    found: list[Path] = []

    def on_error(error: OSError) -> None:
        if skipped is None:
            return
        name = getattr(error, "filename", None)
        skipped.append(Path(name) if name else Path(root))

    for dirpath, _dirnames, filenames in os.walk(root, onerror=on_error):
        base = Path(dirpath)
        for name in filenames:
            path = base / name
            if path.suffix.lower() in VALID_EXTENSIONS and path.is_file():
                found.append(path)
    found.sort()
    return found


def scan_library(
    root: Path,
    wit_binary: str = "wit",
    progress_cb: Optional[Callable[[int, int], None]] = None,
    skipped_dirs: Optional[list] = None,
) -> list[Game]:
    """Escanea recursivamente `root` buscando ISO/WBFS/CISO/WDF.

    `skipped_dirs`, si se pasa, recibe las carpetas que no se pudieron
    leer (permisos, unidad que se desconectó a mitad del escaneo). Ver
    `find_game_files`."""
    if not root.exists():
        return []

    candidates = find_game_files(root, skipped_dirs)

    games: list[Game] = []
    total = len(candidates)
    for i, path in enumerate(candidates, start=1):
        game = identify_file(path, wit_binary)
        if game is not None:
            games.append(game)
        if progress_cb:
            progress_cb(i, total)

    games.sort(key=lambda g: g.title.lower())
    return games


def standard_filename(game: Game) -> str:
    """Nombre estándar sugerido: 'Título [GAMEID].ext', como usa el
    WiiBackup Manager clásico y la mayoría de USB Loaders.

    Si el juego no está identificado (o su ID no es un ID6 válido) se
    omite el sufijo '[ID]' en vez de meter en el nombre del archivo algo
    que no es un ID: '??????' trae caracteres que FAT32 no acepta, y un ID
    arbitrario leído del header podría traer separadores de ruta."""
    safe_title = sanitize_filename(game.title)
    ext = game.path.suffix
    if not is_valid_game_id(game.game_id):
        return f"{safe_title}{ext}"
    return f"{safe_title} [{validate_game_id(game.game_id)}]{ext}"


def free_variant(path: Path) -> Path:
    """Variante libre de `path` agregando un sufijo: 'Juego.wbfs' ->
    'Juego (2).wbfs'. Se usa cuando el nombre que corresponde ya está
    ocupado por OTRO archivo y pisarlo perdería un juego."""
    n = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f"{path.stem} ({n}){path.suffix}")
        n += 1
    return candidate


# Cuántos nombres alternativos se prueban antes de darse por vencido.
_MAX_RENAME_ATTEMPTS = 100


def rename_no_replace(src: Path, dest: Path) -> None:
    """Renombra `src` a `dest` sin pisar `dest` NUNCA.

    `Path.rename` en Linux reemplaza el destino en silencio, así que el
    patrón "si no existe, renombrar" tiene una ventana entre las dos
    cosas: si en ese intervalo aparece un archivo ahí -el gestor de
    archivos, un script, otra copia de esta app- se lo borra sin aviso.

    Acá el nombre se reserva primero con O_CREAT|O_EXCL, que es atómico y
    falla con FileExistsError si alguien llegó antes, y recién después se
    mueve el archivo encima de esa reserva propia. Se hace así y no con
    renameat2(RENAME_NOREPLACE) porque esto anda en cualquier filesystem
    (los pendrives suelen ser FAT32/exFAT) y sin ctypes.

    Si el proceso se muere justo entre la reserva y el movimiento queda un
    archivo de 0 bytes con el nombre nuevo: es lo peor que puede pasar, y
    es preferible a perder un juego."""
    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(fd)
    try:
        os.replace(src, dest)
    except OSError:
        # El movimiento falló: sacar la reserva para no dejar basura.
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise


def rename_to_standard(game: Game, dry_run: bool = False,
                        on_collision: str = "error") -> Path:
    """Renombra el archivo del juego a la convención 'Título [ID].ext'
    dentro de la misma carpeta. Devuelve la nueva ruta.

    `on_collision` decide qué pasa si ese nombre ya está tomado por otro
    archivo: "error" levanta `FileExistsError` (flujo de un juego suelto,
    donde el usuario ve el aviso y decide) y "suffix" busca una variante
    libre con sufijo (flujo en lote, donde frenar por cada choque no
    tendría sentido). En ninguno de los dos casos se pisa nada.

    Dos juegos con el mismo título y sin ID identificado dan el mismo
    nombre estándar: ese es el caso real de colisión, y en lote termina
    como 'Título.iso' y 'Título (2).iso'."""
    new_name = standard_filename(game)
    new_path = game.path.with_name(new_name)
    if new_path == game.path:
        return game.path

    if dry_run:
        if new_path.exists():
            if on_collision != "suffix":
                raise FileExistsError(f"Ya existe un archivo en {new_path}")
            new_path = free_variant(new_path)
        return new_path

    # Sin chequear-y-después-renombrar: `rename_no_replace` reserva el
    # nombre de forma atómica y avisa si estaba tomado, así que un archivo
    # que aparezca justo en el medio no se pierde.
    try:
        rename_no_replace(game.path, new_path)
    except FileExistsError:
        if on_collision != "suffix":
            raise FileExistsError(f"Ya existe un archivo en {new_path}")
        # Buscar una variante libre reintentando: si otro proceso se queda
        # con "Juego (2).wbfs" mientras tanto, se sigue con la siguiente.
        base = new_path
        for n in range(2, _MAX_RENAME_ATTEMPTS + 2):
            candidate = base.with_name(f"{base.stem} ({n}){base.suffix}")
            try:
                rename_no_replace(game.path, candidate)
            except FileExistsError:
                continue
            new_path = candidate
            break
        else:
            raise FileExistsError(
                f"No se encontró un nombre libre para {base.name}"
            )
    game.path = new_path
    return new_path


def needs_rename(game: Game) -> bool:
    """True si el archivo no está ya con el nombre estándar."""
    return game.path.name != standard_filename(game)


# Tamaño de bloque para la copia manual con progreso (_copy_with_progress).
# 1 MiB: bastante grande para no perder tiempo en overhead de syscalls en
# un archivo de varios GB, y bastante chico para que cancelar surta efecto
# rápido, porque la cancelación se revisa una vez por bloque: con los
# 4 MiB de antes, un USB lento (~10 MB/s) seguía escribiendo casi medio
# segundo después de tocar "Cancelar", y en un pendrive malo varios
# segundos.
#
# Medido con 1 GiB: entre 4 MiB y 256 KiB no hay diferencia de velocidad
# fuera del ruido (~1.6 GB/s en las dos puntas), o sea que a estos tamaños
# manda el disco, no la cantidad de syscalls. La granularidad del progreso
# no depende de esto: `progress_cb` está limitado a una llamada por
# segundo aparte.
_COPY_CHUNK_BYTES = 1024 * 1024


def _copy_with_progress(
    src: Path,
    dest: Path,
    progress_cb: Callable[[int], None],
    cancel: Optional["wit_wrapper.CancellationToken"] = None,
) -> None:
    """Igual que `shutil.copy2(src, dest)` (copia contenido + metadata),
    pero reportando cuánto lleva copiado cada ~1s vía `progress_cb`, algo
    que `shutil.copy2` no ofrece.

    Si se pasa `cancel`, se revisa entre bloques: cancelar corta la copia
    en el momento (no cuando el archivo termina solo, que con varios GB
    sobre USB pueden ser 20 minutos) y borra el destino a medio escribir,
    que no sirve para nada y ocuparía lugar en la unidad."""
    written = 0
    last_report = time.monotonic()
    try:
        with open(src, "rb") as fsrc, open(dest, "wb") as fdst:
            while True:
                if cancel is not None and cancel.cancelled:
                    raise wit_wrapper.OperationCancelled(
                        "Transferencia cancelada por el usuario."
                    )
                buf = fsrc.read(_COPY_CHUNK_BYTES)
                if not buf:
                    break
                fdst.write(buf)
                written += len(buf)
                now = time.monotonic()
                if now - last_report >= 1.0:
                    progress_cb(written)
                    last_report = now
    except BaseException:
        # El destino quedó truncado a mitad de camino (open("wb") ya lo
        # había vaciado): un WBFS parcial no sirve y confundiría al
        # próximo escaneo, así que se borra.
        try:
            dest.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    progress_cb(written)
    shutil.copystat(src, dest)


class DestinationExistsError(FileExistsError):
    """El archivo destino en la unidad WBFS ya existe y no se pidió
    sobrescribirlo. Es una condición esperable (el juego ya está en la
    unidad), no un error de la operación: quien llama decide si preguntar
    (flujo individual) o contarlo como omitido (flujo en lote)."""

    def __init__(self, dest: Path):
        super().__init__(f"Ya existe un archivo en {dest}")
        self.dest = dest


def wbfs_dest_path(game: Game, drive_root: Path) -> Path:
    """Ruta final que va a ocupar `game` dentro de `drive_root`, en la
    estructura 'wbfs/<ID6>/<ID6>.wbfs' que reconocen los USB Loaders.

    Se expone aparte de `send_to_wbfs_drive` para que la interfaz pueda
    chequear de antemano si ese destino ya existe (y preguntar antes de
    pisarlo) sin duplicar cómo se arma la ruta."""
    # Validar ANTES de armar la ruta (no solo al mostrar el ID en la
    # interfaz): el game_id sale del header del archivo, que la app no
    # controla, y acá se convierte en un componente de ruta real. Ver
    # `disc_header.validate_game_id`.
    game_id = validate_game_id(game.game_id)
    return Path(drive_root) / "wbfs" / game_id / f"{game_id}.wbfs"


# Un disco de Wii de una capa son 4.7 GB; los de doble capa, 8.5 GB. Se
# usan como cota superior cuando no hay forma de saber el tamaño real.
_WII_SINGLE_LAYER_BYTES = 4_699_979_776
# Margen sobre el tamaño de datos que informa `wit`: el WBFS de destino
# redondea a su tamaño de bloque y guarda su propia tabla, así que ocupa
# un poco más que los datos puros (medido: ~1%).
_WBFS_OVERHEAD = 1.05

# Formatos cuyo tamaño de archivo NO es una cota superior del WBFS final:
# guardan el disco de forma compacta y al pasarlos a WBFS pueden crecer.
_COMPACT_FORMATS = {"CISO", "WDF"}


def estimate_transfer_size(game: Game, wit_binary: str = "wit") -> int:
    """Cuántos bytes va a ocupar `game` en la unidad de destino.

    Antes se usaba directamente `game.size_bytes` con el argumento de que
    "la conversión solo achica". Eso vale para un ISO plano (que trae todo
    el padding del disco) pero NO para CISO ni WDF, que ya vienen
    compactos: ahí el archivo puede pesar bastante menos que el WBFS que
    va a generar, el chequeo previo de espacio pasaba igual y `wit`
    fallaba a mitad de una transferencia larga con el disco lleno.

    Se le pregunta a `wit` (barato: lee el header, no el archivo). Si no
    se puede, se cae a la cota que corresponda: para ISO/WBFS el propio
    tamaño del archivo sigue siendo una cota superior razonable; para los
    formatos compactos, el tamaño de un disco de una capa, que es lo
    mínimo honesto que se puede afirmar sin abrir el archivo."""
    real = wit_wrapper.iso_size_bytes(game.path, wit_binary)
    if real:
        return int(real * _WBFS_OVERHEAD)
    if game.fmt.upper() in _COMPACT_FORMATS:
        return max(game.size_bytes, _WII_SINGLE_LAYER_BYTES)
    return game.size_bytes


def free_space(path: Path) -> Optional[int]:
    """Bytes libres en el filesystem de `path`, o None si no se puede
    saber (unidad desconectada a mitad de camino, por ejemplo)."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def wbfs_dest_paths(games, drive_root: Path) -> list:
    """Las rutas que van a ocupar `games` dentro de `drive_root`.

    Se saltean los juegos cuyo Game ID no sea válido: para esos no hay
    ruta que calcular (los rechaza `wbfs_dest_path`) y la transferencia
    los va a reportar como error igual. Se usa para declararle al
    OperationManager qué archivos va a escribir la transferencia."""
    destinos = []
    for game in games:
        try:
            destinos.append(wbfs_dest_path(game, drive_root))
        except ValueError:
            continue
    return destinos


def send_to_wbfs_drive(
    game: Game,
    drive_root: Path,
    wit_binary: str = "wit",
    bytes_progress_cb: Optional[Callable[[int], None]] = None,
    overwrite: bool = False,
    cancel: Optional["wit_wrapper.CancellationToken"] = None,
) -> Path:
    """Copia `game` a la estructura estándar 'wbfs/<ID6>/<ID6>.wbfs' que
    reconocen los USB Loaders de Wii (USB Loader GX, CFG USB Loader, etc.)
    dentro de `drive_root`. Si el origen ya es WBFS y entra entero se copia
    tal cual; para cualquier otro formato (ISO/CISO/WDF), o si el destino
    puede necesitar dividir el archivo, se delega en `wit`, que es quien
    sabe empaquetar (y, si hace falta, partir) el WBFS correctamente.

    FAT32 tiene un límite duro de ~4GiB por archivo, y hay discos Wii
    dual-layer que lo superan: si el filesystem del destino no se puede
    determinar con confianza, se asume que hace falta dividir (ver
    `drives.needs_wbfs_split`).

    `bytes_progress_cb`, si se pasa, se llama periódicamente con los bytes
    escritos hasta el momento hacia `dest` (copia directa o vía `wit`, ver
    `wit_wrapper.convert`), para poder mostrar progreso real dentro de la
    copia/conversión de un solo juego grande.

    Si el destino ya existe y no se pasa `overwrite=True`, levanta
    `DestinationExistsError` sin tocar nada: los dos caminos (copia
    directa con open("wb"), que trunca al instante, y `wit COPY
    --overwrite`) reemplazan el archivo sin vuelta atrás, así que la
    decisión de pisarlo es de quien llama, igual que ya pasaba al
    convertir.

    `cancel`, si se pasa, hace que cancelar corte esta copia/conversión en
    el momento (matando el `wit` en curso o abortando la copia directa) y
    levante `wit_wrapper.OperationCancelled`."""
    if cancel is not None and cancel.cancelled:
        raise wit_wrapper.OperationCancelled("Transferencia cancelada por el usuario.")

    dest = wbfs_dest_path(game, drive_root)
    dest_dir = dest.parent

    if not overwrite and dest.exists():
        raise DestinationExistsError(dest)

    dest_dir.mkdir(parents=True, exist_ok=True)

    split = drives.needs_wbfs_split(dest_dir)

    # Copia directa solo si ya es WBFS Y sabemos que entra entero sin
    # dividir (si hiciera falta dividir, una copia plana no puede hacerlo:
    # hay que pasar por `wit COPY --split`).
    if game.fmt.upper() == "WBFS" and not (split and game.size_bytes >= _FAT32_SIZE_LIMIT_BYTES):
        if bytes_progress_cb is not None or cancel is not None:
            _copy_with_progress(game.path, dest,
                                bytes_progress_cb or (lambda _n: None), cancel)
        else:
            shutil.copy2(game.path, dest)
        return dest

    if not wit_wrapper.is_available(wit_binary):
        raise wit_wrapper.WitNotFoundError(wit_binary)

    result = wit_wrapper.convert(game.path, dest, "WBFS", wit_binary, split=split,
                                  bytes_progress_cb=bytes_progress_cb, cancel=cancel)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Error desconocido al convertir con wit")
    return dest


# --------------------------------------------------------------- Exportar --
#
# El armado del texto vive acá y no en la ventana para poder probarlo sin
# levantar GTK, y porque es lo mismo que se exportaría desde cualquier otra
# vista que muestre juegos.

EXPORT_CSV = "csv"
EXPORT_TEXT = "text"


# Caracteres con los que Excel y LibreOffice arrancan a interpretar una
# celda como fórmula. El título de un juego sale del header de un archivo
# que la app no controla, así que uno llamado "=1+1" o
# "@SUM(1+1)*cmd|'/c calc'!A0" se ejecutaría al abrir la lista exportada
# en la computadora de un cliente. Tab y retorno de carro entran en la
# lista porque algunas versiones los tratan como separadores y corren la
# interpretación a la celda siguiente.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str) -> str:
    """Neutraliza una celda que podría interpretarse como fórmula.

    Se le antepone un apóstrofe, que es la marca de "esto es texto" que
    entienden las hojas de cálculo: no se ve al abrir el archivo y la
    celda queda con el valor literal."""
    text = str(value)
    if text.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


def export_games(games, fmt: str = EXPORT_CSV) -> str:
    """Devuelve el contenido del archivo a exportar para `games`.

    `EXPORT_CSV` arma una planilla con Título, ID, Formato y Tamaño. El
    tamaño va dos veces, legible y en bytes: "4.3 GB" se lee de una pero
    se ordena mal en una planilla, y el número crudo ordena bien pero no
    se lee. Poner las dos columnas sale gratis y evita tener que elegir.

    `EXPORT_TEXT` arma una lista suelta ("Título — 4.3 GB", una por línea)
    para pegar en un chat, con el total al final."""
    if fmt == EXPORT_TEXT:
        lineas = [f"{game.title} — {format_size(game.size_bytes)}" for game in games]
        total = sum(game.size_bytes for game in games)
        noun = "juego" if len(games) == 1 else "juegos"
        lineas.append("")
        lineas.append(f"{len(games)} {noun} · {format_size(total)}")
        return "\n".join(lineas) + "\n"

    buffer = io.StringIO()
    # QUOTE_MINIMAL con la coma como separador: los títulos de Wii traen
    # comas y dos puntos ("Zelda: Skyward Sword"), y el módulo csv ya los
    # entrecomilla solo cuando hace falta.
    writer = csv.writer(buffer)
    writer.writerow(["Título", "ID", "Formato", "Tamaño", "Tamaño (bytes)"])
    for game in games:
        # Los tres campos de texto pasan por `_csv_safe`; los tamaños son
        # números que arma la app, no hace falta.
        writer.writerow([_csv_safe(game.title), _csv_safe(game.game_id),
                         _csv_safe(game.fmt),
                         format_size(game.size_bytes), game.size_bytes])
    return buffer.getvalue()
