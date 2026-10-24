"""Qué hace la Store con cada ruta de un ZIP: la instala, la omite o
rechaza el paquete entero.

39 apps del catálogo de Open Shop Channel escriben fuera de `apps/` y
`controllers/` y se rechazaban enteras. `tests/entradas_osc_fuera_de_apps.json`
trae sus rutas reales. Se clasifican en:

- `Destino.APP`: `apps/` y `controllers/`, como siempre.
- `Destino.DATOS`: una carpeta de datos conocida de la app (`smw/`,
  `mame/`, `WiiFlow/plugins/`...), una ruta fija que la app lee
  (`usr/local/lib/timidity/` de OpenTTD) o `/database.txt`, que
  AnyTitleDeleterMOD abre por ese nombre.
- `Destino.OMITIDO`: documentación suelta en la raíz (`readme.txt`,
  `LICENSE`...), `__MACOSX/`, la guía en HTML del Homebrew Browser, el
  código fuente de gcmm y `neek2obooter.*` de postloader, que es para
  neek: no se copian.
- None: cualquier otra cosa, y el paquete se sigue rechazando entero.

Y ninguna ruta puede terminar fuera de la raíz de la unidad, ni siquiera
a través de un enlace simbólico que ya esté en ella.
"""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from wiibackup_manager import oscwii_installer
from wiibackup_manager.oscwii_installer import Destino

FIXTURE = Path(__file__).with_name("entradas_osc_fuera_de_apps.json")


def _catalogo() -> dict:
    datos = json.loads(FIXTURE.read_text(encoding="utf-8"))
    datos.pop("_origen")
    return datos


# Todo lo que se omite de las 39 apps, con su motivo. Si el catálogo trae
# algo nuevo que se omita, este test obliga a mirarlo.
OMITIDOS = {
    "fceugx-channel-installer": {"readme.txt"},
    "FCEURX": {"readme_FCEURX.txt"},
    "ftpii": {"README"},
    "gcmm": {"gcmm_1.5.2/"},
    "gnuboy": {"gnuboy-gx.txt", "history.txt", "libogc_license.txt"},
    "gxgeo": {"READ.MII"},
    "hatari": {"Manual_HatariWii.pdf", "README.txt", "manual.html"},
    "homebrew_browser": {"Homebrew Browser Guide and Help/"},
    "MinimaLauncher": {"__MACOSX/"},
    "postloader": {"gpl.txt", "neek2obooter.app", "neek2obooter.dol",
                   "postloader_historii.txt", "postloader_readmii.txt"},
    "priiloader": {"LICENSE"},
    "snes9xgx-channel-installer": {"readme.txt"},
    "Snes9xRX": {"readme_S9xRX.txt"},
    "vbagx-channel-installer": {"readme.txt"},
    "wiimmfipatcher": {"readme.txt"},
    "WiiPhysics": {"WiiPhysics_Readme.txt"},
    "wiiSXR": {"README"},
}


def _resumen_omitidos(nombres) -> set:
    """Las carpetas omitidas se resumen en su primer nivel con "/"."""
    return {n.split("/")[0] + "/" if "/" in n else n for n in nombres}


def test_el_fixture_tiene_las_39_apps():
    assert len(_catalogo()) == 39


def test_ninguna_ruta_de_las_39_apps_queda_bloqueada():
    bloqueadas = [(slug, nombre) for slug, entradas in _catalogo().items()
                  for nombre, _tam in entradas
                  if oscwii_installer._clasificar(nombre) is None]
    assert bloqueadas == []


def test_lo_que_se_omite_es_exactamente_la_documentacion_conocida():
    omitidos = {}
    for slug, entradas in _catalogo().items():
        nombres = [n for n, _t in entradas
                   if oscwii_installer._clasificar(n) is Destino.OMITIDO]
        if nombres:
            omitidos[slug] = _resumen_omitidos(nombres)
    assert omitidos == OMITIDOS


def test_apps_y_controllers_siguen_siendo_de_la_app():
    for slug, entradas in _catalogo().items():
        for nombre, _t in entradas:
            es_app = nombre.split("/")[0] in ("apps", "controllers")
            assert (oscwii_installer._clasificar(nombre) is Destino.APP) == es_app, nombre


@pytest.mark.parametrize("nombre", [
    "smw/gfx/packs/Classic/tilesets/SMB1/tileset.png",
    "WiiFlow/plugins/hatari.ini",
    "wiiflow/plugins/vbcgx.ini",
    "usr/local/lib/timidity/Drum_000/035_Kick_1.pat",
    "etc/timidity/timidity.cfg",
    "data/supersonicwii/assets/menu/x.png",
    "wad/plforwarder.5.1.5.wad",
    "database.txt",
    "MCBACKUP/not64.gci",
])
def test_carpetas_de_datos(nombre):
    assert oscwii_installer._clasificar(nombre) is Destino.DATOS


