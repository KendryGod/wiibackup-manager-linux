"""Reinstalar o actualizar una app desde la Store no borra lo del cliente.

La instalación reemplaza la carpeta de la app entera (`atomicfs.StagedSwap`,
todo o nada), y con ella se iba lo que el cliente tenía adentro: en USB
Loader GX, su GXGlobal.cfg, las carátulas de images/, los temas... (lo
probamos con el instalador real).

Ahora cada app tiene una lista explícita de lo que es del cliente
(`oscwii_installer.PRESERVED_PATHS`, hoy solo USB Loader GX, con la
evidencia en su código fuente). Eso se COPIA de la versión instalada a la
carpeta de staging antes del intercambio: si algo falla o se cancela, la
versión anterior queda entera con lo suyo, y su respaldo se borra recién
cuando entró todo. Si el ZIP trae algo con el mismo nombre, gana lo del
cliente. El espacio libre se mira contando también lo que se conserva.
"""
from __future__ import annotations

import os
import threading
import zipfile
from pathlib import Path

import pytest

from homebrew_falso import dol
from wiibackup_manager import atomicfs, golden_configs, oscwii_installer, transfer_plan
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallStatus

APP = "apps/usbloader_gx"

# Lo que tenía instalado el cliente: la versión vieja y sus datos.
VIEJO = {
    f"{APP}/boot.dol": dol(b"dol viejo"),
    f"{APP}/meta.xml": b"<app version='r1280'/>",
    f"{APP}/language/spanish.lang": b"viejo",
    f"{APP}/cache/titulos.bin": b"cache",            # se regenera: no se conserva
    f"{APP}/GXGlobal.cfg": b"# USB Loader GX R1280\nlanguage = 4\n",
    f"{APP}/GXGameSettings.cfg": b"# USB Loader GX R1280\ngame:RMCE01 = 1\n",
    f"{APP}/GXGameStatistics.cfg": b"# USB Loader GX R1280\nRMCE01 = 7\n",
    f"{APP}/GXGameCategories.xml": b"<Categories/>",
    f"{APP}/Wiinnertag.xml": b"<Wiinnertag/>",
    f"{APP}/font.ttf": b"fuente",
    f"{APP}/GameList.csv": b"ID;Titulo",
    f"{APP}/images/RMCE01.png": b"3D",
    f"{APP}/images/2D/RMCE01.png": b"2D",
    f"{APP}/images/disc/RMCE01.png": b"disco",
    f"{APP}/theme/oscuro.them": b"tema",
    f"{APP}/theme/oscuro/fondo.png": b"fondo",
}
DEL_CLIENTE = {k: v for k, v in VIEJO.items()
               if not k.endswith(("boot.dol", "meta.xml", "spanish.lang", "titulos.bin"))}

# Lo que trae el ZIP de la versión nueva (como el de OSC).
NUEVO = {
    f"{APP}/boot.dol": dol(b"dol nuevo" * 10),
    f"{APP}/meta.xml": b"<app version='r1283'/>",
    f"{APP}/icon.png": b"icono",
    f"{APP}/wiitdb.xml": b"<datafile/>",
    f"{APP}/language/spanish.lang": b"nuevo",
}


def _app(slug="usbloader_gx") -> HomebrewApp:
    return HomebrewApp(slug=slug, name="USB Loader GX",
                       zip_url=f"https://hbb1.oscwii.org/api/contents/{slug}/{slug}.zip")


def _escribir(raiz: Path, archivos: dict) -> None:
    for nombre, datos in archivos.items():
        (raiz / nombre).parent.mkdir(parents=True, exist_ok=True)
        (raiz / nombre).write_bytes(datos)


def _foto(raiz: Path) -> dict:
    """Todo lo que hay (ocultos incluidos), con su contenido."""
    return {str(p.relative_to(raiz)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(raiz.rglob("*"))}


def _archivos(raiz: Path) -> dict:
    return {k: v for k, v in _foto(raiz).items() if v is not None}


def _bajo(rutas, raiz: Path) -> set:
    """Los archivos que hay debajo de cada ruta, relativos a la carpeta de la app."""
    app = raiz / APP
    return {str(f.relative_to(app)) for r in rutas
            for f in ([r] if r.is_file() else r.rglob("*")) if f.is_file()}


def _ocultos(raiz: Path) -> list:
    return [str(p.relative_to(raiz)) for p in raiz.rglob(".*")]


@pytest.fixture
def zip_nuevo(monkeypatch):
    contenido = dict(NUEVO)

    def _fake(app, dest_path, cancel_event, on_progress):
        with zipfile.ZipFile(dest_path, "w") as zf:
            for nombre, datos in contenido.items():
                zf.writestr(nombre, datos)
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _fake)
    return contenido


