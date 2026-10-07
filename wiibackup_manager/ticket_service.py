"""Ticket de Entrega: qué lleva adentro una unidad ya preparada.

Para qué existe
---------------
Cuando el taller entrega una Wii con su USB/SD, el cliente se va con un
pendrive del que no puede ver el contenido por su cuenta: no tiene una PC
a mano, y aunque la tuviera, "wbfs/RMCP01/RMCP01.wbfs" no le dice nada.
El ticket es el resumen legible de esa entrega -cuántos juegos, cuánto
espacio, en qué formato quedó la unidad- para mandárselo por WhatsApp.

Qué hace este módulo y qué no
-----------------------------
Acá se REÚNEN los datos y nada más: contar lo que hay en la unidad, medir
el espacio y averiguar el filesystem. No arma el PDF (eso es
`pdf_export`, que decide cómo se ve) ni sabe nada de ventanas, diálogos ni
toasts (eso es la interfaz). Esa separación es la que permite testear el
conteo contra unidades de prueba armadas a mano, sin abrir una ventana ni
generar un PDF.

Solo LEE
--------
El ticket no escribe, no mueve, no borra y no toca ninguna de las
primitivas de storage (`atomicfs`, `DestinationGuard`, el instalador de
Homebrew). Es un lector, y por eso puede correr sobre una unidad que el
usuario está por desconectar sin ningún riesgo.

Cómo se cuenta
--------------
Por la ESTRUCTURA de carpetas que la app ya construye al copiar
(`transfer_plan.wbfs_dest_path`, `transfer_plan.gc_dest_path`,
`oscwii_installer`), y no abriendo cada archivo con `wit` como hace
`scanning.scan_library`. Son dos trabajos distintos: el escaneo identifica
juego por juego -título, ID, formato- y para eso paga el precio de leer
headers; el ticket solo necesita CUÁLES hay (ID y un título legible), y
hacerlo por estructura es inmediato, no depende de que `wit` esté
instalado, y funciona sobre una unidad llena de juegos grandes sin leer un
solo byte de contenido. El ID sale de los nombres que arma la app y el
título de la caché de GameTDB (ver `collect_ticket_data`).
"""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import drives
from .disc_header import is_valid_game_id
from .game_model import VALID_EXTENSIONS, sanitize_filename

# Las tres carpetas de primer nivel que la app crea en una unidad
# preparada. Los nombres son los que esperan los programas de la Wii, no
# una elección nuestra: "wbfs/" y "games/" los buscan los USB Loaders y
# Nintendont, y "apps/" el Homebrew Channel.
WII_DIR = "wbfs"
GAMECUBE_DIR = "games"
HOMEBREW_DIR = "apps"

# Un homebrew es una carpeta con un ejecutable que el Homebrew Channel
# pueda arrancar. Sin esto, cualquier carpeta suelta dentro de "apps/"
# -restos de una app borrada a medias, una carpeta que creó el usuario-
# contaría como una app instalada.
_HOMEBREW_ENTRYPOINTS = ("boot.dol", "boot.elf")

# Nombre del filesystem tal como lo reporta `findmnt` -> como lo escribe
# la gente. El ticket lo lee un cliente, no un técnico: "vfat" no le dice
# nada y "FAT32" sí. Lo que no está en esta tabla se muestra tal cual
# (en mayúsculas), que es mejor que esconderlo detrás de un "otro".
_FILESYSTEM_LABELS = {
    "vfat": "FAT32",
    "fat": "FAT32",
    "fat12": "FAT32",
    "fat16": "FAT32",
    "fat32": "FAT32",
    "msdos": "FAT32",
    "exfat": "exFAT",
    "ntfs": "NTFS",
    "ntfs3": "NTFS",
    "ext4": "ext4",
    "ext3": "ext3",
    "ext2": "ext2",
}

