"""Pruebas de `golden_configs.py`: inyección de configuraciones maestras
después de instalar una app puntual de la Homebrew Store.

Ninguna de estas pruebas depende de los archivos reales de
`assets/configs/` (ver `_registry` y `_isolate_asset_dirs` más abajo): la
función bajo prueba acepta un `registry` inyectado y `find_asset` busca en
los directorios que le devuelva `_candidate_asset_dirs`, así que cada test
arma su propio archivo maestro de mentira en `tmp_path`. Lo que SÍ se
prueba contra el registro real (`GOLDEN_CONFIGS`) son solo los datos
estáticos -a qué ruta apunta cada spec-, como guarda de regresión de la
investigación del Paso 1 (ver el comentario de golden_configs.py)."""
from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest

from wiibackup_manager import golden_configs, oplog
from wiibackup_manager.golden_configs import GoldenConfigSpec, GoldenConfigStatus
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallResult, InstallStatus

NIN_MAGIC = golden_configs.NIN_CFG_MAGIC


def _app(slug: str, name: str | None = None) -> HomebrewApp:
    return HomebrewApp(slug=slug, name=name or slug)


def _ok(slug: str, installed=("boot.dol",)) -> InstallResult:
    return InstallResult(InstallStatus.OK, slug, installed_paths=tuple(installed))


def _failed(slug: str) -> InstallResult:
    return InstallResult(InstallStatus.BAD_ZIP, slug, error="ZIP corrupto")


@pytest.fixture
def registry(tmp_path):
    """Un registro de prueba con UNA sola app ("Nintendont") apuntando a
    un archivo maestro real (bien formado) dentro de `tmp_path`, separado
    del árbol de assets del repo."""
    asset_dir = tmp_path / "assets" / "nintendont"
    asset_dir.mkdir(parents=True)
    asset = asset_dir / "nincfg.bin"
    asset.write_bytes(NIN_MAGIC + b"\x00" * 44)  # 48 bytes, header válido

    spec = GoldenConfigSpec(
        label="Nintendont",
        asset_relative="nintendont/nincfg.bin",
        dest_relative=PurePosixPath("nincfg.bin"),
        min_size_bytes=32,
        magic=NIN_MAGIC,
    )
    return {"Nintendont": spec}, asset_dir.parent


@pytest.fixture
def op_log(tmp_path):
    return oplog.OperationLog(path=tmp_path / "history.json")


# ------------------------------------------------------- Caso feliz --
def test_instalar_app_con_config_registrada_copia_el_maestro(
        tmp_path, registry, op_log, monkeypatch):
    reg, asset_base = registry
    monkeypatch.setattr(golden_configs, "_candidate_asset_dirs", lambda: [asset_base])

    dest_root = tmp_path / "usb"
    dest_root.mkdir()
    app = _app("Nintendont")

    result = golden_configs.maybe_apply(
        app, dest_root, _ok("Nintendont"), op_log=op_log, registry=reg)

    assert result is not None
    assert result.applied
    assert result.status is GoldenConfigStatus.APPLIED

    dest_file = dest_root / "nincfg.bin"
    assert dest_file.exists()
    # En la RAÍZ del destino, no en apps/nintendont/ (ver la investigación
    # del Paso 1 en el docstring de golden_configs.py).
    assert dest_file.parent == dest_root
    assert dest_file.read_bytes() == (asset_base / "nintendont" / "nincfg.bin").read_bytes()

    entradas = op_log.entries()
    assert len(entradas) == 1
    assert entradas[0].target == "Nintendont"
    assert entradas[0].status == oplog.STATUS_OK


# --------------------------------------------- Otra app: no dispara nada --
def test_instalar_otra_app_no_dispara_ninguna_copia(tmp_path, registry, op_log, monkeypatch):
    reg, asset_base = registry
    monkeypatch.setattr(golden_configs, "_candidate_asset_dirs", lambda: [asset_base])

    dest_root = tmp_path / "usb"
    dest_root.mkdir()
    # Una app cualquiera del catálogo que no tiene golden config, ej. WiiMC.
    app = _app("wiimc", "WiiMC")

    result = golden_configs.maybe_apply(
        app, dest_root, _ok("wiimc"), op_log=op_log, registry=reg)

    assert result is None
    # Nada de nada: ni el archivo de Nintendont, ni ningún otro.
    assert list(dest_root.iterdir()) == []
    assert op_log.entries() == []


