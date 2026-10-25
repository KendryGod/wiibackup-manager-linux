"""Si un paquete deja un WAD en /wad/, la Store ofrece instalar también YAWM
ModMii Edition, sin hacerlo en silencio.

Un WAD se instala en la NAND con un gestor de WADs desde la Wii. Del
catálogo de Open Shop Channel se ofrece `yawmME` ("Manage WADs and Launch
Apps"), y solo si:

- el paquete deja al menos un WAD en `/wad/`;
- `yawmME` está en el catálogo cargado;
- la unidad no lo tiene ya (`apps/yawmME`, sin distinguir mayúsculas).

Es una casilla DESMARCADA en el aviso antes de instalar. Si se marca, YAWM
se instala DESPUÉS y solo si la app se instaló bien, por el flujo normal
(su tarjeta, su progreso, su historial), en el mismo destino.
"""
from __future__ import annotations

import os
import types

import pytest

from wiibackup_manager.operations import OperationKind, OperationManager
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import AvisoDeInstalacion, InstallResult, InstallStatus

YAWM = HomebrewApp(slug="yawmME", name="YAWM ModMii Edition",
                   zip_url="https://hbb1.oscwii.org/api/contents/yawmME/yawmME.zip")
POSTLOADER = HomebrewApp(slug="postloader", name="postLoader",
                         zip_url="https://hbb1.oscwii.org/api/contents/postloader/x.zip")


def _aviso(wads=("wad/plforwarder.5.1.5.wad",)) -> AvisoDeInstalacion:
    return AvisoDeInstalacion(reemplaza=("apps/postloader/",), carpetas=(("wad/", 1),),
                              distintos=(), intocables=(), omitidos=(), wads=wads)


