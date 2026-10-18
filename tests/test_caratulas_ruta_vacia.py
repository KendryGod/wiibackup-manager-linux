"""Un `covers2d_path` vacío en GXGlobal.cfg no deja un aviso roto.

Con `covers2d_path = ` el aviso decía "lee las carátulas 2D de : no se
copió nada.". No se copia nada, a propósito: USB Loader GX copia el valor
tal cual (`CSettings::SetSetting`, source/settings/CSettings.cpp,
`strlcpy(covers2d_path, value, ...)`, sin mirar si está vacío), así que
vacío no es "la de fábrica" sino una ruta vacía, y las buscaría en otro
lado. Solo cambia el texto.
"""
from __future__ import annotations

import os

import pytest

from wiibackup_manager import usbloader_covers


def _unidad(tmp_path, linea: str):
    raiz = tmp_path / "usb"
    app = raiz / "apps" / "usbloader_gx"
    app.mkdir(parents=True)
    (app / "GXGlobal.cfg").write_text(linea)
    (raiz / "wbfs" / "RMCE01").mkdir(parents=True)
    (raiz / "wbfs" / "RMCE01" / "RMCE01.wbfs").write_bytes(b"juego")
    return raiz


@pytest.mark.parametrize("linea", ["covers2d_path = \n", "covers2d_path =\n",
                                   "covers2d_path=   \r\n"])
def test_vacio_no_copia_nada(tmp_path, linea):
    raiz = _unidad(tmp_path, linea)

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert r.target.problem == usbloader_covers.OTHER_PATH
    assert r.target.configured == ""
    assert not (raiz / "apps" / "usbloader_gx" / "images").exists()


@pytest.mark.gui
def test_el_aviso_dice_que_esta_vacia(tmp_path):
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("sin display")
    from wiibackup_manager.widgets.transfer_view import TransferView

    r = usbloader_covers.copy_drive_covers(_unidad(tmp_path, "covers2d_path = \n"), "EN")

    assert TransferView._covers_message(r, None) == (
        "El GXGlobal.cfg de USB Loader GX en esta unidad tiene vacía la carpeta de "
        "las carátulas 2D (covers2d_path): no se copió nada.")
