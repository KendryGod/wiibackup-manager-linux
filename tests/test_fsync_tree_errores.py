"""Si no se puede recorrer la staging para bajarla a disco, no se intercambia.

En el camino sin `syncfs`, `atomicfs._fsync_tree` usaba `os.walk(raiz)`
sin `onerror`: `os.walk` descarta en silencio el error al listar una
carpeta, así que una subcarpeta ilegible de la staging (images/2D,
theme...) se salteaba sin bajar a disco, y `StagedSwap.commit` seguía,
intercambiaba y borraba el respaldo. Ahora el error se propaga: el
intercambio no ocurre y la versión anterior queda entera.
"""
from __future__ import annotations

import os
import zipfile

from homebrew_falso import dol
from wiibackup_manager import atomicfs, oscwii_installer
from wiibackup_manager.oscwii_client import HomebrewApp

APP = "apps/usbloader_gx"
VIEJO = {f"{APP}/boot.dol": dol(b"viejo"), f"{APP}/GXGlobal.cfg": b"cfg",
         f"{APP}/images/2D/RMCE01.png": b"png"}


def test_una_subcarpeta_que_no_se_puede_listar_aborta_sin_borrar_el_respaldo(
        tmp_path, monkeypatch):
    raiz = tmp_path / "usb"
    for nombre, datos in VIEJO.items():
        (raiz / nombre).parent.mkdir(parents=True, exist_ok=True)
        (raiz / nombre).write_bytes(datos)

    def _fake(app, dest_path, cancel_event, on_progress):
        with zipfile.ZipFile(dest_path, "w") as zf:
            zf.writestr(f"{APP}/boot.dol", dol(b"nuevo"))
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _fake)
    monkeypatch.setattr(atomicfs, "_SYNCFS", None)
    scandir_real = os.scandir
    fallas = []

    def _scandir(ruta="."):
        if isinstance(ruta, int):          # rmtree recorre por descriptor
            return scandir_real(ruta)
        ruta_txt = os.fsdecode(ruta)
        # Una sola vez (un error de E/S pasajero): así la limpieza de la
        # staging, que también la lista, puede terminar.
        if (".wbm-staging-" in ruta_txt and ruta_txt.endswith("images/2D")
                and not fallas):
            fallas.append(ruta_txt)
            raise OSError(5, "simulado: error de E/S al listar", ruta_txt)
        return scandir_real(ruta)

    monkeypatch.setattr(os, "scandir", _scandir)
    descartes = []
    discard_real = atomicfs.SetAside.discard
    monkeypatch.setattr(atomicfs.SetAside, "discard",
                        lambda self: (descartes.append(1), discard_real(self))[1])

    r = oscwii_installer.install_app(
        HomebrewApp(slug="usbloader_gx", name="USB Loader GX",
                    zip_url="https://hbb1.oscwii.org/api/contents/usbloader_gx/x.zip"),
        raiz)

    assert not r.ok and fallas
    assert descartes == [], "borró el respaldo sin haber bajado todo a disco"
    despues = {str(p.relative_to(raiz)): p.read_bytes()
               for p in sorted(raiz.rglob("*")) if p.is_file()}
    assert despues == VIEJO
    assert [p.name for p in raiz.rglob(".*")] == []
