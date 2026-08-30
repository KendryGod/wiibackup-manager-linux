"""Todo lo que se puede saber de una transferencia ANTES de tocar el disco.

Dónde va a caer cada juego en la unidad de destino, cuánto va a ocupar
ahí, y cuánto lugar hay. Es deliberadamente de solo lectura: nada de este
módulo escribe. La transferencia de verdad la hace `library_ops`, que usa
estas cuentas para decidir.
"""
from __future__ import annotations

import concurrent.futures
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import wit_wrapper
from .disc_header import validate_game_id
from .game_model import Game, sanitize_filename


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


def gc_dest_path(game: Game, drive_root: Path) -> Path:
    """Ruta final que va a ocupar `game` dentro de `drive_root`, en la
    estructura que espera Nintendont para juegos de GameCube:

        games/Título del juego [ID6]/game.ext

    A diferencia de un WBFS de Wii, acá Nintendont identifica el juego por
    la CARPETA, no por el nombre del archivo, y la extensión se conserva
    tal cual (.iso o .ciso): Nintendont lee esos dos formatos directo, sin
    pasar por `wit` (ver `send_to_wbfs_drive`).

    Soporte multidisco: si `game.disc_number` dice que es el segundo disco
    (o el tercero, ...) el archivo se llama 'discN.ext' en vez de
    'game.ext', DENTRO DE LA MISMA CARPETA que el disco 1 -misma carpeta
    porque Nintendont los busca ahí, no en carpetas separadas-, siempre que
    los dos discos se hayan importado con el mismo título (si el título
    difiere -typo, otra fuente- van a parar a carpetas distintas y
    Nintendont no los va a poder ver como un solo juego; eso lo tiene que
    resolver quien importa, no esta función)."""
    game_id = validate_game_id(game.game_id)
    folder = sanitize_filename(f"{game.title} [{game_id}]")
    ext = game.path.suffix.lower()
    filename = f"disc{game.disc_number + 1}{ext}" if game.disc_number else f"game{ext}"
    return Path(drive_root) / "games" / folder / filename


def game_dest_path(game: Game, drive_root: Path) -> Path:
    """Ruta final de `game` dentro de `drive_root`, delegando en la
    estructura que corresponda según la consola: `wbfs_dest_path` para Wii,
    `gc_dest_path` para GameCube. Es el único punto que hace falta tocar
    para saber "dónde va a parar esto", tanto para copiar de verdad como
    para chequear de antemano si el destino ya existe."""
    if game.console == "gc":
        return gc_dest_path(game, drive_root)
    return wbfs_dest_path(game, drive_root)


def wbfs_dest_paths(games, drive_root: Path) -> list:
    """Las rutas que van a ocupar `games` dentro de `drive_root`, sea cual
    sea su consola (ver `game_dest_path`): a pesar del nombre -que se
    mantiene por compatibilidad con quien ya la llama- no es solo para Wii.

    Se saltean los juegos cuyo Game ID no sea válido: para esos no hay
    ruta que calcular (los rechaza `game_dest_path`) y la transferencia
    los va a reportar como error igual. Se usa para declararle al
    OperationManager qué archivos va a escribir la transferencia."""
    destinos = []
    for game in games:
        try:
            destinos.append(game_dest_path(game, drive_root))
        except ValueError:
            continue
    return destinos


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