@pytest.fixture
def unidad(tmp_path):
    raiz = tmp_path / "usb"
    _escribir(raiz, VIEJO)
    return raiz


def _instalar(raiz, cancel_event=None, on_progress=None, slug="usbloader_gx"):
    return oscwii_installer.install_app(_app(slug), raiz, cancel_event=cancel_event,
                                        on_progress=on_progress)


# -------------------------------------------------- Lo del cliente queda --
def test_reinstalar_conserva_la_configuracion_y_las_caratulas(unidad, zip_nuevo):
    r = _instalar(unidad)

    assert r.ok, r.error
    assert _archivos(unidad) == {**NUEVO, **DEL_CLIENTE}
    assert _ocultos(unidad) == []
    # Lo que no es del cliente se va con la versión vieja: la caché y el
    # idioma viejo (el del ZIP es el nuevo).
    assert not (unidad / APP / "cache").exists()
    # Cada cosa de la lista que el cliente tenía (GameList.txt, no).
    assert sorted(p.name for p in r.preserved_paths) == sorted(
        {Path(k).relative_to(APP).parts[0] for k in DEL_CLIENTE})
    assert set(DEL_CLIENTE) <= {f"{APP}/{n}" for n in _bajo(r.preserved_paths, unidad)}


def test_despues_la_configuracion_maestra_no_la_pisa(unidad, zip_nuevo):
    r = _instalar(unidad)
    dorado = golden_configs.maybe_apply(_app(), unidad, r)

    assert dorado.status is golden_configs.GoldenConfigStatus.KEPT_EXISTING
    assert (unidad / APP / "GXGlobal.cfg").read_bytes() == VIEJO[f"{APP}/GXGlobal.cfg"]


def test_reinstalar_sin_datos_del_cliente_no_inventa_nada(tmp_path, zip_nuevo):
    raiz = tmp_path / "usb"
    _escribir(raiz, {f"{APP}/boot.dol": dol(b"dol viejo"), f"{APP}/meta.xml": b"<app/>"})

    r = _instalar(raiz)

    assert r.ok, r.error
    assert _foto(raiz) == {"apps": None, APP: None, f"{APP}/language": None, **NUEVO}
    assert r.preserved_paths == ()


def test_si_en_lugar_de_la_carpeta_hay_un_archivo_se_reemplaza_como_antes(tmp_path,
                                                                          zip_nuevo):
    raiz = tmp_path / "usb"
    _escribir(raiz, {APP: b"un archivo suelto con el nombre de la carpeta"})

    r = _instalar(raiz)

    assert r.ok, r.error
    assert _archivos(raiz) == NUEVO and r.preserved_paths == ()


def test_una_instalacion_nueva_tampoco(tmp_path, zip_nuevo):
    raiz = tmp_path / "usb"
    raiz.mkdir()

    r = _instalar(raiz)

    assert r.ok and _archivos(raiz) == NUEVO and r.preserved_paths == ()


def test_si_el_zip_trae_algo_con_el_mismo_nombre_gana_lo_del_cliente(unidad, zip_nuevo):
    zip_nuevo.update({
        f"{APP}/GXGlobal.cfg": b"# el del ZIP\n",
        f"{APP}/images/2D/RMCE01.png": b"2D del ZIP",
        f"{APP}/images/2D/SMNE01.png": b"solo en el ZIP",
        f"{APP}/theme": b"un archivo donde el cliente tiene una carpeta",
    })

    r = _instalar(unidad)

    assert r.ok, r.error
    final = _archivos(unidad)
    assert final[f"{APP}/GXGlobal.cfg"] == VIEJO[f"{APP}/GXGlobal.cfg"]
    assert final[f"{APP}/images/2D/RMCE01.png"] == b"2D"
    assert final[f"{APP}/images/2D/SMNE01.png"] == b"solo en el ZIP"
    assert final[f"{APP}/theme/oscuro.them"] == b"tema"
    assert final[f"{APP}/boot.dol"] == NUEVO[f"{APP}/boot.dol"]


