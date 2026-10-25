"""La Store dice qué apps escriben en la NAND de la Wii.

51 apps del catálogo de Open Shop Channel traen la bandera `WRITES_TO_NAND`
(instaladores de cIOS, Priiloader, parcheadores, borradores de títulos):
al usarlas en la consola, modifican la memoria interna de la Wii.
`HomebrewApp.from_api_dict` descartaba `flags` y la tienda no lo decía.

Ahora la tarjeta muestra "Escribe en la NAND" y, si el aviso antes de
instalar aparece por otro motivo, agrega una línea. El aviso NO se fuerza
por esto: lo que se copia es la app a la SD/USB, como cualquier otra.
"""
from __future__ import annotations

import os
import types
import zipfile

import pytest

from homebrew_falso import dol
from wiibackup_manager import oscwii_client, oscwii_installer
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import AvisoDeInstalacion

LINEA = "modifica la NAND de la Wii"


def _api(**extra) -> dict:
    d = {"slug": "priiloader", "name": "Priiloader Installer",
         "url": {"zip": "https://hbb1.oscwii.org/api/contents/priiloader/priiloader.zip"}}
    d.update(extra)
    return d


# ------------------------------------------------------------- catálogo --
def test_lee_la_bandera_de_la_api():
    app = HomebrewApp.from_api_dict(_api(flags=["WRITES_TO_NAND"]))

    assert app.flags == ("WRITES_TO_NAND",)
    assert app.writes_to_nand


@pytest.mark.parametrize("flags", [[], ["DEPRECATED"], None, "WRITES_TO_NAND", [3]])
def test_sin_la_bandera_no_escribe_en_la_nand(flags):
    app = HomebrewApp.from_api_dict(_api(flags=flags))

    assert not app.writes_to_nand


def test_el_cache_guarda_la_bandera():
    app = HomebrewApp.from_api_dict(_api(flags=["WRITES_TO_NAND"]))

    assert HomebrewApp.from_api_dict(app.to_cache_dict()).writes_to_nand


def test_un_cache_viejo_sin_flags_sigue_cargando():
    viejo = HomebrewApp.from_api_dict(_api()).to_cache_dict()
    viejo.pop("flags", None)

    app = HomebrewApp.from_api_dict(viejo)

    assert app is not None and app.flags == ()


# ---------------------------------------------------------------- aviso --
@pytest.fixture
def paquete(monkeypatch):
    entradas = {}

    def _falsa(app, destino, cancel_event, on_progress):
        with zipfile.ZipFile(destino, "w") as zf:
            for nombre, datos in entradas.items():
                zf.writestr(nombre, datos)
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _falsa)
    return entradas


def _app_nand(slug="AnyTitleDeleterMOD") -> HomebrewApp:
    return HomebrewApp(slug=slug, name=slug, flags=("WRITES_TO_NAND",),
                       zip_url=f"https://hbb1.oscwii.org/api/contents/{slug}/x.zip")


def test_si_el_aviso_aparece_dice_que_escribe_en_la_nand(tmp_path, paquete):
    """AnyTitleDeleterMOD deja `/database.txt` en la raíz: el aviso aparece
    por eso, y suma la línea de la NAND."""
    paquete.update({"apps/AnyTitleDeleterMOD/boot.dol": dol(b"atd"),
                    "database.txt": b"HAK-EULA"})
    avisos = []

    oscwii_installer.install_app(_app_nand(), tmp_path,
                                 confirmar=lambda a: avisos.append(a) or False)

    [aviso] = avisos
    assert aviso.escribe_en_nand


def test_la_bandera_sola_no_hace_aparecer_el_aviso(tmp_path, paquete):
    paquete.update({"apps/priiloader/boot.dol": dol(b"prii")})
    avisos = []

    r = oscwii_installer.install_app(_app_nand("priiloader"), tmp_path,
                                     confirmar=lambda a: avisos.append(a) or False)

    assert r.ok, r.error
    assert avisos == []


def test_una_app_sin_la_bandera_no_lo_dice(tmp_path, paquete):
    paquete.update({"apps/smw/boot.dol": dol(b"smw"), "smw/x.wav": b"RIFF"})
    avisos = []
    app = HomebrewApp(slug="smw", name="smw",
                      zip_url="https://hbb1.oscwii.org/api/contents/smw/x.zip")

    oscwii_installer.install_app(app, tmp_path, confirmar=lambda a: avisos.append(a) or False)

    [aviso] = avisos
    assert not aviso.escribe_en_nand


def _aviso(**kw) -> AvisoDeInstalacion:
    base = dict(reemplaza=("apps/AnyTitleDeleterMOD/",), carpetas=(("database.txt", 1),),
                distintos=(), intocables=(), omitidos=())
    base.update(kw)
    return AvisoDeInstalacion(**base)


def test_el_texto_lleva_la_linea_de_la_nand():
    from wiibackup_manager.widgets.homebrew_store_view import _texto_del_aviso

    assert LINEA in _texto_del_aviso(_aviso(escribe_en_nand=True))
    assert "no instala nada en la NAND" in _texto_del_aviso(_aviso(escribe_en_nand=True))
    assert LINEA not in _texto_del_aviso(_aviso())


# -------------------------------------------------------------- tarjeta --
@pytest.fixture(scope="module")
def gtk():
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("sin display")
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    if not Gtk.init_check():
        pytest.skip("GTK no pudo inicializarse")
    Adw.init()
    return Gtk


@pytest.mark.gui
@pytest.mark.parametrize("flags, visible", [(("WRITES_TO_NAND",), True), ((), False)])
def test_la_tarjeta_muestra_la_etiqueta(gtk, monkeypatch, flags, visible):
    from wiibackup_manager.widgets.homebrew_store_view import (
        HomebrewAppCard, HomebrewStoreView)

    monkeypatch.setattr(oscwii_client, "fetch_icon_async", lambda *_a, **_k: None)
    card = HomebrewAppCard()
    card.app = HomebrewApp(slug="priiloader", name="Priiloader", flags=flags,
                           zip_url="https://hbb1.oscwii.org/x.zip")
    vista = types.SimpleNamespace(_install_states={}, _apply_install_state=lambda *_a: None)

    HomebrewStoreView._render_card(vista, card)

    assert card.nand_label.get_visible() is visible
    assert card.nand_label.get_label() == "Escribe en la NAND"