def free_space(path: Path) -> Optional[int]:
    """Bytes libres en el filesystem de `path`, o None si no se puede
    saber (unidad desconectada a mitad de camino, por ejemplo)."""
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


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
    mínimo honesto que se puede afirmar sin abrir el archivo.

    Un juego de GameCube nunca se convierte -Nintendont lee ISO y CISO tal
    cual, ver `send_to_wbfs_drive`- así que lo que va a ocupar en el
    destino es exactamente lo que pesa el archivo de origen, ni más ni
    menos: no hace falta (ni corresponde) preguntarle a `wit` ni aplicar
    el margen de `_WBFS_OVERHEAD`, que es un ajuste de la conversión a
    WBFS."""
    if game.console == "gc":
        return game.size_bytes
    real = wit_wrapper.iso_size_bytes(game.path, wit_binary)
    if real:
        return int(real * _WBFS_OVERHEAD)
    if game.fmt.upper() in _COMPACT_FORMATS:
        return max(game.size_bytes, _WII_SINGLE_LAYER_BYTES)
    return game.size_bytes


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
    hilo de GTK congela la ventana entera.

    Para un lote grande conviene `plan_transfer_fast`, que hace exactamente
    lo mismo pero preguntándole a varios `wit` a la vez. Esta versión
    secuencial se mantiene porque para uno o dos juegos no hay nada que
    ganar armando un pool de hilos."""
    return [
        TransferItem(game=game, source_bytes=game.size_bytes,
                     output_bytes=estimate_transfer_size(game, wit_binary))
        for game in games
    ]


# Cuántos `wit` se lanzan a la vez para medir tamaños. Cada uno lee el
# header del archivo y sale (milisegundos de CPU, casi todo espera de I/O),
# así que el cuello de botella real es el disco, no el procesador: cuatro
# alcanzan para tapar la latencia de un USB lento sin llenar la unidad de
# lecturas que compiten entre sí. Es medir, no copiar; la copia sigue
# siendo estrictamente de a uno (ver queue_manager.TransferQueue).
DEFAULT_PLAN_WORKERS = 4


def plan_transfer_fast(games, wit_binary: str = "wit",
                       max_workers: int = DEFAULT_PLAN_WORKERS) -> list:
    """Igual que `plan_transfer` pero midiendo los juegos en paralelo.

    Planificar un lote de 200 juegos son 200 invocaciones de `wit` una
    detrás de otra: cada una es barata, pero encadenadas se sienten, y
    mientras tanto la interfaz solo puede decir "calculando…". Acá las
    consultas van a un `ThreadPoolExecutor` chico, que es exactamente el
    caso para el que sirve un pool de hilos en Python: el trabajo pesado
    lo hace un subproceso y el hilo se pasa la vida esperando I/O, así que
    el GIL no lo estorba.

    Dos garantías que importan más que la velocidad:

    - **El orden se respeta.** La lista devuelta sigue el orden de
      `games`, no el orden en que fueron terminando los hilos. Ese orden
      es el que después ve el usuario en la cola de transferencia, y que
      cambiara según qué archivo respondiera primero sería desconcertante.
    - **Un `wit` que falla no voltea la planificación.** Si la consulta de
      un juego revienta (archivo dañado, unidad que se desconectó, `wit`
      que no está), ese juego cae a `game.size_bytes` como respaldo y el
      resto del lote sigue su curso. Es la misma cota que usa el camino
      secuencial cuando `wit` no contesta.

    Sigue siendo una función bloqueante: se la llama desde un hilo de
    fondo, nunca desde el hilo de GTK."""
    games = list(games)
    if not games:
        return []

    # Ni más hilos que juegos (un pool de 4 para 1 juego es solo overhead)
    # ni menos de uno (`max_workers=0` es un ValueError de la stdlib, y
    # llegar a cero por un cálculo de otra capa no debería romper el plan).
    workers = max(1, min(int(max_workers), len(games)))

    # Se indexa por posición y no se usa `executor.map`: map propaga la
    # primera excepción y aborta el resto, y acá justamente queremos que un
    # juego problemático no arrastre a los demás.
    sizes: list[Optional[int]] = [None] * len(games)
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="plan-transfer") as pool:
        futures = {
            pool.submit(estimate_transfer_size, game, wit_binary): index
            for index, game in enumerate(games)
        }
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                sizes[index] = future.result()
            except Exception:
                # A propósito sin distinguir el tipo: cualquier cosa que
                # salga mal midiendo un juego se resuelve igual, con el
                # respaldo de abajo. El error real, si lo hay, va a volver
                # a aparecer -y con contexto- cuando se intente copiarlo.
                sizes[index] = None

    return [
        TransferItem(game=game, source_bytes=game.size_bytes,
                     output_bytes=sizes[index] or game.size_bytes)
        for index, game in enumerate(games)
    ]
