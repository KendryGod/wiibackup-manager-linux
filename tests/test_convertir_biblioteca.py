"""Convertir un juego de la Biblioteca pisando uno que ya existe
(`WiiBackupWindow._start_convert`).

Cómo se prueba: con el método REAL atado a un `self` de mentira, como en
`test_window_close.py`. El hilo de fondo corre en el acto (`Thread`
sincrónico) y `GLib.idle_add` llama en el acto, así que al volver de
`_start_convert` ya pasó todo lo que en la app pasaría en segundo plano.
`wit` es de mentira: escribe su temporal y lo renombra al nombre final,
como el de verdad.
"""
from __future__ import annotations

import errno
import subprocess
import types
from pathlib import Path

import pytest

from wiibackup_manager import (atomicfs, drives, fileops, library_ops, oplog,
                               transfer_plan, wit_wrapper)
from wiibackup_manager.game_model import Game
from wiibackup_manager.operations import OperationKind, OperationManager

ORIGINAL = b"juego original del usuario"
NUEVO = b"juego convertido, completo"


class _Hilo:
    """`threading.Thread` que corre `target` en el acto, al `start()`."""

    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        self._target()


class _Barra:
    def __init__(self):
        self.fracciones = []

    def set_fraction(self, valor):
        self.fracciones.append(valor)


class _VentanaDeMentira:
    def __init__(self, tmp_path):
        # Como en la app (`window.py`): el resultado de cada operación
        # queda en el historial al cerrarse.
        self.op_log = oplog.OperationLog(tmp_path / "historial.json")
        self.ops = OperationManager(log=self.op_log)
        self.settings = types.SimpleNamespace(wit_binary="wit")
        self.progress_bar = _Barra()
        self.toasts = []
        self.token = None
        from wiibackup_manager.window import WiiBackupWindow
        self._start_convert = types.MethodType(WiiBackupWindow._start_convert, self)

    def _begin_cancellable_progress(self, _titulo, _cancelando):
        self.token = wit_wrapper.CancellationToken()
        return self.token

    def _hide_progress(self):
        pass

    def _show_toast(self, mensaje):
        self.toasts.append(mensaje)

    def rescan_library(self):
        pass


@pytest.fixture
def ventana(tmp_path, monkeypatch):
    from wiibackup_manager import window
    monkeypatch.setattr(window.threading, "Thread", _Hilo)
    monkeypatch.setattr(window.GLib, "idle_add", lambda f, *a: f(*a) and False)
    monkeypatch.setattr(transfer_plan, "estimate_output_size",
                        lambda game, ext, binary: len(NUEVO))
    return _VentanaDeMentira(tmp_path)


@pytest.fixture
def juego(tmp_path):
    biblioteca = tmp_path / "biblioteca"
    biblioteca.mkdir()
    src = biblioteca / "Brawl.iso"
    src.write_bytes(b"x" * 64)
    dest = biblioteca / "Brawl.wbfs"
    dest.write_bytes(ORIGINAL)          # el usuario confirmó pisarlo
    game = Game(path=src, game_id="RSBE01", title="Super Smash Bros. Brawl",
                fmt="ISO", size_bytes=64, identified_by="iso", console="wii")
    return game, dest


def _wit_que_convierte(monkeypatch, al_convertir=None):
    def convert(src, dest, fmt, binary, **kw):
        dest = Path(dest)
        tmp = dest.parent / f".{dest.name}.AbCdEf.tmp"
        tmp.write_bytes(NUEVO)
        if al_convertir is not None:
            al_convertir(kw)
        tmp.replace(dest)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(wit_wrapper, "convert", convert)


# ------------------------------------------------- A: bajar antes de borrar --
def test_lo_convertido_se_baja_a_disco_antes_de_borrar_el_original(
        ventana, juego, monkeypatch):
    """`wit` termina con lo escrito en la caché del kernel. Si el respaldo
    del original se borra antes de bajarlo, un corte de luz o un USB que
    se desenchufa en ese momento deja al usuario sin el juego viejo y con
    el nuevo a medias. Mismo orden que `library_ops._write_to_drive`."""
    game, dest = juego
    _wit_que_convierte(monkeypatch)
    eventos = []

    def flush_anotado(paths, cancel=None, on_progress=None):
        eventos.append(("baja", sorted(Path(p).name for p in paths)))

    real_discard = atomicfs.SetAside.discard

    def discard_anotado(self):
        eventos.append(("borra el respaldo", None))
        return real_discard(self)

    monkeypatch.setattr(fileops, "flush_and_drop_cache", flush_anotado)
    monkeypatch.setattr(atomicfs.SetAside, "discard", discard_anotado)

    ventana._start_convert(game, dest, ".wbfs")

    assert [e[0] for e in eventos] == ["baja", "borra el respaldo"], eventos
    assert eventos[0][1] == ["Brawl.wbfs"]
    assert dest.read_bytes() == NUEVO
    assert not [p for p in dest.parent.iterdir() if "respaldo" in p.name]


def test_si_bajar_a_disco_falla_el_original_vuelve(ventana, juego, monkeypatch):
    """Si la unidad no acepta lo escrito (error de E/S al bajarlo), la
    conversión no se da por buena y el original vuelve a su lugar."""
    game, dest = juego
    _wit_que_convierte(monkeypatch)

    def flush_que_falla(paths, cancel=None, on_progress=None):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(fileops, "flush_and_drop_cache", flush_que_falla)

    ventana._start_convert(game, dest, ".wbfs")

    assert dest.read_bytes() == ORIGINAL
    assert not [p for p in dest.parent.iterdir() if p.name.startswith(".")]
    assert ventana.toasts and "Error al convertir" in ventana.toasts[-1]


# ---------------------------------------------- D: biblioteca desenchufada --
def test_desconexion_a_mitad_de_un_reemplazo_dice_que_el_original_quedo(
        ventana, juego, monkeypatch):
    """La biblioteca puede estar en un USB. Si se desenchufa mientras se
    convierte pisando un juego, el original queda apartado en la unidad:
    se dice eso y queda como "Unidad desconectada", igual que en la cola,
    y no como un error con rutas internas."""
    game, dest = juego
    biblioteca = dest.parent
    ida = {"si": False}
    real_replace, real_responde = atomicfs.os.replace, drives._responde

    def replace(a, b, *k, **kw):
        if ida["si"]:
            raise OSError(errno.EIO, "Input/output error")
        return real_replace(a, b, *k, **kw)

    def responde(path):
        if ida["si"] and str(biblioteca) in str(path):
            return False
        return real_responde(path)

    def wit_que_corta(src, dest, fmt, binary, **kw):
        ida["si"] = True
        return subprocess.CompletedProcess([], 1, "", "wit: write error")

    monkeypatch.setattr(atomicfs.os, "replace", replace)
    monkeypatch.setattr(drives, "_responde", responde)
    monkeypatch.setattr(wit_wrapper, "convert", wit_que_corta)

    ventana._start_convert(game, dest, ".wbfs")

    assert ventana.toasts[-1] == library_ops.original_kept_message()
    [conversion] = [e for e in ventana.op_log.entries()
                    if e.operation == OperationKind.CONVERTING.value]
    assert conversion.status == oplog.STATUS_DISCONNECTED
    # Los datos no cambian: el original sigue entero, apartado.
    ida["si"] = False
    [respaldo] = [p for p in biblioteca.iterdir() if ".respaldo-" in p.name]
    assert respaldo.read_bytes() == ORIGINAL
