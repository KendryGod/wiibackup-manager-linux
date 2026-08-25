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
    """Renombra `src` a `dest` sin pisar un archivo ajeno.

    `Path.rename` en Linux reemplaza el destino en silencio, así que el
    patrón "si no existe, renombrar" tiene una ventana entre las dos
    cosas: si en ese intervalo aparece un archivo ahí -el gestor de
    archivos, un script, otra copia de esta app- se lo borra sin aviso.

    Acá el nombre se reserva primero con O_CREAT|O_EXCL, que es atómico y
    falla con FileExistsError si alguien llegó antes, y recién después se
    mueve el archivo encima de esa reserva propia. Se hace así y no con
    renameat2(RENAME_NOREPLACE) porque esto anda en cualquier filesystem
    (los pendrives suelen ser FAT32/exFAT) y sin ctypes.

    Hasta dónde llega la garantía, con precisión:

    - contra las carreras de la propia app (dos operaciones sobre la misma
      carpeta) y contra el uso normal de otros programas: el nombre queda
      reservado de forma atómica, así que no se pisa nada;
    - lo que NO cubre es un proceso externo que borre o reemplace
      justamente nuestra reserva entre el O_CREAT|O_EXCL y el os.replace.
      Ahí el replace pisaría lo que haya quedado con ese nombre. Es una
      ventana de microsegundos y hace falta que alguien esté buscando
      pisarla a propósito; no hay forma de cerrarla del todo sin
      renameat2, y no vale la pena pagar esa complejidad por un escenario
      que no es el de esta app.

    Si el proceso se muere justo entre la reserva y el movimiento queda un
    archivo de 0 bytes con el nombre nuevo: es lo peor que puede pasar por
    las buenas, y es preferible a perder un juego."""
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
    sobre USB pueden ser 20 minutos).

    NUNCA se escribe sobre `dest` directamente. Se copia a un archivo
    temporal en la misma carpeta y recién cuando la copia terminó entera
    (y bajó a disco) se lo mueve encima del destino, que es una operación
    atómica dentro del mismo filesystem.

    El motivo es el caso que más caro sale: sobrescribir. `open(dest,
    "wb")` vacía el archivo destino en el acto, así que si la copia se
    caía después -USB desenchufado, cancelación, disco lleno- el respaldo
    bueno que el cliente ya tenía en esa unidad ya no existía, y lo único
    que hacía el `except` era borrar la basura que había quedado. Con el
    temporal, un fallo en cualquier punto deja el destino original
    exactamente como estaba."""
    written = 0
    last_report = time.monotonic()
    # Oculto y con el PID adentro: no lo toma un escaneo de la biblioteca
    # y dos copias simultáneas hacia el mismo destino no se pisan el
    # temporal entre sí.
    tmp = dest.with_name(f".{dest.name}.parcial-{os.getpid()}")
    try:
        with open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
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
            # A disco ANTES del intercambio: si no, el rename puede quedar
            # registrado mientras los datos siguen en cache, y un tirón del
            # cable dejaría el destino nuevo incompleto y el viejo ya
            # borrado.
            fdst.flush()
            os.fsync(fdst.fileno())
        shutil.copystat(src, tmp)
        os.replace(tmp, dest)
    except BaseException:
        # Solo se borra el temporal: el destino original -si había uno- no
        # se tocó en ningún momento.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    progress_cb(written)


def copy_atomic(src: Path, dest: Path) -> None:
    """Copia `src` encima de `dest` sin que exista un instante en que
    `dest` esté a medio escribir. Es `_copy_with_progress` sin progreso ni
    cancelación: para quien quiere solo la garantía de atomicidad.

    Se usa donde el usuario YA confirmó que quiere pisar ese archivo. Que
    haya dado el permiso no significa que quiera perder las dos copias si
    la escritura se corta a mitad, que es lo que pasa con `shutil.copy2`:
    abre el destino con "wb" y lo vacía en el acto."""
    _copy_with_progress(src, dest, lambda _n: None)


def copy_no_replace(src: Path, dest: Path) -> None:
    """Copia `src` a `dest` sin pisar un archivo ajeno.

    Es a `copy_atomic` lo que `rename_no_replace` es a `Path.rename`, y
    existe por el mismo motivo: el patrón "si no existe, copiar" tiene una
    ventana entre las dos cosas. En la importación esa ventana es larga de
    verdad -los destinos se planifican en el hilo de GTK y la copia
    arranca después, tras identificar cada archivo con `wit`- así que
    alcanza con que otro programa, un script o una segunda instancia de
    esta app cree un archivo con ese nombre en el medio para que la copia
    se lo lleve puesto sin preguntar.

    El nombre se reserva primero con O_CREAT|O_EXCL, que es atómico y
    falla con `FileExistsError` si alguien llegó antes, y recién después
    se copia el contenido encima de esa reserva propia. Quien llama decide
    qué hacer con esa colisión tardía (buscar otro nombre, avisar); lo que
    no puede pasar es que se pise en silencio.

    La garantía llega hasta donde llega la de `rename_no_replace`: cubre
    las carreras de la propia app y el uso normal de otros programas, y no
    cubre a alguien que borre o reemplace justamente nuestra reserva entre
    el O_CREAT|O_EXCL y el movimiento final."""
    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.close(fd)
    try:
        copy_atomic(src, dest)
    except BaseException:
        # La copia no llegó a completarse, así que lo que hay en `dest` es
        # la reserva vacía y no el archivo de nadie: se saca para no dejar
        # un archivo de 0 bytes ocupando el nombre.
        try:
            os.unlink(dest)
        except OSError:
            pass
        raise


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
# un poco más que los datos puros (medido: ~1%; se usa 5% para no andar
# al filo).
#
# Es una heurística conservadora, no una garantía matemática: el número
# exacto depende del tamaño de bloque que elija `wit` y de si divide el
# archivo. Por eso el espacio libre se vuelve a comprobar antes de cada
# juego en vez de confiar en una única cuenta hecha al principio.
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


@dataclass(frozen=True)
class TransferItem:
    """Un juego dentro de una transferencia, con sus DOS tamaños.

    Confundirlos es fácil y da números falsos: `source_bytes` es lo que
    pesa el archivo de origen (lo que se lee) y `output_bytes` lo que va a
    ocupar en el destino (lo que se escribe). Para un ISO plano el
    segundo es menor -el WBFS descarta el padding-, y para un CISO o un
    WDF es al revés, porque esos ya vienen compactos. El chequeo de
    espacio y la barra de progreso tienen que hablar de lo que se ESCRIBE;
    usar el tamaño del archivo de origen para las dos cosas hacía que en
    CISO/WDF la barra y el tiempo restante no tuvieran nada que ver con
    la realidad."""

    game: "Game"
    source_bytes: int
    output_bytes: int


def plan_transfer(games, wit_binary: str = "wit") -> list:
    """Arma los `TransferItem` de un lote.

    OJO: esto puede tardar. Le pregunta a `wit` por cada juego (barato,
    milisegundos) pero con un archivo dañado o una unidad lenta puede
    demorar, así que va SIEMPRE en un hilo de fondo: llamarlo desde el
    hilo de GTK congela la ventana entera."""
    return [
        TransferItem(game=game, source_bytes=game.size_bytes,
                     output_bytes=estimate_transfer_size(game, wit_binary))
        for game in games
    ]


# Un disco de Wii de doble capa: lo que ocupa un ISO plano de esos.
_WII_DUAL_LAYER_BYTES = 8_511_160_320


def estimate_output_size(game: Game, target_ext: str, wit_binary: str = "wit") -> int:
    """Cuánto va a pesar `game` convertido a `target_ext`.

    No es lo mismo según a qué se convierta, y por eso no alcanza con
    `estimate_transfer_size`: un WBFS guarda solo los sectores usados,
    pero un ISO plano trae el disco entero con su relleno. Convertir un
    WBFS de 350 MB a ISO da 4.7 GB, no 350 MB.

    Se usa como denominador de la barra de progreso de la conversión: el
    callback de `wit` informa bytes escritos en el DESTINO, así que
    dividir por el tamaño del archivo de origen daba una barra que llegaba
    al final antes de tiempo (o que no llegaba nunca)."""
    if target_ext.lower().lstrip(".") == "iso":
        usado = wit_wrapper.iso_size_bytes(game.path, wit_binary)
        if usado and usado > _WII_SINGLE_LAYER_BYTES:
            return _WII_DUAL_LAYER_BYTES
        return _WII_SINGLE_LAYER_BYTES
    return estimate_transfer_size(game, wit_binary)


def free_space(path: Path) -> Optional[int]:
    """Bytes libres en el filesystem de `path`, o None si no se puede
    saber (unidad desconectada a mitad de camino, por ejemplo)."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def wbfs_group(dest: Path) -> list:
    """`dest` y las partes que lo acompañan si el WBFS está dividido.

    `wit` parte los juegos grandes en 'juego.wbfs' + 'juego.wbf1' +
    'juego.wbf2'…, y todas esas piezas son UN respaldo: reemplazar unas y
    dejar otras deja un juego inservible, así que se tratan siempre como
    un conjunto."""
    miembros = []
    try:
        if dest.exists():
            miembros.append(dest)
    except OSError:
        return miembros
    stem = dest.with_suffix("")
    numero = 1
    while True:
        parte = stem.with_suffix(f".wbf{numero}")
        try:
            if not parte.exists():
                break
        except OSError:
            break
        miembros.append(parte)
        numero += 1
    return miembros


