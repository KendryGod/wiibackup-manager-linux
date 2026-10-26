"""Las configuraciones maestras se buscan primero en una carpeta elegida en
Preferencias.

Los archivos maestros se buscaban solo donde instala la app (el repo,
/usr/share, ~/.local/share...), y el primero que existía ganaba aunque
fuera el de relleno del repositorio: no había forma de apuntar a una
carpeta propia del taller. Ahora `Settings.golden_configs_dir`, si está,
va primero, con la misma estructura que `assets/configs`
(`nintendont/nincfg.bin`, `usbloadergx/GXGlobal.cfg`). Un archivo de
relleno ahí no gana: se sigue buscando como antes. El resto no cambia:
solo Nintendont y USB Loader GX, las mismas comprobaciones de tamaño y
firma, y nunca se pisa una configuración que ya esté en la unidad.
"""
from __future__ import annotations

import types

import pytest

from wiibackup_manager import config, golden_configs
from wiibackup_manager.golden_configs import GoldenConfigStatus
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallResult, InstallStatus

NINTENDONT = HomebrewApp(slug="Nintendont", name="Nintendont")
SPEC = golden_configs.GOLDEN_CONFIGS["Nintendont"]


def _nincfg(marca: bytes) -> bytes:
    """Un nincfg.bin con la firma y el tamaño mínimo de uno de verdad."""
    cuerpo = golden_configs.NIN_CFG_MAGIC + marca
    return cuerpo.ljust(SPEC.min_size_bytes + 16, b"\0")


@pytest.fixture
def instalado(tmp_path, monkeypatch):
    """Lo que trae la app instalada: un nincfg.bin propio en su carpeta."""
    base = tmp_path / "instalado"
    (base / "nintendont").mkdir(parents=True)
    (base / "nintendont" / "nincfg.bin").write_bytes(_nincfg(b"de la app"))
    monkeypatch.setattr(golden_configs, "_candidate_asset_dirs", lambda: [base])
    return base


@pytest.fixture
def local(tmp_path):
    carpeta = tmp_path / "mis-configs"
    (carpeta / "nintendont").mkdir(parents=True)
    return carpeta


