"""Instalar desde una carpeta de esta PC (`oscwii_installer.install_local_folder`).

Una app que no está en Open Shop Channel, o la carpeta que el taller arma
con lo que va en cada SD, se instala con las mismas reglas que un paquete
del catálogo. La carpeta puede tener dos formas:

- la raíz de una unidad: trae `apps/` (y puede traer `controllers/`,
  `wad/` y las carpetas de datos conocidas), y se copia tal cual;
- la carpeta de una app: trae su `boot.dol` o `boot.elf`, y va a
  `apps/<nombre de la carpeta>/`.

Antes de tocar la unidad se recorre la carpeta sin seguir enlaces, y se
rechaza entera si trae un enlace simbólico, un archivo especial, un nombre
que FAT32 no acepta, algo fuera de las carpetas permitidas, dos nombres que
en FAT32 son el mismo o más de 512 MB. Con eso se arma un ZIP sin comprimir
en la caché del usuario (no en /tmp, que en Fedora vive en RAM), y desde
ahí es el mismo camino que una app del catálogo: la misma validación, el
mismo aviso (que acá sale siempre: no hay catálogo detrás que diga qué
trae), el espacio, la transacción y lo del cliente que se conserva.
"""
from __future__ import annotations

import os
import shutil
import threading
import zipfile
from pathlib import Path

import pytest

from homebrew_falso import dol, elf, wad
from wiibackup_manager import config, oscwii_installer
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallStatus

WAD = wad((b"contenido",))

# La carpeta que pidió probar el taller: tres apps y un WAD, como en la
# raíz de una SD.
RAIZ_DEL_TALLER = {
    "apps/usbloader_gx/boot.dol": dol(b"usbloader_gx"),
    "apps/usbloader_gx/meta.xml": b"<app><name>USB Loader GX</name></app>",
    "apps/usbloader_gx/icon.png": b"\x89PNG\r\n\x1a\n icono",
    "apps/Nintendont/boot.dol": dol(b"nintendont"),
    "apps/yawmME/boot.dol": dol(b"yawm"),
    "wad/forwarder.wad": WAD,
}


def _poner(base: Path, archivos: dict) -> Path:
    for nombre, datos in archivos.items():
        (base / nombre).parent.mkdir(parents=True, exist_ok=True)
        (base / nombre).write_bytes(datos)
    return base


def _foto(raiz: Path) -> dict:
    return {p.relative_to(raiz).as_posix(): (p.read_bytes() if p.is_file() else None)
            for p in sorted(raiz.rglob("*"))}


def _archivos(raiz: Path) -> dict:
    return {k: v for k, v in _foto(raiz).items() if v is not None}


@pytest.fixture
def raiz(tmp_path):
    r = tmp_path / "usb"
    r.mkdir()
    return r


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    c = tmp_path / "cache"
    monkeypatch.setattr(config, "CACHE_DIR", c)
    return c


class _Confirmar:
    """Anota cada aviso y la foto de la unidad en ese momento, y responde
    `respuesta`."""

    def __init__(self, raiz, respuesta=False):
        self.raiz, self.respuesta = raiz, respuesta
        self.avisos, self.fotos = [], []

    def __call__(self, aviso):
        self.avisos.append(aviso)
        self.fotos.append(_foto(self.raiz))
        return self.respuesta


def _instalar(carpeta, raiz, confirmar=None, **kw):
    return oscwii_installer.install_local_folder(carpeta, raiz, confirmar=confirmar, **kw)


