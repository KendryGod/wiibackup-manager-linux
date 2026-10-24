"""La Store valida `boot.dol`, `boot.elf` y los `.wad` antes de instalar.

`testzip` comprueba que el ZIP esté sano (el CRC de cada archivo), no que
lo de adentro sirva: un ZIP íntegro con un `boot.dol` que es un "Not
Found" de 9 bytes, un "404: Not Found" o una página HTML se instalaba como
"Instalada ✓" (reproducido con el instalador real). Ahora se valida la
ESTRUCTURA de cada uno -nunca un tamaño mínimo: en el catálogo hay
archivos legítimos de 9 bytes, y un DOL válido puede medir 260-:

- DOL: la tabla de secciones (0x100 bytes). Cada sección tiene que caer
  dentro del archivo y en la memoria de la Wii, tiene que haber código y
  el punto de entrada tiene que estar en él.
- ELF: firma, 32 bits, big-endian, PowerPC; los segmentos dentro del
  archivo y el punto de entrada en uno de ellos.
- WAD: cabecera de 0x20 con tipo "Is" o "ib", secciones que entran en el
  archivo y un TMD coherente con los datos.

El Homebrew Channel decide si `boot.dol` o `boot.elf` es DOL o ELF por el
contenido, no por el nombre: en el catálogo, el `boot.dol` de "grim"
(ResidualVM) es un ELF.

Las reglas se midieron contra el catálogo entero de Open Shop Channel el
2026-10-24 (299 apps, 395 archivos: 372 DOL, 22 ELF y un WAD): pasan
todos. Acá van 13 de esos (`tests/ejecutables_osc.json`).
"""
from __future__ import annotations

import gettext
import struct
import types
import zipfile
from pathlib import Path

import pytest

from homebrew_falso import archivo_real, dol, elf, reales, wad
from wiibackup_manager import oscwii_installer, wii_formats
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallResult, InstallStatus

REALES = reales()
HTML = (b"<!DOCTYPE html>\n<html><head><title>404 Not Found</title></head>"
        b"<body><h1>Not Found</h1><p>The requested URL was not found on this "
        b"server.</p></body></html>\n") * 8


def _problema(nombre: str, datos: bytes):
    return wii_formats.problema(nombre, datos[:wii_formats.CABEZA], len(datos))


# ---------------------------------------------------- Los archivos reales --
@pytest.mark.parametrize("real", REALES, ids=lambda r: f"{r['app']}:{r['archivo']}")
def test_acepta_los_archivos_reales_del_catalogo(real):
    if real["tipo"] == "wad":
        problema = wii_formats.problema_wad(real["cabeza"], real["tamano"])
    else:
        problema = wii_formats.problema_ejecutable(real["cabeza"], real["tamano"])
    assert problema is None, problema


def test_son_los_trece_que_se_midieron():
    assert len(REALES) == 13
    assert sorted({r["tipo"] for r in REALES}) == ["dol", "elf", "wad"]


def test_el_boot_dol_de_grim_es_un_elf_y_se_acepta():
    grim = next(r for r in REALES if r["app"] == "grim")
    assert grim["archivo"].endswith("boot.dol") and grim["cabeza"][:4] == b"\x7fELF"
    assert wii_formats.problema(grim["archivo"], grim["cabeza"], grim["tamano"]) is None


# --------------------------------------------------- Lo que se rechaza --
BASURA = {
    "Not Found de 9 bytes": b"Not Found",
    "404: Not Found": b"404: Not Found",
    "una página HTML": HTML,
    "vacío": b"",
}


@pytest.mark.parametrize("nombre", ["apps/App/boot.dol", "apps/App/boot.elf",
                                    "apps/App/BOOT.DOL", "wad/forwarder.wad"])
@pytest.mark.parametrize("contenido", list(BASURA.values()), ids=list(BASURA))
def test_rechaza_la_basura(nombre, contenido):
    assert _problema(nombre, contenido) is not None


def test_no_hay_un_tamano_minimo():
    """Lo chico pero bien formado pasa: lo que se mira es la estructura."""
    assert len(dol(b"\x60\x00\x00\x00")) == 260
    assert _problema("apps/App/boot.dol", dol(b"\x60\x00\x00\x00")) is None
    assert _problema("apps/App/boot.elf", elf(b"\x60\x00\x00\x00")) is None
    assert _problema("wad/x.wad", wad([b"x"])) is None


@pytest.mark.parametrize("direccion", [0x80004000, 0x90000000, 0xC0004000, 0xD0000000])
def test_un_dol_puede_cargar_en_mem1_o_en_mem2(direccion):
    """Los del catálogo cargan todos en MEM1 con caché, pero MEM2 (y las
    direcciones sin caché de las dos) también son memoria de la Wii."""
    d = _con(_con(dol(), 0x48, ">I", direccion), 0xE0, ">I", direccion)
    assert wii_formats.problema_ejecutable(d, len(d)) is None