# Lo que no se cuenta: los temporales y respaldos de la app empiezan con
# punto (ver `atomicfs.hidden_sibling`), igual que la basura que dejan
# macOS y Windows en un pendrive ("._Juego.wbfs", ".Spotlight-V100").
# Si un respaldo de `DestinationGuard` quedó huérfano en la unidad, es un
# archivo oculto que NO es un juego entregado y no tiene que inflar el
# número que ve el cliente.
def _es_visible(nombre: str) -> bool:
    return not nombre.startswith(".")


def _entradas(directorio: Path) -> list:
    """Contenido de `directorio` como lista de `os.DirEntry`, o vacío si
    no se puede leer.

    Que una carpeta ilegible devuelva 0 y no una excepción es a propósito:
    el ticket es un resumen informativo y vale mucho más entregarlo con un
    número incompleto que no poder generarlo porque la unidad tiene una
    carpeta con permisos raros."""
    try:
        with os.scandir(directorio) as it:
            return [e for e in it if _es_visible(e.name)]
    except OSError:
        return []


def _es_archivo_de_juego(entry) -> bool:
    try:
        return (entry.is_file()
                and Path(entry.name).suffix.lower() in VALID_EXTENSIONS)
    except OSError:
        return False


# Consolas de la lista de juegos. Son las mismas claves que usa
# `game_model.Game.console`, para que el día que el ticket necesite cruzar
# datos con la biblioteca no haya que traducir de un vocabulario a otro.
CONSOLE_WII = "wii"
CONSOLE_GAMECUBE = "gc"

# "Título del juego [ID6]": así nombra las carpetas `transfer_plan.gc_dest_path`
# y así las nombran también USB Loader GX y Nintendont en las unidades que
# vienen de otras herramientas.
_ID_EN_CARPETA = re.compile(r"\s*\[([A-Za-z0-9]{6})\]\s*$")

# Nintendont arranca un juego por el `game.iso` (o `game.ciso`) de su
# carpeta. Una carpeta con solo `disc2.iso` es una entrega a medias -falta
# el disco que la consola busca primero- y no se lista como juego.
_GAMECUBE_PRINCIPALES = ("game.iso", "game.ciso")

TitleLookup = Callable[[str], Optional[str]]


@dataclass(frozen=True)
class GameEntry:
    """Un juego de la lista del ticket: consola, ID6 (vacío si no se pudo
    averiguar) y el título que se imprime."""

    console: str
    game_id: str
    title: str


def _sin_id(nombre: str) -> str:
    """`nombre` sin el sufijo " [ID6]", que en el ticket va en su propia
    columna y repetirlo al lado del título sería ruido."""
    return _ID_EN_CARPETA.sub("", nombre).strip()


def _id_de_carpeta(nombre: str) -> str:
    m = _ID_EN_CARPETA.search(nombre)
    return m.group(1).upper() if m else ""


def _orden_alfabetico(entrada: GameEntry):
    """Clave de orden sin mayúsculas ni acentos: "Épica" va con la E y no
    después de la Z, que es donde la deja un orden por código Unicode."""
    base = unicodedata.normalize("NFKD", entrada.title)
    base = "".join(ch for ch in base if not unicodedata.combining(ch))
    return (base.casefold(), entrada.game_id)


def _titulo(game_id: str, respaldo: str, title_lookup: TitleLookup) -> str:
    """El título de GameTDB si lo hay; si no, el nombre de la carpeta. Un
    fallo de la búsqueda no rompe el ticket: el respaldo siempre está."""
    if game_id:
        try:
            titulo = title_lookup(game_id)
        except Exception:  # noqa: BLE001 - el título es un adorno
            titulo = None
        if titulo:
            return titulo
    return respaldo or game_id


def _sin_titulos(_game_id: str) -> Optional[str]:
    return None


