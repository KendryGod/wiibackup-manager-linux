"""Instalar una app de Homebrew es todo o nada, aunque toque varias rutas.

Una app puede traer más de una "unidad" (`apps/<App>/` y otra carpeta) o
archivos sueltos (los perfiles de `controllers/` de Nintendont). Antes cada
unidad se intercambiaba apenas terminaba de extraerse y su respaldo se
borraba en el acto; los sueltos iban después, uno por uno. Si la
instalación se cancelaba o fallaba a mitad, quedaba la primera unidad nueva
(sin forma de volver atrás) y el resto viejo, y la tarjeta decía
"cancelada".

Ahora todo se prepara aparte, se intercambia junto, y los respaldos se
borran recién cuando entró TODO. Si se cancela o algo falla, vuelve la
versión anterior entera y no quedan restos.
"""
from __future__ import annotations

import os
import threading
import zipfile
from pathlib import Path

import pytest

from homebrew_falso import dol
from wiibackup_manager import atomicfs, oscwii_installer
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallStatus

VIEJO = {
    "apps/Nintendont/boot.dol": dol(b"nintendont viejo"),
    "apps/Nintendont/meta.xml": b"<app version='1'/>",
    "apps/Otra/boot.dol": dol(b"otra vieja"),
    "controllers/perfil.ini": b"[perfil viejo]",
}
NUEVO = {
    "apps/Nintendont/boot.dol": dol(b"nintendont nuevo" * 10),
    "apps/Nintendont/meta.xml": b"<app version='2'/>",
    "apps/Otra/boot.dol": dol(b"otra nueva" * 10),
    "controllers/perfil.ini": b"[perfil nuevo]",
    "controllers/otro.ini": b"[otro nuevo]",
}


def _app() -> HomebrewApp:
    return HomebrewApp(slug="Nintendont", name="Nintendont",
                       zip_url="https://hbb1.oscwii.org/api/contents/Nintendont/x.zip")


def _unidad(tmp_path) -> Path:
    raiz = tmp_path / "usb"
    for nombre, datos in VIEJO.items():
        (raiz / nombre).parent.mkdir(parents=True, exist_ok=True)
        (raiz / nombre).write_bytes(datos)
    return raiz


def _foto(raiz: Path) -> dict:
    """Todo lo que hay en la unidad (ocultos incluidos), con su contenido."""
    return {str(p.relative_to(raiz)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(raiz.rglob("*"))}


@pytest.fixture
def descarga(monkeypatch):
    def _fake(app, dest_path, cancel_event, on_progress):
        with zipfile.ZipFile(dest_path, "w") as zf:
            for nombre, datos in NUEVO.items():
                zf.writestr(nombre, datos)
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _fake)


def _instalar(raiz, cancel_event=None, on_progress=None):
    return oscwii_installer.install_app(_app(), raiz, cancel_event=cancel_event,
                                        on_progress=on_progress)


def test_si_todo_sale_bien_se_instala_todo_y_no_queda_nada(tmp_path, descarga):
    raiz = _unidad(tmp_path)

    r = _instalar(raiz)

    assert r.ok, r.error
    assert {k: v for k, v in _foto(raiz).items() if v is not None} == NUEVO
    assert not [p for p in raiz.rglob(".*")]
    assert len(r.installed_paths) == len(NUEVO)


def test_una_falla_en_la_segunda_unidad_deja_todo_como_estaba(tmp_path, descarga,
                                                              monkeypatch):
    raiz = _unidad(tmp_path)
    antes = _foto(raiz)
    real = oscwii_installer.shutil.copyfileobj

    def _copia(src, dst, *a, **k):
        if "Otra" in str(getattr(dst, "name", "")):
            raise OSError(5, "simulado: falla a mitad de la segunda unidad")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(oscwii_installer.shutil, "copyfileobj", _copia)

    r = _instalar(raiz)

    assert r.status is InstallStatus.IO_ERROR, r.error
    assert _foto(raiz) == antes


def test_cancelar_mientras_se_escriben_los_sueltos_deja_todo_como_estaba(tmp_path,
                                                                         descarga):
    """El caso de la auditoría: "Cancelar" con `apps/Nintendont` ya
    extraída y los perfiles de `controllers/` a medio escribir."""
    raiz = _unidad(tmp_path)
    antes = _foto(raiz)
    cancelar = threading.Event()
    pasos = []

    def on_progress(p):
        if p.phase == "extract":
            pasos.append(p.fraction)
            if len(pasos) == len(NUEVO) - 1:       # falta el último suelto
                cancelar.set()

    r = _instalar(raiz, cancel_event=cancelar, on_progress=on_progress)

    assert r.status is InstallStatus.CANCELLED, r.error
    assert _foto(raiz) == antes


def test_una_falla_al_intercambiar_la_segunda_ruta_devuelve_la_primera(tmp_path, descarga,
                                                                       monkeypatch):
    raiz = _unidad(tmp_path)
    antes = _foto(raiz)
    real = os.replace

    def _replace(origen, destino):
        if Path(destino).name == "Otra" and ".wbm-staging-" in Path(origen).name:
            raise OSError(28, "simulado: no se pudo poner la segunda unidad")
        return real(origen, destino)

    monkeypatch.setattr(atomicfs.os, "replace", _replace)

    r = _instalar(raiz)

    assert r.status is InstallStatus.IO_ERROR, r.error
    assert _foto(raiz) == antes


def test_cancelar_justo_al_final_tambien_vuelve_atras(tmp_path, descarga, monkeypatch):
    """La última oportunidad de cancelar es con todo ya intercambiado y
    antes de borrar los respaldos: todavía se puede volver atrás entero."""
    raiz = _unidad(tmp_path)
    antes = _foto(raiz)
    cancelar = threading.Event()
    real = os.replace

    def _replace(origen, destino):
        resultado = real(origen, destino)
        if Path(destino).name == "otro.ini":       # el último en entrar
            cancelar.set()
        return resultado

    monkeypatch.setattr(atomicfs.os, "replace", _replace)

    r = _instalar(raiz, cancel_event=cancelar)

    assert r.status is InstallStatus.CANCELLED, r.error
    assert _foto(raiz) == antes


def test_una_carpeta_que_creo_la_instalacion_no_queda_si_se_cancela(tmp_path, descarga):
    """La unidad no tenía `controllers/`: si se cancela, tampoco queda la
    carpeta vacía."""
    raiz = tmp_path / "usb"
    (raiz / "apps").mkdir(parents=True)
    cancelar = threading.Event()

    def on_progress(p):
        if p.phase == "extract" and p.fraction and p.fraction > 0.7:
            cancelar.set()

    r = _instalar(raiz, cancel_event=cancelar, on_progress=on_progress)

    assert r.status is InstallStatus.CANCELLED, r.error
    assert sorted(p.name for p in raiz.iterdir()) == ["apps"]
    assert list((raiz / "apps").iterdir()) == []
