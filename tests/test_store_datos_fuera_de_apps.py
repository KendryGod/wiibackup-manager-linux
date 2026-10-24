"""La Store instala las carpetas de datos fuera de `apps/` sin borrar ni
pisar lo del usuario.

En `apps/` la carpeta de la app se reemplaza entera, como siempre. Fuera de
`apps/` (`smw/`, `uae/`, `mame/`...) viven también las ROMs, las partidas y
la configuración del usuario: `uae/saves/dummy` reemplazando `uae/saves`
entera borraría sus partidas, y `wiicolem.conf` (0 bytes en el paquete)
vaciaría su configuración. Ahí se fusiona:

- lo que no existe entra (una carpeta nueva entra entera, de una);
- lo que ya está igual no se toca;
- un archivo que ya existe con otro contenido se conserva, salvo que se
  pida reemplazar; uno de 0 bytes del paquete NUNCA reemplaza;
- nada que el paquete no traiga se borra;
- todo va en la misma transacción que `apps/`: si algo falla o se
  cancela, la unidad vuelve a estar como antes.
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

PAQUETE = {
    "apps/smw-wiigii/boot.dol": dol(b"smw"),
    "apps/smw-wiigii/meta.xml": b"<app/>",
    "smw/sfx/announcer/Mario/lets_go.wav": b"RIFF lets go",
    "smw/gfx/packs/Classic/tilesets/SMB1/tileset.png": b"PNG smb1",
    "smw/worlds/Contest.txt": b"mundo del paquete",
    "uae/saves/dummy": b"",
    "mame/mame.ini": b"ini del paquete",
    "wiicolem/wiicolem.conf": b"",
    "readme.txt": b"leeme",
    "__MACOSX/smw/._meta.xml": b"basura",
}


def _app() -> HomebrewApp:
    return HomebrewApp(slug="smw-wiigii", name="Super Mario War",
                       zip_url="https://hbb1.oscwii.org/api/contents/smw-wiigii/x.zip")


@pytest.fixture
def paquete(monkeypatch):
    """`_download_zip` escribe este dict como ZIP; se puede cambiar antes
    de instalar."""
    entradas = dict(PAQUETE)

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


def _poner(raiz: Path, archivos: dict) -> None:
    for nombre, datos in archivos.items():
        (raiz / nombre).parent.mkdir(parents=True, exist_ok=True)
        (raiz / nombre).write_bytes(datos)


def _foto(raiz: Path) -> dict:
    return {str(p.relative_to(raiz)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(raiz.rglob("*"))}


def _instalar(raiz, **kw):
    return oscwii_installer.install_app(_app(), raiz, **kw)


# ------------------------------------------------------- primera vez --
def test_en_una_unidad_vacia_se_instala_todo_menos_lo_omitido(raiz, paquete):
    r = _instalar(raiz)

    assert r.ok, r.error
    foto = _foto(raiz)
    for nombre, datos in PAQUETE.items():
        if nombre in ("readme.txt", "__MACOSX/smw/._meta.xml"):
            assert nombre not in foto
        else:
            assert foto[nombre] == datos, nombre
    assert not (raiz / "__MACOSX").exists()
    assert set(r.omitidos) == {"readme.txt", "__MACOSX/smw/._meta.xml"}
    assert raiz / "smw/worlds/Contest.txt" in r.datos_escritos
    assert raiz / "mame/mame.ini" in r.datos_escritos
    assert not any("apps" in p.relative_to(raiz).parts[:1] for p in r.datos_escritos)
    # Ni restos ocultos.
    assert [n for n in foto if "/." in "/" + n] == []


# --------------------------------------------- lo del usuario se queda --
def test_reinstalar_no_borra_lo_que_el_usuario_tiene_en_las_carpetas_de_datos(raiz, paquete):
    _poner(raiz, {
        "uae/saves/partida.sav": b"mi partida",
        "uae/roms/kick.rom": b"mi rom",
        "smw/worlds/MiMundo.txt": b"mi mundo",
        "smw/sfx/viejo_que_ya_no_viene.wav": b"de la version anterior",
    })

    r = _instalar(raiz)

    assert r.ok, r.error
    assert (raiz / "uae/saves/partida.sav").read_bytes() == b"mi partida"
    assert (raiz / "uae/roms/kick.rom").read_bytes() == b"mi rom"
    assert (raiz / "smw/worlds/MiMundo.txt").read_bytes() == b"mi mundo"
    assert (raiz / "smw/sfx/viejo_que_ya_no_viene.wav").exists()
    assert (raiz / "uae/saves/dummy").read_bytes() == b""


def test_un_archivo_distinto_del_usuario_se_conserva_por_defecto(raiz, paquete):
    _poner(raiz, {"mame/mame.ini": b"mi configuracion"})

    r = _instalar(raiz)

    assert r.ok, r.error
    assert (raiz / "mame/mame.ini").read_bytes() == b"mi configuracion"
    assert raiz / "mame/mame.ini" in r.datos_conservados
    assert raiz / "mame/mame.ini" not in r.datos_escritos


def test_con_reemplazar_un_archivo_distinto_se_reemplaza(raiz, paquete):
    _poner(raiz, {"mame/mame.ini": b"mi configuracion"})

    r = _instalar(raiz, reemplazar=True)

    assert r.ok, r.error
    assert (raiz / "mame/mame.ini").read_bytes() == b"ini del paquete"
    assert raiz / "mame/mame.ini" in r.datos_escritos
    assert raiz / "mame/mame.ini" not in r.datos_conservados
    assert [n for n in _foto(raiz) if "wbm-" in n or "parcial" in n] == []


def test_un_archivo_vacio_del_paquete_nunca_reemplaza_aunque_se_pida(raiz, paquete):
    _poner(raiz, {"wiicolem/wiicolem.conf": b"mi configuracion",
                  "uae/saves/dummy": b"algo que puso el usuario"})

    r = _instalar(raiz, reemplazar=True)

    assert r.ok, r.error
    assert (raiz / "wiicolem/wiicolem.conf").read_bytes() == b"mi configuracion"
    assert (raiz / "uae/saves/dummy").read_bytes() == b"algo que puso el usuario"
    assert {raiz / "wiicolem/wiicolem.conf", raiz / "uae/saves/dummy"} <= set(r.datos_conservados)


def test_lo_que_ya_esta_igual_no_se_toca(raiz, paquete):
    _poner(raiz, {"smw/worlds/Contest.txt": b"mundo del paquete"})
    antes = os.stat(raiz / "smw/worlds/Contest.txt")

    r = _instalar(raiz, reemplazar=True)

    assert r.ok, r.error
    despues = os.stat(raiz / "smw/worlds/Contest.txt")
    assert (despues.st_ino, despues.st_mtime_ns) == (antes.st_ino, antes.st_mtime_ns)
    assert raiz / "smw/worlds/Contest.txt" not in r.datos_escritos
    assert raiz / "smw/worlds/Contest.txt" not in r.datos_conservados


def test_una_carpeta_del_usuario_donde_el_paquete_trae_un_archivo_se_conserva(raiz, paquete):
    _poner(raiz, {"mame/mame.ini/adentro.txt": b"del usuario"})

    r = _instalar(raiz, reemplazar=True)

    assert r.ok, r.error
    assert (raiz / "mame/mame.ini/adentro.txt").read_bytes() == b"del usuario"
    assert raiz / "mame/mame.ini" in r.datos_conservados


def test_un_archivo_del_usuario_donde_el_paquete_necesita_una_carpeta_se_conserva(raiz, paquete):
    _poner(raiz, {"smw/worlds": b"un archivo que se llama worlds"})

    r = _instalar(raiz, reemplazar=True)

    assert r.ok, r.error
    assert (raiz / "smw/worlds").read_bytes() == b"un archivo que se llama worlds"
    assert raiz / "smw/worlds/Contest.txt" in r.datos_conservados


# ------------------------------------------------------- mayúsculas --
def test_se_escribe_en_la_carpeta_que_ya_existe_aunque_cambien_las_mayusculas(raiz, paquete):
    """En FAT32 es la misma carpeta; en una carpeta de ext4 elegida a mano
    se crearían dos."""
    paquete.clear()
    paquete.update({"apps/vbcgx/boot.dol": dol(b"vbcgx"),
                    "wiiflow/plugins/vbcgx.ini": b"[vbcgx]"})
    _poner(raiz, {"WiiFlow/plugins/otro.ini": b"del usuario"})

    r = _instalar(raiz)

    assert r.ok, r.error
    assert sorted(os.listdir(raiz)) == ["WiiFlow", "apps"]
    assert (raiz / "WiiFlow/plugins/vbcgx.ini").read_bytes() == b"[vbcgx]"
    assert (raiz / "WiiFlow/plugins/otro.ini").read_bytes() == b"del usuario"


def test_dos_grafias_en_el_mismo_paquete_lo_rechazan(raiz, paquete):
    """Como en `apps/`: un paquete ambiguo para FAT32 no se instala."""
    paquete.clear()
    paquete.update({"apps/hatari/boot.dol": dol(b"hatari"),
                    "WiiFlow/plugins/hatari.ini": b"a",
                    "wiiflow/plugins/Hatari-alt/CFG/hatari.ini": b"b"})

    r = _instalar(raiz)

    assert r.status is InstallStatus.UNSAFE_ZIP, r
    assert os.listdir(raiz) == []


# --------------------------------------------------------- todo o nada --
def test_una_falla_al_intercambiar_deja_la_unidad_como_estaba(raiz, paquete, monkeypatch):
    _poner(raiz, {"mame/mame.ini": b"mi configuracion",
                  "smw/worlds/MiMundo.txt": b"mi mundo",
                  "apps/smw-wiigii/boot.dol": dol(b"version anterior")})
    antes = _foto(raiz)
    real = os.replace

    def _replace(origen, destino):
        if Path(destino).name == "tileset.png" or Path(destino).name == "uae":
            raise OSError(5, "simulado: falla al intercambiar")
        return real(origen, destino)

    monkeypatch.setattr(atomicfs.os, "replace", _replace)

    r = _instalar(raiz, reemplazar=True)

    assert r.status is InstallStatus.IO_ERROR, r
    assert _foto(raiz) == antes


def test_cancelar_al_final_deja_la_unidad_como_estaba(raiz, paquete, monkeypatch):
    _poner(raiz, {"mame/mame.ini": b"mi configuracion"})
    antes = _foto(raiz)
    cancelar = threading.Event()
    real = os.replace

    def _replace(origen, destino):
        resultado = real(origen, destino)
        cancelar.set()
        return resultado

    monkeypatch.setattr(atomicfs.os, "replace", _replace)

    r = _instalar(raiz, reemplazar=True, cancel_event=cancelar)

    assert r.status is InstallStatus.CANCELLED, r
    assert _foto(raiz) == antes


def test_si_algo_aparece_donde_iba_una_carpeta_nueva_no_se_pisa(raiz, paquete, monkeypatch):
    """Entre que se decide qué es nuevo y el intercambio, el usuario (u otro
    programa) puede crear la carpeta. Lo nuevo nunca aparta algo que existe:
    se deshace todo y lo que apareció queda intacto."""
    real = oscwii_installer._copiar_miembro

    def _copiar(zf, info, destino):
        real(zf, info, destino)
        if info.filename == "mame/mame.ini" and not (raiz / "uae").exists():
            _poner(raiz, {"uae/saves/partida.sav": b"apareci"})

    monkeypatch.setattr(oscwii_installer, "_copiar_miembro", _copiar)

    r = _instalar(raiz)

    assert not r.ok
    assert (raiz / "uae/saves/partida.sav").read_bytes() == b"apareci"
    assert sorted(os.listdir(raiz)) == ["uae"]


# ---------------------------------------------------------- atomicfs --
@pytest.mark.parametrize("es_carpeta", [True, False])
def test_stagedswap_exclusivo_no_aparta_lo_que_existe(tmp_path, es_carpeta):
    destino = tmp_path / "nuevo"
    otro = tmp_path / "otro"
    otro.write_bytes(b"viejo")
    tx = atomicfs.StagedSwap(staging_marca="st", backup_marca="resp")
    tx.stage_file(otro).write_bytes(b"nuevo")
    if es_carpeta:
        (tx.stage_dir(destino, exclusivo=True) / "x").write_bytes(b"x")
    else:
        tx.stage_file(destino, exclusivo=True).write_bytes(b"x")
    destino.write_bytes(b"aparecio")

    with pytest.raises(FileExistsError):
        tx.commit()

    assert destino.read_bytes() == b"aparecio"
    assert otro.read_bytes() == b"viejo"
    assert sorted(os.listdir(tmp_path)) == ["nuevo", "otro"]


# ---------------------------------------------- validación del paquete --
def test_un_zip_con_carpetas_de_datos_ya_no_se_rechaza(tmp_path):
    zip_path = tmp_path / "smw.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for nombre, datos in PAQUETE.items():
            zf.writestr(nombre, datos)
    r = tmp_path / "usb"
    r.mkdir()

    zf, status, err = oscwii_installer._validate_zip(zip_path, r)

    assert status is None, err
    zf.close()


def test_lo_que_no_esta_en_la_lista_sigue_rechazando_el_paquete(raiz, paquete):
    paquete["roms/juego.gcm"] = b"x"

    r = _instalar(raiz)

    assert r.status is InstallStatus.UNSAFE_ZIP
    assert "roms/juego.gcm" in r.error
    assert os.listdir(raiz) == []


def test_mismo_tamano_pero_otro_contenido_es_distinto(raiz, paquete):
    _poner(raiz, {"mame/mame.ini": b"ini DEL usuario"})
    assert len(b"ini DEL usuario") == len(PAQUETE["mame/mame.ini"])

    r = _instalar(raiz)

    assert r.ok, r.error
    assert (raiz / "mame/mame.ini").read_bytes() == b"ini DEL usuario"
    assert raiz / "mame/mame.ini" in r.datos_conservados


def test_si_aparece_un_archivo_nuevo_en_una_carpeta_que_ya_estaba_no_se_pisa(
        raiz, paquete, monkeypatch):
    _poner(raiz, {"mame/roms/mia.zip": b"mi rom"})
    real = oscwii_installer._copiar_miembro

    def _copiar(zf, info, destino):
        real(zf, info, destino)
        if info.filename == "mame/mame.ini":
            _poner(raiz, {"mame/mame.ini": b"apareci"})

    monkeypatch.setattr(oscwii_installer, "_copiar_miembro", _copiar)

    r = _instalar(raiz)

    assert not r.ok
    assert (raiz / "mame/mame.ini").read_bytes() == b"apareci"
    assert (raiz / "mame/roms/mia.zip").read_bytes() == b"mi rom"
    assert sorted(os.listdir(raiz)) == ["mame"]
