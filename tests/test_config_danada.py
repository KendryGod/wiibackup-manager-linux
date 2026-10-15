"""Un config.json dañado no se pierde, y Preferencias no guarda si no cambió nada.

Antes, con un config.json ilegible (una coma de más al editarlo a mano, un
archivo vacío, sin permiso de lectura) la app arrancaba con los valores por
defecto avisando solo por stderr, y el primer guardado lo pisaba: abrir y
cerrar Preferencias -que guardaba siempre al cerrarse- alcanzaba para
perder los destinos guardados y los datos de "Mi taller".

Ahora el archivo se aparta como `config.json.corrupto-<fecha>` antes de
usar los valores por defecto, la ventana lo avisa con un banner, y
Preferencias guarda solo si algo cambió. Además `library_path` vacío o
relativo ya no pasa: se volvía la carpeta desde donde se lanzó la app.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wiibackup_manager import config


@pytest.fixture
def archivo(tmp_path, monkeypatch):
    ruta = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_FILE", ruta)
    return ruta


def _apartados(archivo: Path) -> list:
    return sorted(archivo.parent.glob("config.json.corrupto-*"))


@pytest.mark.parametrize("contenido", [b"{ roto", b"", b"[1, 2]", b"\xff\xfe\x00"],
                         ids=["coma-de-mas", "vacio", "no-es-objeto", "no-es-utf8"])
def test_un_config_danado_se_aparta_antes_de_usar_los_valores_por_defecto(archivo,
                                                                         contenido):
    archivo.write_bytes(contenido)

    s = config.Settings.load()

    assert s.wit_binary == "wit" and s.dest_presets == []
    [apartado] = _apartados(archivo)
    assert apartado.read_bytes() == contenido
    assert s.config_was_damaged and s.damaged_copy == apartado
    assert not archivo.exists()
    # El primer guardado ya no pisa nada: el dañado sigue aparte, entero.
    s.save()
    assert apartado.read_bytes() == contenido


@pytest.mark.skipif(os.geteuid() == 0, reason="root lee aunque no tenga permiso")
def test_un_config_sin_permiso_de_lectura_tambien_se_aparta(archivo):
    archivo.write_text(json.dumps({"shop_name": "Mi taller"}), encoding="utf-8")
    archivo.chmod(0)
    try:
        s = config.Settings.load()
    finally:
        for p in [archivo, *_apartados(archivo)]:
            if p.exists():
                p.chmod(0o600)

    [apartado] = _apartados(archivo)
    assert json.loads(apartado.read_text(encoding="utf-8")) == {"shop_name": "Mi taller"}
    assert s.config_was_damaged and s.shop_name == ""


def test_dos_configs_danadas_no_se_pisan_entre_si(archivo, monkeypatch):
    class _Reloj:
        @staticmethod
        def now():
            from datetime import datetime
            return datetime(2026, 10, 15, 9, 30, 0)

    monkeypatch.setattr(config, "datetime", _Reloj)
    archivo.write_bytes(b"{ uno")
    config.Settings.load()
    archivo.write_bytes(b"{ dos")
    config.Settings.load()

    assert sorted(p.read_bytes() for p in _apartados(archivo)) == [b"{ dos", b"{ uno"]


def test_un_config_sano_no_se_aparta(archivo):
    archivo.write_text(json.dumps({"cover_region": "JA"}), encoding="utf-8")

    s = config.Settings.load()

    assert s.cover_region == "JA"
    assert not s.config_was_damaged and s.damaged_copy is None
    assert _apartados(archivo) == [] and archivo.exists()


def test_un_campo_invalido_no_aparta_el_archivo(archivo):
    """Validación por campo de siempre: se pierde solo ese campo."""
    archivo.write_text(json.dumps({"auto_scan_on_start": "si", "cover_region": "JA"}),
                       encoding="utf-8")

    s = config.Settings.load()

    assert s.cover_region == "JA" and not s.config_was_damaged
    assert _apartados(archivo) == []


@pytest.mark.parametrize("valor", ["", "   ", "WiiGames", "./Juegos"])
def test_library_path_vacio_o_relativo_no_pasa(archivo, valor):
    archivo.write_text(json.dumps({"library_path": valor, "cover_region": "JA"}),
                       encoding="utf-8")

    s = config.Settings.load()

    assert s.library_path == config.Settings().library_path
    assert Path(s.library_path).is_absolute()
    assert s.cover_region == "JA"


def test_library_path_con_tilde_se_expande(archivo):
    archivo.write_text(json.dumps({"library_path": "~/Juegos Wii"}), encoding="utf-8")

    assert config.Settings.load().library_path == str(Path.home() / "Juegos Wii")


# ------------------------------------------------------------- Con GTK --
@pytest.fixture(scope="module")
def gtk():
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        if os.environ.get("WBM_REQUIRE_GUI") == "1":
            pytest.fail("WBM_REQUIRE_GUI=1 pero no hay display")
        pytest.skip("sin display")
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("GTK no pudo inicializarse")
    return gi


@pytest.mark.gui
def test_preferencias_no_guarda_al_cerrar_si_no_cambio_nada(gtk, tmp_path):
    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    guardados = []
    dialogo = PreferencesDialog(config.Settings(library_path=str(tmp_path)),
                                guardados.append)
    dialogo.emit("closed")

    assert guardados == []


@pytest.mark.gui
def test_preferencias_guarda_al_cerrar_si_algo_cambio(gtk, tmp_path):
    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    guardados = []
    dialogo = PreferencesDialog(config.Settings(library_path=str(tmp_path)),
                                guardados.append)
    dialogo.settings.cover_region = "JA"
    dialogo.emit("closed")

    assert [s.cover_region for s in guardados] == ["JA"]


@pytest.mark.gui
def test_la_ventana_avisa_que_la_config_estaba_danada(gtk, tmp_path, monkeypatch):
    """La ventana entera: arranca con una config que `load` encontró
    dañada y muestra el aviso, con dónde quedó guardada."""
    from gi.repository import Adw, GLib

    from wiibackup_manager import oscwii_client
    from wiibackup_manager.styles import load_css
    from wiibackup_manager.window import WiiBackupWindow

    biblioteca = tmp_path / "biblioteca"
    biblioteca.mkdir()
    copia = tmp_path / "config.json.corrupto-2026-10-15-093000"

    def _load(cls):
        s = config.Settings(library_path=str(biblioteca))
        s.config_was_damaged, s.damaged_copy = True, copia
        return s

    monkeypatch.setattr(config.Settings, "load", classmethod(_load))
    monkeypatch.setattr(oscwii_client, "fetch_apps_async", lambda on_done=None: None)
    resultado = {}

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id="com.gamefixsps.WiiBackupManager.TestConfig")

        def do_activate(self):
            load_css()
            win = WiiBackupWindow(self)
            win.present()
            GLib.timeout_add(1500, self._revisar, win)

        def _revisar(self, win):
            try:
                resultado["visible"] = win._config_banner.get_revealed()
                resultado["texto"] = win._config_banner.get_title()
            finally:
                self.quit()
            return False

    GLib.timeout_add_seconds(60, lambda: (resultado.setdefault("colgada", True), False)[1])
    App().run([])

    assert not resultado.get("colgada")
    assert resultado.get("visible") is True
    assert str(copia) in resultado.get("texto", "")
