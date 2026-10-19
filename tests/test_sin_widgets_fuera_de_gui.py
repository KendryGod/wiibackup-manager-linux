"""La guardia de `conftest.py` (`sin_widgets_fuera_de_gui`): un test sin el
marcador `gui` que crea un widget falla en todos lados, antes de crearlo.

Sin ella, un test así pasaba en local (GTK más nuevo, o con pantalla) y en
el job sin display de CI era un segfault que mataba la suite entera."""
from __future__ import annotations

import pytest


def test_un_test_sin_gui_no_puede_crear_widgets():
    from wiibackup_manager.window import Gtk

    with pytest.raises(pytest.fail.Exception, match="sin el marcador gui"):
        Gtk.Box()


def test_lo_que_no_es_widget_sigue_permitido():
    """Lo usan tests de lógica de verdad (el filtro de Agregar archivos,
    los avisos de Preferencias) y no necesita display."""
    from wiibackup_manager.window import Adw, Gtk

    assert Gtk.FileFilter() is not None
    assert Adw.Toast(title="x").get_title() == "x"