@pytest.mark.parametrize("nombre", [
    "roms/juego.gcm",            # una carpeta que no está en la lista
    "boot.dol",                  # un ejecutable suelto en la raíz
    "autoexec.bat",
    "data/otra_app/x.png",       # data/ solo para supersonicwii
    "usr/bin/sh",                # usr/ solo para timidity
    "etc/passwd",
    "wad/boot.dol",              # en wad/ solo van WADs
    "smw",                       # el nombre de una carpeta de datos como archivo
    "data/supersonicwii",
    "apps",                      # ni siquiera apps/ como archivo
    "private/wii/title/x.bin",
])
def test_lo_que_no_esta_en_la_lista_se_bloquea(nombre):
    assert oscwii_installer._clasificar(nombre) is None


def test_la_comparacion_no_distingue_mayusculas():
    """FAT32 no distingue mayúsculas: `WiiFlow/` y `wiiflow/` son la misma
    carpeta, y los ZIP de hatari y vbcgx usan una y otra."""
    for nombre in ("SMW/x.wav", "Smw/x.wav", "USR/LOCAL/LIB/TIMIDITY/x.pat",
                   "Database.TXT"):
        assert oscwii_installer._clasificar(nombre) is Destino.DATOS, nombre


# -------------------------------------------------------------- contención --
def test_un_symlink_de_apps_hacia_afuera_se_rechaza(tmp_path):
    """La ruta resuelta se comparaba contra `apps/` ya resuelta: si `apps`
    es un enlace a otro lado, las dos apuntan afuera y el control pasaba.
    En FAT32 no hay enlaces, pero el destino puede ser una carpeta
    cualquiera elegida a mano."""
    raiz = tmp_path / "usb"
    raiz.mkdir()
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    os.symlink(afuera, raiz / "apps")

    assert not oscwii_installer._is_safe_member("apps/X/boot.dol", raiz)


def test_un_symlink_adentro_de_la_raiz_no_se_rechaza(tmp_path):
    raiz = tmp_path / "usb"
    (raiz / "otra").mkdir(parents=True)
    os.symlink(raiz / "otra", raiz / "apps")

    assert oscwii_installer._is_safe_member("apps/X/boot.dol", raiz)


def test_la_raiz_puede_ser_un_symlink(tmp_path):
    """Lo que tiene que quedar adentro es la raíz REAL de la unidad: un
    destino elegido a través de un enlace sigue siendo válido."""
    real = tmp_path / "real"
    real.mkdir()
    os.symlink(real, tmp_path / "enlace")

    assert oscwii_installer._is_safe_member("apps/X/boot.dol", tmp_path / "enlace")


# ------------------------------------------------------------------- plan --
def _infos(nombres) -> list:
    return [zipfile.ZipInfo(n) for n in nombres]


def test_el_plan_separa_unidades_sueltos_datos_y_omitidos():
    plan = oscwii_installer._planificar(_infos([
        "apps/hatari/boot.dol",
        "apps/hatari/meta.xml",
        "controllers/perfil.ini",
        "hatari/fd/yak.st",
        "WiiFlow/plugins/hatari.ini",
        "README.txt",
        "__MACOSX/hatari/._meta.xml",
    ]))

    assert {k: [i.filename for i in v] for k, v in plan.unidades.items()} == {
        "apps/hatari": ["apps/hatari/boot.dol", "apps/hatari/meta.xml"]}
    assert [i.filename for i in plan.sueltos] == ["controllers/perfil.ini"]
    assert [i.filename for i in plan.datos] == ["hatari/fd/yak.st",
                                                "WiiFlow/plugins/hatari.ini"]
    assert plan.omitidos == ("README.txt", "__MACOSX/hatari/._meta.xml")


def test_por_ahora_un_zip_con_carpetas_de_datos_se_sigue_rechazando(tmp_path):
    """Hasta que exista la escritura que fusiona sin borrar lo del usuario,
    aceptar estas rutas instalaría la app sin sus datos. Este test cambia
    cuando llegue esa escritura."""
    from homebrew_falso import dol
    zip_path = tmp_path / "smw.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("apps/smw-wiigii/boot.dol", dol(b"A"))
        zf.writestr("smw/sfx/x.wav", b"RIFF")
        zf.writestr("readme.txt", b"hola")
    raiz = tmp_path / "usb"
    raiz.mkdir()

    zf, status, err = oscwii_installer._validate_zip(zip_path, raiz)

    assert zf is None
    assert status is oscwii_installer.InstallStatus.UNSAFE_ZIP
