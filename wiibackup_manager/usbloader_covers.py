"""Carátulas 2D para USB Loader GX, copiadas a la unidad del cliente.

USB Loader GX muestra la carátula plana ("Flat Covers") de cada juego
desde la carpeta `covers2d_path`, como `<ID6>.png`, y las baja del mismo
lugar de GameTDB que esta app (`gametdb.COVER_URL_TEMPLATES`: la imagen
"cover", PNG de 160×224). Copiar las que ya están en la caché de este
equipo le ahorra a la consola bajarlas.

Fuentes (wiidev/usbloadergx, rama "enhanced", la de la release
v4.0-r1283; leídas en el código, no en foros):

- source/settings/CSettings.cpp: `ConfigPath` es
  `<dispositivo>/apps/usbloader_gx/` y, por defecto,
  `covers2d_path = <ConfigPath>images/2D/`. Se puede cambiar en
  GXGlobal.cfg (`ParseLine` corta en el primer "=" y gana la última
  línea). `URL_Covers2D` es https://art.gametdb.com/wii/cover/.
- source/StartUpProcess.cpp: el dispositivo es desde donde se lanzó (el
  Homebrew Channel pasa `usb:/...`, que queda como `usb1:`); sin eso,
  `FindConfig` prueba primero la SD.
- source/GUI/LoadCoverImage.cpp: busca `<covers2d_path><ID6>.png` y, si
  no está, `<ID3>.png`.

Por eso solo se copia si la unidad tiene USB Loader GX
(`apps/usbloader_gx/`) y su GXGlobal.cfg no manda las carátulas a otro
lado. Aun así hay un caso que desde la PC no se ve: si el cliente lo
lanza desde una SD que también lo tiene, lee las carátulas de la SD. No
se probó en una Wii real (ver "Límites conocidos" en el README).

Solo se escribe en esa carpeta: nunca en `wbfs/` ni `games/`, nunca se
pisa una carátula que ya esté, y cada una se escribe con un temporal
`.parcial` y un rename (`atomicfs`), así una copia cortada no deja un PNG
a medias.
"""
from __future__ import annotations

import errno
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import atomicfs, gametdb, ticket_service

# La carpeta de USB Loader GX en la unidad: la que traen su ZIP de GitHub
# y el de Open Shop Channel, y la que busca al arrancar.
APP_DIR = Path("apps") / "usbloader_gx"
COVERS_2D_DIR = APP_DIR / "images" / "2D"
CONFIG_NAME = "GXGlobal.cfg"

# Lo que USB Loader GX guarda en `covers2d_path` cuando arranca desde el
# USB y nadie lo cambió (en minúsculas y sin la barra final, para
# comparar; FAT32 no distingue mayúsculas).
_DEFAULT_COVERS2D = frozenset({
    "usb1:/apps/usbloader_gx/images/2d",
    "usb:/apps/usbloader_gx/images/2d",
})

# Un GXGlobal.cfg real mide unos KB; esto es solo para no leer entero un
# archivo enorme que no es lo que dice ser.
_MAX_CONFIG_BYTES = 1024 * 1024

# El temporal con el que `_copiar_sin_pisar` escribe cada carátula
# (`atomicfs`: `.<ID6>.png.parcial-<azar>`). Si el proceso muere a mitad
# -kill -9, corte de luz- queda ahí, y el Recovery Manager no baja hasta
# images/2D (`recovery_service.MAX_DEPTH`): por eso se limpia al empezar la
# próxima copia. Solo los de esta forma exacta, con un ID6 en mayúsculas
# como los que escribe esta app: nada más de esa carpeta es nuestro.
_PARCIAL_PROPIO = re.compile(
    r"^\.[A-Z0-9]{6}\.png\." + re.escape(atomicfs.MARCA_PARCIAL) + r"-[^/]+$")

# Por qué no se puede copiar (ver `covers_target`).
NO_LOADER = "no-loader"      # la unidad no tiene apps/usbloader_gx
OTHER_PATH = "other-path"    # GXGlobal.cfg manda las carátulas 2D a otro lado


@dataclass(frozen=True)
class CoversTarget:
    """Adónde van las carátulas en una unidad, o por qué no se puede:
    `folder` es None y `problem` dice el motivo. `configured` es el
    `covers2d_path` de GXGlobal.cfg cuando apunta a otro lado."""

    folder: Optional[Path]
    problem: Optional[str] = None
    configured: str = ""


def configured_covers2d_path(config_file: Path) -> Optional[str]:
    """El `covers2d_path` de un GXGlobal.cfg, leído como lo lee USB
    Loader GX (`CSettings::ParseLine` y `TrimLine`: corta en el primer
    "=", saca espacios adelante y espacios y fines de línea atrás, y si
    la clave se repite gana la última). None si el archivo no está o no
    la trae. Levanta OSError si está pero no se puede leer."""
    try:
        with open(config_file, "rb") as f:
            datos = f.read(_MAX_CONFIG_BYTES)
    except FileNotFoundError:
        return None
    valor = None
    for linea in datos.decode("latin-1").splitlines():
        nombre, igual, resto = linea.partition("=")
        if igual and nombre.lstrip(" ").rstrip(" \r\n") == "covers2d_path":
            valor = resto.lstrip(" ").rstrip(" \r\n")
    return valor


def _es_el_de_fabrica(valor: str) -> bool:
    return valor.strip().rstrip("/").lower() in _DEFAULT_COVERS2D