# ------------------------------------------------ la raíz de una unidad --
def test_una_carpeta_con_forma_de_raiz_se_instala_entera_de_una_vez(tmp_path, raiz):
    from wiibackup_manager.widgets.homebrew_store_view import _texto_del_aviso

    carpeta = _poner(tmp_path / "SD del taller", RAIZ_DEL_TALLER)
    confirmar = _Confirmar(raiz)

    r = _instalar(carpeta, raiz, confirmar)

    assert r.ok, r.error
    # Una sola pregunta, antes de tocar la unidad, con todo lo que trae.
    [aviso] = confirmar.avisos
    assert confirmar.fotos == [{}]
    assert aviso.reemplaza == ("apps/Nintendont/", "apps/usbloader_gx/", "apps/yawmME/")
    assert aviso.carpetas == (("wad/", 1),)
    assert aviso.wads == ("wad/forwarder.wad",)
    # Es el mismo texto del diálogo que para una app del catálogo.
    texto = _texto_del_aviso(aviso)
    assert "Va a escribir en:" in texto
    assert all(carpeta_de_app in texto for carpeta_de_app in aviso.reemplaza)
    assert "wad/: 1 archivo nuevo" in texto
    assert "se instala con YAWM desde la Wii" in texto
    # Todo en su sitio, byte por byte, y nada más.
    assert _archivos(raiz) == RAIZ_DEL_TALLER
    assert len(r.installed_paths) == len(RAIZ_DEL_TALLER)
    assert r.wads == (raiz / "wad/forwarder.wad",)
    assert r.app_slug == "local:SD del taller"


def test_lo_que_un_paquete_omite_tampoco_se_copia_desde_una_carpeta(tmp_path, raiz):
    carpeta = _poner(tmp_path / "SD", {"apps/WiiDonut/boot.dol": dol(b"x"),
                                       "readme.txt": b"leeme",
                                       "__MACOSX/apps/._boot.dol": b"basura"})
    confirmar = _Confirmar(raiz)

    r = _instalar(carpeta, raiz, confirmar)

    assert r.ok, r.error
    [aviso] = confirmar.avisos
    assert sorted(aviso.omitidos) == ["__MACOSX/apps/._boot.dol", "readme.txt"]
    assert sorted(os.listdir(raiz)) == ["apps"]


def test_desde_la_carpeta_queda_igual_que_desde_el_zip(tmp_path, monkeypatch):
    """Un paquete descomprimido en una carpeta deja la unidad igual que el
    paquete mismo."""
    paquete = {"apps/smw-wiigii/boot.dol": dol(b"smw"),
               "apps/smw-wiigii/meta.xml": b"<app/>",
               "controllers/perfil.ini": b"[perfil]",
               "smw/sfx/a.wav": b"RIFF a",
               "wiicolem/wiicolem.conf": b"",
               "database.txt": b"titulos",
               "wad/fw.wad": WAD,
               "readme.txt": b"leeme",
               "__MACOSX/._x": b"basura"}

    def _falsa(app, destino, cancel_event, on_progress):
        with zipfile.ZipFile(destino, "w") as zf:
            for nombre, datos in paquete.items():
                zf.writestr(nombre, datos)
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _falsa)
    desde_zip, desde_carpeta = tmp_path / "zip", tmp_path / "carpeta"
    desde_zip.mkdir()
    desde_carpeta.mkdir()
    app = HomebrewApp(slug="smw-wiigii", name="Super Mario War",
                      zip_url="https://hbb1.oscwii.org/api/contents/smw-wiigii/x.zip")

    a = oscwii_installer.install_app(app, desde_zip)
    b = _instalar(_poner(tmp_path / "descomprimido", paquete), desde_carpeta)

    assert a.ok and b.ok, (a, b)
    assert _foto(desde_zip) == _foto(desde_carpeta)
    assert sorted(a.omitidos) == sorted(b.omitidos)


# ------------------------------------------------ la carpeta de una app --
def test_la_carpeta_de_una_app_va_a_apps_con_su_nombre(tmp_path, raiz):
    archivos = {"boot.dol": dol(b"donut"), "meta.xml": b"<app/>", "icon.png": b"png",
                "datos/nivel1.bin": b"nivel"}
    carpeta = _poner(tmp_path / "WiiDonut", archivos)
    confirmar = _Confirmar(raiz)

    r = _instalar(carpeta, raiz, confirmar)

    assert r.ok, r.error
    [aviso] = confirmar.avisos
    assert aviso.reemplaza == ("apps/WiiDonut/",)
    assert _archivos(raiz) == {f"apps/WiiDonut/{k}": v for k, v in archivos.items()}


