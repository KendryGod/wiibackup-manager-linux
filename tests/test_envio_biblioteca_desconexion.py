"""Enviar desde la Biblioteca (`WiiBackupWindow._start_send`) a un punto
de montaje fijo (`/mnt/usb`) que se desconecta a mitad de un reemplazo.

La carpeta queda vacía y responde: la única señal es que dejó de ser punto
de montaje. La cola ya la miraba (`raiz_era_montaje`); este envío llamaba a
`replace_cut_by_disconnect` sin `mount_point` y mostraba "No se pudo
restaurar…" con rutas internas en vez de "el original quedó guardado".
"""
from __future__ import annotations

import types
from pathlib import Path

from wiibackup_manager import drives, library_ops, transfer_plan, wit_wrapper
from wiibackup_manager.operations import OperationManager


class _Hilo:
    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        self._target()


def _enviar(monkeypatch, tmp_path, make_game, falla_con):
    from wiibackup_manager import window
    raiz = tmp_path / "mnt_usb"
    raiz.mkdir()
    ida = {"si": False}
    real = drives.is_mount_point
    monkeypatch.setattr(drives, "is_mount_point",
                        lambda p: (not ida["si"]) if Path(p) == raiz else real(p))
    monkeypatch.setattr(window.threading, "Thread", _Hilo)
    monkeypatch.setattr(window.GLib, "idle_add", lambda f, *a: f(*a) and False)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)
    monkeypatch.setattr(transfer_plan, "plan_transfer",
                        lambda games, binary: [types.SimpleNamespace(game=g, output_bytes=64)
                                               for g in games])

    def send(*_a, **_kw):
        ida["si"] = True
        raise falla_con

    monkeypatch.setattr(library_ops, "send_to_wbfs_drive", send)
    resultado = {}
    ventana = types.SimpleNamespace(
        ops=OperationManager(), op_log=None,
        settings=types.SimpleNamespace(wit_binary="wit"),
        progress_bar=types.SimpleNamespace(set_text=lambda _t: None,
                                           set_fraction=lambda _f: None),
        _show_toast=lambda _m: None,
        _begin_cancellable_progress=lambda *_a: wit_wrapper.CancellationToken(),
        _describe_target=lambda games: "x",
        _update_send_progress=lambda *a: None,
        _on_send_done=lambda ok, errors, cancelled, skipped, op, target:
            resultado.update(errors=errors))
    juego = make_game(name="juego.wbfs", fmt="WBFS")
    window.WiiBackupWindow._start_send(ventana, [juego], raiz, overwrite=True)
    return resultado["errors"]


def test_reemplazo_cortado_por_desconexion_dice_que_el_original_quedo(
        monkeypatch, tmp_path, make_game):
    corte = library_ops.RollbackFailedError(
        [(tmp_path / "x.wbfs", tmp_path / ".x.wbfs.respaldo-1")],
        original_error=RuntimeError("wit: write error"))
    errores = _enviar(monkeypatch, tmp_path, make_game, corte)
    assert len(errores) == 1
    assert library_ops.original_kept_message() in errores[0]
