"""Convertir un juego de GameCube no produce un WBFS.

"Convertir ISO ↔ WBFS" elegía el destino solo por el formato
(`.wbfs` si no era WBFS), sin mirar la consola: un CISO de GameCube
(D43E01) salía como `D43E01.wbfs`, que Nintendont no lee -lee `game.iso`
o `game.ciso`- y que quedaba duplicado en la Biblioteca. Pasaba por el
menú de la fila y por "Convertir" en lote. La cola y Transferir no
convierten GameCube: lo copian tal cual (se comprueba acá también).

Un GameCube en CISO o ISO se convierte entre esos dos formatos (ver
`test_convertir_gamecube_ciso_iso.py`); en cualquier otro formato (un
`.wbfs` que dejó una versión anterior) no se convierte.
"""
from __future__ import annotations

import subprocess
import time
import types
from pathlib import Path

import pytest

from wiibackup_manager import library_ops, transfer_plan, wit_wrapper
from wiibackup_manager.game_model import Game
from wiibackup_manager.operations import OperationManager
from wiibackup_manager.queue_manager import JobStatus, TransferQueue


def _gc(tmp_path, nombre="Zelda.ciso"):
    p = tmp_path / "biblioteca" / nombre
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"gamecube ciso")
    return Game(path=p, game_id="D43E01", title="Zelda OoT MQ",
                fmt=p.suffix.lstrip(".").upper(),
                size_bytes=p.stat().st_size, identified_by="wit", console="gc")


def _wii(tmp_path, nombre="Brawl.iso"):
    p = tmp_path / "biblioteca" / nombre
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"wii iso")
    return Game(path=p, game_id="RSBE01", title="Brawl", fmt="ISO",
                size_bytes=p.stat().st_size, identified_by="iso", console="wii")


class _Hilo:
    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        self._target()


@pytest.fixture
def wit_falso(monkeypatch):
    """`wit COPY` de mentira que deja el archivo de salida y anota a qué
    formato se convirtió cada cosa."""
    convertidos = []

    def convert(src, dest, fmt, binary, **_kw):
        Path(dest).write_bytes(b"convertido")
        convertidos.append((Path(src).name, Path(dest).name))
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(wit_wrapper, "convert", convert)
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b: True)
    monkeypatch.setattr(transfer_plan, "estimate_output_size", lambda g, e, b: 10)
    return convertidos


# ------------------------------------------------------- Menú de la fila --
def _desde_la_fila(juego):
    from wiibackup_manager import window
    toasts, convertidas = [], []
    ventana = types.SimpleNamespace(
        settings=types.SimpleNamespace(wit_binary="wit"),
        _show_toast=toasts.append,
        _reject_if_busy=lambda *a, **k: False,
        _start_convert=lambda game, dest, ext: convertidas.append(dest.name))
    window.WiiBackupWindow._on_convert_requested(ventana, types.SimpleNamespace(game=juego))
    return toasts, convertidas


def test_convertir_desde_la_fila_no_hace_un_wbfs_de_gamecube(tmp_path, wit_falso):
    """Un CISO de GameCube va a ISO, nunca a WBFS."""
    toasts, convertidas = _desde_la_fila(_gc(tmp_path))

    assert convertidas == ["Zelda.iso"]
    assert toasts == []
    assert not list((tmp_path / "biblioteca").glob("*.wbfs"))


def test_un_wbfs_de_gamecube_no_se_convierte_desde_la_fila(tmp_path, wit_falso):
    toasts, convertidas = _desde_la_fila(_gc(tmp_path, "Zelda.wbfs"))

    assert convertidas == [], "lanzó la conversión de un WBFS de GameCube"
    assert len(toasts) == 1 and "GameCube" in toasts[0]