def test_los_demas_archivos_no_se_validan():
    for nombre in ("apps/App/meta.xml", "apps/App/icon.png", "neek2obooter.dol",
                   "apps/App/datos.bin", "apps/gcmm/gcmm_geckoA.cli"):
        assert not wii_formats.hay_que_validar(nombre)
        assert _problema(nombre, b"Not Found") is None


def _con(datos: bytes, offset: int, formato: str, *valores) -> bytes:
    b = bytearray(datos)
    struct.pack_into(formato, b, offset, *valores)
    return bytes(b)


def _real(app: str, archivo: str) -> dict:
    return next(r for r in REALES if r["app"] == app and r["archivo"].endswith(archivo))


def test_dol_real_cortado():
    r = _real("WiiDonut", "boot.dol")
    assert wii_formats.problema_ejecutable(r["cabeza"], r["tamano"] // 2) is not None


def _solo_datos() -> bytes:
    """El código de `dol()` movido a la primera sección de DATOS (la 7)."""
    d = _con(dol(), 0x1C, ">I", 0x100)               # offset de data0
    d = _con(d, 0x48 + 0x1C, ">I", 0x80004000)       # dirección de data0
    d = _con(d, 0x90 + 0x1C, ">I", 4)                # tamaño de data0
    return _con(_con(d, 0x90, ">I", 0), 0x00, ">I", 0)  # text0 vacía


def _codigo_en(direccion: int) -> bytes:
    """`dol()` con el código -y el punto de entrada, para que lo único
    raro sea la dirección- en `direccion`."""
    return _con(_con(dol(), 0x48, ">I", direccion), 0xE0, ">I", direccion)


@pytest.mark.parametrize("roto", [
    _codigo_en(0x00004000),                # código en una dirección que no es RAM
    _codigo_en(0x817FFFFE),                # empieza en MEM1 y se pasa del final
    _con(dol(), 0xE0, ">I", 0x80000000),   # entrada fuera del código
    _con(dol(), 0x00, ">I", 0x20),         # sección que pisa la tabla
    dol()[:0x80],                          # la tabla cortada a la mitad
    b"A" * 100,                            # ni la tabla entera
])
def test_dol_mal_formado(roto):
    assert wii_formats.problema_ejecutable(roto, len(roto)) is not None


def test_un_dol_sin_codigo_lo_dice():
    """Sin secciones de código el punto de entrada tampoco puede caer en
    él, pero el motivo que se muestra es el de verdad."""
    assert wii_formats.problema_ejecutable(_solo_datos(), len(_solo_datos())) == \
        "el DOL no tiene código"


@pytest.mark.parametrize("roto", [
    _con(elf(), 18, ">H", 3),              # x86
    _con(elf(), 18, ">H", 62),             # x86-64
    _con(elf(), 5, ">B", 1),               # little-endian
    _con(elf(), 4, ">B", 2),               # 64 bits
    _con(elf(), 44, ">H", 0),              # sin segmentos
    _con(elf(), 42, ">H", 16),             # entradas más cortas que las de ELF32
    _con(elf(), 52 + 16, ">I", 1 << 20),   # segmento más grande que el archivo
    _con(elf(), 24, ">I", 0x90000000),     # entrada fuera de los segmentos
    _con(elf(), 28, ">I", 1 << 20),        # tabla de segmentos fuera del archivo
])
def test_elf_mal_formado(roto):
    assert wii_formats.problema_ejecutable(roto, len(roto)) is not None


def test_wad_real_cortado():
    r = _real("postloader", ".wad")
    assert wii_formats.problema_wad(r["cabeza"], r["tamano"] - 1000) is not None


@pytest.mark.parametrize("roto", [
    _con(wad(), 0, ">I", 0x40),                  # cabecera de otro tamaño
    wad(tipo=b"Bk"),                             # WAD de respaldo, no instalable
    _con(wad(), 0x14, ">I", 0x1E4 + 36 * 2),     # TMD que no coincide con sus contenidos
    # Datos más grandes que los contenidos, aunque entren en el archivo.
    _con(wad([b"x" * 100]) + bytes(0x200), 0x18, ">I", 0x200),
    _con(wad([b"x" * 100]), 0x18, ">I", 0x40),   # datos más chicos que los contenidos
])
def test_wad_mal_formado(roto):
    assert wii_formats.problema_wad(roto, len(roto)) is not None


# ------------------------------------------------- En la instalación --
def _instalar(tmp_path, monkeypatch, miembros: dict, previo: dict | None = None):
    def _descarga(app, dest_path, cancel_event, on_progress):
        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for nombre, datos in miembros.items():
                zf.writestr(nombre, datos)
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _descarga)
    destino = tmp_path / "usb"
    for nombre, datos in (previo or {}).items():
        (destino / nombre).parent.mkdir(parents=True, exist_ok=True)
        (destino / nombre).write_bytes(datos)
    destino.mkdir(exist_ok=True)
    app = HomebrewApp(slug="App", name="App", zip_url="https://hbb1.oscwii.org/x.zip")
    return oscwii_installer.install_app(app, destino), destino


