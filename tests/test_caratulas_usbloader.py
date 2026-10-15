"""Carátulas 2D para USB Loader GX, copiadas a la unidad desde Transferir.

USB Loader GX muestra la carátula plana de cada juego desde
`<su carpeta>/images/2D/<ID6>.png` (fuentes en `usbloader_covers`), y la
baja del mismo lugar de GameTDB que esta app (`art.gametdb.com/wii/cover/`,
PNG de 160×224). El botón "Copiar carátulas a la unidad" copia las que ya
están en la caché de este equipo, para los juegos que hay en la unidad:

- solo si la unidad tiene USB Loader GX (`apps/usbloader_gx/`) y su
  GXGlobal.cfg no manda las carátulas 2D a otro lado (por ejemplo a la SD);
- sin pisar ninguna que ya esté;
- cada una con escritura atómica (temporal `.parcial` y rename): una copia
  que falla no deja un PNG a medias;
- sin tocar los juegos, y sin arrancar mientras la cola le escribe a esa
  unidad.

No se probó en una Wii real.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from wiibackup_manager import config, gametdb, usbloader_covers
from wiibackup_manager.fsutil import PNG_MAGIC

_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"


def _png(contenido: bytes) -> bytes:
    return PNG_MAGIC + contenido + _IEND


@pytest.fixture
def cache(tmp_path, monkeypatch):
    carpeta = tmp_path / "covers"
    carpeta.mkdir()
    monkeypatch.setattr(config, "COVERS_DIR", carpeta)
    (carpeta / "RMCE01.EN.png").write_bytes(_png(b"mario kart"))
    (carpeta / "SMNE01.EN.png").write_bytes(_png(b"nsmb de la cache"))
    (carpeta / "GALE01.EN.gc.png").write_bytes(_png(b"melee"))
    # Una de otra región no sirve para la región elegida.
    (carpeta / "RSPE01.US.png").write_bytes(_png(b"wii sports us"))
    return carpeta


def _unidad(tmp_path: Path, con_loader: bool = True) -> Path:
    raiz = tmp_path / "usb"
    for game_id in ("RMCE01", "SMNE01", "RSPE01"):
        (raiz / "wbfs" / game_id).mkdir(parents=True)
        (raiz / "wbfs" / game_id / f"{game_id}.wbfs").write_bytes(b"juego " + game_id.encode())
    melee = raiz / "games" / "Melee [GALE01]"
    melee.mkdir(parents=True)
    (melee / "game.iso").write_bytes(b"juego GALE01")
    if con_loader:
        app = raiz / "apps" / "usbloader_gx"
        app.mkdir(parents=True)
        (app / "boot.dol").write_bytes(b"dol")
    return raiz


def _juegos(raiz: Path) -> dict:
    return {str(p.relative_to(raiz)): p.read_bytes()
            for p in sorted(raiz.rglob("*")) if p.is_file()
            and p.relative_to(raiz).parts[0] in ("wbfs", "games")}


def _carpeta_2d(raiz: Path) -> Path:
    return raiz / "apps" / "usbloader_gx" / "images" / "2D"


def _ocultos(carpeta: Path) -> list:
    return sorted(p.name for p in carpeta.rglob(".*")) if carpeta.exists() else []


# ------------------------------------------------- Dónde las busca --
def test_sin_usb_loader_gx_en_la_unidad_no_hay_donde_copiar(tmp_path):
    raiz = _unidad(tmp_path, con_loader=False)

    destino = usbloader_covers.covers_target(raiz)

    assert destino.folder is None and destino.problem == usbloader_covers.NO_LOADER


def test_sin_gxglobal_van_a_images_2d(tmp_path):
    raiz = _unidad(tmp_path)

    destino = usbloader_covers.covers_target(raiz)

    assert destino.problem is None
    assert destino.folder == _carpeta_2d(raiz)


@pytest.mark.parametrize("contenido", [
    "videomode = 1\ncovers_path = sd:/config/images/\n",          # sin covers2d_path
    "covers2d_path = usb1:/apps/usbloader_gx/images/2D/\n",       # el de fábrica
    "covers2d_path   =   usb1:/apps/usbloader_gx/images/2D  \r\n",
    "covers2d_path = USB:/apps/usbloader_gx/images/2d/\n",
    "covers2d_path = sd:/otro/\ncovers2d_path = usb1:/apps/usbloader_gx/images/2D/\n",
    "Covers2d_path = sd:/no/cuenta/\n",                           # no es la clave
], ids=["sin-clave", "de-fabrica", "con-espacios", "usb-mayusculas",
        "gana-la-ultima", "otra-clave"])
def test_un_gxglobal_que_no_las_cambia_de_lugar(tmp_path, contenido):
    raiz = _unidad(tmp_path)
    (raiz / "apps" / "usbloader_gx" / "GXGlobal.cfg").write_bytes(contenido.encode())

    destino = usbloader_covers.covers_target(raiz)

    assert destino.problem is None and destino.folder == _carpeta_2d(raiz)


@pytest.mark.parametrize("valor", ["sd:/apps/usbloader_gx/images/2D/",
                                   "usb1:/covers/2D/", "usb2:/apps/usbloader_gx/images/2D/"])
def test_un_gxglobal_que_las_manda_a_otro_lado_no_se_toca(tmp_path, valor):
    raiz = _unidad(tmp_path)
    (raiz / "apps" / "usbloader_gx" / "GXGlobal.cfg").write_text(
        f"videomode = 1\ncovers2d_path = {valor}\n", encoding="latin-1")

    destino = usbloader_covers.covers_target(raiz)

    assert destino.folder is None and destino.problem == usbloader_covers.OTHER_PATH
    assert destino.configured == valor


# ------------------------------------------------------------- Copiar --
def test_copia_las_que_faltan_sin_pisar_ni_tocar_los_juegos(tmp_path, cache):
    raiz = _unidad(tmp_path)
    carpeta = _carpeta_2d(raiz)
    carpeta.mkdir(parents=True)
    (carpeta / "SMNE01.png").write_bytes(b"la que puso el cliente")
    juegos_antes = _juegos(raiz)

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert sorted(r.copied) == ["GALE01", "RMCE01"]
    assert r.existing == ["SMNE01"]
    assert r.missing == ["RSPE01"]
    assert r.failed == [] and not r.stopped
    assert (carpeta / "RMCE01.png").read_bytes() == _png(b"mario kart")
    assert (carpeta / "GALE01.png").read_bytes() == _png(b"melee")
    assert (carpeta / "SMNE01.png").read_bytes() == b"la que puso el cliente"
    assert not (carpeta / "RSPE01.png").exists()
    assert _juegos(raiz) == juegos_antes
    assert _ocultos(raiz / "apps") == []


def test_crea_images_2d_si_no_esta(tmp_path, cache):
    raiz = _unidad(tmp_path)

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert sorted(p.name for p in _carpeta_2d(raiz).iterdir()) == [
        "GALE01.png", "RMCE01.png", "SMNE01.png"]
    assert len(r.copied) == 3


def test_sin_usb_loader_gx_no_escribe_nada(tmp_path, cache):
    raiz = _unidad(tmp_path, con_loader=False)
    antes = sorted(str(p) for p in raiz.rglob("*"))

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert r.target.problem == usbloader_covers.NO_LOADER and r.copied == []
    assert sorted(str(p) for p in raiz.rglob("*")) == antes


def test_con_las_caratulas_en_otro_lado_no_escribe_nada(tmp_path, cache):
    raiz = _unidad(tmp_path)
    (raiz / "apps" / "usbloader_gx" / "GXGlobal.cfg").write_text(
        "covers2d_path = sd:/apps/usbloader_gx/images/2D/\n")
    antes = sorted(str(p) for p in raiz.rglob("*"))

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert r.target.problem == usbloader_covers.OTHER_PATH and r.copied == []
    assert sorted(str(p) for p in raiz.rglob("*")) == antes


def test_una_copia_que_falla_no_deja_un_png_a_medias(tmp_path, cache, monkeypatch):
    raiz = _unidad(tmp_path)
    real = shutil.copyfileobj

    def _corta_con_mario(origen, destino, *a, **k):
        if "RMCE01" in Path(origen.name).name:
            destino.write(b"\x89PNG a med")
            raise OSError(28, "No space left on device")
        return real(origen, destino, *a, **k)

    monkeypatch.setattr(usbloader_covers.shutil, "copyfileobj", _corta_con_mario)

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert r.failed == [("RMCE01", "No space left on device")]
    assert sorted(r.copied) == ["GALE01", "SMNE01"]
    assert not (_carpeta_2d(raiz) / "RMCE01.png").exists()
    assert _ocultos(raiz / "apps") == []


def test_una_que_aparece_mientras_se_copia_no_se_pisa(tmp_path, cache, monkeypatch):
    """La última mirada es justo antes del rename: si en el medio apareció
    una carátula con ese nombre, se queda la que estaba."""
    raiz = _unidad(tmp_path)
    real = gametdb.cached_cover

    def _aparece(game_id, region, console):
        if game_id == "RMCE01":
            _carpeta_2d(raiz).mkdir(parents=True, exist_ok=True)
            (_carpeta_2d(raiz) / "RMCE01.png").write_bytes(b"llego antes")
        return real(game_id, region, console)

    monkeypatch.setattr(gametdb, "cached_cover", _aparece)

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert "RMCE01" in r.existing and "RMCE01" not in r.copied
    assert (_carpeta_2d(raiz) / "RMCE01.png").read_bytes() == b"llego antes"
    assert _ocultos(raiz / "apps") == []


def test_si_la_unidad_se_va_a_mitad_se_corta_sin_romper(tmp_path, cache, monkeypatch):
    raiz = _unidad(tmp_path)
    real = gametdb.cached_cover
    vistos = []

    def _se_va(game_id, region, console):
        vistos.append(game_id)
        if len(vistos) == 2:
            shutil.rmtree(raiz)
        return real(game_id, region, console)

    monkeypatch.setattr(gametdb, "cached_cover", _se_va)

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert r.stopped
    assert len(vistos) == 2


def test_se_puede_cortar_desde_afuera(tmp_path, cache):
    raiz = _unidad(tmp_path)
    pedidos = []

    def _cortar():
        pedidos.append(1)
        return len(pedidos) > 1

    r = usbloader_covers.copy_drive_covers(raiz, "EN", should_stop=_cortar)

    assert r.stopped and len(r.copied) == 1


def test_una_unidad_sin_juegos_no_copia_nada(tmp_path, cache):
    raiz = tmp_path / "usb"
    (raiz / "apps" / "usbloader_gx").mkdir(parents=True)

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert (r.copied, r.existing, r.missing, r.failed) == ([], [], [], [])
    assert not _carpeta_2d(raiz).exists()


def test_la_cache_solo_da_caratulas_enteras(cache):
    (cache / "RSBE01.EN.png").write_bytes(PNG_MAGIC + b"cortada")

    assert gametdb.cached_cover("RMCE01", "EN", "wii") == cache / "RMCE01.EN.png"
    assert gametdb.cached_cover("GALE01", "EN", "gc") == cache / "GALE01.EN.gc.png"
    assert gametdb.cached_cover("RSBE01", "EN", "wii") is None
    assert gametdb.cached_cover("RSPE01", "EN", "wii") is None
    assert gametdb.cached_cover("??????", "EN", "wii") is None


def test_no_baja_nada_de_internet(tmp_path, cache, monkeypatch):
    def _no(*_a, **_k):
        raise AssertionError("no tenía que bajar nada")

    monkeypatch.setattr(gametdb.urllib.request, "urlopen", _no)
    monkeypatch.setattr(gametdb, "get_cover_path", _no)

    r = usbloader_covers.copy_drive_covers(_unidad(tmp_path), "EN")

    assert r.missing == ["RSPE01"]


def test_una_que_ya_esta_cuenta_como_que_ya_esta_aunque_no_este_en_la_cache(tmp_path, cache):
    raiz = _unidad(tmp_path)
    _carpeta_2d(raiz).mkdir(parents=True)
    (_carpeta_2d(raiz) / "RSPE01.png").write_bytes(b"del cliente")

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert "RSPE01" in r.existing and r.missing == []


def test_un_juego_repetido_o_sin_id_en_la_unidad(tmp_path, cache):
    raiz = _unidad(tmp_path)
    # El mismo juego de Wii en las dos disposiciones, y uno de GameCube sin ID.
    (raiz / "wbfs" / "RMCE01.wbfs").write_bytes(b"otra copia")
    sin_id = raiz / "games" / "Sin ID"
    sin_id.mkdir()
    (sin_id / "game.iso").write_bytes(b"?")

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert sorted(r.copied) == ["GALE01", "RMCE01", "SMNE01"]
    assert r.existing == [] and r.missing == ["RSPE01"]


def test_cada_caratula_se_baja_a_disco_antes_del_rename(tmp_path, cache, monkeypatch):
    raiz = _unidad(tmp_path)
    real = os.fsync
    llamadas = []

    def _fsync(fd):
        llamadas.append(fd)
        return real(fd)

    monkeypatch.setattr(os, "fsync", _fsync)

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert len(llamadas) == len(r.copied) == 3


# ------------------------------------------------------------- Con GTK --
@pytest.fixture(scope="module")
def gtk():
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        if os.environ.get("WBM_REQUIRE_GUI") == "1":
            pytest.fail("WBM_REQUIRE_GUI=1 pero no hay display")
        pytest.skip("sin display")
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    if not Gtk.init_check():
        pytest.skip("GTK no pudo inicializarse")
    Adw.init()
    return Gtk


def _esperar(condicion, segundos: float = 5.0) -> bool:
    from gi.repository import GLib
    contexto = GLib.MainContext.default()
    limite = time.monotonic() + segundos
    while True:
        while contexto.iteration(False):
            pass
        if condicion() or time.monotonic() > limite:
            return condicion()
        time.sleep(0.01)


@pytest.fixture
def vista(gtk, monkeypatch, tmp_path, cache):
    from wiibackup_manager import drives
    from wiibackup_manager.widgets import transfer_view

    monkeypatch.setattr(drives, "list_removable_drives", lambda: [])
    monkeypatch.setattr(gametdb, "fetch_cover_async", lambda *a, **k: None)
    toasts = []
    v = transfer_view.TransferView(config.Settings(library_path=str(tmp_path)),
                                   toasts.append)
    ventana = gtk.Window()
    ventana.set_child(v)
    v.toasts = toasts
    yield v
    v.shutdown()
    ventana.destroy()


def _elegir_destino(v, carpeta: Path):
    v._on_folder_picked(SimpleNamespace(
        select_folder_finish=lambda _r: SimpleNamespace(get_path=lambda: str(carpeta))),
        None)
    assert v._dest_path == carpeta


@pytest.mark.gui
def test_el_boton_esta_apagado_sin_usb_loader_gx(vista, tmp_path):
    raiz = _unidad(tmp_path, con_loader=False)
    _elegir_destino(vista, raiz)

    assert vista.covers_button.get_label() == "Copiar carátulas a la unidad"
    assert not vista.covers_button.get_sensitive()
    assert "apps/usbloader_gx" in vista.covers_button.get_tooltip_text()


@pytest.mark.gui
def test_el_boton_copia_y_avisa_cuantas(vista, tmp_path):
    raiz = _unidad(tmp_path)
    _elegir_destino(vista, raiz)
    assert vista.covers_button.get_sensitive()

    vista.covers_button.emit("clicked")

    assert _esperar(lambda: vista.toasts and "Carátulas" in vista.toasts[-1])
    assert vista.toasts[-1] == ("Carátulas para USB Loader GX: 3 copiadas, 1 juego sin "
                                "carátula guardada en este equipo.")
    assert sorted(p.name for p in _carpeta_2d(raiz).iterdir()) == [
        "GALE01.png", "RMCE01.png", "SMNE01.png"]
    assert _esperar(lambda: vista.covers_button.get_sensitive())
    assert vista.ops.is_resource_busy(raiz) is None


@pytest.mark.gui
def test_mientras_copia_no_se_puede_expulsar_ni_transferir_ahi(vista, tmp_path, monkeypatch):
    from wiibackup_manager.operations import OperationKind

    raiz = _unidad(tmp_path)
    _elegir_destino(vista, raiz)
    seguir = threading.Event()
    durante = {}
    real = usbloader_covers.copy_drive_covers

    def _lento(drive_root, region, should_stop=None):
        durante["hilo_de_gtk"] = threading.current_thread() is threading.main_thread()
        durante["ocupada"] = vista.ops.is_resource_busy(raiz)
        seguir.wait(5)
        return real(drive_root, region, should_stop)

    monkeypatch.setattr(usbloader_covers, "copy_drive_covers", _lento)
    vista.covers_button.emit("clicked")
    assert _esperar(lambda: "ocupada" in durante)
    assert not vista.covers_button.get_sensitive()
    seguir.set()

    assert durante["hilo_de_gtk"] is False
    assert durante["ocupada"] is not None
    assert durante["ocupada"].kind is OperationKind.COPYING_COVERS
    assert _esperar(lambda: vista.ops.is_resource_busy(raiz) is None)


@pytest.mark.gui
def test_cerrar_la_app_corta_la_copia(vista, tmp_path, monkeypatch):
    raiz = _unidad(tmp_path)
    _elegir_destino(vista, raiz)
    seguir = threading.Event()
    visto = {}

    def _lento(drive_root, region, should_stop=None):
        visto["empezo"] = True
        seguir.wait(5)
        visto["cortar"] = should_stop()
        return usbloader_covers.CopyResult(usbloader_covers.covers_target(drive_root),
                                           stopped=True)

    monkeypatch.setattr(usbloader_covers, "copy_drive_covers", _lento)
    vista.covers_button.emit("clicked")
    assert _esperar(lambda: visto.get("empezo"))

    vista.shutdown()
    seguir.set()

    assert _esperar(lambda: "cortar" in visto)
    assert visto["cortar"] is True


@pytest.mark.gui
def test_los_mensajes_y_el_historial(gtk, tmp_path):
    from wiibackup_manager import oplog
    from wiibackup_manager.widgets.transfer_view import TransferView

    raiz = tmp_path / "SD cliente"
    CopyResult, CoversTarget = usbloader_covers.CopyResult, usbloader_covers.CoversTarget
    sin_loader = CopyResult(CoversTarget(None, usbloader_covers.NO_LOADER))
    vacia = CopyResult(CoversTarget(raiz))
    mezcla = CopyResult(CoversTarget(raiz), copied=["RMCE01"], existing=["A", "B"],
                        failed=[("SMNE01", "Input/output error")], stopped=True)

    assert TransferView._covers_message(sin_loader, None) == (
        "Esta unidad no tiene USB Loader GX (falta la carpeta apps/usbloader_gx): "
        "no se copió nada.")
    assert TransferView._covers_message(vacia, None) == (
        "No hay juegos en la unidad para copiarles la carátula.")
    assert TransferView._covers_message(mezcla, None) == (
        "Carátulas para USB Loader GX: 1 copiada, 2 ya estaban, 1 no se pudo copiar. "
        "Se cortó antes de terminar: revisá que la unidad siga conectada.")
    assert TransferView._covers_message(None, "Permiso denegado") == (
        "No se pudieron copiar las carátulas: Permiso denegado")

    assert TransferView._covers_outcome(raiz, sin_loader, None) is None
    assert TransferView._covers_outcome(raiz, vacia, None) is None
    con_error = TransferView._covers_outcome(raiz, mezcla, None)
    assert (con_error.status, con_error.target) == (oplog.STATUS_ERROR, "SD cliente")
    bien = TransferView._covers_outcome(raiz, CopyResult(CoversTarget(raiz),
                                                         copied=["RMCE01"]), None)
    assert (bien.status, bien.detail) == (oplog.STATUS_OK,
                                          "Carátulas para USB Loader GX: 1 copiada.")
    assert TransferView._covers_outcome(raiz, None, "Sin espacio").status == oplog.STATUS_ERROR


@pytest.mark.gui
def test_con_la_cola_escribiendo_en_esa_unidad_no_arranca(vista, tmp_path, monkeypatch):
    raiz = _unidad(tmp_path)
    _elegir_destino(vista, raiz)
    monkeypatch.setattr(vista.queue, "is_writing_to", lambda ruta: Path(ruta) == raiz)

    vista.covers_button.emit("clicked")

    assert vista.toasts[-1] == ("Esperá a que la cola termine con esta unidad para "
                                "copiar las carátulas.")
    assert not _carpeta_2d(raiz).exists()


@pytest.mark.gui
def test_si_gxglobal_las_manda_a_la_sd_lo_dice_y_no_copia(vista, tmp_path):
    raiz = _unidad(tmp_path)
    (raiz / "apps" / "usbloader_gx" / "GXGlobal.cfg").write_text(
        "covers2d_path = sd:/apps/usbloader_gx/images/2D/\n")
    _elegir_destino(vista, raiz)

    vista.covers_button.emit("clicked")

    assert _esperar(lambda: vista.toasts and "sd:/" in vista.toasts[-1])
    assert vista.toasts[-1] == ("USB Loader GX en esta unidad lee las carátulas 2D de "
                                "sd:/apps/usbloader_gx/images/2D/: no se copió nada.")
    assert not _carpeta_2d(raiz).exists()
