"""Un paquete grande avisa que puede tardar, muestra cuántos archivos van y
se puede cancelar.

En una USB montada por udisks (vfat con `flush`), el kernel espera ~100 ms
al cerrar cada archivo escrito: cavestory (4728 archivos) tardó 494,8 s en
una FAT32 real, lo mismo que con `open`/`write`/`close` sin nada más. Así
que:

- con más de 1000 archivos para escribir, el aviso antes de instalar lo
  dice con el número ("puede tardar varios minutos"), y aparece aunque el
  paquete escriba solo en `apps/` (neverball: 1173 archivos);
- el progreso dice "N de M archivos", no solo el porcentaje;
- cancelar corta entre un archivo y el siguiente, y la unidad queda como
  estaba.
"""
from __future__ import annotations

import os
import threading
import types
import zipfile
from pathlib import Path

import pytest

from homebrew_falso import dol
from wiibackup_manager import oscwii_installer
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import (AvisoDeInstalacion, InstallProgress,
                                                InstallStatus)


def _app() -> HomebrewApp:
    return HomebrewApp(slug="neverball", name="Neverball",
                       zip_url="https://hbb1.oscwii.org/api/contents/neverball/x.zip")


def _entradas(n: int, carpeta: str = "apps/neverball/data") -> dict:
    entradas = {"apps/neverball/boot.dol": dol(b"neverball")}
    entradas.update({f"{carpeta}/f{i:04}.bin": b"x" for i in range(n - 1)})
    return entradas


@pytest.fixture
def paquete(monkeypatch):
    entradas = {}

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


def _foto(raiz: Path) -> dict:
    return {str(p.relative_to(raiz)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(raiz.rglob("*"))}


# ---------------------------------------------------------------- aviso --
def test_mas_de_1000_archivos_avisa_aunque_solo_escriba_en_apps(raiz, paquete):
    paquete.update(_entradas(1001))
    avisos = []

    r = oscwii_installer.install_app(_app(), raiz,
                                     confirmar=lambda a: avisos.append(a) or False)

    assert r.ok, r.error
    [aviso] = avisos
    assert aviso.archivos == 1001
    assert aviso.reemplaza == ("apps/neverball/",)


def test_1000_archivos_justos_no_avisa(raiz, paquete):
    paquete.update(_entradas(1000))
    avisos = []

    r = oscwii_installer.install_app(_app(), raiz,
                                     confirmar=lambda a: avisos.append(a) or False)

    assert r.ok, r.error
    assert avisos == []


def test_cuenta_lo_que_se_va_a_escribir_no_lo_que_ya_esta(raiz, paquete):
    """Reinstalar cavestory no vuelve a escribir sus 4728 archivos de datos
    (ya están iguales): no tarda minutos y no hay que decir que sí."""
    paquete.update(_entradas(1200, carpeta="cavestory/data"))
    oscwii_installer.install_app(_app(), raiz)
    avisos = []

    r = oscwii_installer.install_app(_app(), raiz,
                                     confirmar=lambda a: avisos.append(a) or False)

    assert r.ok, r.error
    assert avisos == []


def test_en_un_aviso_con_datos_cuenta_todo_lo_que_se_escribe(raiz, paquete):
    paquete.update(_entradas(1300, carpeta="cavestory/data"))
    avisos = []

    oscwii_installer.install_app(_app(), raiz, confirmar=lambda a: avisos.append(a) or False)

    [aviso] = avisos
    assert aviso.archivos == 1300
    assert aviso.carpetas == (("cavestory/", 1299),)


def _aviso(archivos: int) -> AvisoDeInstalacion:
    return AvisoDeInstalacion(reemplaza=("apps/neverball/",), carpetas=(), distintos=(),
                              intocables=(), omitidos=(), archivos=archivos)


def test_el_texto_dice_que_puede_tardar_con_el_numero():
    from wiibackup_manager.widgets.homebrew_store_view import _texto_del_aviso

    texto = _texto_del_aviso(_aviso(4728))

    assert "4728 archivos" in texto
    assert "puede tardar varios minutos" in texto


def test_con_1000_o_menos_el_texto_no_lo_dice():
    from wiibackup_manager.widgets.homebrew_store_view import _texto_del_aviso

    assert "puede tardar" not in _texto_del_aviso(_aviso(1000))


# ------------------------------------------------------------- progreso --
def test_el_progreso_dice_cuantos_archivos_van(raiz, paquete):
    paquete.update(_entradas(1500))
    eventos = []

    r = oscwii_installer.install_app(_app(), raiz, on_progress=eventos.append)

    assert r.ok, r.error
    extraer = [e for e in eventos if e.phase == "extract"]
    assert [e.done for e in extraer] == list(range(1, 1501))
    assert {e.total for e in extraer} == {1500}


def test_la_tarjeta_muestra_n_de_m_archivos():
    from wiibackup_manager.widgets.homebrew_store_view import (
        HomebrewStoreView, _CardState, _InstallState)

    estado = _InstallState(kind=_CardState.EXTRACTING)
    vista = types.SimpleNamespace(_install_states={"neverball": estado}, _card_by_slug={})

    HomebrewStoreView._on_install_progress(
        vista, _app(), InstallProgress("extract", 700 / 1500, done=700, total=1500))

    assert "700 de 1500 archivos" in estado.message
    assert "46%" in estado.message


def test_la_descarga_sigue_mostrando_el_porcentaje():
    from wiibackup_manager.widgets.homebrew_store_view import (
        HomebrewStoreView, _CardState, _InstallState)

    estado = _InstallState(kind=_CardState.DOWNLOADING)
    vista = types.SimpleNamespace(_install_states={"neverball": estado}, _card_by_slug={})

    HomebrewStoreView._on_install_progress(vista, _app(), InstallProgress("download", 0.5))

    assert estado.message == "Descargando… 50%"


# ------------------------------------------------------------- cancelar --
def test_cancelar_a_mitad_de_un_paquete_grande_deja_la_unidad_como_estaba(raiz, paquete):
    paquete.update(_entradas(1500))
    os.makedirs(raiz / "apps/neverball")
    (raiz / "apps/neverball/boot.dol").write_bytes(dol(b"version anterior"))
    antes = _foto(raiz)
    cancelar = threading.Event()
    despues_de_cancelar = []

    def progreso(e):
        if cancelar.is_set():
            despues_de_cancelar.append(e.done)
        if e.phase == "extract" and e.done == 700:
            cancelar.set()

    r = oscwii_installer.install_app(_app(), raiz, cancel_event=cancelar,
                                     on_progress=progreso)

    assert r.status is InstallStatus.CANCELLED, r
    assert despues_de_cancelar == []          # no escribió ni un archivo más
    assert _foto(raiz) == antes