def test_el_ajuste_esta_vacio_por_defecto_y_se_guarda(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")

    assert config.Settings().golden_configs_dir == ""
    config.Settings(golden_configs_dir="/home/taller/configs").save()
    assert config.Settings.load().golden_configs_dir == "/home/taller/configs"


# ---------------------------------------------------------- búsqueda --
def test_la_carpeta_local_va_primero(instalado, local):
    (local / "nintendont" / "nincfg.bin").write_bytes(_nincfg(b"del taller"))

    assert golden_configs.find_asset("nintendont/nincfg.bin", str(local)) == (
        local / "nintendont" / "nincfg.bin")


def test_sin_carpeta_local_se_busca_como_siempre(instalado):
    esperado = instalado / "nintendont" / "nincfg.bin"

    assert golden_configs.find_asset("nintendont/nincfg.bin") == esperado
    assert golden_configs.find_asset("nintendont/nincfg.bin", "") == esperado


def test_si_la_carpeta_local_no_lo_tiene_se_sigue_buscando(instalado, local):
    assert golden_configs.find_asset("nintendont/nincfg.bin", str(local)) == (
        instalado / "nintendont" / "nincfg.bin")


def test_un_relleno_en_la_carpeta_local_no_gana(instalado, local):
    (local / "nintendont" / "nincfg.bin").write_bytes(b"PLACEHOLDER " + _nincfg(b"x"))

    assert golden_configs.find_asset("nintendont/nincfg.bin", str(local)) == (
        instalado / "nintendont" / "nincfg.bin")


# ------------------------------------------------------------ aplicar --
def _instalada():
    return InstallResult(InstallStatus.OK, "Nintendont")


def test_se_copia_la_de_la_carpeta_local(instalado, local, tmp_path):
    maestro = _nincfg(b"del taller")
    (local / "nintendont" / "nincfg.bin").write_bytes(maestro)
    unidad = tmp_path / "usb"
    unidad.mkdir()

    r = golden_configs.maybe_apply(NINTENDONT, unidad, _instalada(), local_dir=str(local))

    assert r.status is GoldenConfigStatus.APPLIED, r
    assert (unidad / "nincfg.bin").read_bytes() == maestro


def test_tampoco_pisa_una_que_ya_este_en_la_unidad(instalado, local, tmp_path):
    (local / "nintendont" / "nincfg.bin").write_bytes(_nincfg(b"del taller"))
    unidad = tmp_path / "usb"
    unidad.mkdir()
    (unidad / "nincfg.bin").write_bytes(b"la del cliente")

    r = golden_configs.maybe_apply(NINTENDONT, unidad, _instalada(), local_dir=str(local))

    assert r.status is GoldenConfigStatus.KEPT_EXISTING
    assert (unidad / "nincfg.bin").read_bytes() == b"la del cliente"


def test_una_rota_en_la_carpeta_local_no_se_copia(instalado, local, tmp_path):
    (local / "nintendont" / "nincfg.bin").write_bytes(b"corto")
    unidad = tmp_path / "usb"
    unidad.mkdir()

    r = golden_configs.maybe_apply(NINTENDONT, unidad, _instalada(), local_dir=str(local))

    assert r.status is GoldenConfigStatus.ASSET_CORRUPT
    assert not (unidad / "nincfg.bin").exists()


# ----------------------------------------------------------- la Store --
class _Hilo:
    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


def test_la_store_le_pasa_la_carpeta_elegida(monkeypatch, tmp_path):
    from wiibackup_manager import oscwii_installer
    from wiibackup_manager.operations import OperationManager
    from wiibackup_manager.widgets import homebrew_store_view as vista_mod
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    pedidos = []
    monkeypatch.setattr(vista_mod.threading, "Thread", _Hilo)
    monkeypatch.setattr(vista_mod.GLib, "idle_add", lambda f, *a: f(*a))
    monkeypatch.setattr(oscwii_installer, "install_app",
                        lambda app, dest, **_k: InstallResult(InstallStatus.OK, app.slug))
    monkeypatch.setattr(golden_configs, "maybe_apply",
                        lambda app, dest, res, op_log=None, local_dir=None: pedidos.append(local_dir))
    vista = types.SimpleNamespace(
        _dest_path=tmp_path, ops=OperationManager(), op_log=None,
        settings=config.Settings(golden_configs_dir="/home/taller/configs"),
        _show_toast=lambda _m: None, _install_states={}, _card_by_slug={},
        _apply_install_state=lambda *_a: None, _on_install_progress=lambda *_a: None,
        _on_install_done=lambda *_a: None)

    HomebrewStoreView._start_install(vista, NINTENDONT)

    assert pedidos == ["/home/taller/configs"]


# ------------------------------------------------------- Preferencias --
class _Fila:
    def __init__(self):
        self.subtitulo = None

    def set_subtitle(self, texto):
        self.subtitulo = texto


class _Boton:
    def __init__(self):
        self.activo = None

    def set_sensitive(self, valor):
        self.activo = valor


def _preferencias(valor=""):
    return types.SimpleNamespace(settings=config.Settings(golden_configs_dir=valor),
                                 _golden_row=_Fila(), _golden_clear_btn=_Boton())


def test_elegir_la_carpeta_la_guarda_y_la_muestra(tmp_path):
    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    falso = _preferencias()

    PreferencesDialog._usar_carpeta_de_configs(falso, str(tmp_path))

    assert falso.settings.golden_configs_dir == str(tmp_path)
    assert falso._golden_row.subtitulo == str(tmp_path)
    assert falso._golden_clear_btn.activo is True


def test_quitar_la_carpeta_vuelve_a_las_de_la_app(tmp_path):
    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    falso = _preferencias(str(tmp_path))

    PreferencesDialog._usar_carpeta_de_configs(falso, "")

    assert falso.settings.golden_configs_dir == ""
    assert "No configurada" in falso._golden_row.subtitulo
    assert falso._golden_clear_btn.activo is False


def test_la_carpeta_elegida_en_el_dialogo_se_usa(tmp_path):
    from gi.repository import Gio

    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    usadas = []
    falso = types.SimpleNamespace(_usar_carpeta_de_configs=usadas.append)
    elegido = types.SimpleNamespace(select_folder_finish=lambda _r: Gio.File.new_for_path(str(tmp_path)))

    PreferencesDialog._on_golden_dir_picked(falso, elegido, None)

    assert usadas == [str(tmp_path)]


@pytest.mark.gui
def test_la_fila_esta_en_preferencias(tmp_path):
    import os
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("sin display")
    from gi.repository import Adw, Gtk
    if not Gtk.init_check():
        pytest.skip("GTK no pudo inicializarse")
    Adw.init()
    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    guardados = []
    dialogo = PreferencesDialog(config.Settings(golden_configs_dir=str(tmp_path)),
                                guardados.append)

    assert dialogo._golden_row.get_subtitle() == str(tmp_path)
    assert dialogo._golden_row.get_parent() is not None
    assert dialogo._golden_clear_btn.get_sensitive() is True
    dialogo._usar_carpeta_de_configs("")
    dialogo.emit("closed")
    [guardado] = guardados
    assert guardado.golden_configs_dir == ""