@pytest.mark.parametrize("nombre", ["boot.elf", "BOOT.DOL"])
def test_con_boot_elf_o_en_mayusculas_tambien_es_una_app(tmp_path, raiz, nombre):
    datos = elf(b"x") if nombre == "boot.elf" else dol(b"x")
    carpeta = _poner(tmp_path / "MiApp", {nombre: datos})

    r = _instalar(carpeta, raiz)

    assert r.ok, r.error
    assert _archivos(raiz) == {f"apps/MiApp/{nombre}": datos}


def test_el_aviso_sale_aunque_todo_vaya_a_apps(tmp_path, raiz):
    """Una app del catálogo que solo escribe en apps/ no pregunta; desde
    una carpeta, sí: no hay catálogo detrás que diga qué trae."""
    carpeta = _poner(tmp_path / "WiiDonut", {"boot.dol": dol(b"donut")})
    confirmar = _Confirmar(raiz, None)

    r = _instalar(carpeta, raiz, confirmar)

    assert r.status is InstallStatus.CANCELLED, r
    assert len(confirmar.avisos) == 1
    assert os.listdir(raiz) == []


# ---------------------------------------------------- lo que se rechaza --
def _rechazada(r, raiz):
    assert r.status is InstallStatus.BAD_FOLDER, r
    assert os.listdir(raiz) == []


def test_una_carpeta_sin_apps_ni_boot_se_rechaza(tmp_path, raiz):
    carpeta = _poner(tmp_path / "Descargas", {"leeme.txt": b"hola", "fotos/a.jpg": b"jpg"})
    confirmar = _Confirmar(raiz)

    r = _instalar(carpeta, raiz, confirmar)

    _rechazada(r, raiz)
    assert "apps/" in r.error and "boot.dol" in r.error
    assert confirmar.avisos == []


def test_una_carpeta_que_no_existe_se_rechaza(tmp_path, raiz):
    _rechazada(_instalar(tmp_path / "no-esta", raiz), raiz)


@pytest.mark.parametrize("donde", ["apps/WiiDonut/enlace.dol", "apps/Enlace", "apps"])
def test_un_enlace_simbolico_rechaza_la_carpeta_entera(tmp_path, raiz, donde):
    afuera = _poner(tmp_path / "afuera", {"boot.dol": dol(b"afuera"), "secreto": b"no"})
    carpeta = _poner(tmp_path / "SD", {"apps/WiiDonut/boot.dol": dol(b"donut")})
    if donde == "apps":
        shutil.rmtree(carpeta / "apps")
    destino = afuera / "boot.dol" if donde.endswith(".dol") else afuera
    (carpeta / donde).symlink_to(destino)

    r = _instalar(carpeta, raiz)

    _rechazada(r, raiz)
    assert donde in r.error
    assert "enlace simbólico" in r.error


def test_un_archivo_especial_rechaza_la_carpeta_sin_colgarse(tmp_path, raiz):
    carpeta = _poner(tmp_path / "SD", {"apps/WiiDonut/boot.dol": dol(b"donut")})
    os.mkfifo(carpeta / "apps/WiiDonut/tuberia")
    eventos = []

    r = _instalar(carpeta, raiz, on_progress=eventos.append)

    _rechazada(r, raiz)
    assert "apps/WiiDonut/tuberia" in r.error
    # Se rechaza al revisar la carpeta, antes de empezar la copia.
    assert eventos == []


@pytest.mark.parametrize("nombre", ["que?.txt", "a:b.txt", "a<b", "a>b", 'comillas".txt',
                                    "a|b", "a*b", "barra\\invertida", "control\x01",
                                    "termina en punto.", "termina en espacio "])
