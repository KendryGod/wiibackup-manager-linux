"""La configuración maestra no pisa un `nincfg.bin` que ya estaba.

`maybe_apply` copiaba el `nincfg.bin` maestro a la raíz de la unidad
después de CADA instalación correcta de Nintendont, también con
"Reinstalar": actualizar Nintendont en la SD de un cliente que ya lo tenía
configurado (modo de video, mandos, memory cards) le borraba esa
configuración. Ahora, si ya hay uno, no se toca, queda una nota en el
historial y la tienda lo avisa. Sin `nincfg.bin`, se aplica como siempre.
"""
from __future__ import annotations

import types
from pathlib import PurePosixPath

import pytest

from wiibackup_manager import golden_configs, oplog
from wiibackup_manager.golden_configs import GoldenConfigSpec, GoldenConfigStatus
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallResult, InstallStatus

MAESTRO = golden_configs.NIN_CFG_MAGIC + b"\x00" * 44


@pytest.fixture
def registro(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    (assets / "nintendont").mkdir(parents=True)
    (assets / "nintendont" / "nincfg.bin").write_bytes(MAESTRO)
    monkeypatch.setattr(golden_configs, "_candidate_asset_dirs", lambda: [assets])
    return {"Nintendont": GoldenConfigSpec(
        label="Nintendont", asset_relative="nintendont/nincfg.bin",
        dest_relative=PurePosixPath("nincfg.bin"), min_size_bytes=32,
        magic=golden_configs.NIN_CFG_MAGIC)}


@pytest.fixture
def historial(tmp_path):
    return oplog.OperationLog(path=tmp_path / "history.json")


def _aplicar(dest_root, registro, historial):
    return golden_configs.maybe_apply(
        HomebrewApp(slug="Nintendont", name="Nintendont"), dest_root,
        InstallResult(InstallStatus.OK, "Nintendont"), op_log=historial,
        registry=registro)


def test_un_nincfg_que_ya_estaba_no_se_toca(tmp_path, registro, historial):
    unidad = tmp_path / "usb"
    unidad.mkdir()
    (unidad / "nincfg.bin").write_bytes(b"la configuracion del cliente")

    r = _aplicar(unidad, registro, historial)

    assert (unidad / "nincfg.bin").read_bytes() == b"la configuracion del cliente"
    assert r.status is GoldenConfigStatus.KEPT_EXISTING
    assert not r.applied and not r.status.is_error
    assert sorted(p.name for p in unidad.iterdir()) == ["nincfg.bin"]
    [nota] = historial.entries()
    assert nota.operation == golden_configs.KEPT_OPERATION
    assert nota.status == oplog.STATUS_OK and nota.target == "Nintendont"
    assert "nincfg.bin" in nota.detail


def test_sin_nincfg_se_aplica_como_siempre(tmp_path, registro, historial):
    unidad = tmp_path / "usb"
    unidad.mkdir()

    r = _aplicar(unidad, registro, historial)

    assert r.applied
    assert (unidad / "nincfg.bin").read_bytes() == MAESTRO


def test_la_tienda_avisa_que_dejo_la_configuracion_propia(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView
    from wiibackup_manager.operations import OperationManager

    toasts = []
    vista = types.SimpleNamespace(
        ops=OperationManager(), op_log=None, _show_toast=toasts.append,
        _install_states={}, _card_by_slug={})
    app = HomebrewApp(slug="Nintendont", name="Nintendont")
    dorado = golden_configs.GoldenConfigResult(
        GoldenConfigStatus.KEPT_EXISTING, "Nintendont", "Nintendont",
        dest_path=tmp_path / "nincfg.bin")

    HomebrewStoreView._on_install_done(
        vista, app, InstallResult(InstallStatus.OK, "Nintendont"), None, dorado)

    assert any("nincfg.bin" in t and "como estaba" in t for t in toasts), toasts
