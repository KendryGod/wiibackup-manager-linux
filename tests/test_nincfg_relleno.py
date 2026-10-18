"""La Homebrew Store no copia el nincfg.bin sintético del repositorio.

`assets/configs/nintendont/nincfg.bin` no es una configuración de verdad:
548 bytes con solo la firma (01 07 0C F6) y la versión (10), y todo lo
demás en cero. Nintendont lo carga como válido (`LoadNinCFG`,
loader/source/global.c) y queda con:

- `MaxPads = 0`: durante el juego no lee ningún puerto de control de
  GameCube (`PADRead`, loader/source/ppc/PADReadGC/source/PADReadGC.c:
  `for (chan = 0; chan < MaxPads; ++chan)`);
- idioma 0 (inglés forzado) y memoria de 59 bloques.

Sin archivo, Nintendont usa los suyos (`main`, loader/source/main.c):
idioma automático, `MaxPads = 4` y 251 bloques. Así que, como con el
GXGlobal.cfg de relleno, no se copia ninguno: queda una nota en el
historial y la tienda avisa que arranca con su configuración de fábrica.

Como un binario no puede llevar la marca PLACEHOLDER, se lo reconoce por
su sha256 exacto: cualquier otro nincfg.bin -uno de verdad que guarde el
taller- se sigue copiando, y uno que ya esté en la unidad no se toca.
"""
from __future__ import annotations

import struct
import types
import zipfile

import pytest

from wiibackup_manager import golden_configs, oplog, oscwii_installer
from wiibackup_manager.golden_configs import GoldenConfigSpec, GoldenConfigStatus
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallResult, InstallStatus

# Lo que trae el ZIP real de Nintendont en OSC (resumido).
ZIP_OSC = {
    "apps/Nintendont/boot.dol": b"dol",
    "apps/Nintendont/meta.xml": b"<app/>",
    "apps/Nintendont/icon.png": b"png",
    "controllers/054C_05C4.ini": b"[perfil]",
}


def _nincfg(language: int = -1, max_pads: int = 4, memcard: int = 2) -> bytes:
    """Un nincfg.bin como el que guarda Nintendont (struct NIN_CFG de
    CommonConfig.h, big endian): por defecto, sus valores de fábrica."""
    datos = bytearray(548)
    struct.pack_into(">IIIIi", datos, 0, 0x01070CF6, 10, 0, 0, language)
    struct.pack_into(">I", datos, 532, max_pads)
    datos[540] = memcard
    return bytes(datos)


def _app() -> HomebrewApp:
    return HomebrewApp(slug="Nintendont", name="Nintendont",
                       zip_url="https://hbb1.oscwii.org/api/contents/Nintendont/"
                               "Nintendont.zip")


@pytest.fixture
def historial(tmp_path):
    return oplog.OperationLog(path=tmp_path / "history.json")


def _registro_con(tmp_path, monkeypatch, contenido: bytes) -> dict:
    assets = tmp_path / "assets"
    (assets / "nintendont").mkdir(parents=True)
    (assets / "nintendont" / "nincfg.bin").write_bytes(contenido)
    monkeypatch.setattr(golden_configs, "_candidate_asset_dirs", lambda: [assets])
    spec = golden_configs.GOLDEN_CONFIGS["Nintendont"]
    return {"Nintendont": GoldenConfigSpec(
        label=spec.label, asset_relative=spec.asset_relative,
        dest_relative=spec.dest_relative, min_size_bytes=spec.min_size_bytes,
        magic=spec.magic)}


def test_el_nincfg_del_repositorio_es_sintetico_y_de_relleno():
    asset = golden_configs.find_asset("nintendont/nincfg.bin")
    datos = asset.read_bytes()

    # Solo la firma y la versión: todo lo demás en cero (MaxPads incluido).
    assert len(datos) == 548
    assert [i for i, b in enumerate(datos) if b] == [0, 1, 2, 3, 7]
    assert datos[:8] == bytes.fromhex("01070cf6 0000000a")
    assert golden_configs.is_placeholder(asset)


