"""La ruta de `wit` se elige en Preferencias.

`config.Settings.wit_binary` existía, pero la única forma de cambiarlo era
editar config.json a mano, y el aviso de la cola decía "configurá la ruta
en Preferencias", donde solo se veía si `wit` estaba o no. Ahora la fila
de `wit` tiene dos botones: elegir el archivo y volver a buscarlo en el
sistema (el valor por defecto, `"wit"` en el PATH).

Lo que se elige se comprueba antes de guardarlo: se lo corre con
`--version` y tiene que contestar como Wiimms ISO Tool. Un archivo
cualquiera, uno sin permiso de ejecución o un programa que no es `wit`
no cambian nada y se avisa.
"""
from __future__ import annotations

import os
import shutil
import time
import types
from pathlib import Path

import pytest

from wiibackup_manager import config, wit_wrapper

LINEA = "wit: Wiimms ISO Tool v3.05a r8638 x86_64 - Dirk Clemens - 2026-08-14"


def _programa(carpeta: Path, nombre: str, cuerpo: str, ejecutable: bool = True) -> Path:
    p = carpeta / nombre
    p.write_text(f"#!/bin/sh\n{cuerpo}\n")
    p.chmod(0o755 if ejecutable else 0o644)
    return p


@pytest.fixture
def wit_falso(tmp_path):
    return _programa(tmp_path, "wit", f'echo "{LINEA}"')


# ---------------------------------------------------------- wit_version --
def test_un_wit_contesta_su_version(wit_falso):
    assert wit_wrapper.wit_version(str(wit_falso)) == "v3.05a r8638"


@pytest.mark.skipif(shutil.which("wit") is None, reason="sin wit instalado")
def test_el_wit_del_sistema_tambien():
    assert wit_wrapper.wit_version("wit").startswith("v3")


def test_otro_programa_no_es_wit(tmp_path):
    otro = _programa(tmp_path, "otro", 'echo "otro programa 1.0"')

    assert wit_wrapper.wit_version(str(otro)) is None


def test_un_archivo_sin_permiso_de_ejecucion_no_sirve(tmp_path):
    sin_x = _programa(tmp_path, "wit", f'echo "{LINEA}"', ejecutable=False)

    assert wit_wrapper.wit_version(str(sin_x)) is None


@pytest.mark.parametrize("ruta", ["no-existe", ""])
def test_lo_que_no_existe_no_sirve(tmp_path, ruta):
    assert wit_wrapper.wit_version(str(tmp_path / ruta)) is None


def test_un_programa_que_se_cuelga_no_traba(tmp_path):
    colgado = _programa(tmp_path, "colgado", "sleep 10")
    t0 = time.monotonic()

    assert wit_wrapper.wit_version(str(colgado), timeout=0.5) is None
    assert time.monotonic() - t0 < 5


# ------------------------------------------------------------- la fila --
def test_la_fila_dice_la_version_y_la_ruta(wit_falso):
    from wiibackup_manager.widgets.preferences_dialog import _estado_de_wit

    texto = _estado_de_wit(str(wit_falso))

    assert "detectado ✓" in texto
    assert "v3.05a r8638" in texto and str(wit_falso) in texto


def test_la_fila_dice_que_no_lo_encuentra(tmp_path):
    from wiibackup_manager.widgets.preferences_dialog import _estado_de_wit

    texto = _estado_de_wit(str(tmp_path / "no-existe"))

    assert "no encontrado ✗" in texto
    # Una ruta elegida que ya no está se muestra, para saber cuál falta.
    assert str(tmp_path / "no-existe") in texto


def test_sin_wit_en_el_sistema_no_muestra_ninguna_ruta(monkeypatch):
    from wiibackup_manager.widgets.preferences_dialog import _estado_de_wit

    monkeypatch.setenv("PATH", "/nada")

    assert _estado_de_wit("wit") == "Estado: no encontrado ✗"


