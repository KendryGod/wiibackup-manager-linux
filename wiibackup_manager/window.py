from __future__ import annotations

import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gtk, GLib, Gio, Gdk  # noqa: E402

from . import __version__, config, library, operations, oplog, styles, wit_wrapper
from .disc_header import UNKNOWN_GAME_ID
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
    ("Título (A-Z)", lambda g: g.title.lower(), False),
    ("Tamaño (mayor a menor)", lambda g: g.size_bytes, True),
    ("Tamaño (menor a mayor)", lambda g: g.size_bytes, False),
    ("Fecha de agregado (más nuevo primero)", _game_ctime, True),
    ("Formato (A-Z)", lambda g: (g.fmt, g.title.lower()), False),
]
from .library import Game
from .widgets.game_detail_dialog import GameDetailDialog
from .widgets.game_row import GameRow
from .widgets.log_view import LogView
from .widgets import gtk_helpers
from .widgets.preferences_dialog import PreferencesDialog
from .widgets.transfer_view import TransferView


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
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self._toast_overlay.set_child(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        # El stack de vistas se crea acá porque el switcher del header lo
        # necesita (Adw.ViewSwitcher se ata a un Adw.ViewStack), pero
        # se llena de contenido más abajo.
        self.view_stack = Adw.ViewStack()

        # Switcher tipo "pill" siempre arriba, integrado en el header
        # (como en Archivos/Configuración de GNOME), sin comportamiento
        # adaptativo hacia una barra inferior.
        self.title_widget = Adw.ViewSwitcher(stack=self.view_stack,
                                              policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self.title_widget)

        self._add_button = add_button = Gtk.MenuButton(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("Agregar juegos (ISO/WBFS)")
        add_menu = Gio.Menu()
        add_menu.append("Agregar archivos", "win.add-files")
        add_menu.append("Agregar carpeta completa", "win.add-folder")
        add_button.set_menu_model(add_menu)
        header.pack_start(add_button)

        self._refresh_button = refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.set_tooltip_text("Volver a escanear la biblioteca")
        refresh_button.connect("clicked", lambda *_: self.rescan_library())
        header.pack_start(refresh_button)

        self.select_toggle = Gtk.ToggleButton(icon_name="object-select-symbolic")
        self.select_toggle.set_tooltip_text("Selección múltiple")
        self.select_toggle.connect("toggled", self._on_select_mode_toggled)
        header.pack_start(self.select_toggle)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu = Gio.Menu()
        tools_section = Gio.Menu()
        tools_section.append("Renombrar todo a estándar…", "win.rename-all")
        menu.append_section(None, tools_section)
        export_section = Gio.Menu()
        export_section.append("Exportar lista a CSV…", "win.export-csv")
        export_section.append("Exportar lista como texto…", "win.export-text")
        menu.append_section(None, export_section)
        menu.append("Preferencias", "win.preferences")
        menu.append("Acerca de", "win.about")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        self._add_action("preferences", self._on_preferences)
        self._add_action("about", self._on_about)
        self._add_action("add-files", self._on_add_files)
        self._add_action("add-folder", self._on_add_folder)
        self._add_action("rename-all", self._on_rename_all)
        self._add_action("export-csv", lambda: self._on_export(library.EXPORT_CSV))
        self._add_action("export-text", lambda: self._on_export(library.EXPORT_TEXT))

        # Barra de búsqueda
        self.search_entry = Gtk.SearchEntry(placeholder_text="Buscar por título o ID…")
        self.search_entry.connect("search-changed", self._on_search_changed)
        search_bar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, margin_start=12,
                                  margin_end=12, margin_top=8, margin_bottom=8)
        search_bar_box.append(self.search_entry)
        self.search_entry.set_hexpand(True)

        self.sort_dropdown = Gtk.DropDown.new_from_strings(
            [label for label, _fn, _rev in SORT_OPTIONS]
        )
        self.sort_dropdown.set_tooltip_text("Ordenar por")
        self.sort_dropdown.connect("notify::selected", self._on_sort_changed)
        search_bar_box.append(self.sort_dropdown)

        self.progress_bar = Gtk.ProgressBar(visible=False, show_text=True, hexpand=True)
        self.progress_cancel_btn = Gtk.Button(label="Cancelar", visible=False)
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
            title="Sin juegos todavía",
            description="Agregá tus ISO/WBFS o elegí una carpeta de biblioteca en Preferencias.",
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

        self.view_stack.add_titled_with_icon(content_box, "library", "Biblioteca",
                                              "view-list-symbolic")

        self.transfer_view = TransferView(self.settings, self._show_toast, self.ops)
        self.view_stack.add_titled_with_icon(self.transfer_view, "transfer", "Transferir",
                                              "drive-removable-media-symbolic")

        self.log_view = LogView(self.op_log, self._show_toast)
        self.view_stack.add_titled_with_icon(self.log_view, "log", "Log",
                                              "document-open-recent-symbolic")

        toolbar_view.set_content(self.view_stack)

        if not wit_wrapper.is_available(self.settings.wit_binary):
            banner = Adw.Banner(
                title="No se encontró 'wit' (Wiimms ISO Tools): la conversión y "
                      "los WBFS multi-juego estarán limitados. Ver README para instalarlo.",
                revealed=True,
            )
            toolbar_view.add_top_bar(banner)

        self._library_banner = Adw.Banner(button_label="Reintentar")
        self._library_banner.connect("button-clicked", lambda *_: self.rescan_library())
        toolbar_view.add_top_bar(self._library_banner)
        self._update_library_banner()

        # Barra de acciones en lote: aparece al activar el modo selección.
        self._selection_bar = Gtk.ActionBar()
        self._sel_count_label = Gtk.Label(label="0 seleccionados")
        self._selection_bar.pack_start(self._sel_count_label)
        self._selection_bar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        self._batch_send_btn = Gtk.Button(label="Enviar a unidad WBFS")
        self._batch_send_btn.connect("clicked", lambda *_: self._on_batch_send())
        self._selection_bar.pack_start(self._batch_send_btn)

        self._batch_convert_btn = Gtk.Button(label="Convertir")
        self._batch_convert_btn.connect("clicked", lambda *_: self._on_batch_convert())
        self._selection_bar.pack_start(self._batch_convert_btn)

        self._batch_rename_btn = Gtk.Button(label="Renombrar")
        self._batch_rename_btn.set_tooltip_text(
            "Renombrar los archivos elegidos a 'Título [ID].ext'"
        )
        self._batch_rename_btn.connect("clicked", lambda *_: self._on_batch_rename())
        self._selection_bar.pack_start(self._batch_rename_btn)

        self._batch_verify_btn = Gtk.Button(label="Verificar")
        self._batch_verify_btn.connect("clicked", lambda *_: self._on_batch_verify())
        self._selection_bar.pack_start(self._batch_verify_btn)

        self._batch_delete_btn = Gtk.Button(label="Eliminar")
        self._batch_delete_btn.add_css_class("destructive-action")
        self._batch_delete_btn.connect("clicked", lambda *_: self._on_batch_delete())
        self._selection_bar.pack_start(self._batch_delete_btn)

        cancel_selection_btn = Gtk.Button(icon_name="window-close-symbolic")
        cancel_selection_btn.set_tooltip_text("Cancelar selección")
        cancel_selection_btn.connect("clicked", lambda *_: self.select_toggle.set_active(False))
        self._selection_bar.pack_end(cancel_selection_btn)

        self._selection_bar.set_revealed(False)
        toolbar_view.add_bottom_bar(self._selection_bar)
        self._update_selection_bar()

    def _add_action(self, name: str, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", lambda *_: callback())
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
        return (f"Hay una operación en curso: {blocker.label}. "
                "Esperá a que termine.")

    def _update_selection_bar(self):
        if self._suspend_selection_updates:
            return
        games = self._selected_games()
        count = len(games)
        if count:
            total_size = library.format_size(sum(g.size_bytes for g in games))
            self._sel_count_label.set_label(f"{count} seleccionado(s) · {total_size}")
        else:
            self._sel_count_label.set_label("0 seleccionado(s)")

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
                 "Volver a escanear la biblioteca"),
                (self._add_button, OperationKind.IMPORTING, [],
                 "Agregar juegos (ISO/WBFS)")):
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
        return f"{len(games)} juegos"

    @staticmethod
    def _batch_outcome(target: str, ok: int, errors: list[str],
                        skipped: list[str] | None = None,
                        cancelled: bool = False) -> OperationOutcome:
        """Traduce el recuento de un lote al resultado que va al historial.

        Cancelado gana sobre todo lo demás (lo pidió el usuario, no es un
        fallo); después, un lote donde algo salió bien y algo no es
        "parcial" y no "error", que es la diferencia entre "no se copió
        nada" y "se copiaron 18 de 20"."""
        skipped = skipped or []
        detail_parts = [f"{ok} ok"]
        if skipped:
            detail_parts.append(f"{len(skipped)} omitido(s)")
        if errors:
            detail_parts.append(f"{len(errors)} con error")
            # Los primeros motivos concretos: son el dato por el que
            # alguien abre el historial después de que algo falle.
            detail_parts.append("; ".join(errors[:3]))

        if cancelled:
            status = oplog.STATUS_CANCELLED
        elif not errors:
            status = oplog.STATUS_OK
        elif ok:
            status = oplog.STATUS_PARTIAL
        else:
            status = oplog.STATUS_ERROR
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
            self._show_toast(f"No se puede ahora: {e.detail}.")
            return True
        return False

    def _on_progress_cancel_clicked(self, *_):
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
        self.set_title(f"WiiBackup Manager — {title}…")
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
                    kind: OperationKind, cancel_message: str = "Cancelando…"):
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
            self._show_toast(f"No se puede ahora: {e.detail}.")
            return

        cancel = self._begin_cancellable_progress(title, cancel_message)
        total = len(games)

        def worker():
            ok = 0
            errors: list[str] = []
            skipped: list[str] = []
            cancelled = False
            for i, game in enumerate(games, start=1):
                if cancel.cancelled:
                    cancelled = True
                    break
                try:
                    action_fn(game, cancel)
                    ok += 1
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
                          self._describe_target(games), cancelled)

        threading.Thread(target=worker, daemon=True).start()

    def _on_batch_done(self, title: str, ok: int, errors: list[str],
                        skipped: list[str] | None = None, op=None,
                        target: str = "", cancelled: bool = False):
        # Terminar la operación ANTES del rescan de abajo: si no, el escaneo
        # chocaría con ella y quedaría postergado. El resultado que se le
        # pasa acá es lo que queda anotado en la pestaña Log.
        self.ops.finish(op, self._batch_outcome(target, ok, errors, skipped, cancelled))
        skipped = skipped or []
        self._hide_progress()
        if cancelled:
            self._show_toast(
                f"{title}: cancelado tras {ok} completado(s)"
                + (f", {len(errors)} con error" if errors else "")
                + (f", {len(skipped)} omitido(s)" if skipped else "")
                + "."
            )
            self.rescan_library()
            return False
        parts = [f"{ok} ok" if (errors or skipped) else f"{ok} completado(s) ✓"]
        if skipped:
            preview = "; ".join(skipped[:3])
            more = f" (+{len(skipped) - 3} más)" if len(skipped) > 3 else ""
            parts.append(f"{len(skipped)} omitido(s): {preview}{more}")
        if errors:
            preview = "; ".join(errors[:3])
            more = f" (+{len(errors) - 3} más)" if len(errors) > 3 else ""
            parts.append(f"{len(errors)} con error: {preview}{more}")
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
        dialog = Gtk.FileDialog(title="Elegí la unidad/carpeta destino (WBFS)")
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
        if any(g.fmt.upper() != "WBFS" for g in games) and \
                not wit_wrapper.is_available(self.settings.wit_binary):
            self._show_toast(
                "No se encontró 'wit'; no se puede convertir a WBFS los que no lo son ya."
            )
            return

        # Con un solo juego (flujo individual) se pregunta antes de pisar
        # un destino que ya existe, igual que al convertir. En lote no
        # tiene sentido preguntar por cada uno: el worker los omite y los
        # informa aparte en el resumen final.
        if len(games) == 1:
            try:
                dest = library.wbfs_dest_path(games[0], dest_root)
            except ValueError:
                dest = None
            if dest is not None and dest.exists():
                gtk_helpers.confirm_overwrite(
                    self,
                    f"Ya existe un archivo en:\n{dest}\n\n"
                    f"Enviar '{games[0].title}' lo va a reemplazar. "
                    "Esta acción no se puede deshacer.",
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
            self._show_toast(f"No se puede ahora: {e.detail}.")
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
            eta_text = f" · ~{library.format_eta(remaining / speed)} restantes" if speed > 0 else ""
        elif total > 1:
            eta_text = " · calculando tiempo restante…"
        else:
            eta_text = ""
        self.progress_bar.set_text(f"{done}/{total} · {title}{eta_text}")
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
                f"Envío a unidad WBFS cancelado: {ok} ok, {len(errors)} con error"
                + (f", {len(skipped)} omitido(s)" if skipped else "")
                + " antes de cancelar."
            )
        elif errors or skipped:
            parts = [f"{ok} ok"]
            if skipped:
                preview = "; ".join(skipped[:3])
                more = f" (+{len(skipped) - 3} más)" if len(skipped) > 3 else ""
                parts.append(f"{len(skipped)} ya estaban en el destino: {preview}{more}")
            if errors:
                preview = "; ".join(errors[:3])
                more = f" (+{len(errors) - 3} más)" if len(errors) > 3 else ""
                parts.append(f"{len(errors)} con error: {preview}{more}")
            self._show_toast("Enviando a unidad WBFS: " + " · ".join(parts))
        else:
            self._show_toast(f"Enviando a unidad WBFS: {ok} completado(s) ✓")
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
            self._show_toast("No se encontró 'wit'. Instalalo para poder convertir (ver README).")
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
            self._show_toast(f"No se puede ahora: {e.detail}.")
            return

        cancel = self._begin_cancellable_progress(
            "Convirtiendo", "Cancelando la conversión…")

        wit_binary = self.settings.wit_binary

        def worker():
            # Los tamaños de SALIDA (no los de los archivos de origen): el
            # progreso de `wit` cuenta bytes escritos en el destino. Se
            # calcula acá y no en el hilo de GTK, igual que en la
            # transferencia.
            GLib.idle_add(self.progress_bar.set_text, "Calculando…")
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
            ejemplos += f"\n… y {len(pendientes) - 6} más"
        archivos = "archivo" if len(pendientes) == 1 else "archivos"

        dialog = Adw.AlertDialog(
            heading=f"¿Renombrar {len(pendientes)} {archivos}?",
            body=f"Se van a renombrar los archivos en el disco:\n\n{ejemplos}\n\n"
                 "Si el nombre que corresponde ya está ocupado por otro juego, "
                 "el archivo se guarda con un sufijo (2) en vez de pisarlo.",
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("rename", "Renombrar")
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
                # Se renombró igual (cuenta como hecho), pero con otro
                # nombre del esperado: eso no puede quedar solo en el
                # resumen, que dice cuántos y no cuáles. Va al historial,
                # que es donde se mira qué pasó con cada juego.
                self.op_log.record(
                    OperationKind.RENAMING.label, g.title, oplog.STATUS_PARTIAL,
                    f"guardado como {nuevo.name}: '{esperado}' ya estaba ocupado",
                )

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
            self._show_toast("No se encontró 'wit'. Instalalo para poder verificar (ver README).")
            return

        def verify_one(g: Game, cancel):
            ok, _output = wit_wrapper.verify(g.path, self.settings.wit_binary,
                                              cancel=cancel)
            if not ok:
                raise RuntimeError("verificación con errores")

        self._run_batch(games, "Verificando", verify_one, OperationKind.VERIFYING,
                         cancel_message="Cancelando la verificación…")

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
        dialog = Adw.AlertDialog(
            heading="¿Eliminar los juegos seleccionados?",
            body=f"Se van a borrar {len(games)} archivo(s):\n{names}\n\n"
                 "Esta acción no se puede deshacer.",
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("delete", "Eliminar")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_batch_delete_confirmed, games)
        dialog.present(self)

    def _on_batch_delete_confirmed(self, dialog, response, games: list[Game]):
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
            g.path.unlink()

        self._run_batch(games, "Eliminando", delete_one, OperationKind.DELETING,
                         cancel_message="Cancelando el borrado…")

    # -------------------------------------------------------- Library --
    @staticmethod
    def _describe_skipped(skipped: list) -> str:
        """Texto para avisar qué carpetas quedaron afuera del escaneo."""
        nombres = []
        for path in skipped:
            if str(path) not in nombres:
                nombres.append(str(path))
        preview = ", ".join(nombres[:2])
        mas = f" (+{len(nombres) - 2} más)" if len(nombres) > 2 else ""
        carpeta = "carpeta" if len(nombres) == 1 else "carpetas"
        return (f"No se pudo leer {len(nombres)} {carpeta} (permisos): "
                f"{preview}{mas}. Los juegos que haya adentro no aparecen.")

    def _update_library_banner(self):
        if self._library_available:
            self._library_banner.set_revealed(False)
        else:
            self._library_banner.set_title(
                f"Unidad no disponible: {self.settings.library_path} no está "
                "conectada. Conectala y se detectará automáticamente."
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
        self.set_title("WiiBackup Manager — Escaneando…")

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
            self.set_title(f"WiiBackup Manager — {len(self._games)} juegos")
            self._show_toast(
                f"No se pudo escanear la biblioteca ({error}). "
                "Se sigue mostrando la última lista conocida."
            )
            self.op_log.record(OperationKind.SCANNING.label,
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
            self.op_log.record(OperationKind.SCANNING.label,
                               f"{len(ilegibles)} carpeta(s) sin permiso",
                               oplog.STATUS_PARTIAL, mensaje)
        self._skipped_dirs = ilegibles

        self._games = games
        self._apply_sort()
        self.progress_bar.set_visible(False)
        self.set_title(f"WiiBackup Manager — {len(games)} juegos")
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
        noun = "juego" if count == 1 else "juegos"
        self.library_status_label.set_label(f"{count} {noun} · {total_size}")

    def _make_row(self, game: Game) -> GameRow:
        row = GameRow(game, self.settings.cover_region)
        row.connect("rename-requested", self._on_rename_requested)
        row.connect("convert-requested", self._on_convert_requested)
        row.connect("verify-requested", self._on_verify_requested)
        row.connect("delete-requested", self._on_delete_requested)
        row.connect("selection-toggled", lambda *_: self._update_selection_bar())
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

    def _on_sort_changed(self, *_):
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
            self._show_toast("No hay juegos para exportar.")
            return

        extension = "csv" if fmt == library.EXPORT_CSV else "txt"
        dialog = Gtk.FileDialog(title="Guardar la lista de juegos")
        dialog.set_initial_name(
            f"biblioteca-wii-{time.strftime('%Y-%m-%d')}.{extension}")
        dialog.set_initial_folder(gtk_helpers.safe_initial_folder())
        filtro = Gtk.FileFilter()
        if fmt == library.EXPORT_CSV:
            filtro.set_name("Planilla CSV (*.csv)")
            filtro.add_pattern("*.csv")
        else:
            filtro.set_name("Texto plano (*.txt)")
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
            self._show_toast(f"No se pudo guardar la lista: {e}")
            return

        frase = ("1 juego exportado" if len(juegos) == 1
                  else f"{len(juegos)} juegos exportados")
        self._show_toast(f"{frase} a {destino.name}")

    # ---------------------------------------------------------- Filter --
    def _on_search_changed(self, entry):
        self.list_box.invalidate_filter()

    def _filter_row(self, row: GameRow) -> bool:
        query = self.search_entry.get_text().strip().lower()
        if not query:
            return True
        return query in row.game.title.lower() or query in row.game.game_id.lower()

    # ----------------------------------------------------------- Actions --
    def _on_add_files(self, *_):
        dialog = Gtk.FileDialog(title="Agregar archivos")
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

    def _on_add_folder(self, *_):
        dialog = Gtk.FileDialog(title="Agregar carpeta completa")
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
            self._show_toast("No se encontraron archivos válidos en esa carpeta.")
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
                "No se encontraron archivos ISO/WBFS/CISO/WDF válidos en lo soltado."
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
        import shutil as _shutil

        dest_dir = Path(self.settings.library_path)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._show_toast(f"No se pudo escribir en la carpeta de biblioteca: {e}")
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
            self._show_toast(f"No se puede ahora: {e.detail}.")
            return

        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        self.set_title("WiiBackup Manager — Agregando…")

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
                        _shutil.copy2(src, dest)
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
                parts.append(f"{len(skipped)} omitido(s) por ya existir en la biblioteca")
        if renamed:
            # Se informa aparte: el archivo entró, pero con otro nombre que
            # el que tenía, y sin eso el usuario no tendría cómo saberlo.
            if len(renamed) <= 3:
                parts.append("Ya había un archivo con el mismo nombre, se guardó como: "
                             + ", ".join(renamed))
            else:
                parts.append(f"{len(renamed)} se guardaron con otro nombre para no pisar "
                             "archivos que ya estaban")
        if errors:
            preview = "; ".join(errors[:2])
            mas = f" (+{len(errors) - 2} más, ver la pestaña Log)" if len(errors) > 2 else ""
            parts.append(f"{len(errors)} con error: {preview}{mas}")
        if not parts:
            parts.append("No se agregó ningún juego nuevo")
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
            self.op_log.record(OperationKind.RENAMING.label, row.game.title,
                                oplog.STATUS_ERROR, str(e))
            return
        self._show_toast(f"Renombrado a: {new_path.name}")
        self.op_log.record(OperationKind.RENAMING.label, row.game.title,
                            oplog.STATUS_OK, f"a {new_path.name}")
        self.rescan_library()

    def _on_convert_requested(self, row: GameRow):
        game = row.game
        if not wit_wrapper.is_available(self.settings.wit_binary):
            self._show_toast("No se encontró 'wit'. Instalalo para poder convertir (ver README).")
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
                f"Ya existe un archivo en:\n{dest.name}\n\n"
                f"Convertir '{game.title}' lo va a reemplazar. "
                "Esta acción no se puede deshacer.",
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
            self._show_toast(f"No se puede ahora: {e.detail}.")
            return

        # Con botón de cancelar: convertir un dual-layer puede tardar
        # varios minutos y hasta ahora la única salida era cerrar la app.
        cancel = self._begin_cancellable_progress(
            "Convirtiendo", "Cancelando la conversión…")
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
                    result = wit_wrapper.convert(game.path, dest, target_ext.strip("."),
                                                  self.settings.wit_binary,
                                                  bytes_progress_cb=on_progress,
                                                  cancel=cancel)
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
            self._show_toast("No se encontró 'wit'. Instalalo para poder verificar (ver README).")
            return

        # Verificar solo lee, así que convive con otra lectura del mismo
        # archivo; lo que no puede es leer algo que se está reescribiendo.
        try:
            op = self.ops.start(OperationKind.VERIFYING, read=[game.path])
        except OperationBusy as e:
            self._show_toast(f"No se puede ahora: {e.detail}.")
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
        dialog = Adw.AlertDialog(
            heading="¿Eliminar este juego?",
            body=f"Se borrará el archivo:\n{game.path.name}\n\nEsta acción no se puede deshacer.",
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("delete", "Eliminar")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_delete_confirmed, game)
        dialog.present(self)

    def _on_delete_confirmed(self, dialog, response, game: Game):
        if response != "delete":
            return
        # Revalidar: entre que se abrió el diálogo y el usuario confirmó
        # pudo arrancar una conversión o una transferencia sobre este mismo
        # archivo, y borrarlo abajo de esa operación la rompe.
        if self._reject_if_busy(OperationKind.DELETING, write=[game.path]):
            return
        try:
            game.path.unlink()
            self._show_toast(f"Eliminado: {game.path.name}")
            self.op_log.record(OperationKind.DELETING.label, game.title,
                                oplog.STATUS_OK, game.path.name)
        except OSError as e:
            self._show_toast(f"No se pudo eliminar: {e}")
            self.op_log.record(OperationKind.DELETING.label, game.title,
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
            comments="Gestor de respaldos de Wii (ISO/WBFS) para Linux, "
                     "inspirado en Wii Backup Manager de Windows.",
            website="https://github.com/",
        )
        about.present(self)

    def _show_toast(self, message: str):
        self._toast_overlay.add_toast(Adw.Toast(title=message, timeout=3))
