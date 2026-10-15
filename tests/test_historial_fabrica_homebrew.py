"""Modo Fábrica y las instalaciones de Homebrew quedan en el historial.

Los dos cerraban su operación con `ops.finish(op)` sin `OperationOutcome`,
y sin resultado `OperationManager.finish` no anota nada: preparar una
unidad, o que fallara por haber cancelado la contraseña, no dejaba rastro
en la pestaña Log. El formateo de "Verificar Memoria" sí se anotaba.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from wiibackup_manager import drives, oplog
from wiibackup_manager.operations import OperationKind, OperationManager
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallResult, InstallStatus


class _Hilo:
    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        self._target()


def _widget():
    return types.SimpleNamespace(set_sensitive=lambda *_a: None,
                                 set_visible=lambda *_a: None,
                                 set_text=lambda *_a: None, pulse=lambda: None)


@pytest.fixture
def historial(tmp_path):
    return oplog.OperationLog(path=tmp_path / "history.json")


# ---------------------------------------------------------- Modo Fábrica --
@pytest.fixture
def fabrica(historial, monkeypatch):
    from wiibackup_manager import window
    from wiibackup_manager.window import WiiBackupWindow
    monkeypatch.setattr(window.threading, "Thread", _Hilo)
    monkeypatch.setattr(window.GLib, "idle_add", lambda f, *a: f(*a) and False)
    monkeypatch.setattr(window.GLib, "timeout_add", lambda *_a: 1)
    terminados = []
    ventana = types.SimpleNamespace(
        ops=OperationManager(log=historial), _show_toast=lambda _m: None,
        _factory_prepare_btn=_widget(), _factory_refresh_btn=_widget(),
        _factory_row=_widget(), _factory_progress=_widget(),
        _pulse_factory_progress=lambda: False,
        _on_factory_format_done=lambda ok, detalle: terminados.append((ok, detalle)))
    ventana._start_factory_format = types.MethodType(
        WiiBackupWindow._start_factory_format, ventana)
    ventana.terminados = terminados
    return ventana


def _disco():
    return drives.BlockDevice(path=Path("/dev/sdz"), model="Pendrive",
                              size_bytes=8 * 1024 ** 3, identity="X1")


def test_preparar_una_unidad_queda_en_el_historial(fabrica, historial, monkeypatch):
    monkeypatch.setattr(drives, "format_as_wii_usb",
                        lambda device: Path("/run/media/k/WII_USB"))

    fabrica._start_factory_format(_disco())

    [entrada] = historial.entries()
    assert entrada.operation == OperationKind.FORMATTING.value
    assert entrada.status == oplog.STATUS_OK
    assert entrada.target == "/dev/sdz" and "WII_USB" in entrada.detail
    assert fabrica.terminados == [(True, "/run/media/k/WII_USB")]


def test_un_formateo_que_falla_queda_en_el_historial(fabrica, historial, monkeypatch):
    def _falla(device):
        raise RuntimeError("No se obtuvo autorización para formatear")

    monkeypatch.setattr(drives, "format_as_wii_usb", _falla)

    fabrica._start_factory_format(_disco())

    [entrada] = historial.entries()
    assert entrada.status == oplog.STATUS_ERROR
    assert "autorización" in entrada.detail
    assert fabrica.ops.active_operations() == []


# ------------------------------------------------------------- Homebrew --
@pytest.mark.parametrize("resultado, estado", [
    (InstallResult(InstallStatus.OK, "WiiDonut", installed_paths=("a", "b")),
     oplog.STATUS_OK),
    (InstallResult(InstallStatus.CANCELLED, "WiiDonut", "instalación cancelada"),
     oplog.STATUS_CANCELLED),
    (InstallResult(InstallStatus.DEVICE_DISCONNECTED, "WiiDonut", "se desconectó"),
     oplog.STATUS_DISCONNECTED),
    (InstallResult(InstallStatus.BAD_ZIP, "WiiDonut", "el ZIP está corrupto"),
     oplog.STATUS_ERROR),
], ids=["ok", "cancelada", "desconectada", "error"])
def test_una_instalacion_de_homebrew_queda_en_el_historial(historial, tmp_path,
                                                           resultado, estado):
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    ops = OperationManager(log=historial)
    op = ops.start(OperationKind.INSTALLING_HOMEBREW, write=[tmp_path / "apps" / "x"])
    vista = types.SimpleNamespace(ops=ops, op_log=historial, _show_toast=lambda _m: None,
                                  _install_states={}, _card_by_slug={})

    HomebrewStoreView._on_install_done(
        vista, HomebrewApp(slug="WiiDonut", name="Wii Donut"), resultado, op)

    [entrada] = historial.entries()
    assert entrada.operation == OperationKind.INSTALLING_HOMEBREW.value
    assert entrada.target == "Wii Donut"
    assert entrada.status == estado
    # Cancelada no necesita detalle: el estado ya lo dice.
    if estado in (oplog.STATUS_ERROR, oplog.STATUS_DISCONNECTED):
        assert resultado.error in entrada.detail