def test_el_menu_de_un_juego_de_gamecube_no_ofrece_convertir_a_wbfs(tmp_path):
    from wiibackup_manager.widgets.game_row import GameRow

    def etiquetas(game):
        menu = GameRow._build_menu(types.SimpleNamespace(game=game))
        return [menu.get_item_attribute_value(i, "label").get_string()
                for i in range(menu.get_n_items())]

    assert "Convertir ISO ↔ WBFS" not in etiquetas(_gc(tmp_path))
    assert "Convertir CISO ↔ ISO" in etiquetas(_gc(tmp_path))
    assert not [e for e in etiquetas(_gc(tmp_path, "Zelda.wbfs")) if e.startswith("Convertir")]
    assert "Convertir ISO ↔ WBFS" in etiquetas(_wii(tmp_path))


# --------------------------------------------------------------- Lote --
def test_convertir_en_lote_saltea_gamecube_y_convierte_wii(
        tmp_path, wit_falso, monkeypatch):
    from wiibackup_manager import window
    monkeypatch.setattr(window.threading, "Thread", _Hilo)
    monkeypatch.setattr(window.GLib, "idle_add", lambda f, *a: f(*a) and False)
    gc, wii = _gc(tmp_path, "Zelda.wbfs"), _wii(tmp_path)
    resultado = {}
    ventana = types.SimpleNamespace(
        ops=OperationManager(), settings=types.SimpleNamespace(wit_binary="wit"),
        progress_bar=types.SimpleNamespace(set_text=lambda _t: None,
                                           set_fraction=lambda _f: None),
        _selected_games=lambda: [gc, wii],
        _reject_if_busy=lambda *a, **k: False,
        _show_toast=lambda _m: None,
        _begin_cancellable_progress=lambda *_a: wit_wrapper.CancellationToken(),
        # El real: lo que queda en el historial tiene que nombrar lo que
        # el usuario seleccionó, no solo lo que se llegó a convertir.
        _describe_target=window.WiiBackupWindow._describe_target,
        _on_batch_done=lambda title, ok, errors, skipped, op, target, cancelled:
            resultado.update(ok=ok, errors=errors, skipped=skipped, target=target))

    window.WiiBackupWindow._on_batch_convert(ventana)

    assert wit_falso == [("Brawl.iso", "Brawl.wbfs")]
    # Del GameCube no salió nada nuevo: sigue solo su `.wbfs` de origen.
    assert sorted(p.name for p in (tmp_path / "biblioteca").iterdir()) == [
        "Brawl.iso", "Brawl.wbfs", "Zelda.wbfs"]
    assert resultado["ok"] == 1
    assert any("Zelda OoT MQ" in s for s in resultado["skipped"] + resultado["errors"])
    assert resultado["target"] == "2 juegos"


# ------------------------------------------------- Backend y otros caminos --
def test_el_backend_rechaza_convertir_gamecube_con_un_mensaje_claro(tmp_path):
    """Un GameCube en otro formato que CISO/ISO no se convierte, y el
    mensaje dice por qué y qué sí se puede."""
    with pytest.raises(library_ops.UnsupportedConversionError) as info:
        library_ops.conversion_target(_gc(tmp_path, "Zelda.wbfs"))
    assert "Nintendont" in str(info.value)
    assert "ISO y CISO" in str(info.value)
    assert library_ops.conversion_target(_gc(tmp_path)) == ".iso"
    assert library_ops.conversion_target(_gc(tmp_path, "Otro.iso")) == ".ciso"
    assert library_ops.conversion_target(_wii(tmp_path)) == ".wbfs"


def test_la_cola_copia_gamecube_tal_cual_sin_wbfs(tmp_path, monkeypatch):
    """Transferir (la cola) no convierte GameCube: va a games/ como .ciso."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)
    monkeypatch.setattr(wit_wrapper, "convert",
                        lambda *a, **k: pytest.fail("la cola convirtió un GameCube"))
    raiz = tmp_path / "USB"
    raiz.mkdir()
    cola = TransferQueue(OperationManager(), dispatch=lambda f, *a: f(*a))
    job = cola.add_jobs([_gc(tmp_path)], raiz)[0]
    limite = time.monotonic() + 10
    while not job.is_final and time.monotonic() < limite:
        time.sleep(0.01)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg
    assert [p.name for p in raiz.rglob("*") if p.is_file()] == ["game.ciso"]