def list_wii_games(drive_root: Path,
                   title_lookup: TitleLookup = _sin_titulos) -> list:
    """Juegos de Wii en `wbfs/`, ordenados por título.

    Se listan ARCHIVOS de juego y no carpetas, porque las dos
    disposiciones que se ven en la práctica tienen que dar lo mismo: la
    que arma esta app y esperan los USB Loaders
    (`wbfs/<ID6>/<ID6>.wbfs`, ver `transfer_plan.wbfs_dest_path`) y la plana
    (`wbfs/<ID6>.wbfs`) que dejan otras herramientas. Contando archivos,
    las dos dan 1 por juego.

    Un WBFS dividido en partes (`.wbfs` + `.wbf1` + `.wbf2`, ver
    `transfer_plan.wbfs_group`) cuenta UNA vez sin necesidad de un caso
    especial: las partes tienen extensión `.wbf1`, `.wbf2`... que no está
    en `VALID_EXTENSIONS`, así que solo entra el `.wbfs` que las
    encabeza.

    El ID sale del nombre del archivo (así lo nombra la app y así lo
    buscan los USB Loaders) y, si ese nombre no es un ID, del "[ID6]" de
    la carpeta."""
    raiz = Path(drive_root) / WII_DIR
    juegos = []

    def agregar(archivo, carpeta: str = ""):
        stem = Path(archivo.name).stem
        game_id = stem.upper() if is_valid_game_id(stem) else _id_de_carpeta(carpeta)
        respaldo = _sin_id(carpeta) if carpeta else _sin_id(stem)
        juegos.append(GameEntry(CONSOLE_WII, game_id,
                                _titulo(game_id, respaldo, title_lookup)))

    for entry in _entradas(raiz):
        if _es_archivo_de_juego(entry):
            agregar(entry)
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        for sub in _entradas(Path(entry.path)):
            if _es_archivo_de_juego(sub):
                agregar(sub, entry.name)
    return sorted(juegos, key=_orden_alfabetico)


def _es_principal_gamecube(entry) -> bool:
    try:
        return entry.is_file() and entry.name.lower() in _GAMECUBE_PRINCIPALES
    except OSError:
        return False


def list_gamecube_games(drive_root: Path,
                        title_lookup: TitleLookup = _sin_titulos) -> list:
    """Juegos de GameCube en `games/`, ordenados por título.

    Acá se listan CARPETAS y no archivos, al revés que en Wii, porque esa
    es la estructura de Nintendont: `games/<Título [ID6]>/game.iso`, y un
    juego de dos discos son dos archivos (`game.iso` y `disc2.iso`)
    adentro de LA MISMA carpeta (ver `transfer_plan.gc_dest_path`). Contando
    archivos, un juego multidisco se entregaría como si fueran dos juegos
    distintos.

    Se pide que la carpeta tenga un `game.iso` o `game.ciso` TERMINADO:
    una carpeta vacía es un resto de algo borrado, y una copia a medio
    escribir es un `.game.iso.parcial-*` oculto (ver
    `atomicfs.MARCA_PARCIAL`) que `_entradas` ya deja afuera, igual que
    los respaldos de `DestinationGuard`."""
    raiz = Path(drive_root) / GAMECUBE_DIR
    juegos = []
    for entry in _entradas(raiz):
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        if not any(_es_principal_gamecube(sub)
                   for sub in _entradas(Path(entry.path))):
            continue
        game_id = _id_de_carpeta(entry.name)
        juegos.append(GameEntry(CONSOLE_GAMECUBE, game_id,
                                _titulo(game_id, _sin_id(entry.name),
                                        title_lookup)))
    return sorted(juegos, key=_orden_alfabetico)


def count_wii_games(drive_root: Path) -> int:
    """Cuántos juegos de Wii hay: los mismos que lista `list_wii_games`,
    para que el número del ticket y la lista impresa nunca se
    contradigan."""
    return len(list_wii_games(drive_root))


