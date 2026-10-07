"""Utilidades de filesystem compartidas por varios módulos.

Acá vive lo que más de un módulo necesitaba y terminaba reescribiendo por
su cuenta: medir lo que ocupa una ruta, la búsqueda de los datos
instalados de la app, y la firma binaria de PNG. Nada de esto es lógica de
negocio de ninguna parte en particular -por eso no vive en `scanning`,
`gametdb` ni `oscwii_client`- y tener una sola copia de cada cosa
significa que el manejo de errores se lee, se revisa y se arregla en un
solo lugar en vez de en cuatro.

La escritura atómica (`atomic_target`, que vivía acá) se mudó a
`atomicfs`, junto con las demás primitivas de "dejar algo en su lugar sin
pasar por un estado a medias": son una sola familia y se leen mejor
juntas.
"""
from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

# Los 8 primeros bytes de todo PNG. La usan `gametdb` (carátulas de
# GameTDB) y `oscwii_client` (íconos de Open Shop Channel) para descartar
# una respuesta que no es una imagen -el HTML de una página de error, por
# ejemplo- antes de guardarla en la caché. Las dos la re-exportan con este
# mismo nombre, así que `gametdb.PNG_MAGIC` y `oscwii_client.PNG_MAGIC`
# siguen existiendo y apuntan a este único valor.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def path_size(path: Path) -> int:
    """Bytes que ocupa `path`: el tamaño del archivo, o la suma del árbol
    entero si es una carpeta.

    Devuelve 0 si no se puede averiguar (permisos, se lo llevaron en el
    medio, la unidad se desconectó). Es a propósito: esto se usa para
    AVISARLE al usuario cuánto espacio le quedó ocupado un respaldo que no
    se pudo borrar, así que no poder medirlo no puede hacer fallar nada
    -el aviso importa más que el número."""
    try:
        if path.is_dir():
            total = 0
            for raiz, _dirs, archivos in os.walk(path):
                for nombre in archivos:
                    try:
                        total += os.lstat(os.path.join(raiz, nombre)).st_size
                    except OSError:
                        pass
            return total
        return path.stat().st_size
    except OSError:
        return 0


# -------------------------------------------- Datos instalados de la app --
def installed_data_dirs(repo_relative: str, share_relative: str) -> list:
    """Directorios donde puede estar un dato instalado de la app, del más
    específico al más general y sin repetidos.

    La app puede correr desde el repo clonado sin instalar, desde
    `pip install --user`, desde un venv, o desde una instalación de
    sistema, y en cada caso los datos terminan en un lugar distinto. En
    vez de asumir uno, se devuelven todos los candidatos en orden y quien
    llama se queda con el primero que exista de verdad. Ver `pyproject.toml`
    (`[tool.setuptools.data-files]`) para dónde queda cada uno al instalar.

    `repo_relative` es la ruta dentro del repo clonado (p. ej.
    "data/locale"); `share_relative` es la ruta bajo el `share/` de
    cualquier prefijo de instalación (p. ej. "locale").

    El prefijo deducido de dónde quedó instalado el paquete va PRIMERO
    entre los instalados, y es el único que distingue los tres casos:
    site-packages de ~/.local (pip --user), de un venv, o del sistema.
    `sys.prefix` no alcanza solo para esto: con `pip install --user` sigue
    siendo /usr, así que un dato viejo del sistema le ganaría al recién
    instalado."""
    paquete = Path(__file__).resolve().parent
    candidatos = [
        # Repo clonado sin instalar.
        paquete.parent / repo_relative,
    ]
    for padre in paquete.parents:
        if padre.name in ("site-packages", "dist-packages"):
            # …/<prefix>/lib/pythonX.Y/site-packages → <prefix>
            candidatos.append(padre.parent.parent.parent / "share" / share_relative)
            break
    candidatos += [
        Path(sys.prefix) / "share" / share_relative,
        Path.home() / ".local" / "share" / share_relative,
        Path("/usr/local/share") / share_relative,
        Path("/usr/share") / share_relative,
    ]
    vistos = []
    for c in candidatos:
        if c not in vistos:
            vistos.append(c)
    return vistos


# ---------------------------------------------------- Bajar a la unidad --
# `sync_file_range(2)`: pedirle al kernel que escriba (y opcionalmente
# esperar) un RANGO de un archivo, no el archivo entero. Python no lo trae,
# así que va por ctypes; las constantes son las de <fcntl.h> en Linux.
SYNC_FILE_RANGE_WAIT_BEFORE = 1
SYNC_FILE_RANGE_WRITE = 2
SYNC_FILE_RANGE_WAIT_AFTER = 4
_SYNC_AND_WAIT = (SYNC_FILE_RANGE_WAIT_BEFORE | SYNC_FILE_RANGE_WRITE
                  | SYNC_FILE_RANGE_WAIT_AFTER)

_sync_file_range = None
_sync_file_range_buscado = False


def _libc_sync_file_range():
    global _sync_file_range, _sync_file_range_buscado
    if not _sync_file_range_buscado:
        _sync_file_range_buscado = True
        try:
            import ctypes
            libc = ctypes.CDLL(None, use_errno=True)
            funcion = libc.sync_file_range
            funcion.argtypes = (ctypes.c_int, ctypes.c_int64, ctypes.c_int64,
                                ctypes.c_uint)
            funcion.restype = ctypes.c_int
            _sync_file_range = funcion
        except (OSError, AttributeError):
            _sync_file_range = None
    return _sync_file_range