def test_un_nombre_que_fat32_no_acepta_rechaza_la_carpeta(tmp_path, raiz, nombre):
    carpeta = _poner(tmp_path / "SD", {"apps/WiiDonut/boot.dol": dol(b"donut"),
                                       f"apps/WiiDonut/{nombre}": b"x"})

    r = _instalar(carpeta, raiz)

    _rechazada(r, raiz)
    assert "FAT32" in r.error


def test_una_carpeta_con_un_nombre_que_fat32_no_acepta_tambien(tmp_path, raiz):
    en_la_raiz = _poner(tmp_path / "SD", {"apps/Wii:Donut/boot.dol": dol(b"x")})
    la_app = _poner(tmp_path / "Wii?Donut", {"boot.dol": dol(b"x")})

    for carpeta in (en_la_raiz, la_app):
        r = _instalar(carpeta, raiz)

        _rechazada(r, raiz)
        assert "FAT32" in r.error


@pytest.mark.parametrize("nombre", ["wbfs/Juego [RMCE01]/RMCE01.wbfs", "nincfg.bin",
                                    "wad/leeme.txt", ".Trashes/501/basura", ".DS_Store"])
def test_lo_que_no_va_en_una_carpeta_permitida_rechaza_la_carpeta(tmp_path, raiz, nombre):
    carpeta = _poner(tmp_path / "SD", {"apps/WiiDonut/boot.dol": dol(b"x"), nombre: b"x"})

    r = _instalar(carpeta, raiz)

    _rechazada(r, raiz)
    assert nombre in r.error


def test_dos_nombres_que_en_fat32_son_el_mismo_rechazan_la_carpeta(tmp_path, raiz):
    carpeta = _poner(tmp_path / "SD", {"apps/WiiDonut/boot.dol": dol(b"a"),
                                       "apps/wiidonut/boot.dol": dol(b"b")})

    r = _instalar(carpeta, raiz)

    _rechazada(r, raiz)
    assert "apps/WiiDonut" in r.error and "apps/wiidonut" in r.error


def test_mas_de_lo_que_puede_traer_un_paquete_rechaza_la_carpeta(tmp_path, raiz, cache,
                                                                monkeypatch):
    monkeypatch.setattr(oscwii_installer, "MAX_UNCOMPRESSED_TOTAL_BYTES", 1000)
    carpeta = _poner(tmp_path / "SD", {"apps/WiiDonut/boot.dol": dol(b"x"),
                                       "apps/WiiDonut/datos.bin": bytes(1000)})

    r = _instalar(carpeta, raiz)

    _rechazada(r, raiz)
    # Ni siquiera se armó la copia.
    assert not cache.exists() or list(cache.iterdir()) == []


def test_un_boot_dol_danado_se_rechaza_como_en_un_paquete(tmp_path, raiz):
    carpeta = _poner(tmp_path / "WiiDonut", {"boot.dol": b"404: Not Found"})

    r = _instalar(carpeta, raiz)

    assert r.status is InstallStatus.BAD_CONTENT, r
    assert "boot.dol" in r.error
    assert os.listdir(raiz) == []


@pytest.mark.parametrize("cambio", ["enlace", "fifo"])
def test_si_un_archivo_cambia_despues_de_revisar_la_carpeta_tampoco_se_sigue(
        tmp_path, raiz, monkeypatch, cambio):
    """La carpeta se revisa antes de copiar nada, pero puede cambiar entre
    la revisión y la copia: cada archivo se abre sin seguir enlaces y tiene
    que seguir siendo un archivo común."""
    afuera = _poner(tmp_path / "afuera", {"secreto": b"no"})
    carpeta = _poner(tmp_path / "WiiDonut", {"boot.dol": dol(b"x"), "datos.bin": b"d"})
    revisar = oscwii_installer._archivos_de_la_carpeta

    def _revisar_y_cambiar(*a, **k):
        archivos = revisar(*a, **k)
        (carpeta / "datos.bin").unlink()
        if cambio == "enlace":
            (carpeta / "datos.bin").symlink_to(afuera / "secreto")
        else:
            os.mkfifo(carpeta / "datos.bin")
        return archivos

    monkeypatch.setattr(oscwii_installer, "_archivos_de_la_carpeta", _revisar_y_cambiar)

    _rechazada(_instalar(carpeta, raiz), raiz)


