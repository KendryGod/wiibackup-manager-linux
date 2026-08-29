from __future__ import annotations

import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gtk, GLib, Gio, Gdk  # noqa: E402

from . import __version__, config, drives, library, operations, oplog, styles, trash, wit_wrapper
from .disc_header import UNKNOWN_GAME_ID
from .i18n import _, ngettext
from .operations import OperationBusy, OperationKind, OperationOutcome


class BatchSkip(Exception):
    """Una acción de `_run_batch` levanta esto para saltar un juego a
    propósito (ni error ni éxito), p. ej. porque ya existe el destino."""


# (etiqueta, función de orden, invertir) para el desplegable de orden de la
# Biblioteca. La fecha de agregado usa st_ctime (no st_mtime): _start_import
# copia con shutil.copy2, que preserva la fecha de modificación original del
# archivo fuente, así que mtime no sirve para saber cuándo se agregó acá;
# ctime sí, porque el sistema de archivos la fija solo al crear/copiar el
# archivo en destino y no se puede heredar del origen.
def _game_ctime(game: "Game") -> float:
    try:
        return game.path.stat().st_ctime
    except OSError:
        return 0.0


SORT_OPTIONS = [
    (_("Título (A-Z)"), lambda g: g.title.lower(), False),
    (_("Tamaño (mayor a menor)"), lambda g: g.size_bytes, True),
    (_("Tamaño (menor a mayor)"), lambda g: g.size_bytes, False),
    (_("Fecha de agregado (más nuevo primero)"), _game_ctime, True),
    (_("Formato (A-Z)"), lambda g: (g.fmt, g.title.lower()), False),
]
from .library import Game
from .widgets.game_detail_dialog import GameDetailDialog
from .widgets.game_row import GameRow
from .widgets.homebrew_store_view import HomebrewStoreView
from .widgets.log_view import LogView
from .widgets import gtk_helpers
from .widgets.preferences_dialog import PreferencesDialog
from .widgets.transfer_view import TransferView

# Cuántas variantes de nombre se prueban cuando el destino planificado
# aparece ocupado recién a la hora de copiar. Es una carrera con un
# proceso externo: si alguien se queda con cien nombres seguidos mientras
# copiamos, no es una colisión sino algo raro, y conviene informar el
# error en vez de seguir probando para siempre.
_MAX_COLISIONES_IMPORT = 100


class WiiBackupWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app)
        self.set_title("WiiBackup Manager")
        self.set_default_size(820, 620)

        self.settings = config.Settings.load()
        config.ensure_dirs(self.settings)
        styles.apply_color_scheme(self.settings.color_scheme)

        self._games: list[Game] = []
        self._rows: dict[str, GameRow] = {}
        self._library_available = config.library_path_available(self.settings)
        # Ver TransferView: el token guarda el proceso de `wit` en curso
        # para poder matarlo al cancelar, no solo una bandera.
        self._cancel_token = wit_wrapper.CancellationToken()
        # Qué se avisa al tocar "Cancelar": lo pone la operación que está
        # usando la barra en ese momento (enviar o convertir).
        self._cancel_message = "Cancelando…"

        # Historial persistente de operaciones (pestaña Log). Se le pasa al
        # OperationManager para que cada operación que termina informando
        # su resultado quede registrada sin que cada worker tenga que
        # acordarse de hacerlo. Ver oplog.py.
        self.op_log = oplog.OperationLog()

        # Coordinador de operaciones largas, compartido con TransferView:
        # es el que impide, por ejemplo, borrar un juego que se está
        # convirtiendo, o dos escaneos pisándose. Ver operations.py.
        self.ops = operations.OperationManager(log=self.op_log)
        self.ops.add_listener(
            lambda: GLib.idle_add(self._update_operation_ui)
        )

        # Mientras se restauran las casillas de una lista recién
        # reconstruida (ver `_on_sort_changed`) cada fila emite su señal, y
        # recalcular la barra de selección en cada una es trabajo al pedo:
        # se hace una sola vez al final.
        self._suspend_selection_updates = False

        # Generación del escaneo: cada `rescan_library()` la incrementa y el
        # resultado que llega con una generación vieja se descarta, para que
        # un escaneo lento no pise con datos viejos a uno más reciente.
        self._scan_generation = 0
        # Un rescan pedido mientras ya había uno corriendo no se pierde: se
        # anota acá y se dispara cuando el actual termina.
        self._rescan_pending = False
        # Carpetas que el último escaneo no pudo leer, para no repetir el
        # aviso en cada rescan automático. Ver `_on_scan_done`.
        self._skipped_dirs: set[str] = set()

        self._build_ui()

        if self.settings.auto_scan_on_start:
            GLib.idle_add(self.rescan_library)

        GLib.timeout_add_seconds(3, self._poll_library_availability)

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        """Arma la ventana: un `Adw.NavigationSplitView` con 4 destinos en
        el sidebar (Juegos, Cola de Tareas, Modo Fábrica, Ajustes), cada
        uno con su propio `Adw.ToolbarView`/header, intercambiados dentro
        de `self._content_stack`.

        `_build_juegos_page` es, adentro, EXACTAMENTE el contenido que
        tenía la única ventana de antes (mismos widgets, mismos nombres de
        atributo): el resto de la clase -escaneo, lotes, importar, etc.-
        no sabe ni le importa que ahora vive dentro de una página del
        sidebar en vez de ser el contenido entero de la ventana."""
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        self.split_view = Adw.NavigationSplitView()
        self._toast_overlay.set_child(self.split_view)

        self._content_stack = Gtk.Stack()
        self._content_nav_page = Adw.NavigationPage(
            title=_("Juegos"), child=self._content_stack)
        self.split_view.set_content(self._content_nav_page)

        self._add_action("preferences", self._on_preferences)
        self._add_action("about", self._on_about)
        self._add_action("add-files", self._on_add_files)
        self._add_action("add-folder", self._on_add_folder)
        self._add_action("rename-all", self._on_rename_all)
        self._add_action("export-csv", lambda: self._on_export(library.EXPORT_CSV))
        self._add_action("export-text", lambda: self._on_export(library.EXPORT_TEXT))

        # Las páginas se construyen ANTES que el sidebar: seleccionar la
        # primera fila del sidebar dispara `_on_sidebar_row_selected`, que
        # necesita que "juegos" ya exista dentro de `self._content_stack`.
        self._build_juegos_page()
        self._build_cola_page()
        self._build_modo_fabrica_page()
        self._build_homebrew_page()
        self._build_ajustes_page()
        self._build_sidebar()

        # Cerrar la ventana corta la cola de transferencias. El hilo de la
        # cola es daemon, así que el proceso terminaría igual, pero
        # terminaría con un `wit` a mitad de una escritura sobre el
        # pendrive: pedirle que pare (y que mate a `wit`) antes de irse
        # deja la unidad en un estado predecible.
        self.connect("close-request", self._on_close_request)

    # ------------------------------------------------------------ Sidebar --
    def _build_sidebar(self):
        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_title_widget(Adw.WindowTitle(title="WiiBackup Manager"))
        # Los botones de la ventana (cerrar/min/max) quedan solo en el
        # header de la página de contenido, para no duplicarlos: acá y
        # allá se verían dos juegos de controles a la vez.
        sidebar_header.set_show_end_title_buttons(False)
        sidebar_toolbar.add_top_bar(sidebar_header)

        # (id de página, ícono simbólico, etiqueta). Un solo lugar: de acá
        # sale tanto la fila del sidebar como el título que se le pone a
        # `self._content_nav_page` al elegirla.
        self._sidebar_items = [
            ("juegos", "applications-games-symbolic", _("Juegos")),
            ("cola", "emblem-synchronizing-symbolic", _("Transferir")),
            ("fabrica", "drive-removable-media-symbolic", _("Modo Fábrica")),
            ("tienda", "system-software-install-symbolic", _("Homebrew Store")),
            ("ajustes", "emblem-system-symbolic", _("Ajustes")),
        ]

        self._sidebar_list = Gtk.ListBox()
        # Clase de GTK/libadwaita hecha justo para esto: el mismo look que
        # el sidebar de Archivos o Configuración de GNOME.
        self._sidebar_list.add_css_class("navigation-sidebar")
        self._sidebar_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        for page_id, icon_name, label in self._sidebar_items:
            row = Gtk.ListBoxRow()
            row.page_id = page_id
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                          margin_start=10, margin_end=10, margin_top=10,
                          margin_bottom=10)
            box.append(Gtk.Image.new_from_icon_name(icon_name))
            row_label = Gtk.Label(label=label, xalign=0)
            row_label.set_hexpand(True)
            box.append(row_label)
            row.set_child(box)
            self._sidebar_list.append(row)
        self._sidebar_list.connect("row-selected", self._on_sidebar_row_selected)

        sidebar_toolbar.set_content(self._sidebar_list)
        sidebar_page = Adw.NavigationPage(title="WiiBackup Manager",
                                          child=sidebar_toolbar)
        self.split_view.set_sidebar(sidebar_page)

        self._sidebar_list.select_row(self._sidebar_list.get_row_at_index(0))

    def _on_sidebar_row_selected(self, _listbox, row):
        if row is None:
            return
        self._content_stack.set_visible_child_name(row.page_id)
        label = next(lbl for pid, _icon, lbl in self._sidebar_items
                     if pid == row.page_id)
        self._content_nav_page.set_title(label)
        # En pantallas angostas el split view colapsa a una sola columna
        # (sidebar O contenido): elegir un destino tiene que llevar al
        # contenido, si no la fila se ve "seleccionada" pero la pantalla
        # sigue mostrando el sidebar.
        if self.split_view.get_collapsed():
            self.split_view.set_show_content(True)

    # -------------------------------------------------------- Página: Juegos --
    def _build_juegos_page(self):
        toolbar_view = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_("Juegos")))
        toolbar_view.add_top_bar(header)

        self._add_button = add_button = Gtk.MenuButton(icon_name="list-add-symbolic")
        add_button.set_tooltip_text(_("Agregar juegos (ISO/WBFS)"))
        add_menu = Gio.Menu()
        add_menu.append(_("Agregar archivos"), "win.add-files")
        add_menu.append(_("Agregar carpeta completa"), "win.add-folder")
        add_button.set_menu_model(add_menu)
        header.pack_start(add_button)

        self._refresh_button = refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.set_tooltip_text(_("Volver a escanear la biblioteca"))
        refresh_button.connect("clicked", lambda *_a: self.rescan_library())
        header.pack_start(refresh_button)

        self.select_toggle = Gtk.ToggleButton(icon_name="object-select-symbolic")
        self.select_toggle.set_tooltip_text(_("Selección múltiple"))
        self.select_toggle.connect("toggled", self._on_select_mode_toggled)
        header.pack_start(self.select_toggle)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu = Gio.Menu()
        tools_section = Gio.Menu()
        tools_section.append(_("Renombrar todo a estándar…"), "win.rename-all")
        menu.append_section(None, tools_section)
        export_section = Gio.Menu()
        export_section.append(_("Exportar lista a CSV…"), "win.export-csv")
        export_section.append(_("Exportar lista como texto…"), "win.export-text")
        menu.append_section(None, export_section)
        menu.append(_("Preferencias"), "win.preferences")
        menu.append(_("Acerca de"), "win.about")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        # Barra de búsqueda
        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Buscar por título o ID…"))
        self.search_entry.connect("search-changed", self._on_search_changed)
        search_bar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, margin_start=12,
                                  margin_end=12, margin_top=8, margin_bottom=8)
        search_bar_box.append(self.search_entry)
        self.search_entry.set_hexpand(True)

        self.sort_dropdown = Gtk.DropDown.new_from_strings(
            [label for label, _fn, _rev in SORT_OPTIONS]
        )
        self.sort_dropdown.set_tooltip_text(_("Ordenar por"))
        self.sort_dropdown.connect("notify::selected", self._on_sort_changed)
        search_bar_box.append(self.sort_dropdown)

        self.progress_bar = Gtk.ProgressBar(visible=False, show_text=True, hexpand=True)
        self.progress_cancel_btn = Gtk.Button(label=_("Cancelar"), visible=False)
        self.progress_cancel_btn.connect("clicked", self._on_progress_cancel_clicked)

        progress_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                                margin_start=12, margin_end=12)
        progress_box.append(self.progress_bar)
        progress_box.append(self.progress_cancel_btn)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.append(search_bar_box)
        content_box.append(progress_box)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.add_css_class("boxed-list")
        self.list_box.set_filter_func(self._filter_row)
        # El orden lo resuelve el propio ListBox reacomodando las filas que
        # ya existen: cambiar el criterio no reconstruye nada. Ver
        # `_on_sort_changed`.
        self.list_box.set_sort_func(self._sort_rows)

        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self.list_box)
        scroller.set_vexpand(True)
        scroller.set_margin_start(12)
        scroller.set_margin_end(12)
        scroller.set_margin_bottom(12)

        self.status_page = Adw.StatusPage(
            title=_("Sin juegos todavía"),
            description=_("Agregá tus ISO/WBFS o elegí una carpeta de biblioteca "
                          "en Preferencias."),
            icon_name="drive-harddisk-symbolic",
        )

        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, "list")
        self.stack.add_named(self.status_page, "empty")
        content_box.append(self.stack)
        self.stack.set_vexpand(True)

        self.library_status_label = Gtk.Label(xalign=0)
        self.library_status_label.add_css_class("dim-label")
        self.library_status_label.set_margin_start(12)
        self.library_status_label.set_margin_end(12)
        self.library_status_label.set_margin_top(4)
        self.library_status_label.set_margin_bottom(8)
        content_box.append(self.library_status_label)
        self._update_library_status_bar()

        # Arrastrar y soltar archivos/carpetas desde el gestor de archivos
        # del sistema directo sobre la Biblioteca (lista o estado vacío).
        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect("drop", self._on_files_dropped)
        self.stack.add_controller(drop_target)

        toolbar_view.set_content(content_box)

        if not wit_wrapper.is_available(self.settings.wit_binary):
            banner = Adw.Banner(
                title=_("No se encontró 'wit' (Wiimms ISO Tools): la conversión y "
                        "los WBFS multi-juego estarán limitados. Ver README para "
                        "instalarlo."),
                revealed=True,
            )
            toolbar_view.add_top_bar(banner)

        self._library_banner = Adw.Banner(button_label=_("Reintentar"))
        self._library_banner.connect("button-clicked", lambda *_a: self.rescan_library())
        toolbar_view.add_top_bar(self._library_banner)
        self._update_library_banner()

        # Barra de acciones en lote: aparece al activar el modo selección.
        self._selection_bar = Gtk.ActionBar()
        self._sel_count_label = Gtk.Label(label=_("0 seleccionados"))
        self._selection_bar.pack_start(self._sel_count_label)
        self._selection_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self._batch_send_btn = Gtk.Button(label=_("Enviar a unidad WBFS"))
        self._batch_send_btn.connect("clicked", lambda *_a: self._on_batch_send())
        self._selection_bar.pack_start(self._batch_send_btn)

        self._batch_convert_btn = Gtk.Button(label=_("Convertir"))
        self._batch_convert_btn.connect("clicked", lambda *_a: self._on_batch_convert())
        self._selection_bar.pack_start(self._batch_convert_btn)

        self._batch_rename_btn = Gtk.Button(label=_("Renombrar"))
        self._batch_rename_btn.set_tooltip_text(
            _("Renombrar los archivos elegidos a 'Título [ID].ext'")
        )
        self._batch_rename_btn.connect("clicked", lambda *_a: self._on_batch_rename())
        self._selection_bar.pack_start(self._batch_rename_btn)

        self._batch_verify_btn = Gtk.Button(label=_("Verificar"))
        self._batch_verify_btn.connect("clicked", lambda *_a: self._on_batch_verify())
        self._selection_bar.pack_start(self._batch_verify_btn)

        self._batch_delete_btn = Gtk.Button(label=_("Eliminar"))
        self._batch_delete_btn.add_css_class("destructive-action")
        self._batch_delete_btn.connect("clicked", lambda *_a: self._on_batch_delete())
        self._selection_bar.pack_start(self._batch_delete_btn)

        cancel_selection_btn = Gtk.Button(icon_name="window-close-symbolic")
        cancel_selection_btn.set_tooltip_text(_("Cancelar selección"))
        cancel_selection_btn.connect("clicked", lambda *_a: self.select_toggle.set_active(False))
        self._selection_bar.pack_end(cancel_selection_btn)

        self._selection_bar.set_revealed(False)
        toolbar_view.add_bottom_bar(self._selection_bar)
        self._update_selection_bar()

        self._content_stack.add_named(toolbar_view, "juegos")

    # --------------------------------------------------- Página: Cola de Tareas --
    def _build_cola_page(self):
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_("Transferir")))
        toolbar_view.add_top_bar(header)

        # `TransferView` es el mismo widget que antes vivía en la pestaña
        # "Transferir": nada de su lógica cambió, solo dónde se lo monta y
        # cómo se llama la fila del sidebar que lleva hasta acá.
        self.transfer_view = TransferView(self.settings, self._show_toast, self.ops)
        toolbar_view.set_content(self.transfer_view)

        self._content_stack.add_named(toolbar_view, "cola")

    # ----------------------------------------------------- Página: Modo Fábrica --
    def _build_modo_fabrica_page(self):
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_("Modo Fábrica")))
        toolbar_view.add_top_bar(header)

        warning = Adw.Banner(
            title=_("Formatear borra TODO el contenido del disco elegido, sin "
                    "posibilidad de recuperarlo."),
            revealed=True,
        )
        toolbar_view.add_top_bar(warning)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        group = Adw.PreferencesGroup(
            title=_("Preparar unidad"),
            description=_(
                "Formatea un USB o SD como FAT32 (32 KB por clúster) y arma "
                "la estructura de carpetas que esperan USB Loader GX y "
                "Nintendont: apps, games y wbfs. Solo se muestran discos que "
                "el sistema marca como removibles -nunca un disco interno."),
        )
        group.set_margin_start(12)
        group.set_margin_end(12)
        group.set_margin_top(12)

        # BLINDAJE 1 en la interfaz: el modelo del desplegable se llena
        # SOLO con lo que devuelve `drives.list_candidate_drives()`, que ya
        # filtró por removable=1 (ver drives.py). Nunca se agrega nada acá
        # "a mano" a partir de otra fuente.
        self._factory_model = Gtk.StringList.new([])
        self._factory_row = Adw.ComboRow(title=_("Unidad"))
        self._factory_row.set_model(self._factory_model)
        self._factory_row.connect("notify::selected",
                                  self._on_factory_selection_changed)
        group.add(self._factory_row)
        content.append(group)

        buttons_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                              margin_start=12, margin_end=12, margin_top=8)
        self._factory_refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        self._factory_refresh_btn.set_tooltip_text(_("Volver a buscar unidades removibles"))
        self._factory_refresh_btn.connect("clicked",
                                          lambda *_a: self._refresh_factory_drives())
        buttons_box.append(self._factory_refresh_btn)

        self._factory_prepare_btn = Gtk.Button(label=_("Preparar unidad…"))
        self._factory_prepare_btn.add_css_class("destructive-action")
        self._factory_prepare_btn.set_sensitive(False)
        self._factory_prepare_btn.connect("clicked", self._on_factory_prepare_clicked)
        buttons_box.append(self._factory_prepare_btn)
        content.append(buttons_box)

        self._factory_empty_label = Gtk.Label(
            label=_("No se detectó ninguna unidad removible. Conectá un USB "
                    "o SD y tocá el botón de actualizar."),
            wrap=True, xalign=0)
        self._factory_empty_label.add_css_class("dim-label")
        self._factory_empty_label.set_margin_start(12)
        self._factory_empty_label.set_margin_end(12)
        self._factory_empty_label.set_margin_top(8)
        self._factory_empty_label.set_visible(False)
        content.append(self._factory_empty_label)

        self._factory_progress = Gtk.ProgressBar(visible=False, show_text=True)
        self._factory_progress.set_margin_start(12)
        self._factory_progress.set_margin_end(12)
        self._factory_progress.set_margin_top(12)
        content.append(self._factory_progress)

        toolbar_view.set_content(content)
        self._content_stack.add_named(toolbar_view, "fabrica")

        self._factory_drives: list[drives.BlockDevice] = []
        self._factory_busy = False
        self._factory_pulse_id = None
        self._refresh_factory_drives()

    def _refresh_factory_drives(self):
        """BLINDAJE 1: repuebla el desplegable solo con lo que el kernel
        marca removable=1 -ver `drives.list_candidate_drives`. Se llama al
        construir la página, al tocar el botón de actualizar y después de
        cada intento de formateo (la unidad puede haber cambiado de
        nombre, o el usuario puede haberla desconectado)."""
        self._factory_drives = drives.list_candidate_drives()
        while self._factory_model.get_n_items():
            self._factory_model.remove(0)
        for device in self._factory_drives:
            self._factory_model.append(device.display_name)

        hay_candidatos = bool(self._factory_drives)
        self._factory_empty_label.set_visible(not hay_candidatos)
        self._factory_row.set_visible(hay_candidatos)
        if hay_candidatos:
            self._factory_row.set_selected(0)
        self._update_factory_prepare_sensitivity()

    def _selected_factory_drive(self) -> "drives.BlockDevice | None":
        idx = self._factory_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._factory_drives):
            return None
        return self._factory_drives[idx]

    def _on_factory_selection_changed(self, *_a):
        self._update_factory_prepare_sensitivity()

    def _update_factory_prepare_sensitivity(self):
        self._factory_prepare_btn.set_sensitive(
            not self._factory_busy and self._selected_factory_drive() is not None)

    def _on_factory_prepare_clicked(self, *_a):
        device = self._selected_factory_drive()
        if device is None:
            return

        # BLINDAJE 2: confirmación informada (modelo + tamaño + ruta del
        # dispositivo, tal como se ve en este instante) y el usuario tiene
        # que escribir "FORMATEAR" a mano para habilitar el botón
        # destructivo. Nada de esto reemplaza a los blindajes 3 y 4, que
        # se vuelven a correr en `drives.format_as_wii_usb` pase lo que
        # pase acá.
        dialog = Adw.AlertDialog(
            heading=_("¿Formatear esta unidad?"),
            body=_("Vas a formatear:\n{drive}\n\nSe borra TODO su contenido "
                   "actual, sin posibilidad de recuperarlo. Para confirmar, "
                   "escribí FORMATEAR abajo.").format(drive=device.display_name),
        )
        entry = Gtk.Entry(placeholder_text="FORMATEAR")
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancelar"))
        dialog.add_response("format", _("Formatear"))
        dialog.set_response_appearance("format", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_response_enabled("format", False)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        entry.connect(
            "changed",
            lambda e: dialog.set_response_enabled("format",
                                                   e.get_text().strip() == "FORMATEAR"))
        dialog.connect("response", self._on_factory_confirm_response, device)
        dialog.present(self)

    def _on_factory_confirm_response(self, _dialog, response, device):
        if response != "format":
            return
        self._start_factory_format(device)

    def _start_factory_format(self, device: "drives.BlockDevice"):
        """Corre `drives.format_as_wii_usb` (blindajes 3 y 4 + mkfs +
        estructura de carpetas) en un hilo de fondo, para no congelar la
        interfaz mientras `pkexec` pide contraseña y `mkfs.vfat` escribe.
        El resultado vuelve al hilo de GTK con `GLib.idle_add`, igual que
        el resto de las operaciones largas de la ventana.

        Antes de arrancar nada, declara el disco entero (`device.path`,
        no un punto de montaje: `format_as_wii_usb` lo desmonta) como
        recurso ocupado ante el `OperationManager`. Es lo que impide
        formatear una unidad mientras Transferencias o la instalación de
        Homebrew le están escribiendo algo encima -y viceversa: si el
        disco ya está ocupado, `ops.start` levanta `OperationBusy` acá y
        ni siquiera se llega a mostrar la barra de progreso."""
        try:
            op = self.ops.start(OperationKind.FORMATTING,
                                resources=[device.path])
        except OperationBusy as e:
            self._show_toast(
                _("No se puede formatear ahora: {detail}.").format(detail=e.detail))
            return

        self._factory_busy = True
        self._factory_prepare_btn.set_sensitive(False)
        self._factory_refresh_btn.set_sensitive(False)
        self._factory_row.set_sensitive(False)
        self._factory_progress.set_visible(True)
        self._factory_progress.set_text(_("Formateando…"))
        self._factory_progress.pulse()
        self._factory_pulse_id = GLib.timeout_add(150, self._pulse_factory_progress)

        def worker():
            try:
                punto_montaje = drives.format_as_wii_usb(device)
            except Exception as e:  # noqa: BLE001
                GLib.idle_add(self._on_factory_format_done, False, str(e))
            else:
                GLib.idle_add(self._on_factory_format_done, True, str(punto_montaje))
            finally:
                self.ops.finish(op)

        threading.Thread(target=worker, daemon=True, name="factory-format").start()

    def _pulse_factory_progress(self):
        if not self._factory_busy:
            return False
        self._factory_progress.pulse()
        return True

    def _on_factory_format_done(self, ok: bool, detail: str):
        self._factory_busy = False
        if self._factory_pulse_id is not None:
            GLib.source_remove(self._factory_pulse_id)
            self._factory_pulse_id = None
        self._factory_progress.set_visible(False)
        self._factory_refresh_btn.set_sensitive(True)
        self._factory_row.set_sensitive(True)
        if ok:
            self._show_toast(_("Unidad preparada en {path}.").format(path=detail))
        else:
            self._show_toast(
                _("No se pudo preparar la unidad: {error}").format(error=detail))
        # Vuelve a escanear /sys/block: la unidad recién formateada puede
        # haber cambiado de nombre de montaje, y si el formateo falló por
        # un blindaje (ej. dejó de ser removible) tampoco tiene sentido
        # dejarla todavía seleccionada como si nada.
        self._refresh_factory_drives()
        return False

    # -------------------------------------------------- Página: Homebrew Store --
    def _build_homebrew_page(self):
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_("Homebrew Store")))
        toolbar_view.add_top_bar(header)

        # Mismo patrón que `TransferView`/`_build_cola_page`: toda la
        # lógica (catálogo de OSC, descarga, verificación e instalación)
        # vive en el widget, no acá.
        self.homebrew_view = HomebrewStoreView(self.settings, self._show_toast,
                                               self.ops, self.op_log)
        toolbar_view.set_content(self.homebrew_view)

        self._content_stack.add_named(toolbar_view, "tienda")

    # ----------------------------------------------------------- Página: Ajustes --
    def _build_ajustes_page(self):
        toolbar_view = Adw.ToolbarView()

        self._ajustes_stack = Adw.ViewStack()
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.ViewSwitcher(stack=self._ajustes_stack,
                             policy=Adw.ViewSwitcherPolicy.WIDE))
        toolbar_view.add_top_bar(header)

        general_page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=_("Transferencias"))
        self._scrub_switch_row = Adw.SwitchRow(
            title=_("Optimizar espacio (Scrubbing)"),
            subtitle=_(
                "Al convertir a WBFS, descarta la partición de actualización "
                "del disco (no la usan USB Loader GX ni Nintendont). Ahorra "
                "espacio en el destino; ese juego después no se puede "
                "actualizar desde el propio disco."),
            active=self.settings.scrub_update,
        )
        self._scrub_switch_row.connect("notify::active", self._on_scrub_switch_toggled)
        group.add(self._scrub_switch_row)
        general_page.add(group)
        self._ajustes_stack.add_titled_with_icon(
            general_page, "general", _("General"), "preferences-system-symbolic")

        self.log_view = LogView(self.op_log, self._show_toast)
        self._ajustes_stack.add_titled_with_icon(
            self.log_view, "log", _("Log"), "document-open-recent-symbolic")

        toolbar_view.set_content(self._ajustes_stack)
        self._content_stack.add_named(toolbar_view, "ajustes")

    def _on_scrub_switch_toggled(self, row, _pspec):
        self.settings.scrub_update = row.get_active()
        error = config.try_save(self.settings)
        if error:
            self._show_toast(
                _("No se pudo guardar la configuración: {error}. El cambio "
                  "vale para esta sesión.").format(error=error))

    def _add_action(self, name: str, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", lambda *_a: callback())
        self.add_action(action)

    # --------------------------------------------------------- Selección --
    def _on_select_mode_toggled(self, toggle_button):
        enabled = toggle_button.get_active()
        for row in self._rows.values():
            row.set_selection_mode(enabled)
        self._selection_bar.set_revealed(enabled)
        self._update_selection_bar()

    def _selected_games(self) -> list[Game]:
        """Los juegos tildados, en el orden en que se ven en la lista.

        Se recorre el ListBox y no `self._rows` porque ese diccionario está
        en orden de aparición (una fila reusada de un escaneo anterior
        queda donde estaba), mientras que lo que el usuario espera de una
        acción en lote es que vaya de arriba hacia abajo de la pantalla."""
        games = []
        row = self.list_box.get_first_child()
        while row is not None:
            if isinstance(row, GameRow) and row.is_selected():
                games.append(row.game)
            row = row.get_next_sibling()
        return games

    # (atributo del botón, tipo de operación, si escribiría esos archivos).
    # Enviar y verificar solo leen; convertir, renombrar y eliminar
    # escriben, y por eso chocan con cualquier otra cosa que toque el mismo
    # archivo.
    _BATCH_BUTTONS = (
        ("_batch_send_btn", OperationKind.TRANSFERRING, "read"),
        ("_batch_convert_btn", OperationKind.CONVERTING, "write"),
        ("_batch_rename_btn", OperationKind.RENAMING, "write"),
        ("_batch_verify_btn", OperationKind.VERIFYING, "read"),
        ("_batch_delete_btn", OperationKind.DELETING, "write"),
    )

    @staticmethod
    def _busy_tooltip(blocker) -> str:
        return _("Hay una operación en curso: {op}. Esperá a que termine.").format(
            op=blocker.label)

    def _update_selection_bar(self):
        if self._suspend_selection_updates:
            return
        games = self._selected_games()
        count = len(games)
        if count:
            total_size = library.format_size(sum(g.size_bytes for g in games))
            self._sel_count_label.set_label(
                _("{count} seleccionado(s) · {size}").format(count=count,
                                                             size=total_size))
        else:
            self._sel_count_label.set_label(_("0 seleccionado(s)"))

        # Cada botón se apaga solo si SU acción no puede arrancar ahora, no
        # porque haya cualquier cosa en curso: con una verificación suelta
        # corriendo sobre un juego, enviar otros a la unidad sigue siendo
        # perfectamente posible y antes quedaba gris igual. Los lotes van
        # con `uses_progress_bar=True` porque es como se van a registrar.
        #
        # El `is_busy()` de arranque es para no pagar el costo de resolver
        # las rutas de toda la selección en el caso normal (nada en curso),
        # que es el que se recalcula en cada click de una casilla.
        if count and self.ops.is_busy():
            blockers = self.ops.conflicts_for(
                [(attr, kind, role) for attr, kind, role in self._BATCH_BUTTONS],
                read=[g.path for g in games], uses_progress_bar=True)
        else:
            blockers = {}
        for attr, kind, _role in self._BATCH_BUTTONS:
            btn = getattr(self, attr)
            blocker = blockers.get(attr)
            btn.set_sensitive(count > 0 and blocker is None)
            btn.set_tooltip_text(self._busy_tooltip(blocker) if blocker else None)

    def _update_operation_ui(self):
        """Refleja en la interfaz si hay algo en curso. Lo llama el listener
        del OperationManager (reenviado al hilo de GTK) cada vez que una
        operación arranca o termina.

        Igual que en la barra de selección, cada botón mira si lo suyo
        puede arrancar: escanear se apaga cuando algo está escribiendo en
        la biblioteca, y agregar juegos cuando hay otra operación ocupando
        la barra de progreso. Una verificación suelta no apaga ninguno de
        los dos."""
        biblioteca = Path(self.settings.library_path)
        for btn, kind, recursos, idle_tooltip in (
                (self._refresh_button, OperationKind.SCANNING, [biblioteca],
                 _("Volver a escanear la biblioteca")),
                (self._add_button, OperationKind.IMPORTING, [],
                 _("Agregar juegos (ISO/WBFS)"))):
            blocker = self.ops.conflict_for(kind, resources=recursos)
            btn.set_sensitive(blocker is None)
            btn.set_tooltip_text(idle_tooltip if blocker is None
                                 else self._busy_tooltip(blocker))
        self._update_selection_bar()
        return False

    @staticmethod
    def _describe_target(games: list[Game]) -> str:
        """Sobre qué se operó, para el historial: el título del juego si es
        uno solo, o cuántos si es un lote (poner los 30 títulos de un lote
        haría ilegible la entrada)."""
        if len(games) == 1:
            return games[0].title
        return _("{n} juegos").format(n=len(games))

    @staticmethod
    def _batch_outcome(target: str, ok: int, errors: list[str],
                        skipped: list[str] | None = None,
                        cancelled: bool = False,
                        notes: list[str] | None = None,
                        summary_note: str = "") -> OperationOutcome:
        """Traduce el recuento de un lote al resultado que va al historial.

        Cancelado gana sobre todo lo demás (lo pidió el usuario, no es un
        fallo); después, un lote donde algo salió bien y algo no es
        "parcial" y no "error", que es la diferencia entre "no se copió
        nada" y "se copiaron 18 de 20"."""
        skipped = skipped or []
        # `notes` son cosas que salieron bien pero no exactamente como se
        # pidió (un archivo renombrado con sufijo porque el nombre estaba
        # ocupado). Van al MISMO resumen y no a entradas sueltas: una
        # acción del usuario, una entrada de historial.
        notes = notes or []
        # `summary_note` describe CÓMO se hizo el lote entero, no una
        # desviación: "fueron a la papelera". Va al detalle pero no toca el
        # estado, al revés que `notes`. Sin esa distinción, borrar juegos de
        # una unidad sin papelera -que es exactamente lo que el usuario
        # confirmó- quedaba anotado como "Terminada con errores".
        detail_parts = [f"{ok} ok"]
        if summary_note:
            detail_parts.append(summary_note)
        if skipped:
            detail_parts.append(f"{len(skipped)} omitido(s)")
        if notes:
            detail_parts.append("; ".join(notes[:3]))
            if len(notes) > 3:
                detail_parts.append(f"(+{len(notes) - 3} más)")
        if errors:
            detail_parts.append(f"{len(errors)} con error")
            # Los primeros motivos concretos: son el dato por el que
            # alguien abre el historial después de que algo falle.
            detail_parts.append("; ".join(errors[:3]))

        if cancelled:
            status = oplog.STATUS_CANCELLED
        elif errors:
            status = oplog.STATUS_PARTIAL if ok else oplog.STATUS_ERROR
        elif notes:
            # Se hizo todo, pero no todo salió como se pidió.
            status = oplog.STATUS_PARTIAL
        else:
            status = oplog.STATUS_OK
        return OperationOutcome(status=status, target=target,
                                 detail=" · ".join(detail_parts))

    def _reject_if_busy(self, kind: OperationKind, read=(), write=(),
                         resources=(), uses_progress_bar: bool = False) -> bool:
        """True (y avisa al usuario) si la acción pedida choca con algo en
        curso. Se usa en los flujos que arrancan desde el menú de una fila,
        donde no hay un botón que deshabilitar.

        `uses_progress_bar` tiene que coincidir con el `start` posterior:
        los lotes de verificar/eliminar lo pasan en True porque muestran
        progreso, las mismas acciones sobre un juego suelto no."""
        try:
            self.ops.check(kind, read=read, write=write, resources=resources,
                            uses_progress_bar=uses_progress_bar)
        except OperationBusy as e:
            self._show_toast(_("No se puede ahora: {detail}.").format(detail=e.detail))
            return True
        return False

    def _on_progress_cancel_clicked(self, *_args):
        # Mata el `wit` (o corta la copia) que esté corriendo ahora mismo,
        # no solo evita que arranque el próximo juego.
        self._cancel_token.cancel()
        self.progress_cancel_btn.set_sensitive(False)
        self._show_toast(self._cancel_message)

    def _begin_cancellable_progress(self, title: str, cancel_message: str):
        """Muestra la barra de progreso con botón de cancelar y devuelve el
        token de cancelación de esta operación.

        El token es nuevo por operación (no arrastra el estado de una
        cancelación anterior) y queda en `self._cancel_token`, que es lo
        que mira el botón. Las operaciones que llegan hasta acá -enviar a
        una unidad WBFS y convertir- no pueden correr a la vez (ver
        `_SHARED_PROGRESS_KINDS` en operations.py), así que no hay dos
        peleándose por el mismo botón."""
        cancel = wit_wrapper.CancellationToken()
        self._cancel_token = cancel
        self._cancel_message = cancel_message
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        self.progress_cancel_btn.set_visible(True)
        self.progress_cancel_btn.set_sensitive(True)
        self.set_title(_("WiiBackup Manager — {title}…").format(title=title))
        return cancel

    def _hide_progress(self):
        """Esconde la barra y el botón de cancelar al terminar. Se llama
        también desde operaciones que no muestran el botón: esconder algo
        que ya estaba escondido no molesta, y olvidárselo dejaría un
        'Cancelar' muerto en pantalla."""
        self.progress_bar.set_visible(False)
        self.progress_cancel_btn.set_visible(False)
        return False

    # ------------------------------------------------------ Acciones en lote --
    def _run_batch(self, games: list[Game], title: str, action_fn,
                    kind: OperationKind, cancel_message: str = "Cancelando…",
                    summary_note: str = ""):
        """Corre `action_fn(game, cancel)` para cada juego en un hilo
        aparte, mostrando progreso, botón de cancelar y un resumen final de
        éxitos/omitidos/errores. Se reusa para verificar y eliminar en lote.

        `action_fn` puede levantar `BatchSkip` para señalar que ese juego se
        salteó a propósito (no es un error ni un éxito, p. ej. porque ya
        existe el destino): se cuenta y se muestra aparte en el resumen.

        `cancel` es el token de esta corrida: el worker lo mira entre
        juegos y `action_fn` lo puede pasar hacia abajo (lo hace la
        verificación, para que matar el `wit` en curso no espere a que
        termine con el juego que está leyendo). Cancelar deja lo ya
        procesado como está y no toca lo que faltaba.

        Si `action_fn` devuelve un texto, es una NOTA sobre ese juego:
        salió bien, pero no exactamente como se pidió (por ejemplo, se
        renombró con un sufijo porque el nombre estaba ocupado). Las notas
        entran al resumen final, que es la ÚNICA entrada de historial que
        genera el lote.

        `kind` es el tipo de operación con el que se registra el lote
        entero en el OperationManager, para que nada más toque esos
        archivos mientras dure.

        Se registra con `uses_progress_bar=True`: este runner sí muestra
        progreso en la ventana, así que un lote de verificar o eliminar no
        puede solaparse con una conversión o una transferencia aunque sean
        archivos distintos. Las mismas acciones sobre un juego suelto no
        pasan por acá y no reservan la barra."""
        rutas = [g.path for g in games]
        solo_lectura = kind is OperationKind.VERIFYING
        try:
            op = self.ops.start(
                kind,
                read=rutas if solo_lectura else (),
                write=() if solo_lectura else rutas,
                uses_progress_bar=True,
            )
        except OperationBusy as e:
            self._show_toast(_("No se puede ahora: {detail}.").format(detail=e.detail))
            return

        cancel = self._begin_cancellable_progress(title, cancel_message)
        total = len(games)

        def worker():
            ok = 0
            errors: list[str] = []
            skipped: list[str] = []
            notes: list[str] = []
            cancelled = False
            for i, game in enumerate(games, start=1):
                if cancel.cancelled:
                    cancelled = True
                    break
                try:
                    nota = action_fn(game, cancel)
                    ok += 1
                    if nota:
                        notes.append(f"{game.title}: {nota}")
                except wit_wrapper.OperationCancelled:
                    # Cancelado a mitad de ESTE juego: no es un error, y lo
                    # que faltaba queda sin tocar.
                    cancelled = True
                    break
                except BatchSkip as e:
                    skipped.append(f"{game.title}: {e}" if str(e) else game.title)
                except Exception as e:
                    if cancel.cancelled:
                        # El fallo es consecuencia de haber matado el
                        # proceso al cancelar, no un error real.
                        cancelled = True
                        break
                    errors.append(f"{game.title}: {e}")
                GLib.idle_add(self.progress_bar.set_fraction, i / max(total, 1))
            GLib.idle_add(self._on_batch_done, title, ok, errors, skipped, op,
                          self._describe_target(games), cancelled, notes,
                          summary_note)

        threading.Thread(target=worker, daemon=True).start()

    def _on_batch_done(self, title: str, ok: int, errors: list[str],
                        skipped: list[str] | None = None, op=None,
                        target: str = "", cancelled: bool = False,
                        notes: list[str] | None = None,
                        summary_note: str = ""):
        # Terminar la operación ANTES del rescan de abajo: si no, el escaneo
        # chocaría con ella y quedaría postergado. El resultado que se le
        # pasa acá es lo que queda anotado en la pestaña Log.
        self.ops.finish(op, self._batch_outcome(target, ok, errors, skipped,
                                                 cancelled, notes, summary_note))
        skipped = skipped or []
        notes = notes or []
        self._hide_progress()
        if cancelled:
            self._show_toast(
                _("{title}: cancelado tras {ok} completado(s)").format(
                    title=title, ok=ok)
                + (_(", {n} con error").format(n=len(errors)) if errors else "")
                + (_(", {n} omitido(s)").format(n=len(skipped)) if skipped else "")
                + "."
            )
            self.rescan_library()
            return False
        parts = [_("{n} ok").format(n=ok) if (errors or skipped)
                 else _("{n} completado(s) ✓").format(n=ok)]
        if skipped:
            preview = "; ".join(skipped[:3])
            more = (_(" (+{n} más)").format(n=len(skipped) - 3)
                    if len(skipped) > 3 else "")
            parts.append(_("{n} omitido(s): {preview}{more}").format(
                n=len(skipped), preview=preview, more=more))
        if notes:
            preview = "; ".join(notes[:2])
            more = (_(" (+{n} más, ver el Log)").format(n=len(notes) - 2)
                    if len(notes) > 2 else "")
            parts.append(preview + more)
        if errors:
            preview = "; ".join(errors[:3])
            more = (_(" (+{n} más)").format(n=len(errors) - 3)
                    if len(errors) > 3 else "")
            parts.append(_("{n} con error: {preview}{more}").format(
                n=len(errors), preview=preview, more=more))
        self._show_toast(f"{title}: " + " · ".join(parts))
        self.rescan_library()
        return False

    def _on_batch_send(self):
        games = self._selected_games()
        if not games:
            return
        if self._reject_if_busy(OperationKind.TRANSFERRING,
                                 read=[g.path for g in games]):
            return
        dialog = Gtk.FileDialog(title=_("Elegí la unidad/carpeta destino (WBFS)"))
        dialog.select_folder(self, None,
                              lambda d, r: self._on_batch_send_folder_chosen(d, r, games))

    def _on_batch_send_folder_chosen(self, dialog, result, games: list[Game]):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if not folder:
            return
        dest_root = Path(folder.get_path())
        # GameCube nunca pasa por `wit` (se copia tal cual, ver
        # `library.send_to_wbfs_drive`): la falta de `wit` solo bloquea a
        # los juegos de Wii que no sean ya WBFS.
        if any(g.fmt.upper() != "WBFS" and g.console != "gc" for g in games) and \
                not wit_wrapper.is_available(self.settings.wit_binary):
            self._show_toast(
                _("No se encontró 'wit'; no se puede convertir a WBFS los que "
                  "no lo son ya.")
            )
            return

        # Con un solo juego (flujo individual) se pregunta antes de pisar
        # un destino que ya existe, igual que al convertir. En lote no
        # tiene sentido preguntar por cada uno: el worker los omite y los
        # informa aparte en el resumen final.
        if len(games) == 1:
            try:
                dest = library.game_dest_path(games[0], dest_root)
            except ValueError:
                dest = None
            if dest is not None and dest.exists():
                gtk_helpers.confirm_overwrite(
                    self,
                    _("Ya existe un archivo en:\n{dest}\n\n"
                      "Enviar '{title}' lo va a reemplazar. "
                      "Esta acción no se puede deshacer.")
                    .format(dest=dest, title=games[0].title),
                    lambda: self._start_send(games, dest_root, overwrite=True),
                )
                return

        self._start_send(games, dest_root)

    def _start_send(self, games: list[Game], dest_root: Path, overwrite: bool = False):
        # Worker dedicado (no _run_batch genérico) porque acá sí tiene
        # sentido mostrar tiempo estimado y permitir cancelar: es la
        # operación de transferencia real hacia una unidad WBFS, con
        # tamaños de archivo conocidos de antemano.
        # El destino también se registra: los archivos que se van a
        # escribir y, sobre todo, la unidad entera como recurso ocupado,
        # para que no se la pueda expulsar a mitad de la copia.
        try:
            op = self.ops.start(
                OperationKind.TRANSFERRING,
                read=[g.path for g in games],
                write=library.wbfs_dest_paths(games, dest_root),
                resources=[dest_root],
            )
        except OperationBusy as e:
            self._show_toast(_("No se puede ahora: {detail}.").format(detail=e.detail))
            return

        cancel = self._begin_cancellable_progress(
            "Enviando a unidad WBFS", "Cancelando el envío…")

        total = len(games)
        wit_binary = self.settings.wit_binary

        def worker():
            # El plan (cuánto va a ocupar cada juego en el destino) se arma
            # acá y no en el hilo de GTK: implica preguntarle a `wit` por
            # cada juego y con un lote grande congelaría la ventana.
            GLib.idle_add(self.progress_bar.set_text, "Calculando espacio necesario…")
            plan = library.plan_transfer(games, wit_binary)
            total_bytes = sum(item.output_bytes for item in plan)
            libres_ahora = library.free_space(dest_root)
            if libres_ahora is not None and total_bytes > libres_ahora:
                GLib.idle_add(
                    self._on_send_done, 0,
                    [f"No entra en el destino: se necesitan "
                     f"{library.format_size(total_bytes)} y hay "
                     f"{library.format_size(libres_ahora)} libres"],
                    False, [], op, self._describe_target(games))
                return

            ok = 0
            errors: list[str] = []
            skipped: list[str] = []
            # Ver el worker de la pestaña Transferir: `bytes_written` es lo
            # único que se escribió de verdad y da la velocidad; la barra
            # avanza con todo lo ya resuelto (escrito + fallado + omitido).
            bytes_written = 0
            bytes_failed = 0
            bytes_skipped = 0
            start_time = time.monotonic()
            cancelled = False
            for i, item in enumerate(plan, start=1):
                game = item.game
                if cancel.cancelled:
                    cancelled = True
                    break
                base_bytes_done = bytes_written + bytes_failed + bytes_skipped

                def on_game_progress(current: int, _base=base_bytes_done, _item=item,
                                      _written=bytes_written):
                    # Tope al 97%: es una estimación del tamaño de salida,
                    # y wit puede seguir cerrando/renombrando un instante
                    # más después de escribir el último byte.
                    est = min(current, int(_item.output_bytes * 0.97))
                    GLib.idle_add(self._update_send_progress, i, total, _item.game.title,
                                  _base + est, _written + est, total_bytes, start_time)

                GLib.idle_add(self._update_send_progress, i, total, game.title,
                              bytes_written + bytes_failed + bytes_skipped,
                              bytes_written, total_bytes, start_time)

                # Igual que en la pestaña Transferir: el espacio libre se
                # revisa antes de cada juego, no una sola vez al principio.
                necesario = item.output_bytes
                libres = library.free_space(dest_root)
                if libres is not None and necesario > libres:
                    errors.append(
                        f"{game.title}: no entra en el destino "
                        f"(necesita {library.format_size(necesario)}, "
                        f"quedan {library.format_size(libres)})"
                    )
                    bytes_failed += item.output_bytes
                    continue

                try:
                    library.send_to_wbfs_drive(game, dest_root, wit_binary,
                                                bytes_progress_cb=on_game_progress,
                                                overwrite=overwrite, cancel=cancel)
                    ok += 1
                    bytes_written += item.output_bytes
                except wit_wrapper.OperationCancelled:
                    # Cancelado a mitad de ESTE juego: no es un error, y no
                    # se sigue con los que faltaban.
                    cancelled = True
                    break
                except library.DestinationExistsError:
                    # El juego ya está en la unidad: no es un error ni un
                    # éxito, se informa aparte en el resumen final.
                    skipped.append(game.title)
                    bytes_skipped += item.output_bytes
                except Exception as e:
                    if cancel.cancelled:
                        # El fallo es consecuencia de haber matado a `wit`
                        # al cancelar, no un error real del envío.
                        cancelled = True
                        break
                    # No frena el resto de la selección: se cuenta como
                    # error y se sigue con el siguiente juego.
                    errors.append(f"{game.title}: {e}")
                    bytes_failed += item.output_bytes
            GLib.idle_add(self._on_send_done, ok, errors, cancelled, skipped, op,
                          self._describe_target(games))

        threading.Thread(target=worker, daemon=True).start()

    def _update_send_progress(self, done: int, total: int, title: str,
                               bytes_done: int, bytes_written: int,
                               total_bytes: int, start_time: float):
        # Fracción por bytes reales, no por "juegos completados": con un
        # solo juego grande `done` no cambia hasta que termina, así que
        # basarse solo en eso deja la barra clavada en 0% toda la copia.
        if total_bytes > 0:
            fraction = min(bytes_done / total_bytes, 0.99)
        else:
            fraction = (done - 1) / max(total, 1)
        self.progress_bar.set_fraction(fraction)
        elapsed = time.monotonic() - start_time
        # La velocidad sale SOLO de lo que se escribió de verdad: contar
        # los bytes de un juego que falló, o de uno que ya estaba en el
        # destino y se saltó en un instante, daba una velocidad inventada
        # y un tiempo restante demasiado optimista.
        if bytes_written > 0 and elapsed > 1:
            speed = bytes_written / elapsed
            remaining = max(total_bytes - bytes_done, 0)
            eta_text = (_(" · ~{eta} restantes")
                        .format(eta=library.format_eta(remaining / speed))
                        if speed > 0 else "")
        elif total > 1:
            eta_text = _(" · calculando tiempo restante…")
        else:
            eta_text = ""
        self.progress_bar.set_text(
            _("{done}/{total} · {title}{eta}").format(
                done=done, total=total, title=title, eta=eta_text))
        return False

    def _on_send_done(self, ok: int, errors: list[str], cancelled: bool,
                       skipped: list[str] | None = None, op=None,
                       target: str = ""):
        self.ops.finish(op, self._batch_outcome(target, ok, errors, skipped, cancelled))
        skipped = skipped or []
        self._hide_progress()
        # Volver a None (no "") es lo que hace que el ProgressBar caiga de
        # nuevo a mostrar el porcentaje en las demás operaciones (rescan,
        # importar, convertir/verificar/eliminar en lote) en vez de dejar
        # pegado el último "N/total · nombre del juego" de este envío.
        self.progress_bar.set_text(None)
        if cancelled:
            self._show_toast(
                _("Envío a unidad WBFS cancelado: {ok} ok, {err} con error").format(
                    ok=ok, err=len(errors))
                + (_(", {n} omitido(s)").format(n=len(skipped)) if skipped else "")
                + _(" antes de cancelar.")
            )
        elif errors or skipped:
            parts = [_("{n} ok").format(n=ok)]
            if skipped:
                preview = "; ".join(skipped[:3])
                more = (_(" (+{n} más)").format(n=len(skipped) - 3)
                        if len(skipped) > 3 else "")
                parts.append(_("{n} ya estaban en el destino: {preview}{more}").format(
                    n=len(skipped), preview=preview, more=more))
            if errors:
                preview = "; ".join(errors[:3])
                more = (_(" (+{n} más)").format(n=len(errors) - 3)
                        if len(errors) > 3 else "")
                parts.append(_("{n} con error: {preview}{more}").format(
                    n=len(errors), preview=preview, more=more))
            self._show_toast(_("Enviando a unidad WBFS: ") + " · ".join(parts))
        else:
            self._show_toast(
                _("Enviando a unidad WBFS: {n} completado(s) ✓").format(n=ok))
        self.rescan_library()
        return False

    def _on_batch_convert(self):
        games = self._selected_games()
        if not games:
            return
        if self._reject_if_busy(OperationKind.CONVERTING,
                                 read=[g.path for g in games],
                                 write=[g.path.with_suffix(
                                     ".wbfs" if g.fmt.upper() != "WBFS" else ".iso")
                                     for g in games]):
            return
        if not wit_wrapper.is_available(self.settings.wit_binary):
            self._show_toast(_("No se encontró 'wit'. Instalalo para poder convertir "
                               "(ver README)."))
            return

        # Worker dedicado (no _run_batch genérico) porque acá sí tiene
        # sentido mostrar progreso real por bytes dentro de la conversión
        # de un solo juego grande, igual que en el envío a unidad WBFS:
        # con _run_batch la barra solo avanza entre juegos y se queda
        # clavada en 0% durante la conversión de uno solo pesado.
        #
        # Se registran tanto el origen como el destino de cada conversión:
        # el destino también es un archivo que nadie más puede tocar
        # mientras `wit` lo está escribiendo.
        destinos = [g.path.with_suffix(".wbfs" if g.fmt.upper() != "WBFS" else ".iso")
                     for g in games]
        try:
            op = self.ops.start(OperationKind.CONVERTING,
                                 read=[g.path for g in games], write=destinos)
        except OperationBusy as e:
            self._show_toast(_("No se puede ahora: {detail}.").format(detail=e.detail))
            return

        cancel = self._begin_cancellable_progress(
            _("Convirtiendo"), _("Cancelando la conversión…"))

        wit_binary = self.settings.wit_binary

        def worker():
            # Los tamaños de SALIDA (no los de los archivos de origen): el
            # progreso de `wit` cuenta bytes escritos en el destino. Se
            # calcula acá y no en el hilo de GTK, igual que en la
            # transferencia.
            GLib.idle_add(self.progress_bar.set_text, _("Calculando…"))
            salidas = {
                id(g): library.estimate_output_size(
                    g, ".wbfs" if g.fmt.upper() != "WBFS" else ".iso", wit_binary)
                for g in games
            }
            total_bytes = sum(salidas.values())
            GLib.idle_add(self.progress_bar.set_text, None)

            ok = 0
            errors: list[str] = []
            skipped: list[str] = []
            # Igual que en la transferencia: lo omitido y lo fallado no se
            # cuenta como convertido, pero sí como resuelto, para que la
            # barra refleje cuánto falta del lote.
            bytes_written = 0
            bytes_other = 0
            cancelled = False
            for game in games:
                if cancel.cancelled:
                    cancelled = True
                    break
                target_ext = ".wbfs" if game.fmt.upper() != "WBFS" else ".iso"
                dest = game.path.with_suffix(target_ext)
                base_bytes_done = bytes_written + bytes_other

                def on_progress(current: int, _base=base_bytes_done, _game=game,
                                 _salida=salidas[id(game)]):
                    est = min(current, int(_salida * 0.97))
                    frac = min((_base + est) / max(total_bytes, 1), 0.99)
                    GLib.idle_add(self.progress_bar.set_fraction, frac)

                GLib.idle_add(self.progress_bar.set_fraction,
                              min(base_bytes_done / max(total_bytes, 1), 0.99))
                if dest.exists():
                    # En lote no tiene sentido preguntar por cada uno: se
                    # saltea y se informa aparte en el resumen final, en
                    # vez de pisar en silencio o frenar todo el lote.
                    skipped.append(game.title)
                    bytes_other += salidas[id(game)]
                else:
                    try:
                        # Sin `overwrite`: acá el lote ya saltea el juego
                        # si el destino existe (ver el `if` de arriba), así
                        # que no hay nada que pisar. Si algo apareciera con
                        # ese nombre entre el chequeo y esta línea, `wit`
                        # falla y se informa, que es justo lo que se quiere.
                        result = wit_wrapper.convert(game.path, dest, target_ext.strip("."),
                                                      wit_binary, bytes_progress_cb=on_progress,
                                                      cancel=cancel)
                        if result.returncode != 0:
                            raise RuntimeError(result.stderr.strip()[:200] or "error de wit")
                        ok += 1
                        bytes_written += salidas[id(game)]
                    except wit_wrapper.OperationCancelled:
                        # Cancelado a mitad de ESTE juego: no es un error, y
                        # no se sigue con los que faltaban.
                        cancelled = True
                        break
                    except Exception as e:
                        if cancel.cancelled:
                            # El fallo es consecuencia de haber matado a
                            # `wit`, no un error real de la conversión.
                            cancelled = True
                            break
                        errors.append(f"{game.title}: {e}")
                        bytes_other += salidas[id(game)]
            GLib.idle_add(self._on_batch_done, "Convirtiendo", ok, errors, skipped, op,
                          self._describe_target(games), cancelled)

        threading.Thread(target=worker, daemon=True).start()

    def _on_batch_rename(self):
        games = self._selected_games()
        if games:
            self._start_batch_rename(games)

    def _on_rename_all(self):
        """Renombrar toda la biblioteca desde el menú, sin tener que
        tildar nada. Se toma lo que se está viendo (o sea, lo que el
        buscador haya dejado): renombrar en silencio juegos que el usuario
        no tiene en pantalla sería una sorpresa desagradable."""
        games = self._selected_games() or self._visible_games()
        if not games:
            self._show_toast("No hay juegos para renombrar.")
            return
        self._start_batch_rename(games)

    def _start_batch_rename(self, games: list[Game]):
        # Los que ya están con el nombre estándar no se tocan: no hay nada
        # que hacerles y meterlos en el lote solo alarga la lista de la
        # confirmación.
        pendientes = [g for g in games if library.needs_rename(g)]
        if not pendientes:
            self._show_toast("Todos esos juegos ya tienen el nombre estándar ✓")
            return

        if self._reject_if_busy(OperationKind.RENAMING,
                                 write=[g.path for g in pendientes]
                                       + [g.path.with_name(library.standard_filename(g))
                                          for g in pendientes],
                                 uses_progress_bar=True):
            return

        # Se muestran algunos ejemplos concretos: "renombrar 47 archivos"
        # no dice nada si el usuario no ve cómo van a quedar.
        ejemplos = "\n".join(
            f"{g.path.name}  →  {library.standard_filename(g)}"
            for g in pendientes[:6]
        )
        if len(pendientes) > 6:
            ejemplos += _("\n… y {n} más").format(n=len(pendientes) - 6)

        dialog = Adw.AlertDialog(
            heading=ngettext("¿Renombrar {n} archivo?", "¿Renombrar {n} archivos?",
                             len(pendientes)).format(n=len(pendientes)),
            body=_("Se van a renombrar los archivos en el disco:\n\n{examples}\n\n"
                   "Si el nombre que corresponde ya está ocupado por otro juego, "
                   "el archivo se guarda con un sufijo (2) en vez de pisarlo.")
                 .format(examples=ejemplos),
        )
        dialog.add_response("cancel", _("Cancelar"))
        dialog.add_response("rename", _("Renombrar"))
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_batch_rename_confirmed, pendientes)
        dialog.present(self)

    def _on_batch_rename_confirmed(self, dialog, response, games: list[Game]):
        if response != "rename":
            return
        # Revalidar después del diálogo, igual que en borrar y convertir.
        if self._reject_if_busy(OperationKind.RENAMING,
                                 write=[g.path for g in games]
                                       + [g.path.with_name(library.standard_filename(g))
                                          for g in games],
                                 uses_progress_bar=True):
            return

        def rename_one(g: Game, _cancel):
            # `on_collision="suffix"`: en lote no se puede frenar a
            # preguntar por cada choque, y pisar el archivo que ya está
            # sería perder un juego.
            esperado = library.standard_filename(g)
            nuevo = library.rename_to_standard(g, on_collision="suffix")
            if nuevo.name != esperado:
                # Se renombró igual (cuenta como hecho) pero con otro
                # nombre. Se DEVUELVE como nota para que entre en el
                # resumen del lote: escribir acá una entrada de historial
                # aparte rompía el patrón de "una acción del usuario, una
                # entrada de historial" que sigue el resto de la app.
                return f"guardado como {nuevo.name} ('{esperado}' estaba ocupado)"
            return None

        self._run_batch(games, "Renombrando", rename_one, OperationKind.RENAMING,
                         cancel_message="Cancelando el renombrado…")

    def _on_batch_verify(self):
        games = self._selected_games()
        if not games:
            return
        if self._reject_if_busy(OperationKind.VERIFYING, read=[g.path for g in games],
                                 uses_progress_bar=True):
            return
        if not wit_wrapper.is_available(self.settings.wit_binary):
            self._show_toast(_("No se encontró 'wit'. Instalalo para poder verificar "
                               "(ver README)."))
            return

        def verify_one(g: Game, cancel):
            ok, _output = wit_wrapper.verify(g.path, self.settings.wit_binary,
                                              cancel=cancel)
            if not ok:
                raise RuntimeError(_("verificación con errores"))

        self._run_batch(games, _("Verificando"), verify_one, OperationKind.VERIFYING,
                         cancel_message=_("Cancelando la verificación…"))

    def _on_batch_delete(self):
        games = self._selected_games()
        if not games:
            return
        # Chequeo antes de abrir el diálogo (y otra vez al confirmar, en
        # `_on_batch_delete_confirmed`): no tiene sentido preguntar por algo
        # que no se va a poder hacer.
        if self._reject_if_busy(OperationKind.DELETING, write=[g.path for g in games],
                                 uses_progress_bar=True):
            return
        names = "\n".join(g.path.name for g in games[:8])
        if len(games) > 8:
            names += "\n…"
        # Una selección puede mezclar unidades (la biblioteca en el disco y
        # un juego suelto en un pendrive de solo lectura), así que se
        # pregunta por cada archivo y el diálogo dice exactamente cuáles
        # se van a poder recuperar y cuáles no.
        permanentes = {g.path for g in games if not trash.can_trash(g.path)}
        if not permanentes:
            heading = _("¿Mover a la papelera los juegos seleccionados?")
            cierre = _("Van a la papelera del sistema. Podés recuperarlos "
                       "desde ahí.")
            verb = _("Mover a la papelera")
        elif len(permanentes) == len(games):
            heading = _("¿Eliminar definitivamente los juegos seleccionados?")
            cierre = _("La unidad donde están no tiene papelera, así que esta "
                       "acción no se puede deshacer.")
            verb = _("Eliminar")
        else:
            heading = _("¿Eliminar los juegos seleccionados?")
            cierre = _("{trashed} van a la papelera y se pueden recuperar. Los "
                       "otros {permanent} están en una unidad sin papelera: esos "
                       "se borran definitivamente.").format(
                           trashed=len(games) - len(permanentes),
                           permanent=len(permanentes))
            verb = _("Eliminar")
        dialog = Adw.AlertDialog(
            heading=heading,
            body=_("{n} archivo(s):\n{names}\n\n{closing}").format(
                n=len(games), names=names, closing=cierre),
        )
        dialog.add_response("cancel", _("Cancelar"))
        dialog.add_response("delete", verb)
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_batch_delete_confirmed, games, permanentes)
        dialog.present(self)

    def _on_batch_delete_confirmed(self, dialog, response, games: list[Game],
                                    permanentes: set):
        if response != "delete":
            return
        # Igual que en el borrado individual: revalidar después del diálogo.
        if self._reject_if_busy(OperationKind.DELETING, write=[g.path for g in games],
                                 uses_progress_bar=True):
            return

        def delete_one(g: Game, _cancel):
            # Borrar un archivo es instantáneo: no hay nada que cortar a
            # mitad, cancelar solo evita que se sigan borrando los que
            # faltaban.
            #
            # `permanentes` viene de lo que se le mostró al usuario en el
            # diálogo, no se vuelve a preguntar acá: si la unidad perdió la
            # papelera en el medio, `send_to_trash` levanta y el juego
            # queda contado como error en el resumen. Borrarlo igual sería
            # hacer algo distinto de lo que se confirmó.
            if g.path in permanentes:
                trash.delete_permanently(g.path)
                return
            trash.send_to_trash(g.path)

        a_papelera = len(games) - len(permanentes)
        if not permanentes:
            nota = _("a la papelera")
        elif a_papelera == 0:
            nota = _("borrado definitivo (unidad sin papelera)")
        else:
            nota = _("{trashed} a la papelera, {permanent} borrado(s) "
                     "definitivamente (unidad sin papelera)").format(
                         trashed=a_papelera, permanent=len(permanentes))
        self._run_batch(games, _("Eliminando"), delete_one, OperationKind.DELETING,
                         cancel_message=_("Cancelando el borrado…"),
                         summary_note=nota)

    # -------------------------------------------------------- Library --
    @staticmethod
    def _describe_skipped(skipped: list) -> str:
        """Texto para avisar qué carpetas quedaron afuera del escaneo."""
        nombres = []
        for path in skipped:
            if str(path) not in nombres:
                nombres.append(str(path))
        preview = ", ".join(nombres[:2])
        mas = (_(" (+{n} más)").format(n=len(nombres) - 2)
               if len(nombres) > 2 else "")
        return ngettext(
            "No se pudo leer {n} carpeta (permisos): {preview}{more}. "
            "Los juegos que haya adentro no aparecen.",
            "No se pudo leer {n} carpetas (permisos): {preview}{more}. "
            "Los juegos que haya adentro no aparecen.",
            len(nombres)).format(n=len(nombres), preview=preview, more=mas)

    def _update_library_banner(self):
        if self._library_available:
            self._library_banner.set_revealed(False)
        else:
            self._library_banner.set_title(
                _("Unidad no disponible: {path} no está conectada. Conectala y "
                  "se detectará automáticamente.").format(
                      path=self.settings.library_path)
            )
            self._library_banner.set_revealed(True)

    def _poll_library_availability(self):
        available = config.library_path_available(self.settings)
        if available != self._library_available:
            self._library_available = available
            self._update_library_banner()
            self.rescan_library()
        return True  # seguir sondeando

    # ------------------------------------------------------------ Scan --
    def rescan_library(self):
        """Vuelve a escanear la biblioteca en background.

        Nunca hay dos escaneos a la vez: si ya hay uno corriendo (o algo
        escribiendo archivos en la biblioteca, que haría que el escaneo
        viera archivos a medio copiar), el pedido no se descarta sino que
        queda anotado y se dispara solo cuando eso termina."""
        try:
            # La carpeta entera es el recurso: así nada escribe adentro
            # mientras se la recorre, pero una transferencia hacia un USB
            # (otro lugar) puede seguir corriendo.
            op = self.ops.start(OperationKind.SCANNING,
                                 resources=[Path(self.settings.library_path)])
        except OperationBusy:
            self._rescan_pending = True
            return False

        self._scan_generation += 1
        generation = self._scan_generation
        root = Path(self.settings.library_path)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        self.set_title(_("WiiBackup Manager — Escaneando…"))

        def worker():
            def progress(done, total):
                GLib.idle_add(self.progress_bar.set_fraction, done / max(total, 1))

            skipped: list = []
            error = None
            try:
                games = library.scan_library(root, self.settings.wit_binary, progress,
                                              skipped_dirs=skipped)
            except Exception as e:
                # `games = []` acá era una mentira peligrosa: "el escaneo
                # falló" y "no hay juegos" se veían igual, y el usuario
                # abría la app para encontrarse la biblioteca vacía sin
                # saber que fue un error técnico.
                games, error = None, f"{type(e).__name__}: {e}"
            GLib.idle_add(self._on_scan_done, games, generation, op, skipped, error)

        threading.Thread(target=worker, daemon=True).start()
        return False  # para idle_add

    def _on_scan_done(self, games: list[Game] | None, generation: int, op=None,
                       skipped: list | None = None, error: str | None = None):
        self.ops.finish(op)

        # Resultado de un escaneo que ya quedó viejo (arrancó otro después):
        # se descarta en vez de pisar la lista con datos de antes.
        if generation != self._scan_generation:
            return False

        if error is not None:
            # La lista que había se conserva: puede estar desactualizada,
            # pero es mucho mejor que mostrar la biblioteca vacía y hacerle
            # creer al usuario que perdió todo.
            self.progress_bar.set_visible(False)
            self.set_title(_("WiiBackup Manager — {n} juegos").format(
                n=len(self._games)))
            self._show_toast(
                _("No se pudo escanear la biblioteca ({error}). "
                  "Se sigue mostrando la última lista conocida.").format(error=error)
            )
            self.op_log.record(OperationKind.SCANNING.value,
                               str(self.settings.library_path),
                               oplog.STATUS_ERROR, error)
            if self._rescan_pending:
                self._rescan_pending = False
                self.rescan_library()
            return False

        # Carpetas que quedaron afuera por permisos. El escaneo corre solo
        # después de cada operación, así que avisar en cada uno sería un
        # toast cada treinta segundos: se avisa (y se anota en el
        # historial) solo cuando la lista de carpetas ilegibles cambia
        # respecto del escaneo anterior.
        ilegibles = {str(path) for path in (skipped or [])}
        if ilegibles and ilegibles != self._skipped_dirs:
            mensaje = self._describe_skipped(sorted(ilegibles))
            self._show_toast(mensaje)
            self.op_log.record(OperationKind.SCANNING.value,
                               _("{n} carpeta(s) sin permiso").format(
                                   n=len(ilegibles)),
                               oplog.STATUS_PARTIAL, mensaje)
        self._skipped_dirs = ilegibles

        self._games = games
        self._apply_sort()
        self.progress_bar.set_visible(False)
        self.set_title(_("WiiBackup Manager — {n} juegos").format(n=len(games)))
        self._populate_list()
        self.stack.set_visible_child_name("list" if games else "empty")
        self.transfer_view.set_games(games)
        self._update_library_status_bar()

        if self._rescan_pending:
            self._rescan_pending = False
            self.rescan_library()
        return False

    def _update_library_status_bar(self):
        count = len(self._games)
        total_size = library.format_size(sum(g.size_bytes for g in self._games))
        self.library_status_label.set_label(
            ngettext("{count} juego · {size}", "{count} juegos · {size}", count)
            .format(count=count, size=total_size))

    def _make_row(self, game: Game) -> GameRow:
        row = GameRow(game, self.settings.cover_region)
        row.connect("rename-requested", self._on_rename_requested)
        row.connect("convert-requested", self._on_convert_requested)
        row.connect("verify-requested", self._on_verify_requested)
        row.connect("delete-requested", self._on_delete_requested)
        row.connect("selection-toggled", lambda *_a: self._update_selection_bar())
        row.connect("detail-requested", self._on_game_detail_requested)
        row.set_selection_mode(self.select_toggle.get_active())
        row.load_cover_async()
        row.load_extra_info_async()
        return row

    def _populate_list(self):
        """Deja la lista mostrando exactamente `self._games`, reusando las
        filas que ya están.

        Un escaneo corre después de CADA operación, y antes esto tiraba las
        300 filas y creaba 300 nuevas cada vez: casi un segundo de ventana
        congelada, las carátulas volviendo a aparecer de a poco y la
        selección en cero aunque los juegos fueran los mismos. Ahora se
        borran solo las que ya no están, se actualizan las que siguen (una
        conversión les cambia formato y tamaño) y se crean solo las nuevas.

        No hace falta insertar en ninguna posición concreta: el orden lo
        pone `_sort_rows` a través del ListBox."""
        self._suspend_selection_updates = True
        try:
            wanted = {str(game.path): game for game in self._games}

            for key in list(self._rows):
                if key not in wanted:
                    self.list_box.remove(self._rows.pop(key))

            for key, game in wanted.items():
                row = self._rows.get(key)
                if row is None:
                    row = self._make_row(game)
                    self._rows[key] = row
                    row.sort_key = self._sort_key(game)
                    self.list_box.append(row)
                else:
                    row.update_game(game, self.settings.cover_region)
                    row.sort_key = self._sort_key(game)
        finally:
            self._suspend_selection_updates = False
        self.list_box.invalidate_sort()
        self._update_selection_bar()

    # ------------------------------------------------------------ Orden --
    def _current_sort(self):
        idx = self.sort_dropdown.get_selected()
        if idx < 0 or idx >= len(SORT_OPTIONS):
            idx = 0
        return SORT_OPTIONS[idx]

    def _sort_key(self, game: Game):
        _label, key_fn, _reverse = self._current_sort()
        return key_fn(game)

    def _sort_rows(self, row_a, row_b) -> int:
        """Comparador que usa el ListBox. Compara las claves ya calculadas
        (`row.sort_key`) en vez de sacarlas del juego acá: GTK llama a esto
        O(n log n) veces y uno de los criterios lee la fecha del archivo
        del disco."""
        _label, _key_fn, reverse = self._current_sort()
        a, b = getattr(row_a, "sort_key", None), getattr(row_b, "sort_key", None)
        if a == b:
            return 0
        if a is None or b is None:
            # Fila todavía sin clave (no debería pasar: se asigna antes de
            # insertarla). Se la deja donde está en vez de reventar el
            # comparador y con él el orden de toda la lista.
            return 0
        order = -1 if a < b else 1
        return -order if reverse else order

    def _apply_sort(self):
        """Ordena `self._games`, que es lo que se le pasa a la pestaña
        Transferir y de donde salen las filas nuevas. Las filas ya
        existentes las reordena el ListBox por su cuenta."""
        _label, key_fn, reverse = self._current_sort()
        self._games.sort(key=key_fn, reverse=reverse)

    def _on_sort_changed(self, *_args):
        # Sin reconstruir nada: se recalcula la clave de cada fila y el
        # ListBox reacomoda los widgets que ya existen. Así el cambio de
        # orden es instantáneo aunque haya cientos de juegos, y de paso no
        # se pierden ni las casillas marcadas ni las carátulas ya cargadas,
        # que era lo que pasaba cuando esto repoblaba la lista entera.
        self._apply_sort()
        for row in self._rows.values():
            row.sort_key = self._sort_key(row.game)
        self.list_box.invalidate_sort()

    # --------------------------------------------------------- Exportar --
    def _visible_games(self) -> list[Game]:
        """Los juegos que se están viendo ahora, en el orden de la pantalla
        y sin los que el buscador dejó afuera."""
        juegos = []
        row = self.list_box.get_first_child()
        while row is not None:
            if isinstance(row, GameRow) and self._filter_row(row):
                juegos.append(row.game)
            row = row.get_next_sibling()
        return juegos

    def _games_to_export(self) -> list[Game]:
        """Lo tildado si hay algo tildado; si no, lo que se está viendo.

        Es la misma regla que espera cualquiera que use la app: si me tomé
        el trabajo de elegir doce juegos, exportar esos doce; si no elegí
        nada, exportar la lista tal como la tengo filtrada y ordenada."""
        return self._selected_games() or self._visible_games()

    def _on_export(self, fmt: str):
        juegos = self._games_to_export()
        if not juegos:
            self._show_toast(_("No hay juegos para exportar."))
            return

        extension = "csv" if fmt == library.EXPORT_CSV else "txt"
        dialog = Gtk.FileDialog(title=_("Guardar la lista de juegos"))
        dialog.set_initial_name(
            f"biblioteca-wii-{time.strftime('%Y-%m-%d')}.{extension}")
        dialog.set_initial_folder(gtk_helpers.safe_initial_folder())
        filtro = Gtk.FileFilter()
        if fmt == library.EXPORT_CSV:
            filtro.set_name(_("Planilla CSV (*.csv)"))
            filtro.add_pattern("*.csv")
        else:
            filtro.set_name(_("Texto plano (*.txt)"))
            filtro.add_pattern("*.txt")
        filtros = Gio.ListStore.new(Gtk.FileFilter)
        filtros.append(filtro)
        dialog.set_filters(filtros)
        dialog.save(self, None,
                     lambda d, r: self._on_export_file_chosen(d, r, juegos, fmt))

    def _on_export_file_chosen(self, dialog, result, juegos: list[Game], fmt: str):
        try:
            archivo = dialog.save_finish(result)
        except Exception:
            return  # el usuario canceló
        if archivo is None or not archivo.get_path():
            return
        destino = Path(archivo.get_path())

        # Confirmación propia antes de pisar: el selector del sistema suele
        # preguntar, pero eso depende del portal que esté instalado, y el
        # resto de la app no delega esa decisión en nadie.
        if destino.exists():
            gtk_helpers.confirm_overwrite(
                self,
                _("Ya existe un archivo en:\n{name}\n\n"
                  "Exportar la lista lo va a reemplazar.").format(name=destino.name),
                lambda: self._write_export(destino, juegos, fmt),
            )
            return
        self._write_export(destino, juegos, fmt)

    def _write_export(self, destino: Path, juegos: list[Game], fmt: str):
        contenido = library.export_games(juegos, fmt)
        # utf-8-sig en el CSV: sin el BOM, Excel en Windows abre los
        # acentos rotos, y estas listas terminan en la computadora de un
        # cliente. El texto plano va en utf-8 pelado, que es lo que espera
        # cualquier chat o editor.
        codificacion = "utf-8-sig" if fmt == library.EXPORT_CSV else "utf-8"
        try:
            # Misma escritura atómica que config.json y el historial: si el
            # proceso se corta a mitad, el usuario se queda con el archivo
            # anterior entero (o sin archivo), nunca con una lista cortada
            # que parece completa.
            config.write_text_atomic(destino, contenido, encoding=codificacion)
        except OSError as e:
            self._show_toast(_("No se pudo guardar la lista: {error}").format(error=e))
            return

        self._show_toast(
            ngettext("{n} juego exportado a {name}",
                     "{n} juegos exportados a {name}", len(juegos))
            .format(n=len(juegos), name=destino.name))

    # ---------------------------------------------------------- Filter --
    def _on_search_changed(self, entry):
        self.list_box.invalidate_filter()

    def _filter_row(self, row: GameRow) -> bool:
        query = self.search_entry.get_text().strip().lower()
        if not query:
            return True
        return query in row.game.title.lower() or query in row.game.game_id.lower()

    # ----------------------------------------------------------- Actions --
    def _on_add_files(self, *_args):
        dialog = Gtk.FileDialog(title=_("Agregar archivos"))
        filt = Gtk.FileFilter()
        filt.set_name("Imágenes de Wii (*.iso, *.wbfs, *.ciso, *.wdf)")
        for pattern in ("*.iso", "*.wbfs", "*.ciso", "*.wdf"):
            filt.add_pattern(pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filt)
        dialog.set_filters(filters)
        dialog.open_multiple(self, None, self._on_files_chosen)

    def _on_files_chosen(self, dialog, result):
        try:
            files = dialog.open_multiple_finish(result)
        except Exception:
            return
        if not files:
            return

        paths = [Path(files.get_item(i).get_path()) for i in range(files.get_n_items())]
        self._start_import(paths)

    def _on_add_folder(self, *_args):
        dialog = Gtk.FileDialog(title=_("Agregar carpeta completa"))
        dialog.set_initial_folder(gtk_helpers.safe_initial_folder())
        dialog.select_folder(self, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if folder is None:
            return

        root = Path(folder.get_path())
        skipped: list = []
        paths = library.find_game_files(root, skipped)
        if skipped:
            # Acá sí se avisa siempre: el usuario acaba de elegir esa
            # carpeta a propósito y tiene que saber que parte no se leyó.
            self._show_toast(self._describe_skipped(skipped))
        if not paths:
            self._show_toast(_("No se encontraron archivos válidos en esa carpeta."))
            return
        self._start_import(paths)

    def _on_files_dropped(self, drop_target, value, x, y):
        try:
            files = value.get_files()
        except AttributeError:
            return False

        paths: list[Path] = []
        skipped: list = []
        for f in files:
            raw_path = f.get_path()
            if not raw_path:
                continue
            p = Path(raw_path)
            if p.is_dir():
                paths.extend(library.find_game_files(p, skipped))
            elif p.is_file() and p.suffix.lower() in library.VALID_EXTENSIONS:
                paths.append(p)

        if skipped:
            self._show_toast(self._describe_skipped(skipped))

        if not paths:
            self._show_toast(
                _("No se encontraron archivos ISO/WBFS/CISO/WDF válidos en lo "
                  "soltado.")
            )
            return False

        self._start_import(paths)
        return True

    @staticmethod
    def _is_same_file(a: Path, b: Path) -> bool:
        """True si las dos rutas apuntan al mismo archivo (agregar un juego
        que ya está en la carpeta de biblioteca no es una colisión: no hay
        nada que copiar)."""
        try:
            return a.resolve() == b.resolve()
        except OSError:
            return a.absolute() == b.absolute()

    @staticmethod
    def _free_import_dest(dest: Path, reservados: set) -> Path:
        """Variante libre de `dest` agregándole un sufijo: 'Título.wbfs' ->
        'Título (2).wbfs'. Se usa en el flujo en lote, donde parar a
        preguntar por cada colisión no tiene sentido y pisar el archivo
        existente sería perder un juego que el usuario ya tenía.

        "Libre" es libre en el disco Y en este mismo lote: `reservados`
        trae los destinos que ya se le asignaron a otros archivos de la
        misma importación. Sin eso, dos archivos llamados igual que vienen
        de dos pendrives distintos calculaban los dos el mismo destino
        -ninguno existía todavía en el disco al planificar- y el segundo
        pisaba al primero, con los dos anotados como agregados."""
        n = 2
        candidate = dest
        while candidate.exists() or candidate in reservados:
            candidate = dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
            n += 1
        return candidate

    def _start_import(self, src_paths: list[Path], overwrite: bool = False):
        """Copia `src_paths` a la biblioteca en background, identificando
        cada archivo primero para no duplicar juegos que ya están en el
        último escaneo (self._games), comparando por game_id.

        Ojo: esa comparación es por game_id, así que no dice nada de un
        archivo distinto que casualmente se llame igual que uno que ya
        está en la biblioteca (otro juego, o el mismo con otro parche).
        Copiarlo tal cual lo pisaría sin aviso, así que las colisiones de
        nombre se resuelven acá antes de tocar el disco: preguntando si es
        un solo archivo, y con un nombre alternativo si es un lote.
        `overwrite=True` es la respuesta afirmativa de esa pregunta."""
        dest_dir = Path(self.settings.library_path)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._show_toast(_("No se pudo escribir en la carpeta de biblioteca: {error}")
                             .format(error=e))
            return

        known_ids = {g.game_id for g in self._games if g.game_id != UNKNOWN_GAME_ID}

        # Adónde va a parar cada archivo, resuelto ANTES de arrancar el
        # worker: así el OperationManager registra los archivos que se van
        # a escribir de verdad (no los que se hubieran escrito pisando) y
        # la pregunta de sobrescritura se hace en el hilo de GTK.
        plan: list[tuple[Path, Path]] = []
        renamed: list[str] = []
        # Destinos ya comprometidos por ESTE lote: el disco todavía no los
        # tiene, pero están tan ocupados como si los tuviera.
        reservados: set = set()
        for src in src_paths:
            dest = dest_dir / src.name
            ya_esta = self._is_same_file(src, dest)
            if not ya_esta and (dest.exists() or dest in reservados) and not overwrite:
                if len(src_paths) == 1:
                    gtk_helpers.confirm_overwrite(
                        self,
                        f"Ya existe un archivo en la biblioteca con ese nombre:\n"
                        f"{dest.name}\n\nAgregar '{src.name}' lo va a reemplazar. "
                        "Esta acción no se puede deshacer.",
                        lambda: self._start_import(src_paths, overwrite=True),
                    )
                    return
                dest = self._free_import_dest(dest, reservados)
                renamed.append(dest.name)
            reservados.add(dest)
            plan.append((src, dest))

        # Invariante del plan: dos archivos no pueden ir al mismo lado.
        destinos = [d for _s, d in plan]
        assert len(set(destinos)) == len(destinos), "plan de importación con destinos repetidos"

        # Origen y destino: mientras se copia, nadie puede borrar ni
        # convertir ninguno de los dos.
        try:
            op = self.ops.start(
                OperationKind.IMPORTING,
                read=[src for src, _dest in plan],
                write=[dest for _src, dest in plan],
            )
        except OperationBusy as e:
            self._show_toast(_("No se puede ahora: {detail}.").format(detail=e.detail))
            return

        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        self.set_title(_("WiiBackup Manager — Agregando…"))

        def worker():
            added: list[str] = []
            skipped: list[str] = []
            # Un archivo que no se pudo identificar o que no se pudo copiar
            # NO es un "no pasó nada": antes los dos casos hacían `continue`
            # en silencio y el resumen decía "completado" igual, así que el
            # historial -que existe justamente para poder confiar en él-
            # informaba 7 importados cuando 2 habían fallado.
            errors: list[str] = []
            total = len(plan)

            def copiar(src: Path, dest: Path) -> Path:
                """Copia `src` a `dest` y devuelve dónde terminó de verdad.

                El plan se arma en el hilo de GTK y la copia arranca acá,
                después de identificar cada archivo con `wit`: entre una
                cosa y la otra pasa tiempo real, y un proceso EXTERNO a la
                app (otro programa, un script, otra instancia) puede haber
                creado un archivo con ese nombre. `copy_no_replace` reserva
                el nombre de forma atómica y avisa en vez de pisarlo, así
                que esa colisión tardía se trata como lo que es: una
                colisión nueva, que se resuelve con un nombre alternativo,
                igual que las que se detectan al planificar.

                Con `overwrite` el usuario ya confirmó pisar ESE archivo,
                así que ahí sí se reemplaza — pero con `copy_atomic`, que
                no deja el destino a medio escribir si la copia se corta."""
                if overwrite:
                    library.copy_atomic(src, dest)
                    return dest
                # El destino alternativo no está declarado en la
                # operación (el plan no lo preveía), así que el detector de
                # conflictos no lo cubre. Es aceptable: es un archivo que
                # nadie más conoce todavía, y la alternativa -pisar lo que
                # apareció- es peor.
                destino = dest
                for _intento in range(_MAX_COLISIONES_IMPORT):
                    try:
                        library.copy_no_replace(src, destino)
                    except FileExistsError:
                        # Se lo ganaron en el medio: se busca la próxima
                        # variante libre y se reintenta.
                        destino = self._free_import_dest(dest, set())
                        continue
                    if destino != dest:
                        renamed.append(destino.name)
                    return destino
                raise FileExistsError(dest)

            for i, (src, dest) in enumerate(plan, start=1):
                GLib.idle_add(self.progress_bar.set_fraction, i / max(total, 1))

                try:
                    game = library.identify_file(src, self.settings.wit_binary)
                except Exception as e:
                    errors.append(f"{src.name}: no se pudo leer ({e})")
                    continue
                if game is None:
                    errors.append(f"{src.name}: no se pudo identificar el juego")
                    continue

                if game.game_id != UNKNOWN_GAME_ID and game.game_id in known_ids:
                    skipped.append(game.title)
                    continue

                if not self._is_same_file(src, dest):
                    try:
                        dest = copiar(src, dest)
                    except FileExistsError:
                        errors.append(
                            f"{game.title}: apareció otro archivo llamado "
                            f"'{dest.name}' en la biblioteca y no se encontró "
                            "un nombre libre; no se copió nada"
                        )
                        continue
                    except OSError as e:
                        errors.append(f"{game.title}: no se pudo copiar ({e.strerror or e})")
                        continue

                if game.game_id != UNKNOWN_GAME_ID:
                    known_ids.add(game.game_id)
                added.append(game.title)

            GLib.idle_add(self._on_import_done, added, skipped, renamed, op, errors)

        threading.Thread(target=worker, daemon=True).start()

    def _on_import_done(self, added: list[str], skipped: list[str],
                         renamed: list[str] | None = None, op=None,
                         errors: list[str] | None = None):
        renamed = renamed or []
        errors = errors or []
        detail_parts = [f"{len(added)} agregado(s)"]
        if skipped:
            detail_parts.append(f"{len(skipped)} ya estaban en la biblioteca")
        if renamed:
            detail_parts.append(f"{len(renamed)} renombrado(s) para no pisar otro archivo")
        if errors:
            preview = "; ".join(errors[:3])
            mas = f" (+{len(errors) - 3} más)" if len(errors) > 3 else ""
            detail_parts.append(f"{len(errors)} con error: {preview}{mas}")
        # "ningún juego" y no "0 juegos": cuando todos los archivos ya
        # estaban en la biblioteca (o ninguno se pudo identificar) la
        # entrada del historial quedaba como "Agregando juegos · 0 juegos",
        # que se lee como si se hubiera roto algo.
        if not added:
            target = "ningún juego"
        elif len(added) == 1:
            target = added[0]
        else:
            target = f"{len(added)} juegos"

        # El estado tiene que reflejar lo que pasó de verdad: "completada"
        # con dos archivos que no entraron es exactamente la clase de dato
        # falso que hace que un historial no sirva para nada.
        if not errors:
            status = oplog.STATUS_OK
        elif added or skipped:
            status = oplog.STATUS_PARTIAL
        else:
            status = oplog.STATUS_ERROR

        self.ops.finish(op, OperationOutcome(
            status=status,
            target=target,
            detail=" · ".join(detail_parts),
        ))
        parts = []
        if added:
            parts.append(f"{len(added)} juego(s) nuevo(s) agregado(s)")
        if skipped:
            if len(skipped) <= 5:
                parts.append(f"{len(skipped)} omitido(s) por ya existir: " + ", ".join(skipped))
            else:
                parts.append(_("{n} omitido(s) por ya existir en la biblioteca")
                             .format(n=len(skipped)))
        if renamed:
            # Se informa aparte: el archivo entró, pero con otro nombre que
            # el que tenía, y sin eso el usuario no tendría cómo saberlo.
            if len(renamed) <= 3:
                parts.append(_("Ya había un archivo con el mismo nombre, se "
                               "guardó como: {names}").format(
                                   names=", ".join(renamed)))
            else:
                parts.append(_("{n} se guardaron con otro nombre para no pisar "
                               "archivos que ya estaban").format(n=len(renamed)))
        if errors:
            preview = "; ".join(errors[:2])
            mas = (_(" (+{n} más, ver la pestaña Log)").format(n=len(errors) - 2)
                   if len(errors) > 2 else "")
            parts.append(_("{n} con error: {preview}{more}").format(
                n=len(errors), preview=preview, more=mas))
        if not parts:
            parts.append(_("No se agregó ningún juego nuevo"))
        self._show_toast(". ".join(parts) + ".")
        self.rescan_library()
        return False

    def _on_game_detail_requested(self, row: GameRow):
        dialog = GameDetailDialog(row.game, self.settings.cover_region)
        dialog.present(self)

    def _on_rename_requested(self, row: GameRow):
        # Renombrar es instantáneo, pero mover el archivo bajo los pies de
        # una conversión o una transferencia en curso la rompe igual.
        if self._reject_if_busy(
                OperationKind.RENAMING,
                write=[row.game.path,
                       row.game.path.with_name(library.standard_filename(row.game))]):
            return
        # Renombrar y eliminar un juego suelto son las dos únicas acciones
        # de usuario que no se registran en el OperationManager (son
        # instantáneas: no hay un worker ni una barra de progreso que
        # coordinar), así que se anotan derecho en el historial.
        try:
            new_path = library.rename_to_standard(row.game)
        except FileExistsError as e:
            self._show_toast(str(e))
            self.op_log.record(OperationKind.RENAMING.value, row.game.title,
                                oplog.STATUS_ERROR, str(e))
            return
        self._show_toast(_("Renombrado a: {name}").format(name=new_path.name))
        self.op_log.record(OperationKind.RENAMING.value, row.game.title,
                            oplog.STATUS_OK,
                            _("a {name}").format(name=new_path.name))
        self.rescan_library()

    def _on_convert_requested(self, row: GameRow):
        game = row.game
        if not wit_wrapper.is_available(self.settings.wit_binary):
            self._show_toast(_("No se encontró 'wit'. Instalalo para poder convertir "
                               "(ver README)."))
            return

        target_ext = ".wbfs" if game.fmt.upper() != "WBFS" else ".iso"
        dest = game.path.with_suffix(target_ext)

        if self._reject_if_busy(OperationKind.CONVERTING, read=[game.path], write=[dest]):
            return

        if dest.exists():
            # Ya hay un archivo con ese nombre de destino (no necesariamente
            # el mismo juego): confirmar antes de pisarlo, en vez de dejar
            # que --overwrite lo reemplace en silencio.
            gtk_helpers.confirm_overwrite(
                self,
                _("Ya existe un archivo en:\n{name}\n\n"
                  "Convertir '{title}' lo va a reemplazar. "
                  "Esta acción no se puede deshacer.")
                .format(name=dest.name, title=game.title),
                lambda: self._start_convert(game, dest, target_ext),
            )
            return

        self._start_convert(game, dest, target_ext)

    def _start_convert(self, game: Game, dest: Path, target_ext: str):
        # Muestra progreso real por bytes: antes esta conversión individual
        # ni siquiera mostraba la barra, así que con un juego grande no
        # había ninguna señal de que algo estaba pasando durante los
        # varios minutos que puede tardar.
        #
        # Se revalida acá y no solo al abrir el menú: entre medio pudo
        # haber un diálogo de confirmación de sobrescritura, y en ese rato
        # puede haber arrancado otra operación sobre el mismo archivo.
        try:
            op = self.ops.start(OperationKind.CONVERTING, read=[game.path], write=[dest])
        except OperationBusy as e:
            self._show_toast(_("No se puede ahora: {detail}.").format(detail=e.detail))
            return

        # Con botón de cancelar: convertir un dual-layer puede tardar
        # varios minutos y hasta ahora la única salida era cerrar la app.
        cancel = self._begin_cancellable_progress(
            _("Convirtiendo"), _("Cancelando la conversión…"))
        # Tamaño de SALIDA, no del archivo de origen: el progreso de `wit`
        # cuenta bytes escritos en el destino. Se calcula al vuelo dentro
        # del worker (`_calcular_total`) para no frenar el hilo de GTK.
        total_bytes = max(game.size_bytes, 1)
        # Se inicializan acá y no dentro del `try`: el `finally` los lee
        # para armar la entrada del historial, y si la excepción salta
        # antes de la asignación tiene que encontrar algo.
        ok = False
        cancelled = False

        def on_progress(current: int):
            est = min(current, int(game.size_bytes * 0.97))
            GLib.idle_add(self.progress_bar.set_fraction, min(est / total_bytes, 0.99))

        def worker():
            nonlocal ok, cancelled, total_bytes
            total_bytes = max(
                library.estimate_output_size(game, target_ext, self.settings.wit_binary), 1)
            detail = ""
            try:
                # El destino puede existir (el usuario confirmó pisarlo):
                # se lo aparta y se lo devuelve si la conversión no
                # termina bien. Ver library.DestinationGuard.
                with library.DestinationGuard(
                        dest, enabled=bool(library.wbfs_group(dest))) as guard:
                    # `overwrite=True` explícito: el usuario ya confirmó
                    # pisar el destino y el guard de arriba tiene el
                    # respaldo apartado para devolverlo si esto sale mal.
                    result = wit_wrapper.convert(game.path, dest, target_ext.strip("."),
                                                  self.settings.wit_binary,
                                                  bytes_progress_cb=on_progress,
                                                  cancel=cancel, overwrite=True)
                    ok = result.returncode == 0
                    if ok:
                        guard.commit()
                detail = (f"a {dest.name}" if ok else result.stderr.strip()[:200])
                msg = (f"Convertido a {dest.name}" if ok
                       else f"Error al convertir: {result.stderr.strip()[:200]}")
            except wit_wrapper.OperationCancelled:
                # `wit_wrapper` ya mató el proceso y limpió el destino a
                # medio escribir: no es un error, no hay nada que reportar
                # como fallo.
                cancelled = True
                detail = "cancelada por el usuario"
                msg = f"Conversión de '{game.title}' cancelada."
            except library.RollbackFailedError as e:
                # Caso grave: además de fallar la conversión, no se pudo
                # devolver el original a su lugar (ver
                # `library.RollbackFailedError`). `user_message` nombra
                # los dos problemas -no alcanza con "error al convertir"
                # cuando el archivo puede haber quedado inservible.
                ok = False
                msg = e.user_message()
                detail = str(e)
            except Exception as e:
                if cancel.cancelled:
                    cancelled = True
                    detail = "cancelada por el usuario"
                    msg = f"Conversión de '{game.title}' cancelada."
                else:
                    ok, msg = False, f"Error al convertir: {e}"
                    detail = str(e)
            finally:
                # Liberar el archivo antes del rescan: si no, el escaneo
                # chocaría con esta misma operación y quedaría postergado.
                GLib.idle_add(self.ops.finish, op, OperationOutcome(
                    status=(oplog.STATUS_CANCELLED if cancelled
                            else oplog.STATUS_OK if ok else oplog.STATUS_ERROR),
                    target=game.title,
                    detail=detail,
                ))
            GLib.idle_add(self._hide_progress)
            GLib.idle_add(self._show_toast, msg)
            if ok or cancelled:
                # También tras cancelar: el destino a medio escribir se
                # borró, y la lista tiene que reflejar cómo quedó el disco.
                GLib.idle_add(self.rescan_library)

        threading.Thread(target=worker, daemon=True).start()

    def _on_verify_requested(self, row: GameRow):
        game = row.game
        if not wit_wrapper.is_available(self.settings.wit_binary):
            self._show_toast(_("No se encontró 'wit'. Instalalo para poder verificar "
                               "(ver README)."))
            return

        # Verificar solo lee, así que convive con otra lectura del mismo
        # archivo; lo que no puede es leer algo que se está reescribiendo.
        try:
            op = self.ops.start(OperationKind.VERIFYING, read=[game.path])
        except OperationBusy as e:
            self._show_toast(_("No se puede ahora: {detail}.").format(detail=e.detail))
            return

        def worker():
            # Un fallo de verificación NO es lo mismo que no haber podido
            # verificar: el primero dice que el respaldo está dañado (dato
            # valioso para el historial), el segundo que `wit` no pudo
            # correr. Los dos quedan como error, pero con motivos
            # distintos.
            status = oplog.STATUS_ERROR
            detail = "la verificación encontró errores en el archivo"
            try:
                ok, _output = wit_wrapper.verify(game.path, self.settings.wit_binary)
                if ok:
                    status, detail = oplog.STATUS_OK, ""
                msg = (f"'{game.title}' verificado OK ✓" if ok
                       else f"'{game.title}' con errores ✗")
            except Exception as e:
                msg = f"No se pudo verificar '{game.title}': {e}"
                detail = str(e)
            finally:
                GLib.idle_add(self.ops.finish, op, OperationOutcome(
                    status=status, target=game.title, detail=detail))
            GLib.idle_add(self._show_toast, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_delete_requested(self, row: GameRow):
        game = row.game
        if self._reject_if_busy(OperationKind.DELETING, write=[game.path]):
            return
        # Lo que se le promete al usuario depende de si esa unidad tiene
        # papelera: prometer que se puede deshacer y después borrar de
        # verdad sería peor que no ofrecer la papelera.
        self._present_delete_dialog(game, trash.can_trash(game.path))

    def _present_delete_dialog(self, game: Game, to_trash: bool):
        if to_trash:
            heading = _("¿Mover este juego a la papelera?")
            body = _("El archivo:\n{name}\n\nva a la papelera del sistema. "
                     "Podés recuperarlo desde ahí.").format(name=game.path.name)
            verb = _("Mover a la papelera")
        else:
            heading = _("¿Eliminar este juego definitivamente?")
            body = _("Se borrará el archivo:\n{name}\n\nLa unidad donde está "
                     "no tiene papelera, así que esta acción no se puede "
                     "deshacer.").format(name=game.path.name)
            verb = _("Eliminar")
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", _("Cancelar"))
        dialog.add_response("delete", verb)
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_delete_confirmed, game, to_trash)
        dialog.present(self)

    def _on_delete_confirmed(self, dialog, response, game: Game, to_trash: bool):
        if response != "delete":
            return
        # Revalidar: entre que se abrió el diálogo y el usuario confirmó
        # pudo arrancar una conversión o una transferencia sobre este mismo
        # archivo, y borrarlo abajo de esa operación la rompe.
        if self._reject_if_busy(OperationKind.DELETING, write=[game.path]):
            return
        try:
            if to_trash:
                trash.send_to_trash(game.path)
                self._show_toast(_("Movido a la papelera: {name}").format(name=game.path.name))
                detail = _("{name} → papelera").format(name=game.path.name)
            else:
                trash.delete_permanently(game.path)
                self._show_toast(_("Eliminado: {name}").format(name=game.path.name))
                detail = _("{name} (borrado definitivo)").format(
                    name=game.path.name)
            self.op_log.record(OperationKind.DELETING.value, game.title,
                                oplog.STATUS_OK, detail)
        except trash.TrashUnsupported:
            # La unidad dejó de tener papelera entre la pregunta y el
            # borrado (se remontó de solo lectura, por ejemplo). No se
            # borra igual por las dudas: lo que el usuario aceptó era otra
            # cosa, así que se le vuelve a preguntar diciendo la verdad.
            self.rescan_library()
            self._present_delete_dialog(game, to_trash=False)
            return
        except OSError as e:
            self._show_toast(_("No se pudo eliminar: {error}").format(error=e))
            self.op_log.record(OperationKind.DELETING.value, game.title,
                                oplog.STATUS_ERROR, str(e))
        self.rescan_library()

    # ------------------------------------------------------------ Misc --
    def _on_preferences(self):
        dialog = PreferencesDialog(self.settings, self._on_settings_saved)
        dialog.present(self)

    def _on_settings_saved(self, settings: config.Settings):
        error = config.try_save(settings)
        if error:
            self._show_toast(
                f"No se pudo guardar la configuración: {error}. "
                "Los cambios valen para esta sesión."
            )
        config.ensure_dirs(settings)
        styles.apply_color_scheme(settings.color_scheme)
        self._library_available = config.library_path_available(settings)
        self._update_library_banner()
        self.rescan_library()

    def _on_about(self):
        about = Adw.AboutDialog(
            application_name="WiiBackup Manager",
            application_icon=config.APP_ID,
            version=__version__,
            developer_name="GameFix SPS",
            license_type=Gtk.License.MIT_X11,
            comments=_("Gestor de respaldos de Wii (ISO/WBFS) para Linux, "
                       "inspirado en Wii Backup Manager de Windows."),
            website="https://github.com/",
        )
        about.present(self)

    def _on_close_request(self, *_args) -> bool:
        self.transfer_view.shutdown()
        self.homebrew_view.shutdown()
        return False  # False = seguir con el cierre normal

    def _show_toast(self, message: str):
        self._toast_overlay.add_toast(Adw.Toast(title=message, timeout=3))
