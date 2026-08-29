"""Instalación de Homebrew como transacción atómica por carpeta.

ChatGPT encontró que `install_app` era atómico ARCHIVO por archivo
(`_extract_member`, vía `fsutil.atomic_target`) pero no para la app
entera: si la extracción se cortaba a mitad de una actualización, cada
archivo individual quedaba válido, pero la carpeta `apps/<App>/` podía
terminar con una mezcla de archivos de la versión vieja y la nueva -la
app rota aunque ningún archivo suelto lo estuviera.

`_stage_and_swap_unit` (ver su docstring en oscwii_installer.py) es el
mismo patrón que `library.DestinationGuard` (Sesión 2) llevado de
archivos WBFS a una carpeta completa: extraer TODO a una carpeta de
staging oculta y recién intercambiarla -con respaldo si había una
versión anterior- cuando la extracción completa ya salió bien. Estas
pruebas cubren, contra el filesystem real (no mocks del propio
intercambio, solo de la descarga y -donde hace falta- de `os.replace`
puntual): instalación nueva, actualización exitosa, actualización que
falla a mitad de la extracción, y fallo durante el intercambio final."""
from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

from wiibackup_manager import library, oscwii_installer
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallStatus


def _make_zip(path, entries: dict):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


def _fake_app(**overrides) -> HomebrewApp:
    kwargs = dict(slug="TestApp", name="Test App",
                 zip_url="https://hbb1.oscwii.org/api/contents/TestApp/TestApp.zip")
    kwargs.update(overrides)
    return HomebrewApp(**kwargs)


def _con_descarga_de(monkeypatch, zip_entries: dict):
    """Hace que `install_app` "descargue" un ZIP armado en memoria con
    `zip_entries`, sin tocar la red."""
    def _fake_download_zip(app, dest_path, cancel_event, on_progress):
        _make_zip(dest_path, zip_entries)
        return True, "", "d41d8cd98f00b204e9800998ecf8427e", \
               "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    monkeypatch.setattr(oscwii_installer, "_download_zip", _fake_download_zip)


def _sin_rastros_de_staging_o_respaldo(dest_root: Path) -> bool:
    return not any(dest_root.rglob(".*.wbm-staging-*")) and \
        not any(dest_root.rglob(".*.wbm-respaldo-*"))


# --------------------------------------------------------- Instalación nueva --
def test_instalacion_nueva_sin_app_previa(tmp_path, monkeypatch):
    dest_root = tmp_path / "usb"
    dest_root.mkdir()

    _con_descarga_de(monkeypatch, {
        "apps/TestApp/boot.dol": b"contenido nuevo" * 10,
        "apps/TestApp/meta.xml": b"<app version='1'/>",
    })

    result = oscwii_installer.install_app(_fake_app(), dest_root)

    assert result.ok, result.error
    assert result.status is InstallStatus.OK
    app_dir = dest_root / "apps" / "TestApp"
    assert app_dir.is_dir()
    assert (app_dir / "boot.dol").read_bytes() == b"contenido nuevo" * 10
    assert (app_dir / "meta.xml").read_bytes() == b"<app version='1'/>"
    assert len(result.installed_paths) == 2
    assert _sin_rastros_de_staging_o_respaldo(dest_root)


# ------------------------------------------------------- Actualización OK --
def test_actualizacion_exitosa_sobre_app_existente(tmp_path, monkeypatch):
    dest_root = tmp_path / "usb"
    app_dir = dest_root / "apps" / "TestApp"
    app_dir.mkdir(parents=True)
    (app_dir / "boot.dol").write_bytes(b"version vieja")
    (app_dir / "meta.xml").write_bytes(b"<app version='1'/>")
    # Un archivo que la versión nueva ya NO trae: tiene que desaparecer,
    # no quedar mezclado -es justo lo que un intercambio de carpeta
    # entera garantiza y una extracción archivo-por-archivo no.
    (app_dir / "solo-en-la-vieja.txt").write_bytes(b"obsoleto")

    _con_descarga_de(monkeypatch, {
        "apps/TestApp/boot.dol": b"version nueva" * 20,
        "apps/TestApp/meta.xml": b"<app version='2'/>",
    })

    result = oscwii_installer.install_app(_fake_app(), dest_root)

    assert result.ok, result.error
    assert result.status is InstallStatus.OK
    assert (app_dir / "boot.dol").read_bytes() == b"version nueva" * 20
    assert (app_dir / "meta.xml").read_bytes() == b"<app version='2'/>"
    assert not (app_dir / "solo-en-la-vieja.txt").exists()
    assert _sin_rastros_de_staging_o_respaldo(dest_root)


