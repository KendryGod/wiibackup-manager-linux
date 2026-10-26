"""Página 'Homebrew Store': catálogo de Open Shop Channel en tarjetas,
con descarga e instalación a /apps/ del destino elegido.

Sigue el mismo reparto de responsabilidades que el resto de la app: esta
vista dibuja y reacciona a clicks; quien de verdad sabe listar, descargar y
verificar es `oscwii_client`/`oscwii_installer` (Pasos 1 y 2), y todo lo que
tarda -bajar el catálogo, bajar un ZIP, extraerlo- corre en un hilo de
fondo y vuelve al hilo de GTK con `GLib.idle_add`, igual que
`queue_manager.TransferQueue` para las transferencias."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango  # noqa: E402

from .. import (drives, formatting, golden_configs, oplog, oscwii_client,
                oscwii_installer)
from ..i18n import _, ngettext
from ..oscwii_client import HomebrewApp
from ..oscwii_installer import InstallStatus
from ..operations import OperationBusy, OperationKind, OperationOutcome
from . import gtk_helpers

ICON_WIDTH = 160
ICON_HEIGHT = 60  # las carátulas de OSC son PNG de 128x48 (confirmado
                   # bajando tres reales, ver oscwii_client.py); se reserva
                   # ese espacio de entrada para que las tarjetas no salten.
ICON_PLACEHOLDER = "application-x-executable-symbolic"


def _build_icon_widget(width: int = ICON_WIDTH, height: int = ICON_HEIGHT):
    """Mismo patrón que `game_row.build_cover_widget`: tamaño fijo, ícono
    de placeholder de fondo y la imagen real superpuesta cuando carga."""
    overlay = Gtk.Overlay()
    overlay.set_size_request(width, height)

    placeholder = Gtk.Image.new_from_icon_name(ICON_PLACEHOLDER)
    placeholder.set_pixel_size(int(height * 0.55))
    placeholder.add_css_class("dim-label")
    overlay.set_child(placeholder)

    picture = Gtk.Picture()
    picture.set_content_fit(Gtk.ContentFit.CONTAIN)
    picture.set_can_shrink(True)
    overlay.add_overlay(picture)
    return overlay, picture


class _AppItem(GObject.Object):
    """Envoltorio GObject de una `HomebrewApp`: `Gio.ListStore` necesita
    que cada elemento sea un GObject, y `HomebrewApp` es un dataclass
    liviano que no tiene por qué saber nada de GTK."""

    def __init__(self, app: HomebrewApp):
        super().__init__()
        self.app = app


class _CardState(Enum):
    IDLE = "idle"
    DOWNLOADING = "downloading"
    EXTRACTING = "extracting"
    DONE = "done"
    ERROR = "error"


@dataclass
class _InstallState:
    """Estado de instalación de UNA app, por slug. Vive en la vista (no en
    la tarjeta): con un `Gtk.GridView` las tarjetas se reciclan al
    scrollear, así que la fuente de verdad tiene que sobrevivir a que el
    widget que la mostraba deje de existir y vuelva a aparecer después."""

    kind: _CardState = _CardState.IDLE
    fraction: float = 0.0
    message: str = ""
    cancel_event: Optional[threading.Event] = None
    # El aviso antes de instalar, mientras está abierto (ver `_mostrar_aviso`).
    dialogo: Optional[Adw.AlertDialog] = None
    # (YAWM, destino) si en el aviso se marcó instalarlo también: se instala
    # cuando esta app termina bien (ver `_on_install_done`).
    yawm: Optional[tuple] = None


# Motivo legible por `InstallStatus`, para el mensaje corto que se ve en la
# tarjeta (el detalle completo de `oscwii_installer` va en el toast).
_INSTALL_STATUS_LABELS = {
    InstallStatus.DOWNLOAD_ERROR: _("Error de descarga"),
    InstallStatus.INCOMPLETE_DOWNLOAD: _("Descarga incompleta, reintentá"),
    InstallStatus.HASH_MISMATCH: _("El archivo no coincide con lo esperado"),
    InstallStatus.BAD_ZIP: _("El archivo descargado está dañado"),
    InstallStatus.BAD_CONTENT: _("El paquete trae un archivo dañado"),
    InstallStatus.UNSAFE_ZIP: _("El paquete no es seguro para instalar"),
    InstallStatus.BAD_FOLDER: _("La carpeta no se puede instalar"),
    InstallStatus.UNSAFE_URL: _("La descarga apuntaba a un sitio no autorizado"),
    InstallStatus.UNSAFE_DEST_ROOT: _("El destino elegido no es válido"),
    InstallStatus.NO_SPACE: _("No hay espacio suficiente en el destino"),
    InstallStatus.IO_ERROR: _("Error al escribir en el destino"),
    InstallStatus.DEVICE_DISCONNECTED: _("Se desconectó la unidad"),
    InstallStatus.ROLLBACK_FAILED: _("Error grave: no se pudo restaurar la versión anterior"),
}


def _install_outcome(app: HomebrewApp, result) -> OperationOutcome:
    """Lo que queda en el historial de una instalación. Sin esto
    `ops.finish` no anotaba nada y la instalación no dejaba rastro."""
    if result.ok:
        detalle = _("{n} archivo(s)").format(n=len(result.installed_paths))
        # Lo de fuera de `apps/` queda anotado: son carpetas que comparten
        # las ROMs, partidas y configuración del usuario.
        if result.carpetas_de_datos:
            detalle += "; " + _("fuera de apps/: {folders}").format(
                folders=", ".join(f"{carpeta} ({n})"
                                  for carpeta, n in result.carpetas_de_datos))
        if result.wads:
            detalle += "; " + _("WAD en /wad/ (se instala con YAWM desde la Wii): {names}").format(
                names=", ".join(Path(w).name for w in result.wads))
        if result.datos_conservados:
            n = len(result.datos_conservados)
            detalle += "; " + ngettext("se conservó {n} archivo tuyo",
                                       "se conservaron {n} archivos tuyos", n).format(n=n)
        return OperationOutcome(oplog.STATUS_OK, app.name, detalle)
    if result.status is InstallStatus.CANCELLED:
        return OperationOutcome(oplog.STATUS_CANCELLED, app.name)
    if result.status is InstallStatus.DEVICE_DISCONNECTED:
        return OperationOutcome(oplog.STATUS_DISCONNECTED, app.name, result.error)
    return OperationOutcome(
        oplog.STATUS_ERROR, app.name,
        result.error or _INSTALL_STATUS_LABELS.get(result.status, ""))


def _texto_de_wads(n: int) -> str:
    return ngettext("Se deja {n} WAD en /wad/: se instala con YAWM desde la Wii; "
                    "esta app no instala en la NAND.",
                    "Se dejan {n} WAD en /wad/: se instalan con YAWM desde la Wii; "
                    "esta app no instala en la NAND.", n).format(n=n)


# Cuántos nombres de cada lista entran en el aviso antes de "…y N más".
_AVISO_MAX_ITEMS = 8


def _lista(nombres) -> str:
    nombres = list(nombres)
    lineas = [f"• {n}" for n in nombres[:_AVISO_MAX_ITEMS]]
    resto = len(nombres) - _AVISO_MAX_ITEMS
    if resto > 0:
        lineas.append(_("• …y {n} más").format(n=resto))
    return "\n".join(lineas)


def _omitidos_para_mostrar(nombres) -> list:
    """Los archivos sueltos por su nombre y las carpetas en una línea con
    su cantidad: la guía del Homebrew Browser son 48 archivos."""
    sueltos, carpetas = [], {}
    for nombre in nombres:
        if "/" in nombre:
            carpeta = nombre.split("/")[0] + "/"
            carpetas[carpeta] = carpetas.get(carpeta, 0) + 1
        else:
            sueltos.append(nombre)
    return sueltos + [ngettext("{folder} ({n} archivo)", "{folder} ({n} archivos)", n)
                      .format(folder=carpeta, n=n) for carpeta, n in carpetas.items()]


def _texto_del_aviso(aviso: oscwii_installer.AvisoDeInstalacion) -> str:
    """El cuerpo del aviso antes de instalar (ver `_dialogo_del_aviso`)."""
    partes = []
    escribe = ([_("{path} (se reemplaza)").format(path=r) for r in aviso.reemplaza]
               + [ngettext("{folder}: {n} archivo nuevo", "{folder}: {n} archivos nuevos", n)
                  .format(folder=carpeta, n=n) for carpeta, n in aviso.carpetas])
    if escribe:
        partes.append(_("Va a escribir en:") + "\n" + _lista(escribe))
    if aviso.wads:
        partes.append(_texto_de_wads(len(aviso.wads)))
    if aviso.escribe_en_nand:
        partes.append(_("Esta app modifica la NAND de la Wii cuando la usás en la "
                        "consola. WiiBackup Manager no instala nada en la NAND."))
    if aviso.archivos > oscwii_installer.ARCHIVOS_QUE_TARDAN:
        partes.append(_("Son {n} archivos: puede tardar varios minutos.")
                      .format(n=aviso.archivos))
    if aviso.carpetas:
        partes.append(_("Fuera de apps/ no se borra nada de lo que ya tengas: "
                        "solo se agregan archivos."))
    if aviso.distintos:
        n = len(aviso.distintos)
        partes.append(
            ngettext("Ya tenés {n} archivo distinto al del paquete:",
                     "Ya tenés {n} archivos distintos a los del paquete:", n).format(n=n)
            + "\n" + _lista(aviso.distintos) + "\n"
            + _("Si no elegís reemplazarlos, se conservan los tuyos."))
    if aviso.intocables:
        partes.append(_("Se conservan siempre (el paquete los trae vacíos, o no se "
                        "pueden poner sin borrar algo tuyo):")
                      + "\n" + _lista(aviso.intocables))
    if aviso.omitidos:
        partes.append(_("No se copian:") + "\n"
                      + _lista(_omitidos_para_mostrar(aviso.omitidos)))
    return "\n\n".join(partes)


# El gestor de WADs que se ofrece instalar cuando un paquete deja un WAD
# en /wad/: YAWM ModMii Edition, del catálogo de Open Shop Channel.
YAWM_SLUG = "yawmME"


def _yawm_a_ofrecer(aviso, catalogo: dict, dest_root: Path) -> Optional[HomebrewApp]:
    """YAWM, si hay que ofrecerlo: el paquete deja un WAD en /wad/, YAWM
    está en el catálogo, y ni la unidad ni lo que se instala (una carpeta
    de esta PC con `apps/yawmME/`) lo tienen ya. Si no, None."""
    yawm = catalogo.get(YAWM_SLUG)
    if not aviso.wads or yawm is None:
        return None
    if any(r.rstrip("/").lower() == f"apps/{YAWM_SLUG.lower()}" for r in aviso.reemplaza):
        return None
    try:
        with os.scandir(Path(dest_root) / "apps") as entradas:
            if any(e.name.lower() == YAWM_SLUG.lower() for e in entradas):
                return None
    except OSError:
        pass
    return yawm


def _es_local(app: HomebrewApp) -> bool:
    """Si `app` es una carpeta de esta PC (ver `_on_local_folder_picked`)."""
    return app.slug.startswith(oscwii_installer.PREFIJO_LOCAL)


def _texto_de_la_franja(nombre: str, progress) -> str:
    """Lo que dice la franja mientras se instala una carpeta de esta PC."""
    if progress.phase == "prepare":
        return _("Preparando «{name}»… {done} de {total} archivos").format(
            name=nombre, done=progress.done, total=progress.total)
    return _("Instalando «{name}»… {done} de {total} archivos ({pct}%)").format(
        name=nombre, done=progress.done, total=progress.total,
        pct=int((progress.fraction or 0) * 100))


# Lo que cada botón del aviso le contesta a `install_app` (ver su
# parámetro `confirmar`): None no instala, True reemplaza los archivos
# distintos del usuario, False los conserva.
_RESPUESTAS_DEL_AVISO = {"cancel": None, "keep": False, "install": False, "replace": True}


def _dialogo_del_aviso(nombre_app: str, aviso, al_responder,
                       ofrecer_yawm: Optional[tuple] = None) -> Adw.AlertDialog:
    """Dónde va a escribir el paquete, antes de tocar la unidad. Si choca
    con archivos del usuario, lo que se ofrece por defecto es conservarlos.
    `al_responder` recibe None, False o True (`_RESPUESTAS_DEL_AVISO`).

    `ofrecer_yawm` es (nombre de YAWM, al_elegir): agrega una casilla
    DESMARCADA para instalarlo también, y si no se cancela, `al_elegir`
    recibe si quedó marcada (antes que `al_responder`)."""
    dialogo = Adw.AlertDialog(
        heading=_("¿Instalar '{name}'?").format(name=nombre_app),
        body=_texto_del_aviso(aviso))
    dialogo.add_response("cancel", _("Cancelar"))
    if aviso.distintos:
        dialogo.add_response("replace", _("Reemplazar los míos"))
        dialogo.add_response("keep", _("Conservar los míos"))
        dialogo.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
        dialogo.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
        dialogo.set_default_response("keep")
    else:
        dialogo.add_response("install", _("Instalar"))
        dialogo.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dialogo.set_default_response("install")
    dialogo.set_close_response("cancel")
    casilla = None
    if ofrecer_yawm is not None:
        casilla = Gtk.CheckButton(
            label=_("Instalar también {name} (para instalar el WAD desde la Wii)")
            .format(name=ofrecer_yawm[0]), active=False)
        dialogo.set_extra_child(casilla)

    def _respuesta(_d, boton):
        respuesta = _RESPUESTAS_DEL_AVISO.get(boton)
        if casilla is not None and respuesta is not None:
            ofrecer_yawm[1](casilla.get_active())
        al_responder(respuesta)

    dialogo.connect("response", _respuesta)
    return dialogo


def _preguntar_y_esperar(mostrar, cancel_event: threading.Event,
                         intervalo: float = 0.2):
    """Corre en el hilo de la instalación: `mostrar(responder)` pone el
    aviso en el hilo de GTK, y esto espera a que se llame a `responder`.
    Cancelar desde la tarjeta (o cerrar la ventana) también contesta: None,
    no instalar."""
    listo = threading.Event()
    respuesta = []

    def _responder(r):
        respuesta.append(r)
        listo.set()

    mostrar(_responder)
    while not listo.wait(intervalo):
        if cancel_event.is_set():
            return None
    return respuesta[0]


class HomebrewAppCard(Gtk.Box):
    """Una tarjeta del catálogo: ícono + nombre + descripción + botón de
    instalar. Deliberadamente tonta, igual que `JobRow` en
    transfer_view.py: no sabe descargar ni decide nada, solo se pinta a
    partir de lo que le pasa `HomebrewStoreView` y avisa clicks."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("card")
        self.set_size_request(200, -1)
        self.app: Optional[HomebrewApp] = None

        icon_widget, self.icon_picture = _build_icon_widget()
        icon_widget.set_margin_top(8)
        icon_widget.set_margin_start(8)
        icon_widget.set_margin_end(8)
        self.append(icon_widget)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                        margin_start=10, margin_end=10, margin_top=6,
                        margin_bottom=10)
        self.append(inner)

        self.name_label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.name_label.add_css_class("heading")
        inner.append(self.name_label)

        self.desc_label = Gtk.Label(xalign=0, wrap=True, lines=2,
                                    ellipsize=Pango.EllipsizeMode.END,
                                    valign=Gtk.Align.START)
        self.desc_label.add_css_class("dim-label")
        self.desc_label.add_css_class("caption")
        inner.append(self.desc_label)

        self.meta_label = Gtk.Label(xalign=0)
        self.meta_label.add_css_class("dim-label")
        self.meta_label.add_css_class("caption")
        inner.append(self.meta_label)

        # Las apps que modifican la NAND de la Wii al usarlas
        # (`HomebrewApp.writes_to_nand`).
        self.nand_label = Gtk.Label(xalign=0, label=_("Escribe en la NAND"),
                                    tooltip_text=_(
                                        "Al usarla en la consola, esta app modifica la "
                                        "memoria interna de la Wii. WiiBackup Manager "
                                        "solo la copia a la SD/USB."))
        self.nand_label.add_css_class("warning")
        self.nand_label.add_css_class("caption")
        inner.append(self.nand_label)

        # Empuja el botón al fondo de la tarjeta aunque la descripción de
        # esta app en particular sea de una sola línea.
        inner.append(Gtk.Box(vexpand=True))

        self.status_label = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.status_label.add_css_class("caption")
        inner.append(self.status_label)

        self.progress = Gtk.ProgressBar(visible=False)
        inner.append(self.progress)

        self.action_button = Gtk.Button(label=_("Instalar"))
        self.action_button.add_css_class("suggested-action")
        inner.append(self.action_button)


