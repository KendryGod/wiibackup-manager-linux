"""La Store tiene un botón "Instalar desde una carpeta de esta PC…".

Es aparte del "Elegir carpeta…" de siempre, que elige el DESTINO. Elige la
carpeta a instalar (la de una app, o una armada como la raíz de una SD) y
la instala en el destino elegido con `oscwii_installer.install_local_folder`:
el mismo bloqueo de la unidad, el mismo aviso de dónde va a escribir y el
mismo historial que una app del catálogo. Como no tiene tarjeta, su
progreso va en una franja arriba ("Instalando «X»… N de M archivos (P%)")
con un botón Cancelar. Las configuraciones maestras no se aplican: van
atadas a las apps del catálogo.
"""
from __future__ import annotations

import os
import threading
import types
from pathlib import Path

import pytest

from homebrew_falso import dol, wad
from wiibackup_manager import config, golden_configs, oplog, oscwii_installer
from wiibackup_manager.operations import OperationKind, OperationManager
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import AvisoDeInstalacion, InstallProgress, InstallStatus

YAWM = HomebrewApp(slug="yawmME", name="YAWM ModMii Edition",
                   zip_url="https://hbb1.oscwii.org/api/contents/yawmME/yawmME.zip")
RAIZ_DEL_TALLER = {
    "apps/usbloader_gx/boot.dol": dol(b"usbloader_gx"),
    "apps/usbloader_gx/meta.xml": b"<app/>",
    "apps/usbloader_gx/icon.png": b"png",
    "apps/Nintendont/boot.dol": dol(b"nintendont"),
    "apps/yawmME/boot.dol": dol(b"yawm"),
    "wad/forwarder.wad": wad((b"contenido",)),
}


def _poner(base: Path, archivos: dict) -> Path:
    for nombre, datos in archivos.items():
        (base / nombre).parent.mkdir(parents=True, exist_ok=True)
        (base / nombre).write_bytes(datos)
    return base


class _Franja:
    def __init__(self):
        self.titulos, self.visible, self.boton = [], False, ""

    def set_title(self, texto):
        self.titulos.append(texto)

    def set_revealed(self, valor):
        self.visible = valor

    def set_button_label(self, texto):
        self.boton = texto


class _Boton:
    def __init__(self):
        self.activo = True

    def set_sensitive(self, valor):
        self.activo = valor


class _Hilo:
    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


def _vista(tmp_path, historial=None, **extra):
    """La vista con lo que usa la instalación desde una carpeta, sin GTK."""
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    destino = tmp_path / "usb"
    destino.mkdir(exist_ok=True)
    toasts = []
    vista = types.SimpleNamespace(
        _dest_path=destino, ops=OperationManager(log=historial), op_log=historial,
        settings=config.Settings(golden_configs_dir=str(tmp_path / "configs")),
        _show_toast=toasts.append, _install_states={}, _card_by_slug={},
        _apps_por_slug={"yawmME": YAWM}, local_banner=_Franja(), local_btn=_Boton(),
        _app_local=None, toasts=toasts, **extra)
    for metodo in ("_start_install", "_on_install_progress", "_on_install_done",
                   "_mostrar_aviso", "_pintar_franja", "_on_cancelar_local",
                   "_on_pick_local_folder", "_on_local_folder_picked"):
        if metodo not in extra:
            setattr(vista, metodo, types.MethodType(getattr(HomebrewStoreView, metodo), vista))
    return vista


@pytest.fixture
def sincronico(monkeypatch, tmp_path):
    """El hilo de la instalación y los `idle_add` corren en el acto; el
    aviso contesta "Instalar" y anota lo que se le mostró."""
    from wiibackup_manager.widgets import homebrew_store_view as vista_mod

    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(vista_mod.threading, "Thread", _Hilo)
    monkeypatch.setattr(vista_mod.GLib, "idle_add", lambda f, *a: f(*a))
    mostrados = []

    def _dialogo(nombre, aviso, al_responder, ofrecer_yawm=None):
        mostrados.append((nombre, aviso, ofrecer_yawm))
        al_responder(False)
        return types.SimpleNamespace(present=lambda _p: None, force_close=lambda: None)

    monkeypatch.setattr(vista_mod, "_dialogo_del_aviso", _dialogo)
    return mostrados