# ------------------------------------------ Falla a mitad de la extracción --
def test_actualizacion_que_falla_a_mitad_de_extraccion_deja_la_vieja_intacta(
        tmp_path, monkeypatch):
    """La app vieja tiene que quedar EXACTAMENTE como estaba -no solo
    "algún archivo válido", sino ni un byte tocado- si la extracción a
    staging se corta antes de terminar. La staging (todavía incompleta)
    nunca llega a intercambiarse con la carpeta final."""
    dest_root = tmp_path / "usb"
    app_dir = dest_root / "apps" / "TestApp"
    app_dir.mkdir(parents=True)
    (app_dir / "boot.dol").write_bytes(b"version vieja funcional")
    (app_dir / "meta.xml").write_bytes(b"<app version='1'/>")
    contenido_original = {
        p.name: p.read_bytes() for p in app_dir.iterdir()
    }

    _con_descarga_de(monkeypatch, {
        "apps/TestApp/boot.dol": b"version nueva" * 20,
        "apps/TestApp/meta.xml": b"<app version='2'/>",
        "apps/TestApp/extra.bin": b"algo mas",
    })

    real_copyfileobj = oscwii_installer.shutil.copyfileobj
    llamadas = {"n": 0}

    def _falla_en_el_segundo_archivo(src, dst, *a, **kw):
        llamadas["n"] += 1
        if llamadas["n"] == 2:
            dst.write(b"a medio escribir")
            raise OSError("simulado: unidad desconectada a mitad de la extracción")
        return real_copyfileobj(src, dst, *a, **kw)

    monkeypatch.setattr(oscwii_installer.shutil, "copyfileobj",
                        _falla_en_el_segundo_archivo)

    result = oscwii_installer.install_app(_fake_app(), dest_root)

    assert not result.ok
    assert result.status is InstallStatus.IO_ERROR, result.error

    # La app vieja: ni un byte distinto, ni un archivo de más o de menos.
    actual = {p.name: p.read_bytes() for p in app_dir.iterdir()}
    assert actual == contenido_original

    # Nada de la staging incompleta quedó tirado en el destino.
    assert _sin_rastros_de_staging_o_respaldo(dest_root)


# --------------------------------------- Falla durante el intercambio final --
def test_falla_en_el_intercambio_final_recupera_la_version_anterior(
        tmp_path, monkeypatch):
    """La extracción a staging termina perfecta (el ZIP entero, sin
    cortes), pero el paso que promueve la staging a carpeta final falla
    -disco lleno justo ahí, permisos, lo que sea. Tiene que poder
    recuperarse el estado anterior: la app vieja vuelve a su lugar, la
    instalación se reporta como fallida pero NADA se pierde."""
    dest_root = tmp_path / "usb"
    app_dir = dest_root / "apps" / "TestApp"
    app_dir.mkdir(parents=True)
    (app_dir / "boot.dol").write_bytes(b"version vieja funcional")
    (app_dir / "meta.xml").write_bytes(b"<app version='1'/>")
    contenido_original = {
        p.name: p.read_bytes() for p in app_dir.iterdir()
    }

    _con_descarga_de(monkeypatch, {
        "apps/TestApp/boot.dol": b"version nueva" * 20,
        "apps/TestApp/meta.xml": b"<app version='2'/>",
    })

    real_replace = os.replace

    def _falla_solo_al_promover_staging(origen, destino):
        if ".wbm-staging-" in Path(origen).name:
            raise OSError("simulado: no se pudo promover la staging")
        return real_replace(origen, destino)

    monkeypatch.setattr(oscwii_installer.os, "replace",
                        _falla_solo_al_promover_staging)

    result = oscwii_installer.install_app(_fake_app(), dest_root)

    assert not result.ok
    # Se pudo recuperar (no es el caso catastrófico de RollbackFailedError):
    # la instalación fracasó, pero la versión anterior está intacta.
    assert result.status is InstallStatus.IO_ERROR, result.error

    actual = {p.name: p.read_bytes() for p in app_dir.iterdir()}
    assert actual == contenido_original
    assert _sin_rastros_de_staging_o_respaldo(dest_root)


