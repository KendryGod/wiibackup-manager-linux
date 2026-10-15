"""Textos que llegaban a la interfaz solo en español.

Errores del instalador de Homebrew y del catálogo de Open Shop Channel,
mensajes de formateo de `drives` (los que no arma `format_fat32`), errores
de las configuraciones maestras, el "Desconocido" del PDF del ticket, los
nombres de tres entradas del historial y varios avisos de la ventana
(convertir, verificar, renombrar, agregar juegos, guardar la
configuración) se escribían sin `_()`: con la app en inglés aparecían en
español en medio de una interfaz traducida.

Acá se comprueba lo que vería alguien con la app en inglés: el texto que
sale de verdad de cada función, con el catálogo inglés cargado.
"""
from __future__ import annotations

import ast
import gettext
import types
from pathlib import Path, PurePosixPath

import pytest

from test_smoke_gui import _poner_el_catalogo_en_ingles

RAIZ = Path(__file__).resolve().parent.parent
LOCALE = RAIZ / "data" / "locale"


@pytest.fixture
def en():
    return gettext.translation("wiibackup-manager", str(LOCALE), languages=["en"])


@pytest.fixture
def en_toda_la_app(monkeypatch, en):
    _poner_el_catalogo_en_ingles(monkeypatch, en)
    return en


def _sin_espanol(texto: str) -> bool:
    """Sin las palabras en español que delataban estos mensajes."""
    palabras = ("rechazada", "no se pudo", "no hay", "unidad", "juegos", "archivo",
                "maestro", "cancelada", "convertido", "verificado", "con errores",
                "configuración", "Desconocido", "dispositivo", "montaje", "formateo")
    return not any(p.lower() in texto.lower() for p in palabras)


# ------------------------------------------------------------ Historial --
def test_los_nombres_del_historial_estan_traducidos(en):
    from wiibackup_manager import golden_configs, oplog

    for nombre in (oplog.ORPHANED_BACKUP_OPERATION, oplog.UNRESTORED_BACKUP_OPERATION,
                   golden_configs.APPLIED_OPERATION, golden_configs.KEPT_OPERATION):
        assert en.gettext(nombre) != nombre, nombre


# ------------------------------------------------- Mensajes de verdad --
def test_el_instalador_de_homebrew_habla_ingles(en_toda_la_app, tmp_path):
    from wiibackup_manager import oscwii_client, oscwii_installer
    from wiibackup_manager.oscwii_client import HomebrewApp

    r = oscwii_installer.install_app(
        HomebrewApp(slug="x", name="X", zip_url="http://otro.example/x.zip"), tmp_path)
    assert "rejected" in r.error and _sin_espanol(r.error), r.error
    motivo = oscwii_client.url_rejection_reason("ftp://hbb1.oscwii.org/x.zip")
    assert "not allowed" in motivo and _sin_espanol(motivo), motivo


def test_el_catalogo_de_osc_habla_ingles(en_toda_la_app):
    from wiibackup_manager import oscwii_client

    apps, motivo = oscwii_client._parse_apps(b"{ roto")
    assert apps is None and "invalid JSON" in motivo and _sin_espanol(motivo), motivo


def test_las_configuraciones_maestras_hablan_ingles(en_toda_la_app, tmp_path,
                                                     monkeypatch):
    from wiibackup_manager import golden_configs
    from wiibackup_manager.golden_configs import GoldenConfigSpec
    from wiibackup_manager.oscwii_client import HomebrewApp
    from wiibackup_manager.oscwii_installer import InstallResult, InstallStatus

    monkeypatch.setattr(golden_configs, "_candidate_asset_dirs", lambda: [tmp_path])
    registro = {"Nintendont": GoldenConfigSpec(
        label="Nintendont", asset_relative="no-esta/nincfg.bin",
        dest_relative=PurePosixPath("nincfg.bin"), min_size_bytes=32)}
    (tmp_path / "usb").mkdir()

    r = golden_configs.maybe_apply(HomebrewApp(slug="Nintendont", name="Nintendont"),
                                   tmp_path / "usb",
                                   InstallResult(InstallStatus.OK, "Nintendont"),
                                   registry=registro)
    assert r.status.is_error and _sin_espanol(r.error), r.error


def test_el_ticket_dice_unknown(en_toda_la_app):
    from wiibackup_manager import ticket_service

    assert ticket_service.filesystem_label(None) == "Unknown"


def test_los_blindajes_del_formateo_hablan_ingles(en_toda_la_app, tmp_path,
                                                  monkeypatch):
    from wiibackup_manager import drives

    monkeypatch.setattr(drives, "is_removable_block_device", lambda _p: False)
    with pytest.raises(drives.UnsafeDeviceError) as error:
        drives.verify_still_safe(drives.BlockDevice(
            path=Path("/dev/sdz"), model="X", size_bytes=1, identity=None))
    assert _sin_espanol(str(error.value)), str(error.value)


class _Hilo:
    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        self._target()


def test_los_avisos_de_la_ventana_hablan_ingles(en_toda_la_app, tmp_path, monkeypatch):
    from wiibackup_manager import oplog, wit_wrapper, window
    from wiibackup_manager.game_model import Game
    from wiibackup_manager.operations import OperationManager
    from wiibackup_manager.window import WiiBackupWindow

    monkeypatch.setattr(window.threading, "Thread", _Hilo)
    monkeypatch.setattr(window.GLib, "idle_add", lambda f, *a: f(*a) and False)
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b="wit": True)
    monkeypatch.setattr(wit_wrapper, "verify", lambda *a, **k: (False, "malo"))
    toasts = []
    ventana = types.SimpleNamespace(
        settings=types.SimpleNamespace(wit_binary="wit"), _show_toast=toasts.append,
        ops=OperationManager(log=oplog.OperationLog(tmp_path / "h.json")),
        _selected_games=lambda: [], _visible_games=lambda: [])
    juego = tmp_path / "Brawl.wbfs"
    juego.write_bytes(b"x")

    WiiBackupWindow._on_verify_requested(ventana, types.SimpleNamespace(game=Game(
        path=juego, game_id="RSBE01", title="Brawl", fmt="WBFS", size_bytes=1,
        identified_by="wit")))
    WiiBackupWindow._on_rename_all(ventana)

    assert len(toasts) == 2
    for aviso in toasts:
        assert _sin_espanol(aviso), aviso


# ------------------------------------------------------------- Guarda --
def test_ningun_aviso_lleva_texto_sin_traducir():
    """Ningún `_show_toast(...)` del código recibe un texto literal sin
    pasar por `_()`: así un aviso nuevo no vuelve a salir solo en español."""
    problemas = []
    for py in sorted((RAIZ / "wiibackup_manager").rglob("*.py")):
        for nodo in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if not (isinstance(nodo, ast.Call) and isinstance(nodo.func, ast.Attribute)
                    and nodo.func.attr in ("_show_toast", "show_toast")):
                continue
            for arg in nodo.args:
                partes = ([arg] if isinstance(arg, ast.Constant)
                          else [v for v in getattr(arg, "values", [])
                                if isinstance(v, ast.Constant)])
                if any(isinstance(p.value, str) and any(c.isalpha() for c in p.value)
                       for p in partes):
                    problemas.append(f"{py.relative_to(RAIZ)}:{nodo.lineno}")
    assert not problemas, problemas