def _archivos(raiz: Path) -> dict:
    return {str(p.relative_to(raiz)): p.read_bytes() for p in raiz.rglob("*") if p.is_file()}


@pytest.mark.parametrize("nombre, contenido", [
    ("apps/App/boot.dol", b"Not Found"),
    ("apps/App/boot.elf", b"404: Not Found"),
    ("apps/App/BOOT.DOL", HTML),
    ("apps/App/forwarder.wad", HTML),
])
def test_un_paquete_con_un_ejecutable_roto_no_se_instala(tmp_path, monkeypatch,
                                                          nombre, contenido):
    resultado, destino = _instalar(tmp_path, monkeypatch, {
        nombre: contenido, "apps/App/meta.xml": b"<app/>"})

    assert resultado.status is InstallStatus.BAD_CONTENT, resultado
    assert nombre in resultado.error
    assert _archivos(destino) == {}


def test_la_version_instalada_queda_entera(tmp_path, monkeypatch):
    """No se llega a tocar la unidad: la versión anterior sigue igual."""
    previo = {"apps/App/boot.dol": dol(b"vieja"), "apps/App/meta.xml": b"<vieja/>"}

    resultado, destino = _instalar(tmp_path, monkeypatch,
                                   {"apps/App/boot.dol": b"404: Not Found"}, previo)

    assert resultado.status is InstallStatus.BAD_CONTENT
    assert _archivos(destino) == previo


@pytest.mark.parametrize("app, archivo, nombre", [
    ("Eject_DVD", "boot.dol", "apps/App/boot.dol"),
    ("TinyLoad", "boot.elf", "apps/App/boot.elf"),
    ("grim", "boot.dol", "apps/App/boot.dol"),
    ("postloader", ".wad", "apps/App/plforwarder.wad"),
])
def test_los_archivos_reales_se_instalan(tmp_path, monkeypatch, app, archivo, nombre):
    datos = archivo_real(_real(app, archivo))

    resultado, destino = _instalar(tmp_path, monkeypatch, {nombre: datos})

    assert resultado.ok, resultado
    assert (destino / nombre).read_bytes() == datos


# ---------------------------------------------------------- La tienda --
def _mostrar(resultado):
    from wiibackup_manager.operations import OperationManager
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView
    toasts = []
    vista = types.SimpleNamespace(
        ops=OperationManager(), op_log=None, _show_toast=toasts.append,
        _install_states={}, _card_by_slug={})
    HomebrewStoreView._on_install_done(vista, HomebrewApp(slug="App", name="App"),
                                       resultado, None, None)
    return vista._install_states["App"].message, toasts


def test_la_tarjeta_dice_que_el_paquete_trae_un_archivo_danado():
    etiqueta, toasts = _mostrar(InstallResult(
        InstallStatus.BAD_CONTENT, "App", "apps/App/boot.dol está dañado: ..."))

    assert etiqueta == "El paquete trae un archivo dañado"
    assert "apps/App/boot.dol" in toasts[0]


def test_en_ingles(tmp_path, monkeypatch):
    from test_smoke_gui import _poner_el_catalogo_en_ingles
    from wiibackup_manager.widgets.homebrew_store_view import _INSTALL_STATUS_LABELS
    locale = Path(__file__).resolve().parent.parent / "data" / "locale"
    en = gettext.translation("wiibackup-manager", str(locale), languages=["en"])
    _poner_el_catalogo_en_ingles(monkeypatch, en)

    resultado, _destino = _instalar(tmp_path, monkeypatch, {"apps/App/boot.dol": b"Not Found"})

    assert "dañado" not in resultado.error and "DOL" in resultado.error, resultado.error
    assert en.gettext(_INSTALL_STATUS_LABELS[InstallStatus.BAD_CONTENT]) != \
        _INSTALL_STATUS_LABELS[InstallStatus.BAD_CONTENT]
    for basura in BASURA.values():
        for nombre in ("boot.dol", "boot.elf", "x.wad"):
            motivo = _problema(nombre, basura)
            assert motivo and not any(p in motivo for p in ("tabla", "sección", "cabecera",
                                                            "firma", "segmento")), motivo
