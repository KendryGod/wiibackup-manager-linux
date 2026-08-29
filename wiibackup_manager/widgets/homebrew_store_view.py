"""Página 'Homebrew Store': catálogo de Open Shop Channel en tarjetas,
con descarga e instalación a /apps/ del destino elegido.

Sigue el mismo reparto de responsabilidades que el resto de la app: esta
vista dibuja y reacciona a clicks; quien de verdad sabe listar, descargar y
verificar es `oscwii_client`/`oscwii_installer` (Pasos 1 y 2), y todo lo que
tarda -bajar el catálogo, bajar un ZIP, extraerlo- corre en un hilo de
fondo y vuelve al hilo de GTK con `GLib.idle_add`, igual que
`queue_manager.TransferQueue` para las transferencias."""
from __future__ import annotations

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

from .. import drives, golden_configs, oscwii_client, oscwii_installer
from ..i18n import _
from ..oscwii_client import HomebrewApp
from ..oscwii_installer import InstallStatus
from ..operations import OperationBusy, OperationKind
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


# Motivo legible por `InstallStatus`, para el mensaje corto que se ve en la
# tarjeta (el detalle completo de `oscwii_installer` va en el toast).
_INSTALL_STATUS_LABELS = {
    InstallStatus.DOWNLOAD_ERROR: _("Error de descarga"),
    InstallStatus.HASH_MISMATCH: _("El archivo no coincide con lo esperado"),
    InstallStatus.BAD_ZIP: _("El archivo descargado está dañado"),
    InstallStatus.UNSAFE_ZIP: _("El paquete no es seguro para instalar"),
    InstallStatus.UNSAFE_DEST_ROOT: _("El destino elegido no es válido"),
    InstallStatus.NO_SPACE: _("No hay espacio suficiente en el destino"),
    InstallStatus.IO_ERROR: _("Error al escribir en el destino"),
}


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

        self._build_ui()
        self._refresh_dest_choices()
        self._load_apps()
        GLib.timeout_add_seconds(3, self._poll_dest_choices)

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        self.stale_banner = Adw.Banner(revealed=False)
        self.append(self.stale_banner)

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
        self.append(dest_buttons)

        self.append(Gtk.Separator())

        # --- Catálogo: cargando / error / grilla ---
        self.state_stack = Gtk.Stack()
        self.state_stack.set_vexpand(True)
        self.append(self.state_stack)

        loading_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                              vexpand=True)
        spinner = Adw.Spinner()
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
        self._manual_path = Path(folder.get_path())
        self._refresh_dest_choices()

    # ----------------------------------------------------------- Catálogo --
    def _load_apps(self):
        self.state_stack.set_visible_child_name("loading")
        oscwii_client.fetch_apps_async(
            on_done=lambda result: GLib.idle_add(self._on_apps_loaded, result))

    def _on_apps_loaded(self, result) -> bool:
        if not gtk_helpers.widget_is_alive(self):
            return False

        if result.status is oscwii_client.FetchStatus.ERROR:
            self.error_status.set_description(
                result.error or _("motivo desconocido"))
            self.state_stack.set_visible_child_name("error")
            return False

        self._apps_store.remove_all()
        self._card_by_slug.clear()
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
        card.name_label.set_label(GLib.markup_escape_text(app.name))
        card.desc_label.set_label(app.short_description)
        card.desc_label.set_visible(bool(app.short_description))
        meta = [p for p in (app.category, f"v{app.version}" if app.version else "") if p]
        card.meta_label.set_label(" · ".join(meta))
        card.meta_label.set_visible(bool(meta))

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

    def _start_install(self, app: HomebrewApp):
        if self._dest_path is None:
            self._show_toast(_("Elegí primero un destino antes de instalar."))
            return
        dest_root = self._dest_path

        try:
            op = self.ops.start(
                OperationKind.INSTALLING_HOMEBREW,
                write=[dest_root / "apps" / app.slug],
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

        def on_progress(progress):
            GLib.idle_add(self._on_install_progress, app, progress)

        def worker():
            result = oscwii_installer.install_app(
                app, dest_root, cancel_event=cancel_event, on_progress=on_progress)
            # Todavía en el hilo de fondo, a propósito: si `app.slug` no
            # tiene ninguna config maestra registrada (la inmensa mayoría
            # del catálogo), `maybe_apply` vuelve en el acto sin tocar
            # disco ni log. Cuando sí aplica, es una copia chica (un
            # archivo, no un ZIP entero) y no vale la pena otro salto de
            # hilo solo para eso.
            golden_result = golden_configs.maybe_apply(
                app, dest_root, result, op_log=self.op_log)
            GLib.idle_add(self._on_install_done, app, result, op, golden_result)

        threading.Thread(target=worker, daemon=True,
                         name=f"oscwii-install-{app.slug}").start()

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
            state.message = (_("Descargando… {pct}%").format(pct=pct)
                             if progress.phase == "download"
                             else _("Extrayendo… {pct}%").format(pct=pct))
        else:
            state.message = (_("Descargando…") if progress.phase == "download"
                             else _("Extrayendo…"))

        card = self._card_by_slug.get(app.slug)
        if card is not None and gtk_helpers.widget_is_alive(card) and card.app is app:
            self._apply_install_state(card, state)
        return False

    def _on_install_done(self, app: HomebrewApp, result, op, golden_result=None) -> bool:
        self.ops.finish(op)

        if result.ok:
            state = _InstallState(kind=_CardState.DONE, fraction=1.0,
                                  message=_("Instalada ✓"))
            self._show_toast(
                _("'{name}' instalada ({n} archivo(s)).")
                .format(name=app.name, n=len(result.installed_paths)))
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
        return False

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
