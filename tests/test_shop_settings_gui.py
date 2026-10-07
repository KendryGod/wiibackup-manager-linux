"""Ajustes → General → "Mi taller": el grupo de la interfaz que carga los
datos del taller para el Ticket de Entrega.

Se arma el widget de verdad (necesita display, igual que el resto de las
pruebas `gui`) contra una configuración en memoria y un `save` que cuenta
las llamadas: lo que se prueba es que cada campo termine en
`config.Settings` -de donde lo lee el ticket- y que se mande a guardar.
Todos los datos son ficticios.
"""
from __future__ import annotations

import os

import cairo
import pytest

pytestmark = pytest.mark.gui


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


@pytest.fixture
def grupo(gtk):
    from wiibackup_manager import config
    from wiibackup_manager.widgets.shop_settings import ShopSettingsGroup

    settings = config.Settings()
    guardados = []
    g = ShopSettingsGroup(settings, lambda: guardados.append(1))
    g.guardados = guardados
    return g


def test_los_campos_de_texto_van_a_la_configuracion(grupo):
    grupo.name_row.set_text("Taller Ficticio")
    grupo.slogan_row.set_text("Eslogan de prueba")
    grupo.location_row.set_text("Ciudad Ficticia")
    s = grupo.settings
    # En memoria al instante: un ticket generado ya los usa.
    assert (s.shop_name, s.shop_slogan, s.shop_location) == (
        "Taller Ficticio", "Eslogan de prueba", "Ciudad Ficticia")
    # A disco, una sola vez cuando se deja de tipear (o al cerrar).
    assert grupo.guardados == []
    grupo.flush()
    assert grupo.guardados == [1]


def test_el_whatsapp_se_guarda_limpio(grupo):
    grupo.whatsapp_row.set_text("+504 0000-1111")
    assert grupo.settings.shop_whatsapp == "50400001111"


def _png(path):
    superficie = cairo.ImageSurface(cairo.FORMAT_ARGB32, 120, 40)
    superficie.write_to_png(str(path))
    return str(path)


def test_elegir_y_quitar_el_logo(grupo, tmp_path):
    assert not grupo.clear_logo_btn.get_sensitive()
    assert not grupo.logo_preview.get_visible()

    ruta = _png(tmp_path / "logo.png")
    grupo.set_logo(ruta)
    assert grupo.settings.shop_logo_path == ruta
    assert grupo.logo_preview.get_visible()
    assert grupo.logo_preview.get_file().get_path() == ruta
    assert grupo.clear_logo_btn.get_sensitive()
    assert grupo.logo_row.get_subtitle() == ruta

    grupo.clear_logo_btn.emit("clicked")
    assert grupo.settings.shop_logo_path == ""
    assert not grupo.logo_preview.get_visible()
    assert not grupo.clear_logo_btn.get_sensitive()
    assert len(grupo.guardados) == 2


def test_el_color_arranca_en_el_morado_y_se_restablece(grupo):
    from gi.repository import Gdk

    from wiibackup_manager import config

    assert grupo.settings.shop_accent_color == config.DEFAULT_ACCENT_COLOR
    assert not grupo.reset_color_btn.get_sensitive()

    verde = Gdk.RGBA()
    verde.parse("#00FF00")
    grupo.color_btn.set_rgba(verde)
    assert grupo.settings.shop_accent_color == "#00FF00"
    assert grupo.reset_color_btn.get_sensitive()

    grupo.reset_color_btn.emit("clicked")
    assert grupo.settings.shop_accent_color == config.DEFAULT_ACCENT_COLOR
    assert not grupo.reset_color_btn.get_sensitive()
    assert len(grupo.guardados) == 2


def test_el_tema_del_ticket(grupo):
    from wiibackup_manager import config

    assert grupo.theme_row.get_selected() == 0
    grupo.theme_row.set_selected(1)
    assert grupo.settings.ticket_theme == config.TICKET_THEME_LIGHT
    grupo.theme_row.set_selected(0)
    assert grupo.settings.ticket_theme == config.TICKET_THEME_DARK


def test_muestra_lo_que_ya_estaba_guardado(gtk, tmp_path):
    from wiibackup_manager import config
    from wiibackup_manager.widgets.shop_settings import ShopSettingsGroup

    ruta = _png(tmp_path / "logo.png")
    s = config.Settings(shop_name="Taller Ficticio", shop_whatsapp="50400001111",
                        shop_logo_path=ruta, shop_accent_color="#123456",
                        ticket_theme="light")
    g = ShopSettingsGroup(s, lambda: None)
    assert g.name_row.get_text() == "Taller Ficticio"
    assert g.whatsapp_row.get_text() == "50400001111"
    assert g.logo_preview.get_visible()
    assert g.reset_color_btn.get_sensitive()
    assert g.theme_row.get_selected() == 1


def test_lo_cargado_en_ajustes_llega_al_ticket(grupo, tmp_path):
    """De punta a punta: lo que se escribe en "Mi taller" es lo que
    `ShopProfile.from_settings` -lo que usa el ticket- lee."""
    from wiibackup_manager import pdf_export

    grupo.name_row.set_text("Taller Ficticio")
    grupo.whatsapp_row.set_text("+504 0000 1111")
    ruta = _png(tmp_path / "logo.png")
    grupo.set_logo(ruta)
    grupo.theme_row.set_selected(1)
    taller = pdf_export.ShopProfile.from_settings(grupo.settings)
    assert taller.name == "Taller Ficticio"
    assert taller.whatsapp == "50400001111"
    assert taller.logo_path == ruta
    assert taller.theme == "light"
    assert not taller.is_empty()


def test_el_dialogo_del_ticket_avisa_si_falta_el_taller(gtk):
    from wiibackup_manager.widgets.ticket_dialog import TicketDialog

    sin = TicketDialog("USB", lambda *a: None, shop_missing=True)
    con = TicketDialog("USB", lambda *a: None, shop_missing=False)
    assert sin.shop_banner.get_revealed()
    assert "Mi taller" in sin.shop_banner.get_title()
    assert not con.shop_banner.get_revealed()