class HomebrewStoreView(Gtk.Box):
    def __init__(self, settings, show_toast_cb, ops=None, op_log=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.settings = settings
        self._show_toast = show_toast_cb
        if ops is None:
            from ..operations import OperationManager
            ops = OperationManager()
        self.ops = ops
        # Historial de operaciones compartido con la ventana: es donde
        # `golden_configs.maybe_apply` deja trazado cuándo se aplicó (o
        # falló) una configuración maestra. Opcional -None en los tests
        # que no necesitan ese rastro- para no obligar a construir un
        # `OperationLog` real en cada prueba de esta vista.
        self.op_log = op_log

        self._dest_choices: list = []  # [(etiqueta, Path), ...]
        self._dest_path: Optional[Path] = None
        self._manual_path: Optional[Path] = None
        self._known_mounts: set = set()

        self._search_text = ""
        self._apps_store = Gio.ListStore.new(_AppItem)
        # Tarjeta actualmente enlazada a cada slug (si hay alguna visible
        # ahora mismo): así una descarga en curso puede repintar la
        # tarjeta en vivo aunque el `Gtk.GridView` recicle widgets al
        # scrollear. Ver `_on_factory_bind`/`_on_factory_unbind`.
        self._card_by_slug: dict = {}
        # Estado de instalación por slug, independiente de si hay o no una
        # tarjeta mostrándolo en este instante. Ver `_InstallState`.
        self._install_states: dict = {}
        # El catálogo cargado, por slug (para ofrecer YAWM: `_yawm_a_ofrecer`).
        self._apps_por_slug: dict = {}
        # Lo que se está instalando desde una carpeta de esta PC, si hay algo
        # (ver `_on_local_folder_picked`).
        self._app_local: Optional[HomebrewApp] = None

        self._build_ui()
        self._refresh_dest_choices()
        self._load_apps()
        GLib.timeout_add_seconds(3, self._poll_dest_choices)

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        self.stale_banner = Adw.Banner(revealed=False)
        self.append(self.stale_banner)
        # Lo que se instala desde una carpeta de esta PC no tiene tarjeta en
        # el catálogo: su progreso va en esta franja (ver `_pintar_franja`).
        # Sin markup: el título lleva el nombre de la carpeta.
        self.local_banner = Adw.Banner(revealed=False, use_markup=False,
                                       button_label=_("Cancelar"))
        self.local_banner.connect("button-clicked", self._on_cancelar_local)
        self.append(self.local_banner)

        # --- Destino ---
        dest_group = Adw.PreferencesGroup(
            title=_("Destino de instalación"),
            description=_("Las apps se instalan en /apps/ dentro de la "
                          "unidad o carpeta que elijas acá."),
        )
        dest_group.set_margin_start(12)
        dest_group.set_margin_end(12)
        dest_group.set_margin_top(12)

        self._dest_model = Gtk.StringList.new([])
        self._dest_row = Adw.ComboRow(title=_("Destino"))
        self._dest_row.set_model(self._dest_model)
        self._dest_row.connect("notify::selected", self._on_dest_selected)
        dest_group.add(self._dest_row)
        self.append(dest_group)

        dest_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                               margin_start=12, margin_end=12,
                               margin_top=8, margin_bottom=8)
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text(_("Volver a detectar unidades"))
        refresh_btn.connect("clicked", lambda *_a: self._refresh_dest_choices())
        dest_buttons.append(refresh_btn)
        folder_btn = Gtk.Button(label=_("Elegir carpeta…"))
        folder_btn.connect("clicked", self._on_pick_folder)
        dest_buttons.append(folder_btn)
        # Elige lo que se instala, no el destino: va aparte, a la derecha.
        dest_buttons.append(Gtk.Box(hexpand=True))
        self.local_btn = Gtk.Button(label=_("Instalar desde una carpeta de esta PC…"))
        self.local_btn.set_tooltip_text(_(
            "Una app que no está en la tienda (su carpeta, con el boot.dol) o una "
            "carpeta armada como la raíz de una SD (con apps/). Antes de escribir "
            "se revisa entera y se muestra dónde va cada cosa."))
        self.local_btn.connect("clicked", self._on_pick_local_folder)
        dest_buttons.append(self.local_btn)
        self.append(dest_buttons)

        self.append(Gtk.Separator())

        # --- Catálogo: cargando / error / grilla ---
        self.state_stack = Gtk.Stack()
        self.state_stack.set_vexpand(True)
        self.append(self.state_stack)

        loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                              vexpand=True)
        # Gtk.Spinner y no Adw.Spinner: ese es de libadwaita 1.6, y con la
        # 1.5 (Ubuntu 24.04) la ventana entera no llega a armarse.
        spinner = Gtk.Spinner(spinning=True)
        spinner.set_size_request(32, 32)
        loading_box.append(spinner)
        loading_label = Gtk.Label(label=_("Cargando catálogo de Open Shop Channel…"))
        loading_label.add_css_class("dim-label")
        loading_box.append(loading_label)
        self.state_stack.add_named(loading_box, "loading")

        self.error_status = Adw.StatusPage(
            icon_name="network-error-symbolic",
            title=_("No se pudo cargar la tienda"),
        )
        retry_btn = Gtk.Button(label=_("Reintentar"))
        retry_btn.add_css_class("suggested-action")
        retry_btn.set_halign(Gtk.Align.CENTER)
        retry_btn.connect("clicked", lambda *_a: self._load_apps())
        self.error_status.set_child(retry_btn)
        self.state_stack.add_named(self.error_status, "error")

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.search_entry = Gtk.SearchEntry(
            placeholder_text=_("Buscar apps por nombre…"))
        self.search_entry.set_margin_start(12)
        self.search_entry.set_margin_end(12)
        self.search_entry.set_margin_top(8)
        self.search_entry.set_margin_bottom(8)
        self.search_entry.connect("search-changed", self._on_search_changed)
        content_box.append(self.search_entry)

        self._filter = Gtk.CustomFilter.new(self._filter_func)
        filter_model = Gtk.FilterListModel(model=self._apps_store, filter=self._filter)
        selection = Gtk.NoSelection(model=filter_model)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_factory_setup)
        factory.connect("bind", self._on_factory_bind)
        factory.connect("unbind", self._on_factory_unbind)

        self.grid_view = Gtk.GridView(model=selection, factory=factory)
        self.grid_view.set_single_click_activate(False)
        # La separación entre tarjetas ya la da la clase "card" de cada
        # una (mismo estilo que las carátulas de la Biblioteca); el grid
        # en sí no necesita ninguna clase propia.

        scroller = Gtk.ScrolledWindow(vexpand=True)
        scroller.set_child(self.grid_view)
        content_box.append(scroller)

        self.state_stack.add_named(content_box, "content")

    # ------------------------------------------------------------ Destino --
    @staticmethod
    def _is_dest_valid(path: Path) -> bool:
        try:
            return path.is_dir()
        except OSError:
            return False

    def _refresh_dest_choices(self):
        """Repuebla el desplegable de destino con: los accesos guardados en
        Transferir (compartidos vía `self.settings`, la misma unidad que ya
        usa esa pestaña), las unidades removibles montadas ahora, y la
        carpeta elegida a mano si sigue siendo válida. Mismo espíritu que
        `TransferView._refresh_drives`, pero sin guardar/expulsar: eso ya
        vive en Cola de Tareas, y duplicarlo acá crearía dos listas de
        accesos guardados que podrían divergir."""
        selected_path = self._dest_path
        choices: list = []
        seen: set = set()

        for preset in self.settings.dest_presets:
            p = Path(preset.get("path", ""))
            if p not in seen and self._is_dest_valid(p):
                choices.append((f"{preset.get('name', p)} ({p})", p))
                seen.add(p)

        auto_drives = drives.list_removable_drives()
        for drive in auto_drives:
            if drive.mount_point in seen:
                continue
            choices.append((f"{drive.name} ({drive.mount_point})", drive.mount_point))
            seen.add(drive.mount_point)
        self._known_mounts = {d.mount_point for d in auto_drives}

        if (self._manual_path is not None and self._manual_path not in seen
                and self._is_dest_valid(self._manual_path)):
            choices.append((str(self._manual_path), self._manual_path))
            seen.add(self._manual_path)

        self._dest_choices = choices
        while self._dest_model.get_n_items():
            self._dest_model.remove(0)
        for label, _path in choices:
            self._dest_model.append(label)

        if not choices:
            self._dest_path = None
            self._dest_row.set_sensitive(False)
            return
        self._dest_row.set_sensitive(True)

        idx = 0
        if selected_path is not None:
            for i, (_label, p) in enumerate(choices):
                if p == selected_path:
                    idx = i
                    break
        self._dest_row.set_selected(idx)
        self._dest_path = choices[idx][1]

    def _on_dest_selected(self, row, _pspec):
        idx = row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._dest_choices):
            self._dest_path = None
            return
        self._dest_path = self._dest_choices[idx][1]

    def _poll_dest_choices(self):
        current = {d.mount_point for d in drives.list_removable_drives()}
        if current != self._known_mounts:
            self._refresh_dest_choices()
        return True

    def _on_pick_folder(self, *_a):
        dialog = Gtk.FileDialog(title=_("Elegí la carpeta destino"))
        dialog.set_initial_folder(gtk_helpers.safe_initial_folder())
        dialog.select_folder(self.get_root(), None, self._on_folder_picked)

    def _on_folder_picked(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if not folder:
            return
        path = gtk_helpers.local_path(folder)
        if path is None:
            self._show_toast(gtk_helpers.no_local_path_message())
            return
        self._manual_path = path
        self._refresh_dest_choices()

    def _on_pick_local_folder(self, *_a):
        if self._dest_path is None:
            self._show_toast(_("Elegí primero un destino antes de instalar."))
            return
        dialog = Gtk.FileDialog(title=_("Elegí la carpeta a instalar"))
        dialog.set_initial_folder(gtk_helpers.safe_initial_folder())
        dialog.select_folder(self.get_root(), None, self._on_local_folder_picked)

    def _on_local_folder_picked(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if not folder:
            return
        path = gtk_helpers.local_path(folder)
        if path is None:
            self._show_toast(gtk_helpers.no_local_path_message())
            return
        # Sin tarjeta ni catálogo detrás: la app es solo el nombre de la
        # carpeta, con un slug que no choca con ninguno del catálogo.
        nombre = path.name or str(path)
        self._start_install(HomebrewApp(slug=oscwii_installer.PREFIJO_LOCAL + nombre,
                                        name=nombre), carpeta=path)

    # ----------------------------------------------------------- Catálogo --
    def _load_apps(self):
        self.state_stack.set_visible_child_name("loading")
        oscwii_client.fetch_apps_async(
            on_done=lambda result: GLib.idle_add(self._on_apps_loaded, result))

    def _on_apps_loaded(self, result) -> bool:
        if not gtk_helpers.widget_is_alive(self):
            return False

        if result.status is oscwii_client.FetchStatus.ERROR:
            # La descripción de un StatusPage siempre es markup.
            self.error_status.set_description(GLib.markup_escape_text(
                result.error or _("motivo desconocido")))
            self.state_stack.set_visible_child_name("error")
            return False

        self._apps_store.remove_all()
        self._card_by_slug.clear()
        self._apps_por_slug = {app.slug: app for app in result.apps}
        for app in result.apps:
            self._apps_store.append(_AppItem(app))

        if result.status is oscwii_client.FetchStatus.STALE_CACHE:
            self.stale_banner.set_title(
                _("Sin conexión con Open Shop Channel: mostrando la "
                  "última lista guardada ({error}).").format(error=result.error))
            self.stale_banner.set_revealed(True)
        else:
            self.stale_banner.set_revealed(False)

        self.state_stack.set_visible_child_name("content")
        return False

    def _on_search_changed(self, entry):
        self._search_text = entry.get_text().strip().lower()
        self._filter.changed(Gtk.FilterChange.DIFFERENT)

    def _filter_func(self, item) -> bool:
        if not self._search_text:
            return True
        app = item.app
        haystack = f"{app.name}\n{app.short_description}\n{app.author}".lower()
        return self._search_text in haystack

    # --------------------------------------------------------- Fábrica GridView --
    def _on_factory_setup(self, _factory, list_item):
        card = HomebrewAppCard()
        card.action_button.connect("clicked",
                                   lambda *_a: self._on_card_action_clicked(card))
        list_item.set_child(card)

    def _on_factory_bind(self, _factory, list_item):
        item = list_item.get_item()
        card = list_item.get_child()
        card.app = item.app
        self._card_by_slug[item.app.slug] = card
        self._render_card(card)

    def _on_factory_unbind(self, _factory, list_item):
        card = list_item.get_child()
        if card.app is not None and self._card_by_slug.get(card.app.slug) is card:
            del self._card_by_slug[card.app.slug]

    def _render_card(self, card: HomebrewAppCard):
        app = card.app
        card.name_label.set_label(app.name)
        card.desc_label.set_label(app.short_description)
        card.desc_label.set_visible(bool(app.short_description))
        meta = [p for p in (app.category, f"v{app.version}" if app.version else "") if p]
        card.meta_label.set_label(" · ".join(meta))
        card.meta_label.set_visible(bool(meta))
        card.nand_label.set_visible(app.writes_to_nand)

        card.icon_picture.set_paintable(None)
        slug = app.slug
        oscwii_client.fetch_icon_async(
            app, on_done=lambda path: GLib.idle_add(
                self._apply_icon, card, slug, str(path) if path else None))

        self._apply_install_state(card, self._install_states.get(slug, _InstallState()))

    def _apply_icon(self, card: HomebrewAppCard, slug: str, path: Optional[str]) -> bool:
        # La tarjeta puede haberse reciclado hacia otra app (o dejado de
        # existir) mientras el ícono todavía viajaba: mismo chequeo de
        # "pedido viejo" que `game_row.GameRow._apply_cover`.
        if not gtk_helpers.widget_is_alive(card):
            return False
        if card.app is None or card.app.slug != slug:
            return False
        if path:
            try:
                card.icon_picture.set_filename(path)
            except GLib.Error:
                pass
        return False

    # --------------------------------------------------------- Instalación --
    def _apply_install_state(self, card: HomebrewAppCard, state: _InstallState):
        busy = state.kind in (_CardState.DOWNLOADING, _CardState.EXTRACTING)
        card.progress.set_visible(busy)
        if busy:
            card.progress.set_fraction(state.fraction)

        card.status_label.set_visible(bool(state.message))
        card.status_label.set_label(state.message)
        card.status_label.remove_css_class("success")
        card.status_label.remove_css_class("error")

        card.action_button.set_sensitive(True)
        card.action_button.remove_css_class("suggested-action")
        card.action_button.remove_css_class("destructive-action")
        if state.kind is _CardState.DONE:
            card.action_button.set_label(_("Reinstalar"))
            card.status_label.add_css_class("success")
        elif state.kind is _CardState.ERROR:
            card.action_button.set_label(_("Reintentar"))
            card.action_button.add_css_class("destructive-action")
            card.status_label.add_css_class("error")
        elif busy:
            card.action_button.set_label(_("Cancelar"))
            card.action_button.add_css_class("destructive-action")
        else:
            card.action_button.set_label(_("Instalar"))
            card.action_button.add_css_class("suggested-action")

    def _on_card_action_clicked(self, card: HomebrewAppCard):
        app = card.app
        if app is None:
            return
        state = self._install_states.get(app.slug)
        if state is not None and state.kind in (_CardState.DOWNLOADING, _CardState.EXTRACTING):
            # Botón "Cancelar": apagarlo evita el doble click, igual que
            # `JobRow._on_cancel_clicked`. El estado final lo pone el
            # propio hilo de instalación al notar el `cancel_event`.
            card.action_button.set_sensitive(False)
            if state.cancel_event is not None:
                state.cancel_event.set()
            return
        self._start_install(app)

    def _start_install(self, app: HomebrewApp, dest_root: Optional[Path] = None,
                       carpeta: Optional[Path] = None):
        """Instala `app` en el destino elegido, o en `dest_root` si se pasa
        (YAWM va al mismo destino que la app que dejó el WAD). Con `carpeta`,
        lo que se instala es esa carpeta de esta PC
        (`oscwii_installer.install_local_folder`) y `app` solo le pone el
        nombre (ver `_on_local_folder_picked`)."""
        if dest_root is None:
            if self._dest_path is None:
                self._show_toast(_("Elegí primero un destino antes de instalar."))
                return
            dest_root = self._dest_path

        try:
            op = self.ops.start(
                OperationKind.INSTALLING_HOMEBREW,
                # Lo de una carpeta puede ir a cualquier carpeta permitida de
                # la unidad, no solo a `apps/<slug>`.
                write=[dest_root if carpeta is not None else dest_root / "apps" / app.slug],
                resources=drives.resources_for_mount_point(dest_root),
            )
        except OperationBusy as e:
            self._show_toast(_("No se puede ahora: {detail}.").format(detail=e.detail))
            return

        cancel_event = threading.Event()
        state = _InstallState(kind=_CardState.DOWNLOADING, fraction=0.0,
                              message=_("Preparando…"), cancel_event=cancel_event)
        self._install_states[app.slug] = state
        card = self._card_by_slug.get(app.slug)
        if card is not None:
            self._apply_install_state(card, state)
        if carpeta is not None:
            self._app_local = app
            self.local_btn.set_sensitive(False)
            self.local_banner.set_button_label(_("Cancelar"))
            self._pintar_franja(app, _("Preparando «{name}»…").format(name=app.name))

        def on_progress(progress):
            GLib.idle_add(self._on_install_progress, app, progress)

        def confirmar(aviso):
            return _preguntar_y_esperar(
                lambda responder: GLib.idle_add(self._mostrar_aviso, app, aviso,
                                                responder, dest_root),
                cancel_event)

        def worker():
            if carpeta is not None:
                # Sin configuración maestra: van atadas a las apps del catálogo.
                result = oscwii_installer.install_local_folder(
                    carpeta, dest_root, cancel_event=cancel_event,
                    on_progress=on_progress, confirmar=confirmar)
                GLib.idle_add(self._on_install_done, app, result, op, None)
                return
            result = oscwii_installer.install_app(
                app, dest_root, cancel_event=cancel_event, on_progress=on_progress,
                confirmar=confirmar)
            # Todavía en el hilo de fondo, a propósito: si `app.slug` no
            # tiene ninguna config maestra registrada (la inmensa mayoría
            # del catálogo), `maybe_apply` vuelve en el acto sin tocar
            # disco ni log. Cuando sí aplica, es una copia chica (un
            # archivo, no un ZIP entero) y no vale la pena otro salto de
            # hilo solo para eso.
            golden_result = golden_configs.maybe_apply(
                app, dest_root, result, op_log=self.op_log,
                local_dir=self.settings.golden_configs_dir)
            GLib.idle_add(self._on_install_done, app, result, op, golden_result)

        threading.Thread(target=worker, daemon=True,
                         name=f"oscwii-install-{app.slug}").start()

    def _mostrar_aviso(self, app: HomebrewApp, aviso, responder, dest_root: Path) -> bool:
        state = self._install_states.get(app.slug)
        if state is not None:
            state.message = _("Esperando tu confirmación…")
            card = self._card_by_slug.get(app.slug)
            if card is not None and gtk_helpers.widget_is_alive(card) and card.app is app:
                self._apply_install_state(card, state)
        if _es_local(app):
            self._pintar_franja(app, _("«{name}»: esperando tu confirmación…")
                                .format(name=app.name))
        ofrecer = None
        yawm = _yawm_a_ofrecer(aviso, self._apps_por_slug, dest_root)
        if yawm is not None and state is not None:
            def _elegir(marcada: bool) -> None:
                state.yawm = (yawm, dest_root) if marcada else None
            ofrecer = (yawm.name, _elegir)
        dialogo = _dialogo_del_aviso(app.name, aviso, responder, ofrecer)
        if state is not None:
            state.dialogo = dialogo
        dialogo.present(self)
        return False

    def _on_install_progress(self, app: HomebrewApp, progress) -> bool:
        state = self._install_states.get(app.slug)
        if state is None or state.kind not in (_CardState.DOWNLOADING, _CardState.EXTRACTING):
            # La instalación ya terminó (éxito, error o cancelación) y
            # este aviso de progreso llegó tarde: no lo pisa.
            return False

        state.kind = (_CardState.DOWNLOADING if progress.phase == "download"
                     else _CardState.EXTRACTING)
        if progress.fraction is not None:
            state.fraction = progress.fraction
            pct = int(progress.fraction * 100)
            if progress.phase == "download":
                state.message = _("Descargando… {pct}%").format(pct=pct)
            elif progress.total:
                state.message = _("Extrayendo… {done} de {total} archivos ({pct}%)").format(
                    done=progress.done, total=progress.total, pct=pct)
            else:
                state.message = _("Extrayendo… {pct}%").format(pct=pct)
        else:
            state.message = (_("Descargando…") if progress.phase == "download"
                             else _("Extrayendo…"))
        if _es_local(app):
            self._pintar_franja(app, _texto_de_la_franja(app.name, progress))

        card = self._card_by_slug.get(app.slug)
        if card is not None and gtk_helpers.widget_is_alive(card) and card.app is app:
            self._apply_install_state(card, state)
        return False

    def _on_install_done(self, app: HomebrewApp, result, op, golden_result=None) -> bool:
        self.ops.finish(op, _install_outcome(app, result))
        # Si se canceló desde la tarjeta con el aviso abierto, el aviso ya
        # no contesta nada: se cierra.
        anterior = self._install_states.get(app.slug)
        if anterior is not None and anterior.dialogo is not None:
            anterior.dialogo.force_close()
            anterior.dialogo = None
        # YAWM, si se marcó en el aviso: después de esta app, y solo si se
        # instaló bien.
        yawm = anterior.yawm if anterior is not None else None

        if result.ok:
            state = _InstallState(kind=_CardState.DONE, fraction=1.0,
                                  message=_("Instalada ✓"))
            self._show_toast(
                _("'{name}' instalada ({n} archivo(s)).")
                .format(name=app.name, n=len(result.installed_paths)))
            if result.wads:
                self._show_toast(
                    _texto_de_wads(len(result.wads)) + " "
                    + ", ".join(Path(w).name for w in result.wads))
            # Al reinstalar, que se sepa que lo del cliente sigue ahí (ver
            # `oscwii_installer.PRESERVED_PATHS`).
            if result.preserved_paths:
                self._show_toast(
                    _("Se conservaron los datos del cliente de '{name}': {paths}.")
                    .format(name=app.name, paths=", ".join(
                        Path(p).name for p in result.preserved_paths)))
            # Aviso aparte: la app en sí se instaló bien de todos modos,
            # así que un problema acá no cambia el estado de la tarjeta
            # (`state` sigue siendo DONE) -solo se le suma un segundo
            # toast explicando qué pasó con la config maestra.
            if golden_result is not None and golden_result.status.is_error:
                self._show_toast(
                    _("'{name}' se instaló, pero no se pudo aplicar su "
                      "configuración maestra: {detail}")
                    .format(name=app.name, detail=golden_result.error))
            elif golden_result is not None and golden_result.applied:
                self._show_toast(
                    _("Se aplicó la configuración maestra de '{name}'.")
                    .format(name=app.name))
            elif (golden_result is not None and golden_result.status
                  is golden_configs.GoldenConfigStatus.KEPT_EXISTING):
                self._show_toast(
                    _("'{name}' se instaló. Ya había un {file} en la unidad y "
                      "se dejó como estaba: no se aplicó la configuración "
                      "maestra.").format(name=app.name,
                                          file=golden_result.dest_path.name))
            elif (golden_result is not None and golden_result.status
                  is golden_configs.GoldenConfigStatus.SKIPPED_PLACEHOLDER):
                self._show_toast(
                    _("'{name}' se instaló con su configuración de fábrica: la "
                      "configuración maestra del repositorio es de relleno y no "
                      "se copió.").format(name=app.name))
            # Mismo criterio que el aviso de la config maestra: la app
            # quedó instalada (la tarjeta sigue en DONE), pero si el
            # respaldo de la versión anterior no se pudo borrar hay una
            # carpeta oculta ocupando espacio que el usuario no va a
            # encontrar solo. Se avisa y se anota en el historial en vez
            # de dejarlo pasar (ver `oscwii_installer._instalar_todo`).
            if result.orphaned_backups:
                aviso = formatting.format_orphaned_backups(result.orphaned_backups)
                self._show_toast(
                    _("'{name}' se instaló. {detail}")
                    .format(name=app.name, detail=aviso))
                oplog.record_orphaned_backup(self.op_log, app.name, aviso)
        elif result.status is InstallStatus.CANCELLED:
            state = _InstallState(kind=_CardState.IDLE)
            self._show_toast(_("Instalación de '{name}' cancelada.").format(name=app.name))
        else:
            label = _INSTALL_STATUS_LABELS.get(result.status, _("Error al instalar"))
            state = _InstallState(kind=_CardState.ERROR, message=label)
            self._show_toast(
                _("No se pudo instalar '{name}': {detail}")
                .format(name=app.name, detail=result.error or label))

        self._install_states[app.slug] = state
        card = self._card_by_slug.get(app.slug)
        if card is not None and gtk_helpers.widget_is_alive(card) and card.app is app:
            self._apply_install_state(card, state)
        if _es_local(app):
            self._app_local = None
            self.local_banner.set_revealed(False)
            self.local_btn.set_sensitive(True)
        if result.ok and yawm is not None:
            self._start_install(*yawm)
        return False

    def _pintar_franja(self, app: HomebrewApp, texto: str) -> None:
        """La franja de arriba, para lo que se instala desde una carpeta de
        esta PC (no tiene tarjeta). Después de Cancelar sigue diciendo
        "Cancelando…" hasta que termina."""
        state = self._install_states.get(app.slug)
        if state is not None and state.cancel_event is not None and state.cancel_event.is_set():
            return
        self.local_banner.set_title(texto)
        self.local_banner.set_revealed(True)

    def _on_cancelar_local(self, *_a):
        app = self._app_local
        state = self._install_states.get(app.slug) if app is not None else None
        if state is None or state.cancel_event is None:
            return
        # Lo que queda lo dice el hilo de instalación al notar el
        # `cancel_event`, como con la tarjeta (`_on_card_action_clicked`).
        state.cancel_event.set()
        self.local_banner.set_title(_("Cancelando «{name}»…").format(name=app.name))
        self.local_banner.set_button_label("")

    # ------------------------------------------------------------- Cierre --
    def shutdown(self):
        """Pide que se corte cualquier instalación en curso al cerrar la
        ventana, igual que `TransferView.shutdown` con la cola de
        transferencias. Los hilos son daemon (no cuelgan el proceso), pero
        sin esto seguirían escribiendo en la unidad un instante después de
        que la ventana ya se cerró."""
        for state in self._install_states.values():
            if state.cancel_event is not None:
                state.cancel_event.set()