# --------------------------------------------------------- de punta a punta --
def test_una_carpeta_de_esta_pc_se_instala_con_el_mismo_aviso(tmp_path, sincronico, monkeypatch):
    historial = oplog.OperationLog(path=tmp_path / "history.json")
    vista = _vista(tmp_path, historial)
    carpeta = _poner(tmp_path / "SD del taller", RAIZ_DEL_TALLER)
    maestras = []
    monkeypatch.setattr(golden_configs, "maybe_apply", lambda *a, **k: maestras.append(a))
    arranque = {}
    instalar = oscwii_installer.install_local_folder

    def _espiar(*a, **k):
        arranque["boton"] = vista.local_btn.activo
        arranque["franja"] = (vista.local_banner.visible, vista.local_banner.boton)
        arranque["bloqueo"] = [(op.kind, op.write_paths) for op in vista.ops.active_operations()]
        return instalar(*a, **k)

    monkeypatch.setattr(oscwii_installer, "install_local_folder", _espiar)
    dialogo = types.SimpleNamespace(select_folder_finish=lambda _r: _gfile(carpeta))
    # Lo que haya quedado de una cancelación anterior no se arrastra.
    vista.local_banner.boton = ""

    vista._on_local_folder_picked(dialogo, None)

    destino = vista._dest_path
    assert {p.relative_to(destino).as_posix(): p.read_bytes()
            for p in destino.rglob("*") if p.is_file()} == RAIZ_DEL_TALLER
    # El mismo diálogo, con el nombre de la carpeta. YAWM no se ofrece: la
    # carpeta ya lo trae.
    [(nombre, aviso, ofrecer_yawm)] = sincronico
    assert nombre == "SD del taller"
    assert isinstance(aviso, AvisoDeInstalacion) and aviso.wads == ("wad/forwarder.wad",)
    assert ofrecer_yawm is None
    # Mientras tanto: el botón apagado, la franja con Cancelar y la unidad
    # entera ocupada.
    assert arranque["boton"] is False
    assert arranque["franja"] == (True, "Cancelar")
    assert arranque["bloqueo"] == [(OperationKind.INSTALLING_HOMEBREW,
                                    frozenset({destino.resolve()}))]
    # La franja dijo qué pasaba, y al terminar se fue.
    franja = vista.local_banner
    assert franja.titulos[0] == "Preparando «SD del taller»…"
    assert "«SD del taller»: esperando tu confirmación…" in franja.titulos
    assert franja.titulos[-1] == "Instalando «SD del taller»… 6 de 6 archivos (100%)"
    assert franja.visible is False
    assert vista.local_btn.activo is True
    assert vista._app_local is None
    # El resultado: toast e historial con el mismo formato, sin configuración maestra.
    assert "'SD del taller' instalada (6 archivo(s))." in vista.toasts
    [entrada] = historial.entries()
    assert entrada.operation == OperationKind.INSTALLING_HOMEBREW.value
    assert entrada.target == "SD del taller" and entrada.status == oplog.STATUS_OK
    assert "fuera de apps/: wad/ (1)" in entrada.detail
    assert maestras == []
    assert vista.ops.active_operations() == []


def _gfile(ruta):
    from gi.repository import Gio
    return Gio.File.new_for_path(str(ruta))


def test_cancelar_desde_la_franja_mientras_pregunta_no_escribe_nada(tmp_path, monkeypatch,
                                                                   sincronico):
    from wiibackup_manager.widgets import homebrew_store_view as vista_mod

    vista = _vista(tmp_path)
    carpeta = _poner(tmp_path / "SD del taller", RAIZ_DEL_TALLER)

    def _cancelar_y_responder(nombre, aviso, al_responder, ofrecer_yawm=None):
        vista._on_cancelar_local()
        al_responder(False)
        return types.SimpleNamespace(present=lambda _p: None, force_close=lambda: None)

    monkeypatch.setattr(vista_mod, "_dialogo_del_aviso", _cancelar_y_responder)

    vista._start_install(HomebrewApp(slug="local:SD del taller", name="SD del taller"),
                         carpeta=carpeta)

    assert vista.toasts == ["Instalación de 'SD del taller' cancelada."]
    assert "Cancelando «SD del taller»…" in vista.local_banner.titulos
    assert os.listdir(vista._dest_path) == []
    assert vista.local_banner.visible is False and vista.local_btn.activo is True