# ------------------------------------------------------ cuándo se ofrece --
def test_se_ofrece_si_hay_wad_y_yawm_esta_en_el_catalogo(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import _yawm_a_ofrecer

    assert _yawm_a_ofrecer(_aviso(), {"yawmME": YAWM}, tmp_path) is YAWM


def test_sin_wad_no_se_ofrece(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import _yawm_a_ofrecer

    assert _yawm_a_ofrecer(_aviso(wads=()), {"yawmME": YAWM}, tmp_path) is None


def test_si_yawm_no_esta_en_el_catalogo_no_se_ofrece(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import _yawm_a_ofrecer

    otro = HomebrewApp(slug="some-yawmm-mod", name="Some YAWMM Mod",
                       zip_url="https://hbb1.oscwii.org/x.zip")
    assert _yawm_a_ofrecer(_aviso(), {"some-yawmm-mod": otro}, tmp_path) is None


@pytest.mark.parametrize("carpeta", ["yawmME", "YAWMME", "yawmme"])
def test_si_la_unidad_ya_tiene_yawm_no_se_ofrece(tmp_path, carpeta):
    from wiibackup_manager.widgets.homebrew_store_view import _yawm_a_ofrecer

    (tmp_path / "apps" / carpeta).mkdir(parents=True)

    assert _yawm_a_ofrecer(_aviso(), {"yawmME": YAWM}, tmp_path) is None


def test_otra_app_en_apps_no_cuenta_como_yawm(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import _yawm_a_ofrecer

    (tmp_path / "apps" / "postloader").mkdir(parents=True)

    assert _yawm_a_ofrecer(_aviso(), {"yawmME": YAWM}, tmp_path) is YAWM


# --------------------------------------------------- después de instalar --
def _vista(tmp_path, *, yawm_pendiente):
    from wiibackup_manager.widgets.homebrew_store_view import _CardState, _InstallState

    ops = OperationManager()
    op = ops.start(OperationKind.INSTALLING_HOMEBREW, write=[tmp_path / "apps" / "postloader"])
    estado = _InstallState(kind=_CardState.EXTRACTING,
                           yawm=(YAWM, tmp_path) if yawm_pendiente else None)
    arrancadas = []
    vista = types.SimpleNamespace(
        ops=ops, op_log=None, _show_toast=lambda _m: None,
        _install_states={"postloader": estado}, _card_by_slug={},
        _start_install=lambda app, dest_root=None: arrancadas.append(
            (app, dest_root, ops.active_operations())))
    return vista, op, arrancadas


def test_marcada_yawm_se_instala_despues_de_la_app_en_el_mismo_destino(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    vista, op, arrancadas = _vista(tmp_path, yawm_pendiente=True)

    HomebrewStoreView._on_install_done(
        vista, POSTLOADER, InstallResult(InstallStatus.OK, "postloader"), op)

    [(app, destino, activas)] = arrancadas
    assert app is YAWM and destino == tmp_path
    assert activas == []        # la instalación de la app ya había terminado


@pytest.mark.parametrize("estado", [InstallStatus.CANCELLED, InstallStatus.IO_ERROR,
                                    InstallStatus.BAD_CONTENT])
def test_si_la_app_no_se_instalo_yawm_tampoco(tmp_path, estado):
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    vista, op, arrancadas = _vista(tmp_path, yawm_pendiente=True)

    HomebrewStoreView._on_install_done(
        vista, POSTLOADER, InstallResult(estado, "postloader", "motivo"), op)

    assert arrancadas == []


def test_desmarcada_no_se_instala_nada_mas(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    vista, op, arrancadas = _vista(tmp_path, yawm_pendiente=False)

    HomebrewStoreView._on_install_done(
        vista, POSTLOADER, InstallResult(InstallStatus.OK, "postloader"), op)

    assert arrancadas == []


# ------------------------------------------------------------------ GTK --
@pytest.fixture(scope="module")
def adw():
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("sin display")
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    if not Gtk.init_check():
        pytest.skip("GTK no pudo inicializarse")
    Adw.init()
    return Adw


def _dialogo(eligio):
    from wiibackup_manager.widgets.homebrew_store_view import _dialogo_del_aviso

    respuestas = []
    dialogo = _dialogo_del_aviso("postLoader", _aviso(), respuestas.append,
                                 ofrecer_yawm=(YAWM.name, eligio.append))
    return dialogo, respuestas


@pytest.mark.gui
def test_la_casilla_viene_desmarcada(adw):
    eligio = []
    dialogo, _r = _dialogo(eligio)

    casilla = dialogo.get_extra_child()
    assert casilla is not None
    assert not casilla.get_active()
    assert "YAWM ModMii Edition" in casilla.get_label()


@pytest.mark.gui
@pytest.mark.parametrize("marcada", [False, True])
def test_instalar_informa_si_se_marco(adw, marcada):
    eligio = []
    dialogo, respuestas = _dialogo(eligio)
    dialogo.get_extra_child().set_active(marcada)

    dialogo.emit("response", "install")

    assert eligio == [marcada]
    assert respuestas == [False]


@pytest.mark.gui
def test_cancelar_no_instala_yawm_aunque_este_marcada(adw):
    eligio = []
    dialogo, respuestas = _dialogo(eligio)
    dialogo.get_extra_child().set_active(True)

    dialogo.emit("response", "cancel")

    assert eligio == []
    assert respuestas == [None]


@pytest.mark.gui
def test_sin_ofrecer_no_hay_casilla(adw):
    from wiibackup_manager.widgets.homebrew_store_view import _dialogo_del_aviso

    dialogo = _dialogo_del_aviso("postLoader", _aviso(), lambda _r: None)

    assert dialogo.get_extra_child() is None


def test_la_eleccion_del_aviso_queda_en_el_estado_de_la_instalacion(tmp_path, monkeypatch):
    from wiibackup_manager.widgets import homebrew_store_view as vista_mod
    from wiibackup_manager.widgets.homebrew_store_view import (
        HomebrewStoreView, _CardState, _InstallState)

    pedidos = []

    def _dialogo(nombre, aviso, al_responder, ofrecer_yawm=None):
        pedidos.append(ofrecer_yawm)
        return types.SimpleNamespace(present=lambda _parent: None)

    monkeypatch.setattr(vista_mod, "_dialogo_del_aviso", _dialogo)
    estado = _InstallState(kind=_CardState.DOWNLOADING)
    vista = types.SimpleNamespace(_install_states={"postloader": estado}, _card_by_slug={},
                                  _apps_por_slug={"yawmME": YAWM})

    HomebrewStoreView._mostrar_aviso(vista, POSTLOADER, _aviso(), lambda _r: None, tmp_path)

    [(nombre, elegir)] = pedidos
    assert nombre == "YAWM ModMii Edition"
    assert estado.yawm is None
    elegir(True)
    assert estado.yawm == (YAWM, tmp_path)
    elegir(False)
    assert estado.yawm is None


def test_sin_yawm_para_ofrecer_el_aviso_no_lleva_casilla(tmp_path, monkeypatch):
    from wiibackup_manager.widgets import homebrew_store_view as vista_mod
    from wiibackup_manager.widgets.homebrew_store_view import (
        HomebrewStoreView, _CardState, _InstallState)

    pedidos = []
    monkeypatch.setattr(vista_mod, "_dialogo_del_aviso",
                        lambda n, a, r, ofrecer_yawm=None: pedidos.append(ofrecer_yawm)
                        or types.SimpleNamespace(present=lambda _p: None))
    vista = types.SimpleNamespace(
        _install_states={"postloader": _InstallState(kind=_CardState.DOWNLOADING)},
        _card_by_slug={}, _apps_por_slug={})

    HomebrewStoreView._mostrar_aviso(vista, POSTLOADER, _aviso(), lambda _r: None, tmp_path)

    assert pedidos == [None]
