"""El slug del catálogo no puede sacar la descarga del directorio temporal.

`install_app` armaba la ruta del ZIP con el slug tal cual
(`Path(tmp_dir) / f"{app.slug}.zip"`), y el slug es contenido de red: con
uno como "../escapado" el ZIP se escribía fuera del temporal y quedaba ahí
después (comprobado). Ahora se sanea igual que el del ícono
(`oscwii_client.icon_cache_path`).
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from homebrew_falso import dol
from wiibackup_manager import oscwii_client, oscwii_installer
from wiibackup_manager.oscwii_client import HomebrewApp


@pytest.mark.parametrize("slug", ["../escapado", "../../muy/lejos", "a/b", "/absoluta",
                                  "..", "Nintendont"])
def test_el_zip_siempre_cae_dentro_del_temporal(tmp_path, monkeypatch, slug):
    temporales = tmp_path / "tmp"
    temporales.mkdir()
    monkeypatch.setattr(oscwii_installer.tempfile, "tempdir", str(temporales))
    vistos = []

    def _descarga(app, dest_path, cancel_event, on_progress):
        vistos.append(Path(dest_path))
        with zipfile.ZipFile(dest_path, "w") as zf:
            zf.writestr("apps/App/boot.dol", dol(b"dol"))
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _descarga)
    destino = tmp_path / "usb"
    destino.mkdir()

    r = oscwii_installer.install_app(
        HomebrewApp(slug=slug, name="App", zip_url="https://hbb1.oscwii.org/x.zip"),
        destino)

    assert r.ok, r.error
    [zip_path] = vistos
    assert zip_path.resolve().parent.parent == temporales.resolve()
    assert zip_path.name.endswith(".zip") and "/" not in zip_path.name
    # Nada quedó fuera del temporal (que además ya se borró).
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tmp", "usb"]
    assert list(temporales.iterdir()) == []


def test_el_saneado_es_el_mismo_que_el_del_icono():
    for slug in ("../escapado", "Wii Donut!", "", "usbloader_gx", "a-b_c"):
        assert oscwii_client.icon_cache_path(slug).stem == oscwii_client.safe_slug(slug)
