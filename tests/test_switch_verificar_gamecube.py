"""El switch "Verificar copias de GameCube (más lento)" de Ajustes.

Activado por defecto: copiar un GameCube a una memoria que guarda mal y
no enterarse es peor que esperar la relectura. Se guarda en config.json
como cualquier ajuste (un config.json de antes, sin el campo, lo deja
prendido) y se congela en cada tarea al encolar, igual que "Verificar
después de copiar" (ver `TransferJob.verify_gamecube`).
"""
from __future__ import annotations

import json
import types

import pytest

from wiibackup_manager import config
from wiibackup_manager.game_model import Game


def test_esta_prendido_por_defecto():
    assert config.Settings().verify_gamecube_copies is True


def test_se_guarda_y_se_vuelve_a_leer(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    config.Settings(verify_gamecube_copies=False).save()

    assert config.Settings.load().verify_gamecube_copies is False


@pytest.mark.parametrize("guardado", [{}, {"verify_gamecube_copies": "si"},
                                      {"verify_gamecube_copies": 1}])
def test_sin_el_campo_o_con_otro_tipo_queda_prendido(monkeypatch, tmp_path, guardado):
    """Un config.json de una versión anterior no lo trae; uno editado a
    mano puede traer cualquier cosa."""
    archivo = tmp_path / "config.json"
    archivo.write_text(json.dumps({"wit_binary": "wit", **guardado}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", archivo)

    assert config.Settings.load().verify_gamecube_copies is True


# ---------------------------------------------------------------- encolar --
class _Hilo:
    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


@pytest.mark.parametrize("prendido", [True, False])
def test_al_encolar_cada_tarea_se_lleva_el_ajuste(monkeypatch, tmp_path, prendido):
    from wiibackup_manager.widgets import transfer_view
    from wiibackup_manager.widgets.transfer_view import TransferView

    monkeypatch.setattr(transfer_view.threading, "Thread", _Hilo)
    monkeypatch.setattr(transfer_view.transfer_plan, "plan_transfer_fast",
                        lambda juegos, _wit: juegos)
    pedidos = []
    p = tmp_path / "juego.iso"
    p.write_bytes(b"gc")
    juego = Game(path=p, game_id="GZ2E01", title="Twilight Princess", fmt="ISO",
                 size_bytes=2, identified_by="iso", console="gc")
    vista = types.SimpleNamespace(
        settings=config.Settings(verify_gamecube_copies=prendido),
        _descartar_encolados=lambda juegos, _d: juegos,
        _show_toast=lambda _m: None,
        queue=types.SimpleNamespace(add_jobs=lambda items, dest, **kw: pedidos.append(kw)))

    TransferView._enqueue(vista, [juego], tmp_path / "usb")

    [kw] = pedidos
    assert kw["verify_gamecube"] is prendido


# ----------------------------------------------------------------- Ajustes --
def test_tocar_el_switch_cambia_el_ajuste_y_lo_guarda(monkeypatch, tmp_path):
    from wiibackup_manager.window import WiiBackupWindow

    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    avisos = []
    ventana = types.SimpleNamespace(settings=config.Settings(), _show_toast=avisos.append)

    WiiBackupWindow._on_verify_gamecube_switch_toggled(
        ventana, types.SimpleNamespace(get_active=lambda: False), None)

    assert ventana.settings.verify_gamecube_copies is False
    assert config.Settings.load().verify_gamecube_copies is False
    assert avisos == []


def test_si_no_se_puede_guardar_lo_dice(monkeypatch, tmp_path):
    from wiibackup_manager.window import WiiBackupWindow

    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(config, "try_save", lambda _s: "disco lleno")
    avisos = []
    ventana = types.SimpleNamespace(settings=config.Settings(), _show_toast=avisos.append)

    WiiBackupWindow._on_verify_gamecube_switch_toggled(
        ventana, types.SimpleNamespace(get_active=lambda: False), None)

    assert ventana.settings.verify_gamecube_copies is False
    assert len(avisos) == 1 and "disco lleno" in avisos[0]
