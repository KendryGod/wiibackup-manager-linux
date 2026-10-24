"""Instalar homebrew baja todo a disco antes de borrar el respaldo.

`atomicfs.StagedSwap.commit` intercambiaba lo preparado y borraba el
respaldo de la versión anterior en el acto, y nada en ese camino bajaba a
disco: ni los archivos extraídos del ZIP, ni lo copiado del cliente, ni
las carpetas donde se hicieron los renames. Un corte de luz o un tirón del
cable justo después podía dejar la versión nueva (y la configuración del
cliente) a medio escribir, con el respaldo ya borrado.

Ahora, con `durable=True` (lo usa el instalador): antes del intercambio se
baja a disco todo lo preparado; después de los renames, otra vez (las
carpetas donde ocurrieron); y recién ahí se borra el respaldo. Con
`syncfs(2)` son dos llamadas por unidad; sin él, `fsync` archivo por
archivo.
"""
from __future__ import annotations

import os
import zipfile

import pytest

from homebrew_falso import dol
from wiibackup_manager import atomicfs, oscwii_installer
from wiibackup_manager.oscwii_client import HomebrewApp

APP = "apps/usbloader_gx"
VIEJO = {
    f"{APP}/boot.dol": dol(b"viejo"),
    f"{APP}/GXGlobal.cfg": b"# USB Loader GX R1280\n",
    f"{APP}/images/2D/RMCE01.png": b"png",
}
NUEVO = {
    f"{APP}/boot.dol": dol(b"nuevo"),
    f"{APP}/meta.xml": b"<app/>",
    "controllers/perfil.ini": b"[perfil]",
}


def _ruta(fd) -> str:
    return os.readlink(f"/proc/self/fd/{fd}")


@pytest.fixture
def eventos(monkeypatch):
    """Lo que pasa, en orden: ("syncfs" | "fsync", ruta), ("replace",
    destino) y ("discard", None)."""
    registro = []
    fsync_real, replace_real = os.fsync, os.replace
    syncfs_real = getattr(atomicfs, "_SYNCFS", None)

    def _fsync(fd):
        registro.append(("fsync", _ruta(fd)))
        return fsync_real(fd)

    def _syncfs(fd):
        registro.append(("syncfs", (_ruta(fd), os.fstat(fd).st_dev)))
        return syncfs_real(fd) if syncfs_real is not None else 0

    def _replace(origen, destino):
        registro.append(("replace", str(destino)))
        return replace_real(origen, destino)

    discard_real = atomicfs.SetAside.discard

    def _discard(self):
        registro.append(("discard", None))
        return discard_real(self)

    monkeypatch.setattr(os, "fsync", _fsync)
    monkeypatch.setattr(atomicfs, "_SYNCFS", _syncfs, raising=False)
    monkeypatch.setattr(atomicfs.os, "replace", _replace)
    monkeypatch.setattr(atomicfs.SetAside, "discard", _discard)
    return registro


@pytest.fixture
def unidad(tmp_path, monkeypatch):
    raiz = tmp_path / "usb"
    for nombre, datos in VIEJO.items():
        (raiz / nombre).parent.mkdir(parents=True, exist_ok=True)
        (raiz / nombre).write_bytes(datos)

    def _fake(app, dest_path, cancel_event, on_progress):
        with zipfile.ZipFile(dest_path, "w") as zf:
            for nombre, datos in NUEVO.items():
                zf.writestr(nombre, datos)
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _fake)
    return raiz


def _instalar(raiz):
    return oscwii_installer.install_app(
        HomebrewApp(slug="usbloader_gx", name="USB Loader GX",
                    zip_url="https://hbb1.oscwii.org/api/contents/usbloader_gx/x.zip"),
        raiz)


def _indices(eventos, que, cond=lambda r: True):
    return [i for i, (q, r) in enumerate(eventos) if q == que and cond(r)]


def test_con_syncfs_baja_todo_antes_de_intercambiar_y_antes_de_borrar(unidad, eventos):
    r = _instalar(unidad)

    assert r.ok, r.error
    [corte] = _indices(eventos, "discard")
    finales = _indices(eventos, "replace",
                       lambda d: d in (str(unidad / APP), str(unidad / "controllers/perfil.ini")))
    syncs = _indices(eventos, "syncfs")
    # Una antes de poner lo nuevo en su lugar, otra después de los renames,
    # y las dos antes de borrar el respaldo. Del sistema de archivos de la
    # unidad, que es donde está la staging.
    assert len(syncs) == 2
    assert syncs[0] < min(finales) and max(finales) < syncs[1] < corte
    dev = os.stat(unidad).st_dev
    assert all(eventos[i][1][1] == dev for i in syncs)
    assert _indices(eventos, "fsync") == []


def test_sin_syncfs_baja_cada_archivo_preparado(unidad, eventos, monkeypatch):
    monkeypatch.setattr(atomicfs, "_SYNCFS", None, raising=False)

    r = _instalar(unidad)

    assert r.ok, r.error
    [corte] = _indices(eventos, "discard")
    antes = [ruta for que, ruta in eventos[:corte] if que == "fsync"]
    # Del ZIP y del cliente, cada uno en su staging (nombre oculto).
    for rel in ("boot.dol", "meta.xml", "GXGlobal.cfg", "images/2D/RMCE01.png"):
        assert any(".wbm-staging-" in ruta and ruta.endswith("/" + rel) for ruta in antes), rel
    # El suelto de controllers/, en su temporal oculto.
    assert any("/controllers/.perfil.ini.parcial-" in ruta for ruta in antes)
    # Y las carpetas donde ocurrieron los renames, después de hacerlos.
    ultimo_rename = max(_indices(eventos, "replace", lambda d: d == str(unidad / APP)))
    for carpeta in (unidad / "apps", unidad / "controllers"):
        assert any(i > ultimo_rename for i in
                   _indices(eventos[:corte], "fsync", lambda r, c=carpeta: r == str(c)))


@pytest.mark.parametrize("con_syncfs", [True, False], ids=["syncfs", "fsync"])
def test_si_falla_la_bajada_a_disco_vuelve_la_version_anterior(unidad, monkeypatch,
                                                               con_syncfs):
    def _falla(*_a):
        raise OSError(5, "simulado: error de E/S al bajar a disco")

    if con_syncfs:
        def _syncfs(fd):
            import ctypes
            ctypes.set_errno(5)
            return -1
        monkeypatch.setattr(atomicfs, "_SYNCFS", _syncfs, raising=False)
    else:
        monkeypatch.setattr(atomicfs, "_SYNCFS", None, raising=False)
        monkeypatch.setattr(os, "fsync", _falla)
    antes = {str(p.relative_to(unidad)): p.read_bytes()
             for p in sorted(unidad.rglob("*")) if p.is_file()}

    r = _instalar(unidad)

    assert not r.ok
    despues = {str(p.relative_to(unidad)): p.read_bytes()
               for p in sorted(unidad.rglob("*")) if p.is_file()}
    assert despues == antes


def test_sin_durable_staged_swap_no_cambia(tmp_path, eventos):
    """El otro uso (`staged_directory`) sigue como estaba."""
    destino = tmp_path / "carpeta"
    destino.mkdir()
    (destino / "a").write_bytes(b"viejo")
    with atomicfs.staged_directory(destino) as nueva:
        (nueva.path / "a").write_bytes(b"nuevo")

    assert (destino / "a").read_bytes() == b"nuevo"
    assert [q for q, _r in eventos if q != "replace"] == ["discard"]
