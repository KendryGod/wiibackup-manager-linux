"""El aviso de envío a WBFS y el filtro de "Agregar archivos" se traducen.

Tres textos de pantalla se escribían sin `_()` y salían en español con la
app en inglés: el título de la ventana mientras se envían juegos a una
unidad WBFS desde la Biblioteca ("Enviando a unidad WBFS"), el aviso al
cancelar ese envío ("Cancelando el envío…") y el nombre del filtro del
selector "Agregar archivos". La guarda de `test_textos_traducidos` solo
miraba `_show_toast`; acá se mira además lo que se le pasa a
`_begin_cancellable_progress` y a `FileFilter.set_name`.
"""
from __future__ import annotations

import ast
import gettext
import types
from pathlib import Path

import pytest

from test_smoke_gui import _poner_el_catalogo_en_ingles

RAIZ = Path(__file__).resolve().parent.parent
LOCALE = RAIZ / "data" / "locale"

TEXTOS = {
    "Enviando a unidad WBFS": "Sending to WBFS drive",
    "Cancelando el envío…": "Cancelling the send…",
    "Imágenes de Wii (*.iso, *.wbfs, *.ciso, *.wdf)":
        "Wii images (*.iso, *.wbfs, *.ciso, *.wdf)",
}


@pytest.fixture
def en():
    return gettext.translation("wiibackup-manager", str(LOCALE), languages=["en"])


def test_los_tres_textos_tienen_ingles(en):
    for es, ingles in TEXTOS.items():
        assert en.gettext(es) == ingles, es


def test_en_espanol_quedan_como_estaban():
    es = gettext.translation("wiibackup-manager", str(LOCALE), languages=["es"],
                             fallback=True)
    for texto in TEXTOS:
        assert es.gettext(texto) == texto


@pytest.mark.parametrize("ingles, esperado", [
    (True, TEXTOS["Imágenes de Wii (*.iso, *.wbfs, *.ciso, *.wdf)"]),
    (False, "Imágenes de Wii (*.iso, *.wbfs, *.ciso, *.wdf)"),
], ids=["en", "es"])
def test_el_filtro_de_agregar_archivos_sale_en_el_idioma(monkeypatch, en, ingles, esperado):
    from wiibackup_manager import window
    from wiibackup_manager.window import WiiBackupWindow

    if ingles:
        _poner_el_catalogo_en_ingles(monkeypatch, en)
    nombres = []

    class _Dialogo:
        def __init__(self, **_k):
            pass

        def set_filters(self, filtros):
            nombres.extend(filtros.get_item(i).get_name() for i in range(filtros.get_n_items()))

        def open_multiple(self, *_a):
            pass

    monkeypatch.setattr(window.Gtk, "FileDialog", _Dialogo)
    WiiBackupWindow._on_add_files(types.SimpleNamespace(_on_files_chosen=None))

    assert nombres == [esperado]


def test_los_textos_de_la_barra_de_progreso_pasan_por_traducir():
    """Guarda: ningún `_begin_cancellable_progress(...)` ni `set_name(...)`
    recibe un texto literal sin `_()`."""
    problemas = []
    for py in sorted((RAIZ / "wiibackup_manager").rglob("*.py")):
        for nodo in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
                    and nodo.func.attr in ("_begin_cancellable_progress", "set_name")):
                continue
            for arg in nodo.args:
                if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        and any(c.isalpha() for c in arg.value)):
                    problemas.append(f"{py.relative_to(RAIZ)}:{nodo.lineno} {arg.value!r}")
    assert not problemas, problemas