@pytest.mark.parametrize("contenido", [
    _nincfg(),                                   # los valores de fábrica
    _nincfg(language=0, max_pads=0, memcard=0)[:-1] + b"\x01",   # difiere en un byte
], ids=["de-fabrica", "otro-byte"])
def test_un_nincfg_que_no_es_el_sintetico_no_es_de_relleno(tmp_path, contenido):
    archivo = tmp_path / "nincfg.bin"
    archivo.write_bytes(contenido)

    assert not golden_configs.is_placeholder(archivo)


def test_instalar_nintendont_no_copia_el_sintetico(tmp_path, monkeypatch, historial):
    def _fake(app, dest_path, cancel_event, on_progress):
        with zipfile.ZipFile(dest_path, "w") as zf:
            for nombre, datos in ZIP_OSC.items():
                zf.writestr(nombre, datos)
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _fake)
    raiz = tmp_path / "usb"
    raiz.mkdir()

    instalado = oscwii_installer.install_app(_app(), raiz)
    dorado = golden_configs.maybe_apply(_app(), raiz, instalado, op_log=historial)

    assert instalado.ok, instalado.error
    assert dorado.status is GoldenConfigStatus.SKIPPED_PLACEHOLDER
    assert not (raiz / "nincfg.bin").exists()
    [nota] = historial.entries()
    assert nota.operation == golden_configs.KEPT_OPERATION
    assert nota.status == oplog.STATUS_OK and nota.target == "Nintendont"
    assert "relleno" in nota.detail and "fábrica" in nota.detail

    from wiibackup_manager.operations import OperationManager
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    toasts = []
    vista = types.SimpleNamespace(
        ops=OperationManager(), op_log=None, _show_toast=toasts.append,
        _install_states={}, _card_by_slug={})
    HomebrewStoreView._on_install_done(vista, _app(), instalado, None, dorado)

    assert ("'Nintendont' se instaló con su configuración de fábrica: la configuración "
            "maestra del repositorio es de relleno y no se copió.") in toasts


def test_un_nincfg_de_verdad_se_sigue_copiando(tmp_path, monkeypatch, historial):
    registro = _registro_con(tmp_path, monkeypatch, _nincfg())
    raiz = tmp_path / "usb"
    raiz.mkdir()

    r = golden_configs.maybe_apply(_app(), raiz, InstallResult(InstallStatus.OK, "x"),
                                   op_log=historial, registry=registro)

    assert r.applied
    assert (raiz / "nincfg.bin").read_bytes() == _nincfg()


@pytest.mark.parametrize("maestro", [None, _nincfg()], ids=["sintetico-real", "de-verdad"])
def test_un_nincfg_que_ya_esta_en_la_unidad_no_se_toca(tmp_path, monkeypatch, historial,
                                                       maestro):
    registro = (golden_configs.GOLDEN_CONFIGS if maestro is None
                else _registro_con(tmp_path, monkeypatch, maestro))
    raiz = tmp_path / "usb"
    raiz.mkdir()
    propio = _nincfg(language=4, max_pads=2, memcard=3)
    (raiz / "nincfg.bin").write_bytes(propio)

    r = golden_configs.maybe_apply(_app(), raiz, InstallResult(InstallStatus.OK, "x"),
                                   op_log=historial, registry=registro)

    assert r.status is GoldenConfigStatus.KEPT_EXISTING
    assert (raiz / "nincfg.bin").read_bytes() == propio



def test_el_sha256_es_del_archivo_entero(tmp_path, monkeypatch):
    """Un relleno binario de más de 4 KB se reconoce igual, y otro que solo
    comparte el principio, no."""
    import hashlib

    grande = bytes(range(256)) * 40                     # 10 KB
    parecido = grande[:-1] + b"\x00"                    # el último era 0xff
    monkeypatch.setattr(golden_configs, "_PLACEHOLDER_SHA256",
                        frozenset({hashlib.sha256(grande).hexdigest()}))
    (tmp_path / "grande.bin").write_bytes(grande)
    (tmp_path / "parecido.bin").write_bytes(parecido)

    assert golden_configs.is_placeholder(tmp_path / "grande.bin")
    assert not golden_configs.is_placeholder(tmp_path / "parecido.bin")
