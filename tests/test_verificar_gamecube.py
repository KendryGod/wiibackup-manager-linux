""""Verificar integridad" no da un veredicto sobre juegos de GameCube.

`wit VERIFY` solo sabe revisar discos de Wii (ver
`wit_wrapper.verify_result`). Medido con `wit` v3.05a: el CISO real de
Ocarina of Time, sano, sale con error ("No valid source file found"), y un
GameCube dentro de un WBFS con datos rotos sale OK. La Biblioteca mostraba
lo uno como "con errores ✗" y lo otro como "verificado OK ✓".

Ahora el menú de la fila no ofrece verificar un GameCube (ISO, CISO o
dentro de un WBFS), el lote los omite con un aviso, y si igual se llega a
pedir, se avisa en vez de correr `wit VERIFY`. Los de Wii, igual que antes.
"""
from __future__ import annotations

import shutil
import subprocess
import types
from pathlib import Path

import pytest

from wiibackup_manager import oplog, wit_wrapper
from wiibackup_manager.game_model import Game
from wiibackup_manager.operations import OperationKind, OperationManager


class _Hilo:
    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        self._target()


def _juego(tmp_path: Path, nombre: str, console: str, game_id: str) -> Game:
    p = tmp_path / nombre
    if not p.exists():
        p.write_bytes(b"datos")
    return Game(path=p, game_id=game_id, title=p.stem,
                fmt=p.suffix.lstrip(".").upper(), size_bytes=p.stat().st_size,
                identified_by="wit", console=console)


def _gc(tmp_path, nombre="Zelda.ciso"):
    return _juego(tmp_path, nombre, "gc", "D43E01")


def _wii(tmp_path, nombre="Brawl.wbfs"):
    return _juego(tmp_path, nombre, "wii", "RSBE01")


# ------------------------------------------------------- Menú de la fila --
def _etiquetas(game):
    from wiibackup_manager.widgets.game_row import GameRow
    menu = GameRow._build_menu(types.SimpleNamespace(game=game))
    return [menu.get_item_attribute_value(i, "label").get_string()
            for i in range(menu.get_n_items())]


@pytest.mark.parametrize("nombre", ["Zelda.ciso", "Zelda.iso", "Zelda.wbfs"])
def test_el_menu_no_ofrece_verificar_un_gamecube(tmp_path, nombre):
    assert "Verificar integridad" not in _etiquetas(_gc(tmp_path, nombre))


def test_el_menu_sigue_ofreciendo_verificar_un_wii(tmp_path):
    assert "Verificar integridad" in _etiquetas(_wii(tmp_path))


# ------------------------------------------------------------- Ventana --
class _Ventana:
    def __init__(self, tmp_path):
        from wiibackup_manager.window import WiiBackupWindow
        self.op_log = oplog.OperationLog(tmp_path / "historial.json")
        self.ops = OperationManager(log=self.op_log)
        self.settings = types.SimpleNamespace(wit_binary="wit")
        self.progress_bar = types.SimpleNamespace(set_fraction=lambda _f: None,
                                                  set_text=lambda _t: None)
        self.toasts = []
        self.seleccion = []
        for nombre in ("_on_verify_requested", "_on_batch_verify", "_run_batch",
                       "_on_batch_done"):
            setattr(self, nombre, types.MethodType(getattr(WiiBackupWindow, nombre), self))
        self._describe_target = WiiBackupWindow._describe_target
        self._batch_outcome = WiiBackupWindow._batch_outcome

    def _selected_games(self):
        return list(self.seleccion)

    def _reject_if_busy(self, *_a, **_k):
        return False

    def _begin_cancellable_progress(self, *_a):
        return wit_wrapper.CancellationToken()

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
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b="wit": True)
    return _Ventana(tmp_path)


@pytest.fixture
def verify_falso(monkeypatch):
    """`wit VERIFY` de mentira: anota a quién se le pidió y contesta OK."""
    pedidos = []

    def verify(path, binary="wit", timeout=None, cancel=None):
        pedidos.append(Path(path).name)
        return True, "+OK"

    monkeypatch.setattr(wit_wrapper, "verify", verify)
    return pedidos


def _verificar(ventana, juego):
    ventana._on_verify_requested(types.SimpleNamespace(game=juego))


@pytest.mark.parametrize("nombre", ["Zelda.ciso", "Zelda.iso", "Zelda.wbfs"])
def test_verificar_un_gamecube_avisa_en_vez_de_correr_wit(ventana, tmp_path,
                                                          verify_falso, nombre):
    _verificar(ventana, _gc(tmp_path, nombre))

    assert verify_falso == []
    [aviso] = ventana.toasts
    assert "GameCube" in aviso and "con errores" not in aviso
    assert ventana.ops.active_operations() == []
    assert ventana.op_log.entries() == []


def test_verificar_un_wii_sigue_igual(ventana, tmp_path, verify_falso):
    _verificar(ventana, _wii(tmp_path))

    assert verify_falso == ["Brawl.wbfs"]
    assert ventana.toasts == ["'Brawl' verificado OK ✓"]
    [entrada] = ventana.op_log.entries()
    assert entrada.operation == OperationKind.VERIFYING.value
    assert entrada.status == oplog.STATUS_OK


def test_el_lote_omite_los_gamecube_con_un_aviso(ventana, tmp_path, verify_falso):
    ventana.seleccion = [_gc(tmp_path), _wii(tmp_path), _gc(tmp_path, "Viejo.wbfs")]

    ventana._on_batch_verify()

    assert verify_falso == ["Brawl.wbfs"]
    [resumen] = ventana.toasts
    assert "2 omitido(s)" in resumen
    assert "Zelda" in resumen and "Viejo" in resumen and "GameCube" in resumen
    [entrada] = ventana.op_log.entries()
    assert entrada.status == oplog.STATUS_OK


def test_un_lote_solo_de_gamecube_avisa_sin_arrancar(ventana, tmp_path, verify_falso):
    ventana.seleccion = [_gc(tmp_path), _gc(tmp_path, "Viejo.wbfs")]

    ventana._on_batch_verify()

    assert verify_falso == []
    [aviso] = ventana.toasts
    assert "GameCube" in aviso
    assert ventana.ops.active_operations() == []
    assert ventana.op_log.entries() == []


@pytest.mark.skipif(shutil.which("wit") is None, reason="wit no está instalado")
def test_con_wit_real_un_gamecube_sano_no_sale_con_errores(ventana, tmp_path):
    """El caso de la auditoría con el `wit` de verdad: un CISO de GameCube
    sano. Antes salía "con errores ✗"."""
    from test_convertir_gamecube_ciso_iso import _imagen_gc

    iso = tmp_path / "origen.iso"
    _imagen_gc(iso)
    ciso = tmp_path / "Prueba.ciso"
    subprocess.run(["wit", "COPY", str(iso), "--dest", str(ciso)],
                   check=True, capture_output=True)
    juego = Game(path=ciso, game_id="GTST01", title="Prueba", fmt="CISO",
                 size_bytes=ciso.stat().st_size, identified_by="wit", console="gc")

    _verificar(ventana, juego)

    assert ventana.toasts and "con errores" not in ventana.toasts[-1]
    assert not [e for e in ventana.op_log.entries() if e.status == oplog.STATUS_ERROR]