def test_un_enlace_simbolico_no_se_sigue_ni_se_copia(unidad, zip_nuevo, tmp_path):
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    (afuera / "secreto.txt").write_bytes(b"no")
    os.symlink(afuera, unidad / APP / "images" / "enlace")
    pasos = []

    r = _instalar(unidad, on_progress=lambda p: p.phase == "extract"
                  and pasos.append(p.fraction))

    assert r.ok, r.error
    assert not os.path.lexists(unidad / APP / "images" / "enlace")
    assert (afuera / "secreto.txt").read_bytes() == b"no"
    # Tampoco se cuenta: la barra termina en 100%, no antes.
    assert pasos[-1] == 1.0


# ------------------------------------------- Todo o nada, también ahora --
def test_cancelar_mientras_se_copia_lo_del_cliente_deja_todo_como_estaba(unidad,
                                                                         zip_nuevo):
    antes = _foto(unidad)
    cancelar = threading.Event()
    pasos = []

    def on_progress(p):
        if p.phase == "extract":
            pasos.append(p.fraction)
            # Ya entró el ZIP entero y uno de los archivos del cliente.
            if len(pasos) == len(NUEVO) + 1:
                cancelar.set()

    r = _instalar(unidad, cancel_event=cancelar, on_progress=on_progress)

    assert r.status is InstallStatus.CANCELLED, r.error
    assert _foto(unidad) == antes
    # Se cortó en el acto, sin copiar el resto.
    assert len(pasos) == len(NUEVO) + 1


def test_una_falla_copiando_lo_del_cliente_deja_todo_como_estaba(unidad, zip_nuevo,
                                                                 monkeypatch):
    antes = _foto(unidad)
    real = oscwii_installer.shutil.copyfileobj

    def _copia(src, dst, *a, **k):
        if Path(getattr(src, "name", "")).name == "fondo.png":
            raise OSError(5, "simulado: falla leyendo un archivo del cliente")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(oscwii_installer.shutil, "copyfileobj", _copia)

    r = _instalar(unidad)

    assert r.status is InstallStatus.IO_ERROR, r.error
    assert _foto(unidad) == antes


def test_una_falla_al_intercambiar_devuelve_la_version_anterior_entera(unidad, zip_nuevo,
                                                                       monkeypatch):
    antes = _foto(unidad)
    real = os.replace

    def _replace(origen, destino):
        if Path(destino).name == "usbloader_gx" and ".wbm-staging-" in Path(origen).name:
            raise OSError(28, "simulado: no se pudo poner la versión nueva")
        return real(origen, destino)

    monkeypatch.setattr(atomicfs.os, "replace", _replace)

    r = _instalar(unidad)

    assert r.status is InstallStatus.IO_ERROR, r.error
    assert _foto(unidad) == antes


def test_cancelar_justo_al_final_devuelve_la_version_anterior_entera(unidad, zip_nuevo,
                                                                     monkeypatch):
    antes = _foto(unidad)
    cancelar = threading.Event()
    real = os.replace

    def _replace(origen, destino):
        resultado = real(origen, destino)
        if Path(destino).name == "usbloader_gx":
            cancelar.set()
        return resultado

    monkeypatch.setattr(atomicfs.os, "replace", _replace)

    r = _instalar(unidad, cancel_event=cancelar)

    assert r.status is InstallStatus.CANCELLED, r.error
    assert _foto(unidad) == antes


# ------------------------------------------------------- Espacio --
def _con_libres(monkeypatch, libres: int) -> None:
    monkeypatch.setattr(oscwii_installer, "_cluster_size", lambda _p: 1)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: libres)


# Con clúster de 1 byte: el ZIP, lo del cliente, y un clúster por cada
# carpeta conservada (images, images/2D, images/disc, theme, theme/oscuro).
DEL_ZIP = sum(len(v) for v in NUEVO.values())
CONSERVADO = sum(len(v) for v in DEL_CLIENTE.values()) + 5


def test_el_espacio_cuenta_lo_que_se_conserva(unidad, zip_nuevo, monkeypatch):
    # Entra el ZIP solo, pero no el ZIP más lo del cliente.
    _con_libres(monkeypatch, DEL_ZIP + CONSERVADO - 1)
    antes = _foto(unidad)

    r = _instalar(unidad)

    assert r.status is InstallStatus.NO_SPACE, r.error
    assert _foto(unidad) == antes


def test_con_lugar_justo_para_todo_se_instala(unidad, zip_nuevo, monkeypatch):
    _con_libres(monkeypatch, DEL_ZIP + CONSERVADO)

    assert _instalar(unidad).ok


