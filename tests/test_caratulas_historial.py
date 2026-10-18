"""El historial y el aviso de "Copiar carátulas" dicen lo que pasó.

`TransferView._covers_outcome` anotaba "Completada" (STATUS_OK) si no
había fallos, aunque la copia se hubiera cortado: la unidad desconectada
después de copiar 1 de N quedaba como un éxito. Y `_covers_message` solo
contaba los fallos, sin decir por qué (sin espacio, solo lectura, E/S).

Ahora `copy_drive_covers` distingue por qué se cortó (`disconnected`), y
el historial usa los estados que ya existen: "Unidad desconectada",
"Cancelada", "Terminada con errores" (algo copiado y algo no) o "Con
error". El aviso incluye el motivo del primer fallo.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from wiibackup_manager import config, gametdb, oplog, usbloader_covers
from wiibackup_manager.fsutil import PNG_MAGIC

pytestmark = pytest.mark.gui
_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"


@pytest.fixture(scope="module")
def vista():
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        if os.environ.get("WBM_REQUIRE_GUI") == "1":
            pytest.fail("WBM_REQUIRE_GUI=1 pero no hay display")
        pytest.skip("sin display")
    from wiibackup_manager.widgets.transfer_view import TransferView
    return TransferView


@pytest.fixture
def unidad(tmp_path, monkeypatch):
    cache = tmp_path / "covers"
    cache.mkdir()
    monkeypatch.setattr(config, "COVERS_DIR", cache)
    raiz = tmp_path / "usb"
    for game_id in ("RMCE01", "RSBE01", "SMNE01"):
        (raiz / "wbfs" / game_id).mkdir(parents=True)
        (raiz / "wbfs" / game_id / f"{game_id}.wbfs").write_bytes(b"juego")
        (cache / f"{game_id}.EN.png").write_bytes(PNG_MAGIC + game_id.encode() + _IEND)
    (raiz / "apps" / "usbloader_gx").mkdir(parents=True)
    return raiz


def _desconectar_despues_del_primero(raiz: Path, monkeypatch):
    real = gametdb.cached_cover
    vistos = []

    def _se_va(game_id, region, console):
        vistos.append(game_id)
        if len(vistos) == 2:
            shutil.rmtree(raiz)
        return real(game_id, region, console)

    monkeypatch.setattr(gametdb, "cached_cover", _se_va)


def test_si_la_unidad_se_va_despues_de_copiar_una_no_dice_completada(vista, unidad,
                                                                     monkeypatch):
    _desconectar_despues_del_primero(unidad, monkeypatch)

    r = usbloader_covers.copy_drive_covers(unidad, "EN")
    salida = vista._covers_outcome(unidad, r, None)

    assert len(r.copied) == 1 and r.stopped and r.disconnected
    # Y no se escribió nada donde estaba la unidad (en un USB arrancado, el
    # punto de montaje vacío está en el disco interno).
    assert not unidad.exists()
    assert salida.status == oplog.STATUS_DISCONNECTED
    assert oplog.STATUS_LABELS[salida.status] != oplog.STATUS_LABELS[oplog.STATUS_OK]


def test_una_que_falla_y_la_unidad_se_va_tambien_es_desconexion(vista, unidad,
                                                                monkeypatch):
    """El último archivo falla porque la unidad ya no está: no queda una
    vuelta más para notarlo, se mira al terminar."""
    real = usbloader_covers._copiar_sin_pisar

    def _ultima(origen, destino):
        if destino.name == "SMNE01.png":
            shutil.rmtree(unidad)
            raise OSError(5, "Input/output error")
        return real(origen, destino)

    monkeypatch.setattr(usbloader_covers, "_copiar_sin_pisar", _ultima)

    r = usbloader_covers.copy_drive_covers(unidad, "EN")

    assert r.disconnected
    assert vista._covers_outcome(unidad, r, None).status == oplog.STATUS_DISCONNECTED


def test_cortada_a_pedido_es_cancelada(vista, unidad):
    pedidos = []
    r = usbloader_covers.copy_drive_covers(
        unidad, "EN", should_stop=lambda: (pedidos.append(1), len(pedidos) > 1)[1])

    assert r.stopped and not r.disconnected and len(r.copied) == 1
    assert vista._covers_outcome(unidad, r, None).status == oplog.STATUS_CANCELLED
    assert vista._covers_message(r, None).endswith(" Se canceló antes de terminar.")


def test_si_se_va_antes_de_la_primera_tambien_queda_en_el_historial(vista, unidad,
                                                                     monkeypatch):
    real = usbloader_covers._juegos_de_la_unidad

    def _y_se_va(raiz):
        juegos = real(raiz)
        shutil.rmtree(unidad)
        return juegos

    monkeypatch.setattr(usbloader_covers, "_juegos_de_la_unidad", _y_se_va)

    r = usbloader_covers.copy_drive_covers(unidad, "EN")

    assert (r.copied, r.failed) == ([], []) and r.disconnected
    assert vista._covers_outcome(unidad, r, None).status == oplog.STATUS_DISCONNECTED


def test_copiar_una_no_crea_carpetas(tmp_path):
    origen = tmp_path / "origen.png"
    origen.write_bytes(b"png")
    destino = tmp_path / "no-esta" / "2D" / "RMCE01.png"

    with pytest.raises(FileNotFoundError):
        usbloader_covers._copiar_sin_pisar(origen, destino)
    assert not (tmp_path / "no-esta").exists()


def test_con_algun_fallo_es_parcial_y_dice_el_motivo(vista, unidad, monkeypatch):
    real = usbloader_covers._copiar_sin_pisar

    def _sin_lugar(origen, destino):
        if destino.name != "RMCE01.png":
            raise OSError(28, "No space left on device")
        return real(origen, destino)

    monkeypatch.setattr(usbloader_covers, "_copiar_sin_pisar", _sin_lugar)

    r = usbloader_covers.copy_drive_covers(unidad, "EN")
    salida = vista._covers_outcome(unidad, r, None)

    assert salida.status == oplog.STATUS_PARTIAL
    assert vista._covers_message(r, None) == (
        "Carátulas para USB Loader GX: 1 copiada, 2 no se pudieron copiar (la "
        "primera, RSBE01: No space left on device).")


def test_si_no_se_copio_ninguna_es_error_y_dice_el_motivo(vista, unidad, monkeypatch):
    def _solo_lectura(origen, destino):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(usbloader_covers, "_copiar_sin_pisar", _solo_lectura)

    r = usbloader_covers.copy_drive_covers(unidad, "EN")

    assert vista._covers_outcome(unidad, r, None).status == oplog.STATUS_ERROR
    assert "RMCE01: Read-only file system" in vista._covers_message(r, None)


def test_un_solo_fallo_tambien_dice_el_motivo(vista, unidad):
    r = usbloader_covers.CopyResult(usbloader_covers.CoversTarget(unidad),
                                    failed=[("RMCE01", "Input/output error")])

    assert vista._covers_message(r, None) == (
        "Carátulas para USB Loader GX: 1 no se pudo copiar (RMCE01: Input/output "
        "error).")


def test_todo_bien_sigue_siendo_completada(vista, unidad):
    r = usbloader_covers.copy_drive_covers(unidad, "EN")

    assert len(r.copied) == 3 and not r.stopped and not r.disconnected
    assert vista._covers_outcome(unidad, r, None).status == oplog.STATUS_OK