def count_gamecube_games(drive_root: Path) -> int:
    """Cuántos juegos de GameCube hay, con el mismo criterio que
    `list_gamecube_games`."""
    return len(list_gamecube_games(drive_root))


def count_homebrew_apps(drive_root: Path) -> int:
    """Apps de Homebrew instaladas en `apps/`.

    Una app es una carpeta con un ejecutable que el Homebrew Channel pueda
    arrancar (`boot.dol` o `boot.elf`), que es la misma condición que usa
    la consola para mostrarla en su menú. Se compara sin distinguir
    mayúsculas porque en FAT/exFAT -el formato habitual de estas unidades-
    "BOOT.DOL" y "boot.dol" son el mismo archivo, y hay ZIPs de homebrew
    que lo traen en mayúsculas."""
    raiz = Path(drive_root) / HOMEBREW_DIR
    total = 0
    for entry in _entradas(raiz):
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        nombres = {sub.name.lower() for sub in _entradas(Path(entry.path))}
        if nombres.intersection(_HOMEBREW_ENTRYPOINTS):
            total += 1
    return total


@dataclass(frozen=True)
class DriveContents:
    """Cuántas cosas de cada tipo lleva la unidad."""

    wii_games: int = 0
    gamecube_games: int = 0
    homebrew_apps: int = 0

    @property
    def total_games(self) -> int:
        return self.wii_games + self.gamecube_games


def collect_contents(drive_root: Path) -> DriveContents:
    """Cuenta las tres cosas de una sola pasada por la unidad."""
    drive_root = Path(drive_root)
    return DriveContents(
        wii_games=count_wii_games(drive_root),
        gamecube_games=count_gamecube_games(drive_root),
        homebrew_apps=count_homebrew_apps(drive_root),
    )


@dataclass(frozen=True)
class ConsoleInfo:
    """Lo que el técnico anota de la consola que se entrega. Todo
    opcional: un campo vacío no se imprime (ver `pdf_export`)."""

    model: str = ""
    serial: str = ""
    system_version: str = ""
    service: str = ""

    def stripped(self) -> "ConsoleInfo":
        return ConsoleInfo(self.model.strip(), self.serial.strip(),
                           self.system_version.strip(), self.service.strip())

    def is_empty(self) -> bool:
        return not any((self.model, self.serial, self.system_version,
                        self.service))


def filesystem_label(fstype: Optional[str]) -> str:
    """Nombre presentable del filesystem. `None` -es lo que devuelve
    `drives.filesystem_of` cuando no pudo determinarlo con confianza- se
    muestra como "Desconocido" y no se disimula: es más honesto que
    afirmar un formato que no se pudo verificar."""
    if not fstype:
        return "Desconocido"
    return _FILESYSTEM_LABELS.get(fstype.lower(), fstype.upper())


@dataclass(frozen=True)
class TicketData:
    """Todo lo que va impreso en el ticket, ya resuelto.

    Es el contrato entre este módulo y `pdf_export`: acá están los datos,
    allá la decisión de cómo se ven. Que sea `frozen` no es decorativo -es
    una foto de un momento, y el PDF que se genere después tiene que
    describir esa foto y no una unidad que mientras tanto cambió."""

    client_name: str
    notes: str
    generated_at: datetime
    drive_label: str
    drive_path: Path
    total_bytes: int
    used_bytes: int
    free_bytes: int
    filesystem: str
    contents: DriveContents
    console: ConsoleInfo = ConsoleInfo()
    # Wii primero y GameCube después, cada grupo en orden alfabético. Tupla
    # por lo mismo que `frozen`: es una foto y no se edita.
    games: tuple = field(default_factory=tuple)

    @property
    def used_ratio(self) -> Optional[float]:
        """Fracción ocupada, o None si no se pudo medir la capacidad."""
        if not self.total_bytes:
            return None
        return self.used_bytes / self.total_bytes


