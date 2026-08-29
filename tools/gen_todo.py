#!/usr/bin/env python3
"""Regenera `todo_el_codigo.txt`: todo el código Python del proyecto
concatenado en un solo archivo.

Por qué existe
--------------
Ese archivo se usa para revisiones cruzadas: pegarle a otra herramienta el
proyecto entero de una, sin que haya que ir archivo por archivo. Hasta
ahora se armaba a mano, y eso tuvo dos consecuencias que este script
elimina: quedaba desactualizado sin que se notara (le llegaron a faltar
archivos enteros que ya existían en el repo), y se generaba ANTES de los
últimos retoques del commit al que acompañaba, así que el txt commiteado
no coincidía con el código de ese mismo commit.

Uso
---
    python3 tools/gen_todo.py

Conviene correrlo como último paso antes de commitear, justo por lo de
arriba.

Formato
-------
Es el que ya tenía el archivo, reproducido tal cual para que el diff entre
una generación y la siguiente muestre solo los cambios de código de
verdad:

    # ============================================================
    # FILE: <ruta relativa a la raíz del repo>
    # ============================================================
    <contenido del archivo, tal cual>

Los bloques van separados por dos líneas en blanco, y el archivo termina
con un único salto de línea.

Qué incluye
-----------
Todos los `.py` de `wiibackup_manager/`, `tools/` y `tests/` -en ese
orden, que no es alfabético: primero la app, después las herramientas y al
final las pruebas-. Dentro de cada sección el orden es alfabético por ruta
relativa como texto, que es lo que deja `widgets/...` entre `trash.py` y
`window.py`. Se saltea `__pycache__`; los archivos que no son `.py` (como
`update-translations.sh`) no entran.
"""
from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
SECCIONES = ("wiibackup_manager", "tools", "tests")
REGLA = "# " + "=" * 60
SALIDA = "todo_el_codigo.txt"


def archivos() -> list:
    """Los .py a incluir, en el orden en que van al archivo."""
    encontrados = []
    for seccion in SECCIONES:
        rutas = [p for p in (RAIZ / seccion).rglob("*.py")
                 if "__pycache__" not in p.parts]
        encontrados += sorted(rutas, key=lambda p: str(p.relative_to(RAIZ)))
    return encontrados


def main() -> None:
    bloques = []
    for ruta in archivos():
        contenido = ruta.read_text(encoding="utf-8").rstrip("\n")
        cabecera = f"{REGLA}\n# FILE: {ruta.relative_to(RAIZ)}\n{REGLA}"
        # Un archivo vacío (hoy, `widgets/__init__.py`) no aporta ninguna
        # línea de contenido: sin este caso quedarían tres líneas en
        # blanco antes del bloque siguiente en vez de dos.
        bloques.append(f"{cabecera}\n{contenido}" if contenido else cabecera)

    destino = RAIZ / SALIDA
    destino.write_text("\n\n\n".join(bloques) + "\n", encoding="utf-8")
    print(f"{SALIDA}: {len(bloques)} archivos, "
          f"{destino.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