def test_la_fila_dice_que_no_es_wit(tmp_path):
    from wiibackup_manager.widgets.preferences_dialog import _estado_de_wit

    otro = _programa(tmp_path, "otro", 'echo "otro"')

    assert "no es `wit`" in _estado_de_wit(str(otro))


class _Fila:
    def __init__(self):
        self.subtitulo = None

    def set_subtitle(self, texto):
        self.subtitulo = texto


class _Boton:
    def __init__(self):
        self.activo = None

    def set_sensitive(self, valor):
        self.activo = valor


def _dialogo_falso(wit_binary="wit"):
    avisos = []
    falso = types.SimpleNamespace(
        settings=config.Settings(wit_binary=wit_binary), _wit_row=_Fila(),
        _wit_reset_btn=_Boton(), add_toast=lambda t: avisos.append(t.get_title()))
    return falso, avisos


def test_elegir_un_wit_lo_guarda_y_lo_muestra(wit_falso):
    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    falso, avisos = _dialogo_falso()

    assert PreferencesDialog._usar_wit(falso, str(wit_falso)) is True
    assert falso.settings.wit_binary == str(wit_falso)
    assert "v3.05a r8638" in falso._wit_row.subtitulo
    assert falso._wit_reset_btn.activo is True
    assert avisos == []


def test_elegir_algo_que_no_es_wit_no_cambia_nada(tmp_path):
    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    otro = _programa(tmp_path, "otro", 'echo "otro"')
    falso, avisos = _dialogo_falso(wit_binary="/opt/wit/wit")

    assert PreferencesDialog._usar_wit(falso, str(otro)) is False
    assert falso.settings.wit_binary == "/opt/wit/wit"
    assert falso._wit_row.subtitulo is None
    [aviso] = avisos
    assert str(otro) in aviso


def test_volver_al_del_sistema_se_acepta_aunque_no_este(monkeypatch, wit_falso):
    """El valor por defecto es buscar `wit` en el PATH: se puede volver a él
    aunque en este momento no haya ninguno instalado."""
    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    monkeypatch.setenv("PATH", "/nada")
    falso, avisos = _dialogo_falso(wit_binary=str(wit_falso))

    assert PreferencesDialog._usar_wit(falso, "wit") is True
    assert falso.settings.wit_binary == "wit"
    assert "no encontrado ✗" in falso._wit_row.subtitulo
    assert falso._wit_reset_btn.activo is False
    assert avisos == []


def test_el_archivo_elegido_en_el_dialogo_se_comprueba(wit_falso):
    from gi.repository import Gio

    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    usados = []
    falso = types.SimpleNamespace(_usar_wit=usados.append)
    elegido = types.SimpleNamespace(open_finish=lambda _r: Gio.File.new_for_path(str(wit_falso)))

    PreferencesDialog._on_wit_picked(falso, elegido, None)

    assert usados == [str(wit_falso)]


def test_cancelar_el_dialogo_no_hace_nada():
    from gi.repository import GLib

    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    usados = []
    falso = types.SimpleNamespace(_usar_wit=usados.append)

    def _cancelado(_r):
        raise GLib.Error("cancelado")

    PreferencesDialog._on_wit_picked(falso, types.SimpleNamespace(open_finish=_cancelado), None)

    assert usados == []


# --------------------------------------------------------------- GTK --
@pytest.mark.gui
def test_la_fila_de_wit_tiene_los_dos_botones(wit_falso):
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("sin display")
    from gi.repository import Adw, Gtk
    if not Gtk.init_check():
        pytest.skip("GTK no pudo inicializarse")
    Adw.init()
    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    guardados = []
    dialogo = PreferencesDialog(config.Settings(wit_binary=str(wit_falso)), guardados.append)

    assert "v3.05a r8638" in dialogo._wit_row.get_subtitle()
    assert dialogo._wit_pick_btn.get_parent() is not None
    assert dialogo._wit_reset_btn.get_sensitive() is True

    dialogo._usar_wit("wit")
    dialogo.emit("closed")

    assert dialogo._wit_reset_btn.get_sensitive() is False
    [guardado] = guardados
    assert guardado.wit_binary == "wit"
