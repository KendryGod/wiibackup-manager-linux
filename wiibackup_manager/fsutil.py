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
