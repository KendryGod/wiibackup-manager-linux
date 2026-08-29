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
@contextmanager
def atomic_target(dest: Path, *, mkparents: bool = False) -> Iterator[Path]:
    """Cede la ruta de un temporal hermano de `dest` y, si el bloque
    termina sin excepción, lo mueve encima de `dest` con `os.replace`.

    Así `dest` nunca existe a medio escribir: o está el contenido viejo
    entero, o el nuevo entero, nunca una mezcla. Ante CUALQUIER excepción
    -incluida una que levante el propio `os.replace`- se borra el
    temporal y la excepción se propaga tal cual: es quien llama el que
    decide si eso fue un error o un resultado esperado.

    El temporal es oculto y lleva el PID adentro
    (`.<nombre>.parcial-<pid>`), y eso no es cosmético: un escaneo de la
    biblioteca no levanta archivos que empiezan con punto, y dos procesos
    escribiendo al mismo destino no se pisan el temporal entre sí.
    `tools/manual_queue_e2e.py` busca exactamente ese patrón de nombre
    para verificar que no queden temporales huérfanos después de una
    transferencia, así que no se cambia sin actualizar eso también.

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
    tmp = dest.with_name(f".{dest.name}.parcial-{os.getpid()}")
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