def collect_ticket_data(
    drive_root: Path,
    *,
    client_name: str = "",
    notes: str = "",
    console: Optional[ConsoleInfo] = None,
    now: Optional[datetime] = None,
    usage: Callable = shutil.disk_usage,
    filesystem: Callable = drives.filesystem_of,
    title_lookup: Optional[TitleLookup] = None,
) -> TicketData:
    """Reúne todo lo del ticket para la unidad montada en `drive_root`.

    `client_name` y `notes` los escribe el usuario antes de generar y
    pueden venir vacíos: el ticket se genera igual, simplemente sin esos
    campos (ver `pdf_export`). Se les saca el espacio de los bordes acá y
    no en la interfaz, para que el servicio se comporte igual lo llame
    quien lo llame.

    `now` es la fecha del ticket -"la del momento de generación"- y se
    puede pasar para que las pruebas no dependan del reloj. `usage` y
    `filesystem` se inyectan por el mismo motivo: son las dos únicas
    consultas al sistema que hace esta función, y poder reemplazarlas
    permite probar el cálculo de capacidad sin necesitar un pendrive de
    verdad montado.

    Si no se puede leer el espacio (la unidad se desconectó entre que el
    usuario apretó el botón y esto corrió), los tres tamaños quedan en 0
    en vez de fallar: el resto del ticket -que es lo que le importa al
    cliente- sigue siendo válido.

    `title_lookup(game_id)` da el título de cada juego de la lista; por
    defecto es `gametdb.cached_title`, que mira solo la copia de GameTDB
    que ya está en la caché y nunca descarga. Sin título, queda el nombre
    de la carpeta."""
    drive_root = Path(drive_root)
    if title_lookup is None:
        from . import gametdb
        title_lookup = gametdb.cached_title
    try:
        medida = usage(drive_root)
        total_bytes, free_bytes = medida.total, medida.free
    except OSError:
        total_bytes = free_bytes = 0

    # "Usado" se calcula como total - libre, y no con el `used` que
    # devuelve el sistema, por la misma razón que lo hace
    # `transfer_view._update_dest_space`: en ext4 y familia hay bloques
    # reservados para root que están en `total` pero no en `used` ni en
    # `free`, y lo que le importa a quien lee el ticket es cuánto del
    # pendrive NO puede usar.
    used_bytes = max(total_bytes - free_bytes, 0)

    wii = list_wii_games(drive_root, title_lookup)
    gamecube = list_gamecube_games(drive_root, title_lookup)

    return TicketData(
        client_name=client_name.strip(),
        notes=notes.strip(),
        generated_at=now or datetime.now(),
        drive_label=drive_root.name or str(drive_root),
        drive_path=drive_root,
        total_bytes=total_bytes,
        used_bytes=used_bytes,
        free_bytes=free_bytes,
        filesystem=filesystem_label(filesystem(drive_root)),
        contents=DriveContents(
            wii_games=len(wii),
            gamecube_games=len(gamecube),
            homebrew_apps=count_homebrew_apps(drive_root),
        ),
        console=(console or ConsoleInfo()).stripped(),
        games=tuple(wii + gamecube),
    )


def suggested_filename(client_name: str = "",
                       when: Optional[datetime] = None) -> str:
    """Nombre de archivo propuesto para el PDF: lleva el cliente (si lo
    hay) y la fecha, para que una carpeta con los tickets de varias
    entregas se pueda ordenar y buscar sin abrirlos uno por uno.

    Toma el nombre y la fecha sueltos, y no un `TicketData`, porque la
    interfaz lo necesita ANTES de haber leído la unidad: el selector de
    "guardar como" propone el nombre en el momento en que se abre, y
    recorrer el pendrive para eso sería trabajo al pedo."""
    fecha = (when or datetime.now()).strftime("%Y-%m-%d")
    nombre = client_name.strip()
    if nombre:
        return sanitize_filename(f"Ticket {nombre} {fecha}") + ".pdf"
    return f"Ticket {fecha}.pdf"
