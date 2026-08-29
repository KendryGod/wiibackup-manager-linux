"""Utilidades de filesystem compartidas por varios módulos.

Acá vive lo que más de un módulo necesitaba y terminaba reescribiendo por
su cuenta: la escritura atómica a un temporal hermano, la búsqueda de los
datos instalados de la app, y la firma binaria de PNG. Nada de esto es
lógica de negocio de ninguna parte en particular -por eso no vive en
`library`, `gametdb` ni `oscwii_client`- y tener una sola copia de cada
cosa significa que el manejo de errores se lee, se revisa y se arregla en
un solo lugar en vez de en cuatro.
"""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Los 8 primeros bytes de todo PNG. La usan `gametdb` (carátulas de
# GameTDB) y `oscwii_client` (íconos de Open Shop Channel) para descartar
# una respuesta que no es una imagen -el HTML de una página de error, por
# ejemplo- antes de guardarla en la caché. Las dos la re-exportan con este
# mismo nombre, así que `gametdb.PNG_MAGIC` y `oscwii_client.PNG_MAGIC`
# siguen existiendo y apuntan a este único valor.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


# ------------------------------------------------------- Escritura atómica --
_permisos_temporal: "int | None" = None


def _permisos_por_defecto() -> int:
    """Permisos que habría tenido el temporal si lo hubiera creado un
    `open(..., "wb")` normal: 0666 recortado por el umask del proceso.

    Hace falta porque `mkstemp` crea siempre en 0600 -lo correcto para un
    temporal que se queda temporal, pero acá el temporal PASA A SER el
    archivo final-. Sin esto, la caché de carátulas e íconos, las configs
    maestras y las apps instaladas habrían quedado en 0600 de un día para
    el otro: un cambio silencioso de permisos que el esquema anterior no
    hacía.

    El umask se lee de /proc y no con `os.umask()`, porque la única forma
    de consultarlo con `os.umask` es fijarlo y volverlo a poner, y eso es
    exactamente la carrera entre threads que este arreglo viene a cerrar.
    Si /proc no está (o no trae el campo), se asume el umask más común."""
    global _permisos_temporal
    if _permisos_temporal is None:
        umask = 0o022
        try:
            with open("/proc/self/status", "r", encoding="ascii") as f:
                for linea in f:
                    if linea.startswith("Umask:"):
                        umask = int(linea.split()[1], 8)
                        break
        except (OSError, ValueError, IndexError):
            pass
        _permisos_temporal = 0o666 & ~umask
    return _permisos_temporal


def ajustar_permisos_por_defecto(tmp: Path) -> None:
    """Le pone a `tmp` los permisos que habría tenido si lo hubiera creado
    un `open()` normal. La usan este módulo y `library._copy_with_progress`,
    que también crea su temporal con `mkstemp` (o sea, en 0600).

    Mejor esfuerzo: en FAT/exFAT -el destino habitual de esta app- los
    permisos los fija el montaje y `chmod` puede fallar, y eso no tiene
    por qué hacer fracasar la escritura."""
    try:
        os.chmod(tmp, _permisos_por_defecto())
    except OSError:
        pass


@contextmanager
def atomic_target(dest: Path, *, mkparents: bool = False) -> Iterator[Path]:
    """Cede la ruta de un temporal hermano de `dest` y, si el bloque
    termina sin excepción, lo mueve encima de `dest` con `os.replace`.

    Así `dest` nunca existe a medio escribir: o está el contenido viejo
    entero, o el nuevo entero, nunca una mezcla. Ante CUALQUIER excepción
    -incluida una que levante el propio `os.replace`- se borra el
    temporal y la excepción se propaga tal cual: es quien llama el que
    decide si eso fue un error o un resultado esperado.

    El temporal lo crea `tempfile.mkstemp` en la MISMA carpeta que
    `dest` -no en /tmp: el `os.replace` final solo es atómico dentro de un
    filesystem- con el nombre `.<nombre>.parcial-<sufijo aleatorio>`.

    Ese nombre es único de verdad, garantizado por el sistema operativo
    (`mkstemp` crea con O_CREAT|O_EXCL y reintenta hasta conseguir un
    nombre libre), y no por convención del código. Antes llevaba el PID
    adentro, que alcanza para que dos PROCESOS no se pisen pero no para
    dos THREADS del mismo proceso: dos threads escribiendo al mismo
    destino calculaban el mismo nombre de temporal y el segundo truncaba
    lo que estaba escribiendo el primero, justo en el helper que se supone
    que es la pieza central de atomicidad de la app. Hoy ningún camino
    cotidiano llega a eso -las llamadas de arriba están dedupeadas o
    serializadas por `OperationManager`- pero la garantía que este helper
    promete tiene que valer por sí sola, sin depender de quién lo llame.

    Sigue siendo oculto (empieza con punto) a propósito: un escaneo de la
    biblioteca no levanta archivos que empiezan con punto, y
    `tools/manual_queue_e2e.py` los busca así (`rglob(".*")`) para
    verificar que no queden temporales huérfanos después de una
    transferencia.

    `mkparents=True` crea la carpeta de `dest` antes de empezar, para
    quienes escriben a una ruta que puede no existir todavía (el extractor
    de ZIP, la copia de configs maestras).

    NO hace `fsync`: para lo que se guarda con esto -cachés que se pueden
    volver a bajar y copias de archivos chicos- alcanza con la atomicidad
    del rename. Quien además necesita durabilidad ante un corte de luz
    tiene `config.write_text_atomic` y `library._copy_with_progress`, que
    sí bajan a disco antes de intercambiar y por eso no usan este helper.
    """
    dest = Path(dest)
    if mkparents:
        dest.parent.mkdir(parents=True, exist_ok=True)

    fd, nombre = tempfile.mkstemp(dir=dest.parent,
                                  prefix=f".{dest.name}.parcial-")
    tmp = Path(nombre)
    # `mkstemp` devuelve el archivo ya abierto, pero los cuatro usuarios de
    # este helper abren la ruta ellos mismos (`write_bytes`, `copyfile`,
    # `open("wb")`), así que el descriptor se cierra en el acto en vez de
    # cambiar el contrato de la función. El archivo -y con él el nombre
    # reservado- sigue existiendo: lo que se suelta es el descriptor, no
    # la exclusividad.
    os.close(fd)
    ajustar_permisos_por_defecto(tmp)

    try:
        yield tmp
        os.replace(tmp, dest)
    except BaseException:
        # Borrar el temporal es "mejor esfuerzo": si tampoco se puede
        # (permisos, unidad desconectada a mitad de camino), lo que
        # importa es que la excepción original llegue a quien llama sin
        # que esta la tape.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