def covers_target(drive_root: Path) -> CoversTarget:
    """Adónde copiar las carátulas 2D en `drive_root` (ver el comentario
    del módulo). Levanta OSError si la unidad no se puede leer."""
    root = Path(drive_root)
    app = root / APP_DIR
    if not os.path.isdir(app):
        return CoversTarget(None, NO_LOADER)
    configurado = configured_covers2d_path(app / CONFIG_NAME)
    if configurado is not None and not _es_el_de_fabrica(configurado):
        return CoversTarget(None, OTHER_PATH, configurado)
    return CoversTarget(root / COVERS_2D_DIR)


@dataclass
class CopyResult:
    """Qué pasó con cada juego de la unidad: copiada, ya estaba (no se
    pisa), sin carátula en la caché de este equipo, o con error. `stopped`
    es que se cortó antes de terminar, y `disconnected`, que fue porque la
    unidad dejó de estar (si no, se pidió cortar)."""

    target: CoversTarget
    copied: list = field(default_factory=list)
    existing: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    failed: list = field(default_factory=list)   # (ID6, motivo)
    stopped: bool = False
    disconnected: bool = False


def _juegos_de_la_unidad(drive_root: Path) -> list:
    """(ID6, consola) de cada juego de la unidad, sin repetir: los mismos
    que lista el Ticket de entrega."""
    juegos, vistos = [], set()
    for entrada in (ticket_service.list_wii_games(drive_root)
                    + ticket_service.list_gamecube_games(drive_root)):
        if entrada.game_id and entrada.game_id not in vistos:
            vistos.add(entrada.game_id)
            juegos.append((entrada.game_id, entrada.console))
    return juegos


def _copiar_sin_pisar(origen: Path, destino: Path) -> None:
    """Copia `origen` a `destino` con un temporal `.parcial` y un rename.
    La última mirada de si `destino` ya existe es justo antes del rename:
    si apareció en el medio, se tira la copia y queda lo que estaba."""
    def _sin_pisar(_temporal):
        if os.path.lexists(destino):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), str(destino))

    with atomicfs.atomic_write_stream(destino, fsync=True,
                                      before_replace=_sin_pisar) as (f, _temporal):
        with open(origen, "rb") as fuente:
            shutil.copyfileobj(fuente, f)


def _crear_carpeta(app: Path, carpeta: Path) -> None:
    """Crea `carpeta` (images/2D) debajo de la carpeta de USB Loader GX,
    pero nunca la carpeta de la app: si ya no está, la unidad se fue, y
    recrearla escribiría en el punto de montaje vacío -en el disco
    interno- en vez de notarlo. Levanta FileNotFoundError en ese caso."""
    actual = app
    for parte in carpeta.relative_to(app).parts:
        actual = actual / parte
        try:
            os.mkdir(actual)
        except FileExistsError:
            pass


def _limpiar_parciales(carpeta: Path) -> None:
    """Borra de `carpeta` los temporales propios que dejó una copia que no
    terminó (ver `_PARCIAL_PROPIO`). Solo archivos comunes; lo que no se
    pueda borrar se deja."""
    try:
        with os.scandir(carpeta) as entradas:
            huerfanos = [e.path for e in entradas
                         if _PARCIAL_PROPIO.match(e.name)
                         and e.is_file(follow_symlinks=False)]
    except OSError:
        return
    for ruta in huerfanos:
        try:
            os.unlink(ruta)
        except OSError:
            pass


def copy_drive_covers(drive_root: Path, region: str,
                      should_stop: Optional[Callable[[], bool]] = None) -> CopyResult:
    """Copia a la carpeta de USB Loader GX de `drive_root` la carátula de
    la caché (en la región `region`) de cada juego de la unidad que
    todavía no la tenga. No baja nada de internet. Si no hay adónde
    copiar (`covers_target`), no escribe nada. Levanta OSError si la
    unidad no se puede leer de entrada; un error con una carátula no
    corta las demás."""
    root = Path(drive_root)
    destino = covers_target(root)
    resultado = CopyResult(target=destino)
    if destino.folder is None:
        return resultado
    _limpiar_parciales(destino.folder)
    app = root / APP_DIR
    for game_id, console in _juegos_de_la_unidad(root):
        if should_stop is not None and should_stop():
            resultado.stopped = True
            break
        # La carpeta de USB Loader GX tiene que seguir ahí: si no, la
        # unidad se desconectó (o se la cambió) a mitad de camino.
        if not os.path.isdir(app):
            resultado.stopped = resultado.disconnected = True
            break
        archivo = destino.folder / f"{game_id}.png"
        if os.path.lexists(archivo):
            resultado.existing.append(game_id)
            continue
        origen = gametdb.cached_cover(game_id, region, console)
        if origen is None:
            resultado.missing.append(game_id)
            continue
        try:
            _crear_carpeta(app, destino.folder)
            _copiar_sin_pisar(origen, archivo)
        except FileExistsError:
            resultado.existing.append(game_id)
        except OSError as e:
            resultado.failed.append((game_id, e.strerror or str(e)))
        else:
            resultado.copied.append(game_id)
    # Si lo último que falló fue porque la unidad se fue, no quedó otra
    # vuelta para notarlo.
    if resultado.failed and not resultado.stopped and not os.path.isdir(app):
        resultado.stopped = resultado.disconnected = True
    return resultado
