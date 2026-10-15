"""El chequeo de espacio de Homebrew cuenta clústeres, no bytes.

En FAT32 cada archivo ocupa como mínimo un clúster (32 KB en una unidad
que preparó Modo Fábrica). `install_app` sumaba los tamaños exactos: una
app con cientos de archivos chicos pasaba el chequeo en una unidad casi
llena y fallaba a mitad con ENOSPC, después de descargar. Ahora cada
archivo se redondea al clúster del destino (32 KB si no se puede leer).
"""
from __future__ import annotations

import os
import types
import zipfile

import pytest

from wiibackup_manager import oscwii_installer, transfer_plan
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallStatus

ARCHIVOS = 100


@pytest.fixture
def descarga(monkeypatch):
    def _fake(app, dest_path, cancel_event, on_progress):
        with zipfile.ZipFile(dest_path, "w") as zf:
            for i in range(ARCHIVOS):
                zf.writestr(f"apps/App/f{i:03}.txt", b"x")
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _fake)


def _app():
    return HomebrewApp(slug="App", name="App", zip_url="https://hbb1.oscwii.org/x.zip")


def _con_cluster(monkeypatch, tamano):
    real = os.statvfs

    def _statvfs(path):
        st = real(path)
        if tamano is None:
            raise OSError(5, "Input/output error")
        return types.SimpleNamespace(f_frsize=tamano, f_bsize=tamano,
                                     f_blocks=st.f_blocks, f_bavail=st.f_bavail)

    monkeypatch.setattr(os, "statvfs", _statvfs)


def test_cien_archivos_chicos_no_entran_si_no_hay_lugar_para_sus_clusteres(
        tmp_path, descarga, monkeypatch):
    _con_cluster(monkeypatch, 32 * 1024)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: ARCHIVOS + 64 * 1024)

    r = oscwii_installer.install_app(_app(), tmp_path)

    assert r.status is InstallStatus.NO_SPACE, r.error
    assert not (tmp_path / "apps" / "App").exists()


def test_si_no_se_puede_leer_el_cluster_se_asumen_32_kb(tmp_path, descarga, monkeypatch):
    _con_cluster(monkeypatch, None)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: ARCHIVOS * 16 * 1024)

    r = oscwii_installer.install_app(_app(), tmp_path)

    assert r.status is InstallStatus.NO_SPACE, r.error


def test_con_lugar_para_los_clusteres_se_instala(tmp_path, descarga, monkeypatch):
    _con_cluster(monkeypatch, 32 * 1024)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: ARCHIVOS * 32 * 1024)

    r = oscwii_installer.install_app(_app(), tmp_path)

    assert r.ok, r.error
    assert len(list((tmp_path / "apps" / "App").iterdir())) == ARCHIVOS