def test_un_rechazo_de_la_carpeta_se_muestra_con_su_motivo(tmp_path, sincronico):
    historial = oplog.OperationLog(path=tmp_path / "history.json")
    vista = _vista(tmp_path, historial)
    carpeta = _poner(tmp_path / "Descargas", {"leeme.txt": b"hola"})

    vista._start_install(HomebrewApp(slug="local:Descargas", name="Descargas"),
                         carpeta=carpeta)

    [toast] = vista.toasts
    assert toast.startswith("No se pudo instalar 'Descargas': la carpeta no tiene apps/")
    assert sincronico == []
    assert vista.local_banner.visible is False and vista.local_btn.activo is True
    [entrada] = historial.entries()
    assert entrada.status == oplog.STATUS_ERROR and "apps/" in entrada.detail
    assert os.listdir(vista._dest_path) == []


def test_la_etiqueta_del_estado_nuevo_existe():
    from wiibackup_manager.widgets.homebrew_store_view import _INSTALL_STATUS_LABELS

    assert _INSTALL_STATUS_LABELS[InstallStatus.BAD_FOLDER] == "La carpeta no se puede instalar"


# ------------------------------------------------------------- elegir --
def test_sin_destino_pide_elegirlo_primero(tmp_path):
    vista = _vista(tmp_path)
    vista._dest_path = None

    vista._on_pick_local_folder()

    assert vista.toasts == ["Elegí primero un destino antes de instalar."]


def test_la_carpeta_elegida_arranca_la_instalacion_con_su_nombre(tmp_path):
    pedidos = []
    vista = _vista(tmp_path, _start_install=lambda app, dest_root=None, carpeta=None:
                   pedidos.append((app, dest_root, carpeta)))
    carpeta = _poner(tmp_path / "WiiDonut", {"boot.dol": dol(b"x")})

    vista._on_local_folder_picked(
        types.SimpleNamespace(select_folder_finish=lambda _r: _gfile(carpeta)), None)

    [(app, dest_root, elegida)] = pedidos
    assert (app.slug, app.name) == ("local:WiiDonut", "WiiDonut")
    assert app.slug.startswith(oscwii_installer.PREFIJO_LOCAL)
    assert dest_root is None and elegida == carpeta


def test_una_ubicacion_sin_ruta_local_se_avisa(tmp_path):
    from wiibackup_manager.widgets import gtk_helpers

    pedidos = []
    vista = _vista(tmp_path, _start_install=lambda *a, **k: pedidos.append(a))
    sin_ruta = types.SimpleNamespace(get_path=lambda: None)

    vista._on_local_folder_picked(types.SimpleNamespace(select_folder_finish=lambda _r: sin_ruta),
                                  None)

    assert vista.toasts == [gtk_helpers.no_local_path_message()]
    assert pedidos == []


def test_si_la_unidad_esta_ocupada_no_arranca(tmp_path):
    vista = _vista(tmp_path)
    vista.ops.start(OperationKind.FORMATTING, resources=[vista._dest_path])

    vista._start_install(HomebrewApp(slug="local:WiiDonut", name="WiiDonut"),
                         carpeta=tmp_path / "WiiDonut")

    [toast] = vista.toasts
    assert toast.startswith("No se puede ahora:")
    assert vista.local_banner.titulos == [] and vista.local_btn.activo is True


# ------------------------------------------------------------- la franja --
def test_el_texto_de_la_franja():
    from wiibackup_manager.widgets.homebrew_store_view import _texto_de_la_franja

    assert _texto_de_la_franja("Mi SD", InstallProgress("prepare", 0.5, 3, 6)) == (
        "Preparando «Mi SD»… 3 de 6 archivos")
    assert _texto_de_la_franja("Mi SD", InstallProgress("extract", 0.25, 1, 4)) == (
        "Instalando «Mi SD»… 1 de 4 archivos (25%)")