def sync_range(fd: int, offset: int, nbytes: int, flags: int) -> None:
    """`sync_file_range(fd, offset, nbytes, flags)`; `nbytes=0` es "hasta
    el final del archivo".

    Si el sistema no lo tiene (no es Linux) o el filesystem no lo acepta,
    cae a `fdatasync` cuando se pidió ESPERAR -que baja el archivo entero,
    o sea más de lo pedido, pero nunca menos- y a nada cuando solo se pidió
    arrancar la escritura, que es un pedido de "empezá cuando puedas" sin
    garantía que perder.

    Ojo: `sync_file_range` no informa de forma confiable los errores de la
    unidad. Lo que importa ("¿quedó guardado?") lo sigue diciendo el
    `fsync` final de quien escribe; esto es para MEDIR y ACOTAR cuánto
    queda sin bajar, no para dar nada por guardado."""
    funcion = _libc_sync_file_range()
    if funcion is not None:
        if funcion(fd, offset, nbytes, flags) == 0:
            return
        import ctypes
        error = ctypes.get_errno()
        if error not in (errno.ENOSYS, errno.EINVAL, errno.ESPIPE,
                         errno.EOPNOTSUPP):
            raise OSError(error, os.strerror(error))
    if flags & (SYNC_FILE_RANGE_WAIT_BEFORE | SYNC_FILE_RANGE_WAIT_AFTER):
        os.fdatasync(fd)


# Cuánto se deja escribir antes de esperar a que la unidad lo tenga. Es el
# tope de lo que puede quedar en la caché sin bajar, y por eso también el
# tope de lo que tarda una cancelación: a ~14.7 MB/s (un pendrive lento
# real), 8 MiB son ~0.6 s. Con más, cancelar tarda más; con mucho menos, en
# un disco rápido se notaría el costo de tantas esperas chicas.
WRITEBACK_WINDOW = 8 * 1024 * 1024


class BoundedWriteback:
    """Hace que lo escrito en `fd` no se acumule en la caché del kernel.

    Sin esto, con RAM de sobra un archivo de 1 GB "se escribe" en un
    segundo (queda entero en la caché) y lo que tarda de verdad -bajarlo a
    un pendrive lento, más de un minuto- pasa adentro de un único `fsync`
    final que no se puede interrumpir: cancelar no tenía dónde surtir
    efecto, y la barra decía 70% a 700 MB/s.

    Con esto, cada `WRITEBACK_WINDOW` bytes se arranca a bajar esa ventana
    y se espera a que termine la ANTERIOR: la unidad siempre tiene trabajo
    encolado (no se pierde velocidad) y nunca hay más de dos ventanas sin
    bajar. Cada espera es corta, y entre una y otra quien escribe puede
    mirar si lo cancelaron. Lo ya bajado se saca de la caché
    (`POSIX_FADV_DONTNEED`), que además evita llenar la RAM con un archivo
    que nadie va a volver a leer de ahí.

    `confirmed` es cuántos bytes, desde el principio del archivo, ya se
    esperó que estén en la unidad."""

    def __init__(self, fd: int, window: int = WRITEBACK_WINDOW):
        self.fd = fd
        self.window = window
        self.started = 0      # hasta acá ya se pidió escribir
        self.confirmed = 0    # hasta acá ya se esperó

    def advance(self, written: int) -> None:
        """Avisar que el archivo ya tiene `written` bytes escritos."""
        while written - self.started >= self.window:
            inicio = self.started
            sync_range(self.fd, inicio, self.window, SYNC_FILE_RANGE_WRITE)
            self.started += self.window
            if inicio > self.confirmed:
                self._wait_until(inicio)

    def finish(self) -> None:
        """Esperar a que TODO lo escrito esté en la unidad."""
        sync_range(self.fd, self.confirmed, 0, _SYNC_AND_WAIT)
        self._drop(self.confirmed, 0)
        self.started = self.confirmed = os.fstat(self.fd).st_size

    def _wait_until(self, hasta: int) -> None:
        sync_range(self.fd, self.confirmed, hasta - self.confirmed,
                   _SYNC_AND_WAIT)
        self._drop(self.confirmed, hasta - self.confirmed)
        self.confirmed = hasta

    def _drop(self, offset: int, nbytes: int) -> None:
        try:
            os.posix_fadvise(self.fd, offset, nbytes, os.POSIX_FADV_DONTNEED)
        except (OSError, AttributeError):
            pass


def flush_in_windows(fd: int, size: int, cancelled=lambda: False,
                              window: int = WRITEBACK_WINDOW) -> bool:
    """Baja a la unidad un archivo ya escrito, de a una ventana por vez,
    preguntando `cancelled()` entre ventanas. Devuelve False si lo
    cancelaron antes de terminar (lo que faltaba sigue en la caché).

    Es la versión interrumpible de un `fsync` sobre un archivo que otro
    -`wit`, por ejemplo- dejó entero en la caché: el `fsync` de una sola vez
    no se puede cortar, y en un pendrive lento puede tardar minutos."""
    hecho = 0
    while hecho < size:
        if cancelled():
            return False
        tramo = min(window, size - hecho)
        sync_range(fd, hecho, tramo, _SYNC_AND_WAIT)
        try:
            os.posix_fadvise(fd, hecho, tramo, os.POSIX_FADV_DONTNEED)
        except (OSError, AttributeError):
            pass
        hecho += tramo
    return not cancelled()