class DestinationGuard:
    """Aparta lo que ya hay en el destino y lo devuelve si algo falla.

    Es para las operaciones que le pasan `--overwrite` a `wit`. El
    problema con dejar que `wit` sobrescriba solo es que no hay forma
    confiable de deshacerlo: `wit` escribe en temporales propios y los
    renombra a los nombres finales al terminar, así que si lo matan
    justo después de renombrar la primera parte, el nombre final ya
    quedó con contenido parcial y el limpiador no puede distinguirlo del
    archivo que el usuario tenía ahí desde antes.

    Con el respaldo explícito no hay nada que adivinar: los archivos que
    ya estaban se mueven a un nombre oculto (un rename dentro de la misma
    carpeta, instantáneo y sin copiar datos), `wit` escribe sobre nombres
    libres, y al final se borra el respaldo o se lo devuelve a su lugar
    según cómo haya salido.

    El precio es que mientras dura la operación conviven el respaldo y lo
    nuevo, o sea que hace falta espacio para los dos. Es el costo de
    poder volver atrás.

    Uso:

        with DestinationGuard(dest) as guard:
            ...escribir dest...
            guard.commit()      # solo si salió TODO bien
    """

    def __init__(self, dest: Path, enabled: bool = True):
        self.dest = Path(dest)
        self.enabled = enabled
        self._saved: list = []
        self._committed = False
        self._outputs_before: set = set()

    def __enter__(self) -> "DestinationGuard":
        if not self.enabled:
            # Sin respaldo que apartar, pero la foto se toma igual: la
            # limpieza de un fallo tiene que poder distinguir lo que dejó
            # ESTA operación de lo que ya estaba.
            self._outputs_before = wit_wrapper.output_files(self.dest)
            return self
        marca = f".respaldo-{os.getpid()}"
        for original in wbfs_group(self.dest):
            respaldo = original.with_name(f".{original.name}{marca}")
            try:
                os.replace(original, respaldo)
            except OSError:
                # No se pudo apartar: se deshace lo ya apartado y se sale
                # sin tocar nada, mejor que quedar a mitad de camino.
                self._restore()
                raise
            self._saved.append((original, respaldo))
        # La foto va DESPUÉS de apartar el respaldo, no antes: el respaldo
        # se llama `.{nombre}.respaldo-PID` y cae dentro del mismo glob con
        # el que `wit_wrapper` reconoce sus temporales
        # (`.{nombre}.{lo que sea}`). Si la foto se tomara antes, el
        # respaldo aparecería como "archivo nuevo de esta operación" y la
        # limpieza de un fallo lo borraría — o sea, justo lo contrario de
        # para lo que existe esta clase.
        self._outputs_before = wit_wrapper.output_files(self.dest)
        return self

    def commit(self) -> None:
        """La operación salió bien: el respaldo ya no hace falta."""
        self._committed = True

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Ojo: acá NO se corta por `enabled`. Con el respaldo desactivado
        # no hay nada que restaurar (`_saved` está vacío y `_restore` /
        # `_discard` no hacen nada), pero sí hay que barrer lo que la
        # operación fallida dejó a medio escribir.
        if self._committed and exc_type is None:
            self._discard()
        else:
            self._cleanup_partials()
            self._restore()
        return False  # nunca se traga la excepción

    def _cleanup_partials(self) -> None:
        """Borra lo que la operación fallida haya alcanzado a dejar, para
        que el respaldo pueda volver a su lugar y no quede basura ocupando
        espacio.

        Hay que barrer dos cosas, no una: los nombres finales (`dest`,
        `dest.wbf1`, ...) y los temporales OCULTOS que `wit` usa mientras
        escribe (`.{dest}.{random}.tmp`, `.tmp.1`, ...). `wit_wrapper` ya
        limpia los suyos cuando cancela o cuando vence el timeout, pero si
        `wit` simplemente devuelve un código de error después de haber
        escrito medio archivo, esos temporales no los borraba nadie:
        quedaban huérfanos, invisibles para el usuario (empiezan con
        punto) y ocupando varios GB.

        `cleanup_new_output_files` cubre las dos familias y solo toca lo
        que apareció después de `__enter__`, así que no se lleva por
        delante lo que ya estaba."""
        # Los respaldos van explícitos en el conjunto protegido además de
        # estar en la foto: son lo único que no se puede perder acá, y no
        # depende de que la foto se haya tomado en el orden correcto.
        protegidos = self._outputs_before | {resp for _orig, resp in self._saved}
        wit_wrapper.cleanup_new_output_files(self.dest, protegidos)
        for parcial in wbfs_group(self.dest):
            try:
                parcial.unlink()
            except OSError:
                pass

    def _restore(self) -> None:
        for original, respaldo in reversed(self._saved):
            try:
                os.replace(respaldo, original)
            except OSError:
                pass
        self._saved = []

    def _discard(self) -> None:
        for _original, respaldo in self._saved:
            try:
                respaldo.unlink(missing_ok=True)
            except OSError:
                pass
        self._saved = []


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
        # SIEMPRE por `_copy_with_progress`, aunque no haya progreso ni
        # cancelación que reportar: es el único camino que escribe en un
        # temporal y recién al final lo mueve encima del destino. La rama
        # "atajo" con `shutil.copy2` que había acá abría el destino con
        # "wb", o sea que lo vaciaba en el acto, y un fallo posterior
        # (crash, USB desenchufado, disco lleno) dejaba al usuario sin el
        # respaldo que ya tenía. El callback no-op no cuesta nada; tener
        # dos caminos de escritura, uno protegido y otro no, sí.
        _copy_with_progress(game.path, dest,
                            bytes_progress_cb or (lambda _n: None), cancel)
        return dest

    if not wit_wrapper.is_available(wit_binary):
        raise wit_wrapper.WitNotFoundError(wit_binary)

    # Si había algo en el destino, se lo aparta antes de dejar que `wit`
    # escriba: si la conversión falla o se cancela, vuelve a su lugar.
    with DestinationGuard(dest, enabled=bool(wbfs_group(dest))) as guard:
        result = wit_wrapper.convert(game.path, dest, "WBFS", wit_binary, split=split,
                                      bytes_progress_cb=bytes_progress_cb, cancel=cancel)
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or "Error desconocido al convertir con wit")
        guard.commit()
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