def test_cancelar_desde_la_franja_corta_la_instalacion(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import _CardState, _InstallState

    app = HomebrewApp(slug="local:Mi SD", name="Mi SD")
    cancelar = threading.Event()
    vista = _vista(tmp_path)
    vista._app_local = app
    vista._install_states[app.slug] = _InstallState(kind=_CardState.EXTRACTING,
                                                    cancel_event=cancelar)
    vista.local_banner.boton = "Cancelar"

    vista._on_cancelar_local()

    assert cancelar.is_set()
    assert vista.local_banner.titulos == ["Cancelando «Mi SD»…"]
    assert vista.local_banner.boton == ""
    # Un aviso de progreso que llega tarde no la pisa.
    vista._on_install_progress(app, InstallProgress("extract", 0.5, 1, 2))
    assert vista.local_banner.titulos == ["Cancelando «Mi SD»…"]


def test_una_app_del_catalogo_no_usa_la_franja(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import _CardState, _InstallState

    app = HomebrewApp(slug="WiiDonut", name="Wii Donut")
    vista = _vista(tmp_path)
    vista._install_states[app.slug] = _InstallState(kind=_CardState.EXTRACTING)

    vista._on_install_progress(app, InstallProgress("extract", 0.5, 1, 2))

    assert vista.local_banner.titulos == []


# ---------------------------------------------------------------- YAWM --
def test_yawm_no_se_ofrece_si_lo_que_se_instala_ya_lo_trae(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import _yawm_a_ofrecer

    con_yawm = AvisoDeInstalacion(reemplaza=("apps/Nintendont/", "apps/YAWMME/"),
                                  carpetas=(("wad/", 1),), distintos=(), intocables=(),
                                  omitidos=(), wads=("wad/forwarder.wad",))
    sin_yawm = AvisoDeInstalacion(reemplaza=("apps/Nintendont/",), carpetas=(("wad/", 1),),
                                  distintos=(), intocables=(), omitidos=(),
                                  wads=("wad/forwarder.wad",))

    assert _yawm_a_ofrecer(con_yawm, {"yawmME": YAWM}, tmp_path) is None
    assert _yawm_a_ofrecer(sin_yawm, {"yawmME": YAWM}, tmp_path) is YAWM


# ------------------------------------------------------------------ GTK --
@pytest.mark.gui
def test_el_boton_y_la_franja_estan_en_la_store(monkeypatch):
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("sin display")
    from gi.repository import Adw, Gtk
    if not Gtk.init_check():
        pytest.skip("GTK no pudo inicializarse")
    Adw.init()
    from wiibackup_manager.widgets import homebrew_store_view as vista_mod

    monkeypatch.setattr(vista_mod.oscwii_client, "fetch_apps_async", lambda on_done: None)
    monkeypatch.setattr(vista_mod.drives, "list_removable_drives", lambda: [])
    monkeypatch.setattr(vista_mod.GLib, "timeout_add_seconds", lambda *_a: 0)
    toasts = []

    vista = vista_mod.HomebrewStoreView(config.Settings(), toasts.append)

    boton = vista.local_btn
    assert boton.get_label() == "Instalar desde una carpeta de esta PC…"
    # En la fila del destino, pero no es el "Elegir carpeta…" del destino.
    hermanos = []
    hijo = boton.get_parent().get_first_child()
    while hijo is not None:
        hermanos.append(hijo)
        hijo = hijo.get_next_sibling()
    etiquetas = [h.get_label() for h in hermanos if isinstance(h, Gtk.Button)]
    assert "Elegir carpeta…" in etiquetas and boton.get_label() in etiquetas
    franja = vista.local_banner
    assert franja.get_revealed() is False
    assert franja.get_use_markup() is False
    assert franja.get_parent() is vista
    # Conectados: el botón (sin destino, pide elegirlo) y el Cancelar de la franja.
    vista._dest_path = None
    boton.emit("clicked")
    assert toasts == ["Elegí primero un destino antes de instalar."]
    from wiibackup_manager.widgets.homebrew_store_view import _CardState, _InstallState
    cancelar = threading.Event()
    vista._app_local = HomebrewApp(slug="local:Mi SD", name="Mi SD")
    vista._install_states["local:Mi SD"] = _InstallState(kind=_CardState.EXTRACTING,
                                                         cancel_event=cancelar)
    franja.emit("button-clicked")
    assert cancelar.is_set()
