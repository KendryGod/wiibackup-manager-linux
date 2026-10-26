"""Con una libadwaita más vieja que la 1.5, la app avisa en vez de no abrir.

La 1.5 trae `Adw.Dialog`, `Adw.AlertDialog`, `Adw.AboutDialog` y
`Adw.PreferencesDialog`, y cuatro diálogos de la app heredan de ellas al
IMPORTARSE (`TicketDialog`, `GameDetailDialog`, `RecoveryDialog`,
`PreferencesDialog`). Con una 1.4, `from .window import WiiBackupWindow`
revienta con un AttributeError antes de que haya ventana: la app no
abría y la terminal mostraba un traceback.

Ahora `app.py` no importa la ventana al cargarse: mira la versión al
activarse y, si no alcanza, avisa en la terminal y con un diálogo de GTK
puro (no de libadwaita, que es justo lo que falta), y sale con 1.
"""
from __future__ import annotations

import subprocess
import sys
import types

import pytest

from wiibackup_manager import app


@pytest.mark.parametrize("version", [(1, 4, 3), (1, 2, 0), (1, 0, 0), (0, 9, 9)])
def test_mas_vieja_que_la_1_5_tiene_un_aviso(version):
    aviso = app.problema_de_libadwaita(version)

    assert aviso is not None
    assert "1.5" in aviso
    assert ".".join(map(str, version)) in aviso


@pytest.mark.parametrize("version", [(1, 5, 0), (1, 6, 2), (1, 9, 0), (2, 0, 0)])
def test_la_1_5_o_mas_nueva_no_tiene_aviso(version):
    assert app.problema_de_libadwaita(version) is None


def test_lee_la_version_real_de_libadwaita():
    from gi.repository import Adw

    assert app.version_de_libadwaita() == (
        Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version())


def _app_falsa():
    avisos = []
    falsa = types.SimpleNamespace(_window=None, exit_status=0,
                                  _avisar_y_salir=avisos.append)
    return falsa, avisos


def test_con_una_vieja_avisa_y_no_crea_la_ventana(monkeypatch, capsys):
    from wiibackup_manager import window

    def _no(*_a, **_k):
        raise AssertionError("no tiene que crear la ventana")

    monkeypatch.setattr(app, "version_de_libadwaita", lambda: (1, 4, 0))
    monkeypatch.setattr(window, "WiiBackupWindow", _no)
    monkeypatch.setattr(app, "load_css", _no)
    falsa, avisos = _app_falsa()

    app.WiiBackupApp.do_activate(falsa)

    [aviso] = avisos
    assert "1.4.0" in aviso
    assert aviso in capsys.readouterr().err
    assert falsa.exit_status == 1
    assert falsa._window is None


def test_con_la_1_5_crea_la_ventana_como_siempre(monkeypatch):
    from wiibackup_manager import window

    creadas = []

    class _Ventana:
        def __init__(self, aplicacion):
            creadas.append(aplicacion)
            self.presentada = False

        def present(self):
            self.presentada = True

    monkeypatch.setattr(app, "version_de_libadwaita", lambda: (1, 5, 0))
    monkeypatch.setattr(window, "WiiBackupWindow", _Ventana)
    monkeypatch.setattr(app, "load_css", lambda: None)
    falsa, avisos = _app_falsa()

    app.WiiBackupApp.do_activate(falsa)

    assert avisos == []
    assert creadas == [falsa] and falsa._window.presentada
    assert falsa.exit_status == 0


def test_cargar_la_app_no_importa_la_ventana():
    """Lo que hace posible el aviso: la ventana (y los diálogos que heredan
    de libadwaita 1.5) se importan recién al activarse."""
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys, wiibackup_manager.app; "
         "print(sorted(m for m in sys.modules if m == 'wiibackup_manager.window' "
         "or m.startswith('wiibackup_manager.widgets')))"],
        capture_output=True, text=True, timeout=60)

    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]"


def test_main_sale_con_el_codigo_del_aviso(monkeypatch):
    def _run(self, _argv):
        self.exit_status = 1
        return 0

    monkeypatch.setattr(app.WiiBackupApp, "run", _run)

    assert app.main() == 1


@pytest.mark.gui
def test_el_aviso_es_un_dialogo_de_gtk_atado_a_la_app():
    import os
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("sin display")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("GTK no pudo inicializarse")

    aplicacion = app.WiiBackupApp()
    aplicacion.register(None)
    try:
        app.WiiBackupApp._avisar_y_salir(aplicacion, "el aviso")
        [dialogo] = aplicacion.get_windows()

        assert isinstance(dialogo, Gtk.MessageDialog)
        assert dialogo.get_property("secondary-text") == "el aviso"
        assert dialogo.get_property("text") == "No se puede abrir WiiBackup Manager"

        dialogo.emit("response", Gtk.ResponseType.CLOSE)
        assert aplicacion.get_windows() == []
    finally:
        for ventana in aplicacion.get_windows():
            ventana.destroy()