# --------------------------------------------- la copia en la caché --
def test_la_copia_se_arma_sin_comprimir_en_la_cache_y_se_borra(tmp_path, raiz, cache,
                                                              monkeypatch):
    vistos = []
    validar = oscwii_installer._validate_zip

    def _espiar(zip_path, dest_root):
        with zipfile.ZipFile(zip_path) as zf:
            vistos.append((Path(zip_path), {i.compress_type for i in zf.infolist()}))
        return validar(zip_path, dest_root)

    monkeypatch.setattr(oscwii_installer, "_validate_zip", _espiar)
    carpeta = _poner(tmp_path / "WiiDonut", {"boot.dol": dol(b"x"), "meta.xml": b"<app/>"})

    r = _instalar(carpeta, raiz)

    assert r.ok, r.error
    [(zip_path, tipos)] = vistos
    assert cache in zip_path.parents
    assert tipos == {zipfile.ZIP_STORED}
    assert list(cache.iterdir()) == []


def test_cancelar_mientras_se_prepara_corta_ahi_y_no_toca_la_unidad(tmp_path, raiz, cache):
    carpeta = _poner(tmp_path / "SD", {f"apps/App{i}/boot.dol": dol(bytes([65 + i]))
                                       for i in range(5)})
    cancelar = threading.Event()
    preparados = []

    def _progreso(p):
        if p.phase == "prepare":
            preparados.append(p.done)
            cancelar.set()

    r = _instalar(carpeta, raiz, cancel_event=cancelar, on_progress=_progreso)

    assert r.status is InstallStatus.CANCELLED, r
    assert preparados == [1]
    assert os.listdir(raiz) == []
    assert list(cache.iterdir()) == []


def test_el_progreso_cuenta_lo_que_se_prepara_y_lo_que_se_escribe(tmp_path, raiz):
    carpeta = _poner(tmp_path / "SD", RAIZ_DEL_TALLER)
    eventos = []

    r = _instalar(carpeta, raiz,
                  on_progress=lambda p: eventos.append((p.phase, p.done, p.total)))

    assert r.ok, r.error
    n = len(RAIZ_DEL_TALLER)
    assert [e for e in eventos if e[0] == "prepare"] == [
        ("prepare", i, n) for i in range(1, n + 1)]
    assert eventos[-1] == ("extract", n, n)


# ------------------------------------------- lo que ya está en la unidad --
def test_reinstalar_desde_una_carpeta_conserva_lo_del_cliente(tmp_path, raiz):
    _poner(raiz, {"apps/usbloader_gx/boot.dol": dol(b"viejo"),
                  "apps/usbloader_gx/GXGlobal.cfg": b"la config del cliente",
                  "apps/usbloader_gx/images/RMCE01.png": b"caratula"})
    carpeta = _poner(tmp_path / "SD", RAIZ_DEL_TALLER)

    r = _instalar(carpeta, raiz, _Confirmar(raiz))

    assert r.ok, r.error
    assert (raiz / "apps/usbloader_gx/boot.dol").read_bytes() == (
        RAIZ_DEL_TALLER["apps/usbloader_gx/boot.dol"])
    assert (raiz / "apps/usbloader_gx/GXGlobal.cfg").read_bytes() == b"la config del cliente"
    assert (raiz / "apps/usbloader_gx/images/RMCE01.png").read_bytes() == b"caratula"
    # Sin restos de la transacción.
    assert not [p for p in raiz.rglob("*") if "wbm-" in p.name]


