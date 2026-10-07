"""Nombres de juegos, rutas y errores con "&", "<" o ">" en la interfaz.

Origen: al agregar "Ocarina of Time & Master Quest", GTK tiraba
"Failed to set text ... Error parsing markup: entidad no termina con un
punto y coma" y el aviso quedaba VACÍO. Los toasts, las filas de
Adwaita y las descripciones de StatusPage/PreferencesGroup interpretan
markup por defecto.

Lo que se comprueba es lo que vería el usuario: que el texto que llega al
`Gtk.Label` de adentro sea exactamente el nombre (con markup roto, GTK lo
deja vacío; escapado de más, se vería "&amp;").

Los métodos se llaman sobre los widgets reales; donde construir la clase
entera arrastraría red o la ventana completa, con un `self` mínimo.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.gui

NOMBRE = "Ocarina of Time & Master Quest <Rev 1> > copia"


@pytest.fixture(scope="module")
def gtk():
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        if os.environ.get("WBM_REQUIRE_GUI") == "1":
            pytest.fail("WBM_REQUIRE_GUI=1 pero no hay display")
        pytest.skip("sin display")
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    if not Gtk.init_check():
        pytest.skip("GTK no pudo inicializarse")
    Adw.init()
    return Gtk


def _textos(gtk, widget) -> list[str]:
    textos = []
    hijo = widget.get_first_child()
    while hijo is not None:
        if isinstance(hijo, gtk.Label):
            textos.append(hijo.get_text())
        textos += _textos(gtk, hijo)
        hijo = hijo.get_next_sibling()
    return textos


def _procesar_eventos():
    from gi.repository import GLib
    contexto = GLib.MainContext.default()
    for _ in range(100):
        contexto.iteration(False)


def test_el_toast_muestra_el_nombre_tal_cual(gtk):
    from gi.repository import Adw

    from wiibackup_manager.window import WiiBackupWindow

    ventana = gtk.Window()
    overlay = Adw.ToastOverlay()
    ventana.set_child(overlay)
    ventana.present()
    try:
        mensaje = f"3 juego(s) nuevo(s) agregado(s). 2 omitido(s) por ya existir: {NOMBRE}"
        WiiBackupWindow._show_toast(SimpleNamespace(_toast_overlay=overlay), mensaje)
        _procesar_eventos()
        assert mensaje in _textos(gtk, overlay)
    finally:
        ventana.destroy()


def test_las_filas_del_detalle_del_juego(gtk):
    from gi.repository import Adw

    from wiibackup_manager.widgets.game_detail_dialog import GameDetailDialog

    falso = SimpleNamespace(info_group=Adw.PreferencesGroup())
    GameDetailDialog._add_row(falso, "Título", NOMBRE)
    assert NOMBRE in _textos(gtk, falso.info_group)


def test_acceso_rapido_de_transferir(gtk, tmp_path):
    from wiibackup_manager.widgets.transfer_view import TransferView

    ruta = tmp_path / NOMBRE
    falso = SimpleNamespace(_is_dest_valid=lambda _p: False)
    fila = TransferView._build_preset_row(falso, {"name": NOMBRE, "path": str(ruta)})
    textos = _textos(gtk, fila)
    assert NOMBRE in textos
    assert any(str(ruta) in t for t in textos)


def test_rutas_de_preferencias(gtk):
    from wiibackup_manager import config
    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    ruta = f"/run/media/kendry/{NOMBRE}"
    dialogo = PreferencesDialog(
        config.Settings(library_path=ruta, wbfs_drive_path=ruta), lambda _s: None)
    assert _textos(gtk, dialogo.get_child()).count(ruta) == 2


def test_descripcion_del_ticket(gtk):
    from wiibackup_manager.widgets.ticket_dialog import TicketDialog

    dialogo = TicketDialog(NOMBRE, lambda *_a: None)
    assert any(NOMBRE in t for t in _textos(gtk, dialogo.get_child()))


def test_tarjeta_de_homebrew_no_muestra_entidades(gtk, monkeypatch):
    """El caso inverso: este label NO usa markup, así que escaparlo
    mostraba "&amp;" literal."""
    from wiibackup_manager import oscwii_client
    from wiibackup_manager.widgets.homebrew_store_view import (
        HomebrewAppCard, HomebrewStoreView)

    monkeypatch.setattr(oscwii_client, "fetch_icon_async", lambda *_a, **_k: None)
    tarjeta = HomebrewAppCard()
    tarjeta.app = oscwii_client.HomebrewApp(slug="x", name=NOMBRE)
    falso = SimpleNamespace(_install_states={},
                            _apply_install_state=lambda *_a: None)
    HomebrewStoreView._render_card(falso, tarjeta)
    assert tarjeta.name_label.get_text() == NOMBRE


def test_error_de_la_tienda_con_url(gtk, monkeypatch):
    from gi.repository import Adw

    from wiibackup_manager import oscwii_client
    from wiibackup_manager.widgets import gtk_helpers
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    monkeypatch.setattr(gtk_helpers, "widget_is_alive", lambda _w: True)
    error = "HTTP 503 en https://api.oscwii.org/v2/x?a=1&b=<2>"
    falso = SimpleNamespace(error_status=Adw.StatusPage(title="t"),
                            state_stack=SimpleNamespace(
                                set_visible_child_name=lambda _n: None))
    HomebrewStoreView._on_apps_loaded(falso, oscwii_client.AppListResult(
        status=oscwii_client.FetchStatus.ERROR, apps=(), error=error))
    assert error in _textos(gtk, falso.error_status)


def test_ruta_de_ejemplo_existe_en_el_test():
    # Sanidad del propio test: el nombre trae los tres caracteres.
    assert all(c in NOMBRE for c in "&<>")
    assert Path(NOMBRE).name == NOMBRE
