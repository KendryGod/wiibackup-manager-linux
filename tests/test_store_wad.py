"""Los WAD que trae un paquete quedan en /wad/ y la Store dice cómo se
instalan.

Open Shop Channel no publica un WAD por app: las 51 apps con
`WRITES_TO_NAND` no traen ninguno, y el campo `shop` del catálogo es de su
Wii Shop (un protocolo sin documentar). El único WAD real del catálogo es
`wad/plforwarder.5.1.5.wad`, dentro de postloader.

Un WAD se instala en la NAND con un gestor de WADs desde la Wii; esta app
no instala nada en la NAND. Así que:

- un WAD del paquete se valida (`wii_formats.problema_wad`: uno roto o un
  respaldo "Bk" rechaza el paquete entero, sin escribir nada) y queda en
  `/wad/`, fusionado como cualquier carpeta de datos;
- el aviso antes de instalar, el toast final y el historial dicen "se
  instala con YAWM desde la Wii; esta app no instala en la NAND".
"""
from __future__ import annotations

import os
import types
import zipfile

import pytest

from homebrew_falso import dol, wad
from wiibackup_manager import oplog, oscwii_installer
from wiibackup_manager.operations import OperationKind, OperationManager
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import AvisoDeInstalacion, InstallResult, InstallStatus

FRASE = "se instala con YAWM desde la Wii; esta app no instala en la NAND"
WAD = wad((b"contenido 1", b"contenido 2"))


def _app() -> HomebrewApp:
    return HomebrewApp(slug="postloader", name="postLoader",
                       zip_url="https://hbb1.oscwii.org/api/contents/postloader/x.zip")


@pytest.fixture
def paquete(monkeypatch):
    entradas = {"apps/postloader/boot.dol": dol(b"postloader"),
                "wad/plforwarder.5.1.5.wad": WAD}

    def _falsa(app, destino, cancel_event, on_progress):
        with zipfile.ZipFile(destino, "w") as zf:
            for nombre, datos in entradas.items():
                zf.writestr(nombre, datos)
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _falsa)
    return entradas


@pytest.fixture
def raiz(tmp_path):
    r = tmp_path / "usb"
    r.mkdir()
    return r


def _instalar(raiz, avisos=None):
    return oscwii_installer.install_app(
        _app(), raiz, confirmar=(lambda a: avisos.append(a) or False) if avisos is not None else None)


# ------------------------------------------------------------- instalar --
def test_el_wad_queda_en_wad_y_el_aviso_lo_lista(raiz, paquete):
    avisos = []

    r = _instalar(raiz, avisos)

    assert r.ok, r.error
    assert (raiz / "wad/plforwarder.5.1.5.wad").read_bytes() == WAD
    [aviso] = avisos
    assert aviso.wads == ("wad/plforwarder.5.1.5.wad",)
    assert r.wads == (raiz / "wad/plforwarder.5.1.5.wad",)


@pytest.mark.parametrize("roto", [
    WAD[:-0x40],                       # los datos no entran
    b"<html>404</html>",
    wad((b"contenido",), tipo=b"Bk"),  # un respaldo, no un WAD instalable
])
def test_un_wad_roto_rechaza_el_paquete_entero(raiz, paquete, roto):
    paquete["wad/plforwarder.5.1.5.wad"] = roto

    r = _instalar(raiz)

    assert r.status is InstallStatus.BAD_CONTENT, r
    assert "plforwarder.5.1.5.wad" in r.error
    assert os.listdir(raiz) == []


def test_un_wad_distinto_del_usuario_se_conserva(raiz, paquete):
    (raiz / "wad").mkdir()
    (raiz / "wad/plforwarder.5.1.5.wad").write_bytes(b"el mio")
    avisos = []

    r = _instalar(raiz, avisos)

    assert r.ok, r.error
    assert (raiz / "wad/plforwarder.5.1.5.wad").read_bytes() == b"el mio"
    [aviso] = avisos
    assert aviso.wads == ()
    assert aviso.distintos == ("wad/plforwarder.5.1.5.wad",)
    assert r.wads == ()


def test_los_wad_del_usuario_en_wad_no_se_tocan(raiz, paquete):
    (raiz / "wad").mkdir()
    (raiz / "wad/otro.wad").write_bytes(b"de otro lado")

    r = _instalar(raiz)

    assert r.ok, r.error
    assert sorted(os.listdir(raiz / "wad")) == ["otro.wad", "plforwarder.5.1.5.wad"]


def test_un_wad_fuera_de_wad_no_cuenta_como_wad_para_yawm(raiz, paquete):
    """Un .wad en otra carpeta (postloader trae `ploader/wads/`) se valida
    igual, pero no está en /wad/, que es donde lo busca un gestor de WADs."""
    del paquete["wad/plforwarder.5.1.5.wad"]
    paquete["ploader/wads/extra.wad"] = WAD
    avisos = []

    r = _instalar(raiz, avisos)

    assert r.ok, r.error
    assert (raiz / "ploader/wads/extra.wad").read_bytes() == WAD
    [aviso] = avisos
    assert aviso.wads == ()
    assert r.wads == ()


# --------------------------------------------------------------- textos --
def _aviso(wads) -> AvisoDeInstalacion:
    return AvisoDeInstalacion(reemplaza=("apps/postloader/",), carpetas=(("wad/", len(wads)),),
                              distintos=(), intocables=(), omitidos=(), wads=wads)


def test_el_aviso_dice_como_se_instala_el_wad():
    from wiibackup_manager.widgets.homebrew_store_view import _texto_del_aviso

    texto = _texto_del_aviso(_aviso(("wad/plforwarder.5.1.5.wad",)))

    assert FRASE in texto
    assert "1 WAD" in texto
    assert FRASE not in _texto_del_aviso(_aviso(()))


def _vista(tmp_path, toasts):
    historial = oplog.OperationLog(path=tmp_path / "history.json")
    ops = OperationManager(log=historial)
    op = ops.start(OperationKind.INSTALLING_HOMEBREW, write=[tmp_path / "apps" / "x"])
    vista = types.SimpleNamespace(ops=ops, op_log=historial, _show_toast=toasts.append,
                                  _install_states={}, _card_by_slug={})
    return vista, op, historial


def test_el_toast_y_el_historial_lo_dicen(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    toasts = []
    vista, op, historial = _vista(tmp_path, toasts)
    resultado = InstallResult(InstallStatus.OK, "postloader", installed_paths=("a", "b"),
                              datos_escritos=(tmp_path / "wad/plforwarder.5.1.5.wad",),
                              carpetas_de_datos=(("wad/", 1),),
                              wads=(tmp_path / "wad/plforwarder.5.1.5.wad",))

    HomebrewStoreView._on_install_done(vista, _app(), resultado, op)

    assert any(FRASE in t and "plforwarder.5.1.5.wad" in t for t in toasts), toasts
    [entrada] = historial.entries()
    assert "plforwarder.5.1.5.wad" in entrada.detail
    assert "YAWM" in entrada.detail


def test_sin_wad_no_hay_toast_de_wad(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    toasts = []
    vista, op, _h = _vista(tmp_path, toasts)

    HomebrewStoreView._on_install_done(
        vista, _app(), InstallResult(InstallStatus.OK, "postloader", installed_paths=("a",)), op)

    assert not any("WAD" in t for t in toasts)
