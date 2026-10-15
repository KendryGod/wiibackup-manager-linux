"""El aviso de carpetas que no se pudieron leer dice por qué.

Decía siempre "(permisos)". Si la biblioteca está en un USB que se
desconecta a mitad del escaneo, las carpetas fallan con ENOENT/EIO, los
juegos desaparecen de la lista y el aviso mandaba a revisar permisos. Ahora
`find_game_files` guarda también el error de cada carpeta, el escaneo
pregunta `drives.device_is_gone`, y el aviso dice desconexión, permisos o
el error real.
"""
from __future__ import annotations

import errno
import os
import types
from pathlib import Path

import pytest

from wiibackup_manager import drives, oplog, scanning


def _describir(skipped, causas, desconectada):
    from wiibackup_manager.window import WiiBackupWindow
    return WiiBackupWindow._describe_skipped(skipped, causas, desconectada)


@pytest.mark.skipif(os.geteuid() == 0, reason="root lee aunque no tenga permiso")
def test_find_game_files_guarda_el_error_de_cada_carpeta(tmp_path):
    cerrada = tmp_path / "cerrada"
    cerrada.mkdir()
    cerrada.chmod(0)
    try:
        salteadas, causas = [], []
        scanning.find_game_files(tmp_path, salteadas, causas)
    finally:
        cerrada.chmod(0o700)

    assert salteadas == [cerrada]
    assert [type(c) for c in causas] == [PermissionError]


def test_por_permisos_lo_dice():
    texto = _describir([Path("/b/x")], [PermissionError(errno.EACCES, "Permiso denegado")],
                       False)
    assert "permisos" in texto


def test_si_la_unidad_se_desconecto_lo_dice_y_no_habla_de_permisos():
    texto = _describir([Path("/b/x"), Path("/b/y")],
                       [OSError(errno.EIO, "Input/output error")] * 2, True)
    assert "desconect" in texto and "permisos" not in texto


def test_otro_error_dice_cual_y_no_habla_de_permisos():
    texto = _describir([Path("/b/x")], [OSError(errno.EIO, "Input/output error")],
                       False)
    assert "Input/output error" in texto and "permisos" not in texto


class _Hilo:
    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        self._target()


@pytest.mark.parametrize("parcial", [[], ["juego_que_alcanzo_a_leer"]],
                         ids=["vacio", "parcial"])
def test_el_escaneo_pregunta_si_la_unidad_se_fue(tmp_path, monkeypatch, parcial):
    """De punta a punta en la ventana: el escaneo saltea una carpeta, la
    unidad ya no responde y el aviso (y el historial) lo dicen. La lista
    que había se conserva, igual que cuando el escaneo falla: lo que
    alcanzó a leer (nada, o una parte) no describe la biblioteca."""
    from wiibackup_manager import window
    from wiibackup_manager.operations import OperationManager
    from wiibackup_manager.window import WiiBackupWindow

    monkeypatch.setattr(window.threading, "Thread", _Hilo)
    monkeypatch.setattr(window.GLib, "idle_add", lambda f, *a: f(*a) and False)

    def _scan(root, wit_binary, progress, skipped_dirs=None, skipped_errors=None):
        skipped_dirs.append(root / "wbfs")
        skipped_errors.append(OSError(errno.EIO, "Input/output error"))
        return list(parcial)

    monkeypatch.setattr(window.scanning, "scan_library", _scan)
    monkeypatch.setattr(drives, "device_is_gone", lambda **_k: True)
    historial = oplog.OperationLog(tmp_path / "h.json")
    toasts = []
    nada = lambda *a, **k: None   # noqa: E731
    ventana = types.SimpleNamespace(
        ops=OperationManager(log=historial), op_log=historial,
        settings=types.SimpleNamespace(library_path=str(tmp_path), wit_binary="wit"),
        _scan_generation=0, _rescan_pending=False, _skipped_dirs=set(), _games=[],
        progress_bar=types.SimpleNamespace(set_visible=nada, set_fraction=nada),
        set_title=nada, _show_toast=toasts.append, _apply_sort=nada,
        _populate_list=nada, stack=types.SimpleNamespace(set_visible_child_name=nada),
        transfer_view=types.SimpleNamespace(set_games=nada),
        _update_library_status_bar=nada)
    for nombre in ("rescan_library", "_on_scan_done"):
        setattr(ventana, nombre, types.MethodType(getattr(WiiBackupWindow, nombre), ventana))
    ventana._describe_skipped = WiiBackupWindow._describe_skipped

    ventana._games = ["juego_previo"]
    repintadas = []
    ventana._populate_list = lambda: repintadas.append(list(ventana._games))

    ventana.rescan_library()

    assert ventana._games == ["juego_previo"]
    assert repintadas == []
    [aviso] = toasts
    assert "desconect" in aviso and "permisos" not in aviso
    [entrada] = historial.entries()
    assert entrada.status == oplog.STATUS_DISCONNECTED
    assert "permiso" not in entrada.target