def test_falla_en_el_intercambio_y_tambien_falla_la_recuperacion(
        tmp_path, monkeypatch):
    """El caso catastrófico, mismo patrón que
    `library.RollbackFailedError` en `DestinationGuard`: ni promover la
    staging ni devolver el respaldo funcionan. No se pierde nada -el
    respaldo con la versión vieja sigue existiendo en disco- pero tiene
    que reportarse con su propio status, distinguible de un error
    común, y el respaldo tiene que quedar ahí para rescatarlo a mano."""
    dest_root = tmp_path / "usb"
    app_dir = dest_root / "apps" / "TestApp"
    app_dir.mkdir(parents=True)
    (app_dir / "boot.dol").write_bytes(b"version vieja funcional")
    contenido_original = (app_dir / "boot.dol").read_bytes()

    _con_descarga_de(monkeypatch, {
        "apps/TestApp/boot.dol": b"version nueva" * 20,
    })

    real_replace = os.replace

    def _falla_al_promover_y_al_recuperar(origen, destino):
        nombre = Path(origen).name
        if ".wbm-staging-" in nombre or ".wbm-respaldo-" in nombre:
            raise OSError("simulado: falla también al restaurar")
        return real_replace(origen, destino)

    monkeypatch.setattr(oscwii_installer.os, "replace",
                        _falla_al_promover_y_al_recuperar)

    result = oscwii_installer.install_app(_fake_app(), dest_root)

    assert not result.ok
    assert result.status is InstallStatus.ROLLBACK_FAILED, result.error
    assert "no se pudo promover" in result.error.lower() or \
        "restaurar" in result.error.lower()

    # El respaldo con la versión vieja sigue existiendo -rescatable a
    # mano- aunque `app_dir` mismo haya quedado sin el nombre público.
    respaldos = list(dest_root.rglob(".*.wbm-respaldo-*"))
    assert respaldos, "el respaldo debería seguir en disco"
    assert (respaldos[0] / "boot.dol").read_bytes() == contenido_original


# ------------------------------------- No-regresión: apps/ + controllers/ --
def test_zip_con_apps_y_controllers_instala_los_dos(tmp_path, monkeypatch):
    """Caso real de Nintendont (ver test_oscwii_installer.py): el ZIP
    trae una carpeta "apps/Nintendont/" (una unidad, va por
    `_stage_and_swap_unit`) y archivos sueltos directo en "controllers/"
    (sin subcarpeta, van por el `_extract_member` de siempre). Los dos
    caminos coexistiendo en la misma instalación no puede romper
    ninguno de los dos."""
    dest_root = tmp_path / "usb"
    dest_root.mkdir()

    _con_descarga_de(monkeypatch, {
        "apps/Nintendont/boot.dol": b"nintendont" * 50,
        "apps/Nintendont/meta.xml": b"<app/>",
        "controllers/gcadapter.ini": b"[controller]\n",
    })

    result = oscwii_installer.install_app(_fake_app(slug="nintendont"), dest_root)

    assert result.ok, result.error
    assert (dest_root / "apps" / "Nintendont" / "boot.dol").read_bytes() == b"nintendont" * 50
    assert (dest_root / "controllers" / "gcadapter.ini").read_bytes() == b"[controller]\n"
    assert len(result.installed_paths) == 3
    assert _sin_rastros_de_staging_o_respaldo(dest_root)
