from __future__ import annotations

import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gtk, GLib, Gio, Gdk  # noqa: E402

from . import config, library, wit_wrapper

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
from .widgets.preferences_dialog import PreferencesDialog
from .widgets.transfer_view import TransferView


class WiiBackupWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app)
        self.set_title("WiiBackup Manager")
        self.set_default_size(820, 620)

        self.settings = config.Settings.load()
        config.ensure_dirs(self.settings)

        self._games: list[Game] = []
        self._rows: dict[str, GameRow] = {}
        self._library_available = config.library_path_available(self.settings)
        self._send_cancelled = False

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

        add_button = Gtk.MenuButton(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("Agregar juegos (ISO/WBFS)")
        add_menu = Gio.Menu()
        add_menu.append("Agregar archivos", "win.add-files")
        add_menu.append("Agregar carpeta completa", "win.add-folder")
        add_button.set_menu_model(add_menu)
        header.pack_start(add_button)

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.set_tooltip_text("Volver a escanear la biblioteca")
        refresh_button.connect("clicked", lambda *_: self.rescan_library())
        header.pack_start(refresh_button)

        self.select_toggle = Gtk.ToggleButton(icon_name="object-select-symbolic")
        self.select_toggle.set_tooltip_text("Selección múltiple")
        self.select_toggle.connect("toggled", self._on_select_mode_toggled)
        header.pack_start(self.select_toggle)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu = Gio.Menu()
        menu.append("Preferencias", "win.preferences")
        menu.append("Acerca de", "win.about")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        self._add_action("preferences", self._on_preferences)
        self._add_action("about", self._on_about)
        self._add_action("add-files", self._on_add_files)
        self._add_action("add-folder", self._on_add_folder)

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

        self.transfer_view = TransferView(self.settings, self._show_toast)
        self.view_stack.add_titled_with_icon(self.transfer_view, "transfer", "Transferir",
                                              "drive-removable-media-symbolic")

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
        return [row.game for row in self._rows.values() if row.is_selected()]

    def _update_selection_bar(self):
        games = self._selected_games()
        count = len(games)
        if count:
            total_size = library.format_size(sum(g.size_bytes for g in games))
            self._sel_count_label.set_label(f"{count} seleccionado(s) · {total_size}")
        else:
            self._sel_count_label.set_label("0 seleccionado(s)")
        has_selection = count > 0
        for btn in (self._batch_send_btn, self._batch_convert_btn,
                    self._batch_verify_btn, self._batch_delete_btn):
            btn.set_sensitive(has_selection)

    def _on_progress_cancel_clicked(self, *_):
        self._send_cancelled = True
        self.progress_cancel_btn.set_sensitive(False)

    # ------------------------------------------------------ Acciones en lote --
    def _run_batch(self, games: list[Game], title: str, action_fn):
        """Corre `action_fn(game)` para cada juego en un hilo aparte,
        mostrando progreso y un resumen final de éxitos/errores. Se reusa
        para enviar a WBFS, convertir, verificar y eliminar en lote."""
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        self.set_title(f"WiiBackup Manager — {title}…")
        total = len(games)

        def worker():
            ok = 0
            errors: list[str] = []
            for i, game in enumerate(games, start=1):
                try:
                    action_fn(game)
                    ok += 1
                except Exception as e:
                    errors.append(f"{game.title}: {e}")
                GLib.idle_add(self.progress_bar.set_fraction, i / max(total, 1))
            GLib.idle_add(self._on_batch_done, title, ok, errors)

        threading.Thread(target=worker, daemon=True).start()

    def _on_batch_done(self, title: str, ok: int, errors: list[str]):
        self.progress_bar.set_visible(False)
        if errors:
            preview = "; ".join(errors[:3])
            more = f" (+{len(errors) - 3} más)" if len(errors) > 3 else ""
            self._show_toast(f"{title}: {ok} ok, {len(errors)} con error: {preview}{more}")
        else:
            self._show_toast(f"{title}: {ok} completado(s) ✓")
        self.rescan_library()
        return False

    def _on_batch_send(self):
        games = self._selected_games()
        if not games:
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

        # Worker dedicado (no _run_batch genérico) porque acá sí tiene
        # sentido mostrar tiempo estimado y permitir cancelar: es la
        # operación de transferencia real hacia una unidad WBFS, con
        # tamaños de archivo conocidos de antemano.
        self._send_cancelled = False
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        self.progress_cancel_btn.set_visible(True)
        self.progress_cancel_btn.set_sensitive(True)
        self.set_title("WiiBackup Manager — Enviando a unidad WBFS…")

        total = len(games)
        total_bytes = sum(g.size_bytes for g in games)
        wit_binary = self.settings.wit_binary

        def worker():
            ok = 0
            errors: list[str] = []
            bytes_done = 0
            start_time = time.monotonic()
            cancelled = False
            for i, game in enumerate(games, start=1):
                if self._send_cancelled:
                    cancelled = True
                    break
                GLib.idle_add(self._update_send_progress, i, total, game.title,
                              bytes_done, total_bytes, start_time)
                try:
                    library.send_to_wbfs_drive(game, dest_root, wit_binary)
                    ok += 1
                except Exception as e:
                    # No frena el resto de la selección: se cuenta como
                    # error y se sigue con el siguiente juego.
                    errors.append(f"{game.title}: {e}")
                bytes_done += game.size_bytes
            GLib.idle_add(self._on_send_done, ok, errors, cancelled)

        threading.Thread(target=worker, daemon=True).start()

    def _update_send_progress(self, done: int, total: int, title: str,
                               bytes_done: int, total_bytes: int, start_time: float):
        self.progress_bar.set_fraction((done - 1) / max(total, 1))
        elapsed = time.monotonic() - start_time
        if bytes_done > 0 and elapsed > 1:
            speed = bytes_done / elapsed
            remaining = max(total_bytes - bytes_done, 0)
            eta_text = f" · ~{library.format_eta(remaining / speed)} restantes" if speed > 0 else ""
        elif total > 1:
            eta_text = " · calculando tiempo restante…"
        else:
            eta_text = ""
        self.progress_bar.set_text(f"{done}/{total} · {title}{eta_text}")
        return False

    def _on_send_done(self, ok: int, errors: list[str], cancelled: bool):
        self.progress_bar.set_visible(False)
        # Volver a None (no "") es lo que hace que el ProgressBar caiga de
        # nuevo a mostrar el porcentaje en las demás operaciones (rescan,
        # importar, convertir/verificar/eliminar en lote) en vez de dejar
        # pegado el último "N/total · nombre del juego" de este envío.
        self.progress_bar.set_text(None)
        self.progress_cancel_btn.set_visible(False)
        if cancelled:
            self._show_toast(
                f"Envío a unidad WBFS cancelado: {ok} ok, {len(errors)} con error antes de cancelar."
            )
        elif errors:
            preview = "; ".join(errors[:3])
            more = f" (+{len(errors) - 3} más)" if len(errors) > 3 else ""
            self._show_toast(f"Enviando a unidad WBFS: {ok} ok, {len(errors)} con error: {preview}{more}")
        else:
            self._show_toast(f"Enviando a unidad WBFS: {ok} completado(s) ✓")
        self.rescan_library()
        return False

    def _on_batch_convert(self):
        games = self._selected_games()
        if not games:
            return
        if not wit_wrapper.is_available(self.settings.wit_binary):
            self._show_toast("No se encontró 'wit'. Instalalo para poder convertir (ver README).")
            return

        def convert_one(g: Game):
            target_ext = ".wbfs" if g.fmt.upper() != "WBFS" else ".iso"
            dest = g.path.with_suffix(target_ext)
            result = wit_wrapper.convert(g.path, dest, target_ext.strip("."),
                                          self.settings.wit_binary)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip()[:200] or "error de wit")

        self._run_batch(games, "Convirtiendo", convert_one)

    def _on_batch_verify(self):
        games = self._selected_games()
        if not games:
            return
        if not wit_wrapper.is_available(self.settings.wit_binary):
            self._show_toast("No se encontró 'wit'. Instalalo para poder verificar (ver README).")
            return

        def verify_one(g: Game):
            ok, _output = wit_wrapper.verify(g.path, self.settings.wit_binary)
            if not ok:
                raise RuntimeError("verificación con errores")

        self._run_batch(games, "Verificando", verify_one)

    def _on_batch_delete(self):
        games = self._selected_games()
        if not games:
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

        def delete_one(g: Game):
            g.path.unlink()

        self._run_batch(games, "Eliminando", delete_one)

    # -------------------------------------------------------- Library --
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
        root = Path(self.settings.library_path)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        self.set_title("WiiBackup Manager — Escaneando…")

        def worker():
            def progress(done, total):
                GLib.idle_add(self.progress_bar.set_fraction, done / max(total, 1))

            games = library.scan_library(root, self.settings.wit_binary, progress)
            GLib.idle_add(self._on_scan_done, games)

        threading.Thread(target=worker, daemon=True).start()
        return False  # para idle_add

    def _on_scan_done(self, games: list[Game]):
        self._games = games
        self._apply_sort()
        self.progress_bar.set_visible(False)
        self.set_title(f"WiiBackup Manager — {len(games)} juegos")
        self._populate_list()
        self.stack.set_visible_child_name("list" if games else "empty")
        self.transfer_view.set_games(games)
        self._update_library_status_bar()
        return False

    def _update_library_status_bar(self):
        count = len(self._games)
        total_size = library.format_size(sum(g.size_bytes for g in self._games))
        noun = "juego" if count == 1 else "juegos"
        self.library_status_label.set_label(f"{count} {noun} · {total_size}")

    def _populate_list(self):
        child = self.list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.list_box.remove(child)
            child = nxt
        self._rows.clear()

        for game in self._games:
            row = GameRow(game, self.settings.cover_region)
            row.connect("rename-requested", self._on_rename_requested)
            row.connect("convert-requested", self._on_convert_requested)
            row.connect("verify-requested", self._on_verify_requested)
            row.connect("delete-requested", self._on_delete_requested)
            row.connect("selection-toggled", lambda *_: self._update_selection_bar())
            row.connect("detail-requested", self._on_game_detail_requested)
            row.set_selection_mode(self.select_toggle.get_active())
            self.list_box.append(row)
            self._rows[str(game.path)] = row
            row.load_cover_async()

    # ------------------------------------------------------------ Orden --
    def _apply_sort(self):
        idx = self.sort_dropdown.get_selected()
        if idx < 0 or idx >= len(SORT_OPTIONS):
            idx = 0
        _label, key_fn, reverse = SORT_OPTIONS[idx]
        self._games.sort(key=key_fn, reverse=reverse)

    def _on_sort_changed(self, *_):
        self._apply_sort()
        self._populate_list()

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
        dialog.select_folder(self, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if folder is None:
            return

        root = Path(folder.get_path())
        paths = sorted(
            p for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in library.VALID_EXTENSIONS
        )
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
        for f in files:
            raw_path = f.get_path()
            if not raw_path:
                continue
            p = Path(raw_path)
            if p.is_dir():
                paths.extend(
                    sorted(q for q in p.rglob("*")
                           if q.is_file() and q.suffix.lower() in library.VALID_EXTENSIONS)
                )
            elif p.is_file() and p.suffix.lower() in library.VALID_EXTENSIONS:
                paths.append(p)

        if not paths:
            self._show_toast(
                "No se encontraron archivos ISO/WBFS/CISO/WDF válidos en lo soltado."
            )
            return False

        self._start_import(paths)
        return True

    def _start_import(self, src_paths: list[Path]):
        """Copia `src_paths` a la biblioteca en background, identificando
        cada archivo primero para no duplicar juegos que ya están en el
        último escaneo (self._games), comparando por game_id."""
        import shutil as _shutil

        dest_dir = Path(self.settings.library_path)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._show_toast(f"No se pudo escribir en la carpeta de biblioteca: {e}")
            return

        known_ids = {g.game_id for g in self._games if g.game_id != "??????"}

        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        self.set_title("WiiBackup Manager — Agregando…")

        def worker():
            added: list[str] = []
            skipped: list[str] = []
            total = len(src_paths)

            for i, src in enumerate(src_paths, start=1):
                GLib.idle_add(self.progress_bar.set_fraction, i / max(total, 1))

                game = library.identify_file(src, self.settings.wit_binary)
                if game is None:
                    continue

                if game.game_id != "??????" and game.game_id in known_ids:
                    skipped.append(game.title)
                    continue

                dest = dest_dir / src.name
                if src.resolve() != dest.resolve():
                    try:
                        _shutil.copy2(src, dest)
                    except OSError:
                        continue

                if game.game_id != "??????":
                    known_ids.add(game.game_id)
                added.append(game.title)

            GLib.idle_add(self._on_import_done, added, skipped)

        threading.Thread(target=worker, daemon=True).start()

    def _on_import_done(self, added: list[str], skipped: list[str]):
        parts = []
        if added:
            parts.append(f"{len(added)} juego(s) nuevo(s) agregado(s)")
        if skipped:
            if len(skipped) <= 5:
                parts.append(f"{len(skipped)} omitido(s) por ya existir: " + ", ".join(skipped))
            else:
                parts.append(f"{len(skipped)} omitido(s) por ya existir en la biblioteca")
        if not parts:
            parts.append("No se agregó ningún juego nuevo")
        self._show_toast(". ".join(parts) + ".")
        self.rescan_library()
        return False

    def _on_game_detail_requested(self, row: GameRow):
        dialog = GameDetailDialog(row.game, self.settings.cover_region)
        dialog.present(self)

    def _on_rename_requested(self, row: GameRow):
        try:
            new_path = library.rename_to_standard(row.game)
        except FileExistsError as e:
            self._show_toast(str(e))
            return
        self._show_toast(f"Renombrado a: {new_path.name}")
        self.rescan_library()

    def _on_convert_requested(self, row: GameRow):
        game = row.game
        if not wit_wrapper.is_available(self.settings.wit_binary):
            self._show_toast("No se encontró 'wit'. Instalalo para poder convertir (ver README).")
            return

        target_ext = ".wbfs" if game.fmt.upper() != "WBFS" else ".iso"
        dest = game.path.with_suffix(target_ext)

        def worker():
            result = wit_wrapper.convert(game.path, dest, target_ext.strip("."),
                                          self.settings.wit_binary)
            ok = result.returncode == 0
            msg = f"Convertido a {dest.name}" if ok else f"Error al convertir: {result.stderr.strip()[:200]}"
            GLib.idle_add(self._show_toast, msg)
            if ok:
                GLib.idle_add(self.rescan_library)

        threading.Thread(target=worker, daemon=True).start()

    def _on_verify_requested(self, row: GameRow):
        game = row.game
        if not wit_wrapper.is_available(self.settings.wit_binary):
            self._show_toast("No se encontró 'wit'. Instalalo para poder verificar (ver README).")
            return

        def worker():
            ok, output = wit_wrapper.verify(game.path, self.settings.wit_binary)
            msg = f"'{game.title}' verificado OK ✓" if ok else f"'{game.title}' con errores ✗"
            GLib.idle_add(self._show_toast, msg)

        threading.Thread(target=worker, daemon=True).start()

    def _on_delete_requested(self, row: GameRow):
        game = row.game
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
        if response == "delete":
            try:
                game.path.unlink()
                self._show_toast(f"Eliminado: {game.path.name}")
            except OSError as e:
                self._show_toast(f"No se pudo eliminar: {e}")
            self.rescan_library()

    # ------------------------------------------------------------ Misc --
    def _on_preferences(self):
        dialog = PreferencesDialog(self.settings, self._on_settings_saved)
        dialog.present(self)

    def _on_settings_saved(self, settings: config.Settings):
        settings.save()
        config.ensure_dirs(settings)
        self._library_available = config.library_path_available(settings)
        self._update_library_banner()
        self.rescan_library()

    def _on_about(self):
        about = Adw.AboutDialog(
            application_name="WiiBackup Manager",
            application_icon="drive-harddisk-symbolic",
            version="0.1.0",
            developer_name="GameFix SPS",
            license_type=Gtk.License.MIT_X11,
            comments="Gestor de respaldos de Wii (ISO/WBFS) para Linux, "
                     "inspirado en Wii Backup Manager de Windows.",
            website="https://github.com/",
        )
        about.present(self)

    def _show_toast(self, message: str):
        self._toast_overlay.add_toast(Adw.Toast(title=message, timeout=3))