def test_instalacion_fallida_tampoco_dispara_copia(tmp_path, registry, op_log, monkeypatch):
    """Si `oscwii_installer.install_app` no llegó a terminar bien, no hay
    nada que inyectar: la app en sí ni siquiera quedó instalada."""
    reg, asset_base = registry
    monkeypatch.setattr(golden_configs, "_candidate_asset_dirs", lambda: [asset_base])

    dest_root = tmp_path / "usb"
    dest_root.mkdir()
    app = _app("Nintendont")

    result = golden_configs.maybe_apply(
        app, dest_root, _failed("Nintendont"), op_log=op_log, registry=reg)

    assert result is not None
    assert not result.applied
    assert result.status is GoldenConfigStatus.SKIPPED_INSTALL_FAILED
    assert list(dest_root.iterdir()) == []
    assert op_log.entries() == []


# ------------------------------------------------- Asset maestro corrupto --
@pytest.mark.parametrize("contenido", [
    b"",                      # vacío
    b"\x00" * 10,             # demasiado chico Y sin la firma
    b"\xff\xff\xff\xff" + b"\x00" * 44,  # tamaño OK, firma incorrecta
])
def test_asset_maestro_corrupto_aborta_sin_copiar_nada(
        tmp_path, op_log, monkeypatch, contenido):
    asset_dir = tmp_path / "assets" / "nintendont"
    asset_dir.mkdir(parents=True)
    (asset_dir / "nincfg.bin").write_bytes(contenido)
    monkeypatch.setattr(golden_configs, "_candidate_asset_dirs", lambda: [asset_dir.parent])

    spec = GoldenConfigSpec(
        label="Nintendont",
        asset_relative="nintendont/nincfg.bin",
        dest_relative=PurePosixPath("nincfg.bin"),
        min_size_bytes=32,
        magic=NIN_MAGIC,
    )
    reg = {"Nintendont": spec}

    dest_root = tmp_path / "usb"
    dest_root.mkdir()
    app = _app("Nintendont")

    result = golden_configs.maybe_apply(
        app, dest_root, _ok("Nintendont"), op_log=op_log, registry=reg)

    assert result is not None
    assert not result.applied
    assert result.status is GoldenConfigStatus.ASSET_CORRUPT
    assert result.error  # motivo claro, no vacío
    # Nada se copió a la unidad del "cliente".
    assert list(dest_root.iterdir()) == []

    entradas = op_log.entries()
    assert len(entradas) == 1
    assert entradas[0].status == oplog.STATUS_ERROR
    assert entradas[0].target == "Nintendont"


def test_asset_maestro_faltante_aborta_sin_copiar_nada(tmp_path, op_log, monkeypatch):
    monkeypatch.setattr(golden_configs, "_candidate_asset_dirs", lambda: [tmp_path / "nada"])
    spec = GoldenConfigSpec(
        label="Nintendont",
        asset_relative="nintendont/nincfg.bin",
        dest_relative=PurePosixPath("nincfg.bin"),
        min_size_bytes=32,
        magic=NIN_MAGIC,
    )
    dest_root = tmp_path / "usb"
    dest_root.mkdir()

    result = golden_configs.maybe_apply(
        _app("Nintendont"), dest_root, _ok("Nintendont"),
        op_log=op_log, registry={"Nintendont": spec})

    assert result.status is GoldenConfigStatus.ASSET_MISSING
    assert list(dest_root.iterdir()) == []
    assert op_log.entries()[0].status == oplog.STATUS_ERROR


# ------------------------------------------------------- find_asset --
def test_find_asset_busca_en_los_directorios_candidatos(tmp_path, monkeypatch):
    base = tmp_path / "candidato"
    (base / "nintendont").mkdir(parents=True)
    asset = base / "nintendont" / "nincfg.bin"
    asset.write_bytes(b"x" * 40)
    monkeypatch.setattr(golden_configs, "_candidate_asset_dirs", lambda: [base])

    assert golden_configs.find_asset("nintendont/nincfg.bin") == asset


def test_find_asset_none_si_no_existe_en_ningun_candidato(tmp_path, monkeypatch):
    monkeypatch.setattr(golden_configs, "_candidate_asset_dirs", lambda: [tmp_path])
    assert golden_configs.find_asset("nintendont/nincfg.bin") is None


# --------------------------------------- Regresión: registro real (Paso 1) --
def test_registro_real_nintendont_apunta_a_la_raiz_del_destino():
    """Guarda de regresión de la investigación del Paso 1: nincfg.bin va
    en la raíz del dispositivo (confirmado en el propio loader de
    Nintendont), NO en apps/Nintendont/. Si alguien "corrige" esto sin
    volver a leer la fuente, este test lo avisa."""
    spec = golden_configs.GOLDEN_CONFIGS["Nintendont"]
    assert spec.dest_relative == PurePosixPath("nincfg.bin")
    assert spec.magic == golden_configs.NIN_CFG_MAGIC


def test_registro_real_usbloadergx_apunta_a_su_carpeta_con_guion_bajo():
    spec = golden_configs.GOLDEN_CONFIGS["usbloader_gx"]
    assert spec.dest_relative == PurePosixPath("apps/usbloader_gx/GXGlobal.cfg")
