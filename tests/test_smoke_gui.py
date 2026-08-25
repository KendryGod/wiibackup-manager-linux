"""Smoke test: que la app levante de verdad.

Los otros tests son de lógica pura y no tocan GTK. Este arranca la
aplicación completa —CSS, ventana, las tres pestañas, el escaneo inicial—
y comprueba que llega a mostrarse sin errores fatales. Es lo que atrapa
una regresión que las pruebas unitarias no ven: un `_()` que shadowea un
parámetro, un widget que se construye con un argumento que ya no existe,
un import circular.

Necesita un display. En CI se corre bajo Xvfb (ver .github/workflows/ci.yml);
si no hay ninguno disponible, el test se saltea en vez de fallar, para que
correr `pytest` en una terminal sin sesión gráfica siga sirviendo.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.gui


def _hay_display() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _no_disponible(motivo: str):
    """Saltea el test, o lo hace fallar si el entorno prometía un display.

    En CI el workflow exporta WBM_REQUIRE_GUI=1: ahí un skip sería peor
    que un fallo, porque el check saldría verde sin que el smoke test
    haya corrido nunca -exactamente el escenario que este test existe
    para evitar-. En una terminal sin sesión gráfica, en cambio, saltear
    es lo correcto."""
    if os.environ.get("WBM_REQUIRE_GUI") == "1":
        pytest.fail(f"WBM_REQUIRE_GUI=1 pero {motivo}")
    pytest.skip(motivo)


@pytest.fixture(scope="module")
def gtk():
    if not _hay_display():
        _no_disponible("sin display: el smoke test necesita Xvfb o una sesión gráfica")
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk
    if not Gtk.init_check():
        _no_disponible("GTK no pudo inicializarse con este display")
    return gi


def test_la_ventana_se_arma_y_se_muestra(gtk, tmp_path, monkeypatch):
    """Levanta la app entera y espera a que la ventana esté presentada."""
    from gi.repository import Adw, GLib

    from wiibackup_manager import config
    from wiibackup_manager.styles import load_css
    from wiibackup_manager.window import WiiBackupWindow

    biblioteca = tmp_path / "biblioteca"
    biblioteca.mkdir()
    monkeypatch.setattr(config.Settings, "load",
                        classmethod(lambda cls: config.Settings(
                            library_path=str(biblioteca))))

    resultado = {}

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id="com.gamefixsps.WiiBackupManager.Test")

        def do_activate(self):
            load_css()
            win = WiiBackupWindow(self)
            win.present()
            GLib.timeout_add(2500, self._revisar, win)

        def _revisar(self, win):
            resultado["titulo"] = win.get_title()
            resultado["visible"] = win.get_visible()
            resultado["pestañas"] = [
                win.view_stack.get_page(win.view_stack.get_child_by_name(n)).get_title()
                for n in ("library", "transfer", "log")
            ]
            self.quit()
            return False

    # Un timeout de guardia: si la app se cuelga, el test tiene que fallar
    # con un mensaje claro y no dejar la suite colgada para siempre.
    GLib.timeout_add_seconds(60, lambda: (resultado.setdefault("colgada", True),
                                          False)[1])

    App().run([])

    assert not resultado.get("colgada"), "la app no terminó de arrancar"
    assert resultado.get("visible") is True
    assert "WiiBackup Manager" in resultado.get("titulo", "")
    assert resultado.get("pestañas") == ["Biblioteca", "Transferir", "Log"]


def test_la_ventana_tambien_arranca_en_ingles(gtk, tmp_path, monkeypatch):
    """El mismo arranque con el catálogo inglés cargado: comprueba que
    ninguna cadena traducida rompe la construcción de la interfaz."""
    import gettext
    from pathlib import Path

    from gi.repository import Adw, GLib

    from wiibackup_manager import config, i18n
    from wiibackup_manager.styles import load_css

    locale_dir = Path(__file__).resolve().parent.parent / "data" / "locale"
    en = gettext.translation("wiibackup-manager", str(locale_dir), languages=["en"])
    monkeypatch.setattr(i18n, "_", en.gettext)

    # window.py importó `_` por valor, así que se parchea también ahí y en
    # los widgets que lo usan al construir la interfaz.
    from wiibackup_manager import window as window_mod
    from wiibackup_manager.widgets import log_view, transfer_view
    for mod in (window_mod, log_view, transfer_view):
        monkeypatch.setattr(mod, "_", en.gettext)

    biblioteca = tmp_path / "biblioteca"
    biblioteca.mkdir()
    monkeypatch.setattr(config.Settings, "load",
                        classmethod(lambda cls: config.Settings(
                            library_path=str(biblioteca))))

    resultado = {}

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id="com.gamefixsps.WiiBackupManager.TestEn")

        def do_activate(self):
            load_css()
            win = window_mod.WiiBackupWindow(self)
            win.present()
            GLib.timeout_add(2500, self._revisar, win)

        def _revisar(self, win):
            resultado["pestañas"] = [
                win.view_stack.get_page(win.view_stack.get_child_by_name(n)).get_title()
                for n in ("library", "transfer", "log")
            ]
            resultado["vacio"] = win.status_page.get_title()
            self.quit()
            return False

    App().run([])

    assert resultado.get("pestañas") == ["Library", "Transfer", "Log"]
    assert resultado.get("vacio") == "No games yet"