def test_fuera_de_apps_se_conserva_lo_del_usuario(tmp_path, raiz):
    _poner(raiz, {"wad/forwarder.wad": b"el mio", "wad/otro.wad": b"otro"})
    carpeta = _poner(tmp_path / "SD", RAIZ_DEL_TALLER)
    confirmar = _Confirmar(raiz, False)

    r = _instalar(carpeta, raiz, confirmar)

    assert r.ok, r.error
    [aviso] = confirmar.avisos
    assert aviso.distintos == ("wad/forwarder.wad",)
    assert (raiz / "wad/forwarder.wad").read_bytes() == b"el mio"
    assert (raiz / "wad/otro.wad").read_bytes() == b"otro"


def test_reemplazar_sin_preguntar_reemplaza_los_distintos(tmp_path, raiz):
    _poner(raiz, {"wad/forwarder.wad": b"el mio"})
    carpeta = _poner(tmp_path / "SD", RAIZ_DEL_TALLER)

    r = _instalar(carpeta, raiz, reemplazar=True)

    assert r.ok, r.error
    assert (raiz / "wad/forwarder.wad").read_bytes() == WAD


def test_cancelar_mientras_se_escribe_deja_la_unidad_como_estaba(tmp_path, raiz, cache):
    _poner(raiz, {"apps/Nintendont/boot.dol": dol(b"viejo")})
    antes = _foto(raiz)
    carpeta = _poner(tmp_path / "SD", RAIZ_DEL_TALLER)
    cancelar = threading.Event()

    def _progreso(p):
        if p.phase == "extract":
            cancelar.set()

    r = _instalar(carpeta, raiz, cancel_event=cancelar, on_progress=_progreso)

    assert r.status is InstallStatus.CANCELLED, r
    assert _foto(raiz) == antes
    assert list(cache.iterdir()) == []


def test_si_falla_a_mitad_vuelve_la_version_anterior(tmp_path, raiz, monkeypatch):
    _poner(raiz, {"apps/Nintendont/boot.dol": dol(b"viejo")})
    carpeta = _poner(tmp_path / "SD", RAIZ_DEL_TALLER)
    copiar = oscwii_installer._copiar_miembro
    copiados = []

    def _falla_al_tercero(zf, info, destino):
        copiados.append(info.filename)
        if len(copiados) == 3:
            raise OSError(5, "Input/output error")
        copiar(zf, info, destino)

    monkeypatch.setattr(oscwii_installer, "_copiar_miembro", _falla_al_tercero)

    r = _instalar(carpeta, raiz)

    assert r.status is InstallStatus.IO_ERROR, r
    assert _foto(raiz) == {"apps": None, "apps/Nintendont": None,
                           "apps/Nintendont/boot.dol": dol(b"viejo")}


# ------------------------------------------------------------ el destino --
def test_un_destino_critico_se_rechaza(tmp_path):
    carpeta = _poner(tmp_path / "WiiDonut", {"boot.dol": dol(b"x")})

    r = _instalar(carpeta, Path("/"))

    assert r.status is InstallStatus.UNSAFE_DEST_ROOT, r


@pytest.mark.skipif(os.geteuid() == 0, reason="root escribe igual")
def test_un_destino_sin_permiso_se_rechaza_antes_de_preparar_nada(tmp_path, raiz, cache):
    carpeta = _poner(tmp_path / "WiiDonut", {"boot.dol": dol(b"x")})
    eventos = []
    raiz.chmod(0o500)
    try:
        r = _instalar(carpeta, raiz, on_progress=eventos.append)
    finally:
        raiz.chmod(0o700)

    assert r.status is InstallStatus.IO_ERROR, r
    assert eventos == []
    assert not cache.exists() or list(cache.iterdir()) == []