def test_la_barra_cuenta_tambien_lo_que_se_copia_del_cliente(unidad, zip_nuevo):
    pasos = []

    r = _instalar(unidad, on_progress=lambda p: p.phase == "extract"
                  and pasos.append(p.fraction))

    assert r.ok
    assert len(pasos) == len(NUEVO) + len(DEL_CLIENTE)
    # Con el ZIP ya extraído todavía falta lo del cliente: no está en 100%.
    assert pasos[len(NUEVO) - 1] < 1.0
    assert pasos[-1] == 1.0


def test_la_carpeta_de_la_app_con_otras_mayusculas_tambien(tmp_path, monkeypatch):
    raiz = tmp_path / "usb"
    _escribir(raiz, {"apps/USBLoader_GX/boot.dol": dol(b"viejo"),
                     "apps/USBLoader_GX/GXGlobal.cfg": b"del cliente"})

    def _fake(app, dest_path, cancel_event, on_progress):
        with zipfile.ZipFile(dest_path, "w") as zf:
            zf.writestr("apps/USBLoader_GX/boot.dol", dol(b"nuevo"))
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _fake)

    r = _instalar(raiz)

    assert r.ok, r.error
    assert _archivos(raiz) == {"apps/USBLoader_GX/boot.dol": dol(b"nuevo"),
                               "apps/USBLoader_GX/GXGlobal.cfg": b"del cliente"}


def test_si_aparece_un_archivo_despues_de_medir_la_barra_no_pasa_de_100(unidad,
                                                                         zip_nuevo,
                                                                         monkeypatch):
    real = oscwii_installer._plan_del_cliente

    def _y_aparece_otro(unidades, dest_root, cluster):
        plan = real(unidades, dest_root, cluster)
        (unidad / APP / "images" / "2D" / "SMNE01.png").write_bytes(b"recien bajada")
        return plan

    monkeypatch.setattr(oscwii_installer, "_plan_del_cliente", _y_aparece_otro)
    pasos = []

    r = _instalar(unidad, on_progress=lambda p: p.phase == "extract"
                  and pasos.append(p.fraction))

    assert r.ok, r.error
    assert max(pasos) == 1.0
    assert (unidad / APP / "images" / "2D" / "SMNE01.png").read_bytes() == b"recien bajada"


# ------------------------------------------------ Las demás apps igual --
def test_las_demas_apps_se_reemplazan_como_siempre(tmp_path, monkeypatch):
    """Sin lista, la carpeta queda igual al ZIP: una app que se llama
    distinto no conserva un GXGlobal.cfg aunque lo tenga."""
    raiz = tmp_path / "usb"
    _escribir(raiz, {"apps/Nintendont/boot.dol": dol(b"viejo"),
                     "apps/Nintendont/titles.txt": b"bajado por su actualizador",
                     "apps/Nintendont/GXGlobal.cfg": b"no es de esta app",
                     "controllers/054C_05C4.ini": b"[perfil del cliente]"})
    nuevo = {"apps/Nintendont/boot.dol": dol(b"nuevo"), "apps/Nintendont/meta.xml": b"<app/>",
             "controllers/054C_05C4.ini": b"[perfil del ZIP]"}

    def _fake(app, dest_path, cancel_event, on_progress):
        with zipfile.ZipFile(dest_path, "w") as zf:
            for nombre, datos in nuevo.items():
                zf.writestr(nombre, datos)
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _fake)

    r = _instalar(raiz, slug="Nintendont")

    assert r.ok, r.error
    assert _archivos(raiz) == nuevo
    assert r.preserved_paths == ()


def test_la_tienda_avisa_que_conservo_lo_del_cliente(tmp_path):
    import types

    from wiibackup_manager.operations import OperationManager
    from wiibackup_manager.oscwii_installer import InstallResult
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    toasts = []
    vista = types.SimpleNamespace(
        ops=OperationManager(), op_log=None, _show_toast=toasts.append,
        _install_states={}, _card_by_slug={})
    carpeta = tmp_path / APP
    resultado = InstallResult(InstallStatus.OK, "usbloader_gx",
                              installed_paths=(carpeta / "boot.dol",),
                              preserved_paths=(carpeta / "GXGlobal.cfg",
                                               carpeta / "images"))

    HomebrewStoreView._on_install_done(vista, _app(), resultado, None, None)

    assert ("Se conservaron los datos del cliente de 'USB Loader GX': GXGlobal.cfg, "
            "images.") in toasts
