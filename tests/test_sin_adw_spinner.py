"""La app tiene que arrancar con libadwaita 1.5, la de Ubuntu 24.04.

`Adw.Spinner` es de libadwaita 1.6. La Homebrew Store lo creaba al
armarse, así que con la 1.5 la ventana principal no llegaba a construirse
y la app no abría (el smoke test del CI, que corre en Ubuntu 24.04, se
quedaba esperando la ventana). Se usa `Gtk.Spinner(spinning=True)`, que
está en todo GTK 4, como en el detalle del juego.

Se revisa el código en vez de arrancar la app: acá libadwaita es más
nueva, `Adw.Spinner` existe y construir la vista no fallaría nunca."""
from __future__ import annotations

import ast
from pathlib import Path

import wiibackup_manager


def test_ningun_archivo_de_la_app_usa_adw_spinner():
    raiz = Path(wiibackup_manager.__file__).parent
    archivos = sorted(raiz.rglob("*.py"))
    # Que la búsqueda mire donde tiene que mirar y no pase por no encontrar nada.
    assert raiz / "widgets" / "homebrew_store_view.py" in archivos

    usos = []
    for archivo in archivos:
        arbol = ast.parse(archivo.read_text(encoding="utf-8"), filename=str(archivo))
        for nodo in ast.walk(arbol):
            if (isinstance(nodo, ast.Attribute) and nodo.attr == "Spinner"
                    and isinstance(nodo.value, ast.Name) and nodo.value.id == "Adw"):
                usos.append(f"{archivo.relative_to(raiz.parent)}:{nodo.lineno}")

    assert usos == [], (
        "Adw.Spinner es de libadwaita 1.6 y con la 1.5 (Ubuntu 24.04) la "
        "ventana no se arma: usar Gtk.Spinner(spinning=True). Aparece en "
        + ", ".join(usos))
