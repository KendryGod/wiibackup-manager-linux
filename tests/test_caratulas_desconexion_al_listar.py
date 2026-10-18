"""Una unidad que se va mientras se listan sus juegos no pasa por sana.

(A) Si se va justo al listar, `ticket_service._entradas` devuelve [] ante
FileNotFoundError: cero vueltas, y la mirada final de
`copy_drive_covers` exigía algún fallo para notar la desconexión. El
aviso decía "No hay juegos en la unidad" y no quedaba nada en el
historial. Ahora alcanza con que la carpeta de USB Loader GX ya no esté
(al empezar tenía que estar), así que una unidad sana sin juegos sigue
diciendo "No hay juegos" sin anotar ninguna desconexión.

(B) Si el listado levanta un OSError, el `worker` de la vista anotaba
"Con error". Ahora se mira con `drives.device_is_gone`, como en el resto
de la app: con un errno de dispositivo ausente o con la unidad ya ida es
"Unidad desconectada" (y el aviso es `drives.disconnected_message()`);
un EIO con la unidad todavía ahí sigue siendo error.
"""
from __future__ import annotations

import errno
import os
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from wiibackup_manager import config, drives, gametdb, oplog, ticket_service, usbloader_covers
from wiibackup_manager.fsutil import PNG_MAGIC

_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"


@pytest.fixture
def unidad(tmp_path, monkeypatch):
    cache = tmp_path / "covers"
    cache.mkdir()
    monkeypatch.setattr(config, "COVERS_DIR", cache)
    (cache / "RMCE01.EN.png").write_bytes(PNG_MAGIC + b"x" + _IEND)
    raiz = tmp_path / "usb"
    (raiz / "wbfs" / "RMCE01").mkdir(parents=True)
    (raiz / "wbfs" / "RMCE01" / "RMCE01.wbfs").write_bytes(b"juego")
    (raiz / "apps" / "usbloader_gx").mkdir(parents=True)
    return raiz


def _se_va_al_listar(raiz: Path, monkeypatch, error=None):
    real = ticket_service.list_wii_games

    def _listar(drive_root, *a, **k):
        shutil.rmtree(raiz)
        if error is not None:
            raise error
        return real(drive_root, *a, **k)

    monkeypatch.setattr(ticket_service, "list_wii_games", _listar)


# ------------------------------------------------------------ (A) --
def test_si_se_va_justo_al_listar_los_juegos_es_desconexion(unidad, monkeypatch):
    _se_va_al_listar(unidad, monkeypatch)

    r = usbloader_covers.copy_drive_covers(unidad, "EN")

    assert (r.copied, r.failed) == ([], [])
    assert r.stopped and r.disconnected


def test_una_unidad_sana_sin_juegos_no_es_desconexion(tmp_path):
    raiz = tmp_path / "usb"
    (raiz / "apps" / "usbloader_gx").mkdir(parents=True)

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert not r.stopped and not r.disconnected
    assert (r.copied, r.existing, r.missing, r.failed) == ([], [], [], [])


# ------------------------------------------------- Con la vista (A y B) --
@pytest.fixture
def vista(monkeypatch, tmp_path, unidad):
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("sin display")
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from wiibackup_manager.widgets import transfer_view

    monkeypatch.setattr(drives, "list_removable_drives", lambda: [])
    monkeypatch.setattr(gametdb, "fetch_cover_async", lambda *a, **k: None)
    toasts = []
    v = transfer_view.TransferView(config.Settings(library_path=str(tmp_path)),
                                   toasts.append)
    ventana = Gtk.Window()
    ventana.set_child(v)
    v.toasts = toasts
    v.salidas = []
    finish_real = v.ops.finish
    monkeypatch.setattr(v.ops, "finish",
                        lambda op, result=None: (v.salidas.append(result),
                                                 finish_real(op, result))[1])
    v._on_folder_picked(SimpleNamespace(
        select_folder_finish=lambda _r: SimpleNamespace(get_path=lambda: str(unidad))), None)
    yield v
    v.shutdown()
    ventana.destroy()


def _copiar_y_esperar(v):
    from gi.repository import GLib
    v.covers_button.emit("clicked")
    contexto = GLib.MainContext.default()
    limite = time.monotonic() + 5
    while v._covers_cancel is not None and time.monotonic() < limite:
        while contexto.iteration(False):
            pass
        time.sleep(0.01)
    while contexto.iteration(False):
        pass


@pytest.mark.gui
def test_a_la_vista_anota_desconexion_y_no_dice_no_hay_juegos(vista, unidad, monkeypatch):
    _se_va_al_listar(unidad, monkeypatch)

    _copiar_y_esperar(vista)

    [salida] = vista.salidas
    assert salida is not None and salida.status == oplog.STATUS_DISCONNECTED
    assert not any("No hay juegos" in t for t in vista.toasts), vista.toasts


@pytest.mark.gui
@pytest.mark.parametrize("error", [OSError(errno.ENODEV, "No such device"),
                                   OSError(errno.EIO, "Input/output error")],
                         ids=["ENODEV", "EIO-y-la-unidad-ya-no-esta"])
def test_b_un_oserror_de_unidad_ida_es_desconexion(vista, unidad, monkeypatch, error):
    _se_va_al_listar(unidad, monkeypatch, error)

    _copiar_y_esperar(vista)

    [salida] = vista.salidas
    assert salida.status == oplog.STATUS_DISCONNECTED
    assert vista.toasts[-1] == drives.disconnected_message()


@pytest.mark.gui
def test_b_un_eio_con_la_unidad_ahi_sigue_siendo_error(vista, unidad, monkeypatch):
    """EIO solo no es desconexión (regla de la app)."""
    def _eio(*_a, **_k):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(ticket_service, "list_wii_games", _eio)

    _copiar_y_esperar(vista)

    [salida] = vista.salidas
    assert salida.status == oplog.STATUS_ERROR
    assert vista.toasts[-1] == "No se pudieron copiar las carátulas: Input/output error"
