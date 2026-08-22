from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, GLib, Gio  # noqa: E402

from . import config, library, wit_wrapper
from .library import Game
from .widgets.game_row import GameRow
from .widgets.preferences_dialog import PreferencesDialog


class WiiBackupWindow(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app)
        self.set_title("WiiBackup Manager")
        self.set_default_size(820, 620)

        self.settings = config.Settings.load()
        config.ensure_dirs(self.settings)

        self._games: list[Game] = []
        self._rows: dict[str, GameRow] = {}

        self._build_ui()

        if self.settings.auto_scan_on_start:
            GLib.idle_add(self.rescan_library)

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self._toast_overlay.set_child(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        self.title_widget = Adw.WindowTitle(title="WiiBackup Manager", subtitle="")
        header.set_title_widget(self.title_widget)

        add_button = Gtk.Button(icon_name="list-add-symbolic")
        add_button.set_tooltip_text("Agregar juegos (ISO/WBFS)")
        add_button.connect("clicked", self._on_add_games)
        header.pack_start(add_button)

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.set_tooltip_text("Volver a escanear la biblioteca")
        refresh_button.connect("clicked", lambda *_: self.rescan_library())
        header.pack_start(refresh_button)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu = Gio.Menu()
        menu.append("Preferencias", "win.preferences")
        menu.append("Acerca de", "win.about")
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)

        self._add_action("preferences", self._on_preferences)
        self._add_action("about", self._on_about)

        # Barra de búsqueda
        self.search_entry = Gtk.SearchEntry(placeholder_text="Buscar por título o ID…")
        self.search_entry.connect("search-changed", self._on_search_changed)
        search_bar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, margin_start=12,
                                  margin_end=12, margin_top=8, margin_bottom=8)
        search_bar_box.append(self.search_entry)
        self.search_entry.set_hexpand(True)

        self.progress_bar = Gtk.ProgressBar(visible=False)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.append(search_bar_box)
        content_box.append(self.progress_bar)

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

        toolbar_view.set_content(content_box)

        if not wit_wrapper.is_available(self.settings.wit_binary):
            banner = Adw.Banner(
                title="No se encontró 'wit' (Wiimms ISO Tools): la conversión y "
                      "los WBFS multi-juego estarán limitados. Ver README para instalarlo.",
                revealed=True,
            )
            toolbar_view.add_top_bar(banner)

    def _add_action(self, name: str, callback):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", lambda *_: callback())
        self.add_action(action)

    # ------------------------------------------------------------ Scan --
    def rescan_library(self):
        root = Path(self.settings.library_path)
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        self.title_widget.set_subtitle("Escaneando…")

        def worker():
            def progress(done, total):
                GLib.idle_add(self.progress_bar.set_fraction, done / max(total, 1))

            games = library.scan_library(root, self.settings.wit_binary, progress)
            GLib.idle_add(self._on_scan_done, games)

        threading.Thread(target=worker, daemon=True).start()
        return False  # para idle_add

    def _on_scan_done(self, games: list[Game]):
        self._games = games
        self.progress_bar.set_visible(False)
        self.title_widget.set_subtitle(f"{len(games)} juegos")
        self._populate_list()
        self.stack.set_visible_child_name("list" if games else "empty")
        return False

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
            self.list_box.append(row)
            self._rows[str(game.path)] = row
            row.load_cover_async()

    # ---------------------------------------------------------- Filter --
    def _on_search_changed(self, entry):
        self.list_box.invalidate_filter()

    def _filter_row(self, row: GameRow) -> bool:
        query = self.search_entry.get_text().strip().lower()
        if not query:
            return True
        return query in row.game.title.lower() or query in row.game.game_id.lower()

    # ----------------------------------------------------------- Actions --
    def _on_add_games(self, *_):
        dialog = Gtk.FileDialog(title="Agregar juegos")
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

        import shutil as _shutil
        dest_dir = Path(self.settings.library_path)
        dest_dir.mkdir(parents=True, exist_ok=True)

        def worker():
            n = files.get_n_items()
            for i in range(n):
                f = files.get_item(i)
                src = Path(f.get_path())
                dest = dest_dir / src.name
                if src.resolve() != dest.resolve():
                    try:
                        _shutil.copy2(src, dest)
                    except OSError:
                        continue
            GLib.idle_add(self.rescan_library)

        threading.Thread(target=worker, daemon=True).start()

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
