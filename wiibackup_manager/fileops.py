"""Mover y copiar archivos sin pisar lo que no es nuestro.

Primitivas puras de filesystem: no saben qué es un `Game`, solo reciben
rutas. Son la capa de abajo de `library_ops` (que sí razona en términos de
juegos) y la contraparte "con progreso y sin pisar" de `atomicfs`, que
aporta el intercambio atómico que usan por dentro.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

from . import atomicfs, wit_wrapper


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
    exactamente como estaba.

    El temporal, el intercambio y la limpieza ante cualquier fallo son
    `atomicfs.atomic_write_stream`, la misma primitiva que usa el resto de
    la app para no dejar nunca un destino a medio escribir. Se usa la
    variante de "archivo abierto" y no la de "ruta" porque acá se escribe
    de a bloques: el descriptor que devolvió `mkstemp` se usa tal cual, sin
    reabrir la ruta.

    Lo que esta función agrega por encima de la primitiva es lo suyo: el
    progreso, la cancelación entre bloques, el `fsync` antes del
    intercambio (una copia de varios GB a un USB sí necesita durabilidad,
    a diferencia de una carátula que se puede volver a bajar) y el
    `copystat`, que es lo que la hace equivalente a `shutil.copy2` -y lo
    que fija los permisos del resultado, copiados del origen."""
    written = 0
    last_report = time.monotonic()
    with atomicfs.atomic_write_stream(
            dest, fsync=True,
            before_replace=lambda tmp: shutil.copystat(src, tmp)) as (fdst, _tmp):
        with open(src, "rb") as fsrc:
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
