"""El botón "Copiar carátulas a la unidad" se apaga con la unidad ocupada.

`_update_covers_button` solo miraba si había USB Loader GX y si ya se
estaba copiando; "Expulsar unidad" además se apaga si la cola le está
escribiendo a esa unidad (`queue.is_writing_to`) o si otra operación la
ocupa (`ops.is_resource_busy`). Al tocar el botón la copia no arrancaba
(lo frenaba el aviso de la cola o `OperationBusy`), pero el botón se veía
disponible. Ahora usa la misma condición, y se vuelve a prender al
terminar.
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from wiibackup_manager import config, gametdb
from wiibackup_manager.operations import OperationKind

pytestmark = pytest.mark.gui


@pytest.fixture
def vista(monkeypatch, tmp_path):
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("sin display")
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk

    from wiibackup_manager import drives
    from wiibackup_manager.widgets import transfer_view

    monkeypatch.setattr(drives, "list_removable_drives", lambda: [])
    monkeypatch.setattr(gametdb, "fetch_cover_async", lambda *a, **k: None)
    v = transfer_view.TransferView(config.Settings(library_path=str(tmp_path)), lambda m: None)
    ventana = Gtk.Window()
    ventana.set_child(v)
    raiz = tmp_path / "usb"
    (raiz / "apps" / "usbloader_gx").mkdir(parents=True)
    v._on_folder_picked(SimpleNamespace(
        select_folder_finish=lambda _r: SimpleNamespace(get_path=lambda: str(raiz))), None)
    v.raiz = raiz
    yield v
    v.shutdown()
    ventana.destroy()


def test_con_la_cola_escribiendo_en_la_unidad_se_apaga_y_despues_vuelve(vista, monkeypatch):
    assert vista.covers_button.get_sensitive()

    monkeypatch.setattr(vista.queue, "is_writing_to", lambda ruta: Path(ruta) == vista.raiz)
    vista._update_dest_buttons()
    assert not vista.covers_button.get_sensitive()
    assert "cola" in vista.covers_button.get_tooltip_text()

    monkeypatch.setattr(vista.queue, "is_writing_to", lambda ruta: False)
    vista._update_dest_buttons()
    assert vista.covers_button.get_sensitive()


def test_con_otra_operacion_en_la_unidad_se_apaga_y_despues_vuelve(vista):
    op = vista.ops.start(OperationKind.INSTALLING_HOMEBREW, resources=[vista.raiz])
    vista._update_dest_buttons()
    assert not vista.covers_button.get_sensitive()
    assert OperationKind.INSTALLING_HOMEBREW.label in vista.covers_button.get_tooltip_text()

    vista.ops.finish(op)
    vista._update_dest_buttons()
    assert vista.covers_button.get_sensitive()
