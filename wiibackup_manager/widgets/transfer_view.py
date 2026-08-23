"""Pestaña 'Transferir': elegir un destino (USB/SD/carpeta) y copiar los
juegos seleccionados de la biblioteca hacia allí en formato WBFS."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, GLib  # noqa: E402

from .. import drives, gametdb, library
from ..library import Game
from .game_row import build_cover_widget


def _format_bytes(n: int) -> str:
    """GB con un decimal, o MB si es menos de 1 GB (evita mostrar '0.0 GB'
    para transferencias chicas)."""
    gb = n / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{n / (1024 ** 2):.1f} MB"


class TransferGameRow(Adw.ActionRow):
    """Fila de juego para la vista Transferir: igual que GameRow (carátula
    + título) pero con una casilla de selección en vez del menú de
    acciones, ya que acá no se edita el juego, solo se elige para copiar."""

    def __init__(self, game: Game, cover_region: str = "EN"):
        super().__init__()
        self.game = game
        self._cover_region = cover_region

        self.set_title(GLib.markup_escape_text(game.title))
        self.set_subtitle(f"{game.game_id} · {game.fmt} · {game.size_mb:,.0f} MB")

        self.check = Gtk.CheckButton()
        self.check.set_valign(Gtk.Align.CENTER)
        self.add_prefix(self.check)
        # Clickear la fila entera alterna la casilla, no solo el cuadradito.
        self.set_activatable_widget(self.check)

        # Mismo tamaño de carátula (y mismo placeholder) que en la
        # Biblioteca, para que ambas pestañas se vean consistentes.
        self._cover_widget, self._cover = build_cover_widget()
        self.add_prefix(self._cover_widget)

    def load_cover_async(self):
        """Igual que en GameRow: la carátula se busca (o se toma de caché)
        en un hilo aparte para no trabar la interfaz, y se aplica en el
        hilo principal de GTK vía GLib.idle_add."""

        def worker():
            path = gametdb.get_cover_path(self.game.game_id, self._cover_region)
            GLib.idle_add(self._apply_cover, str(path) if path else None)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_cover(self, path: str | None):
        if path:
            try:
                self._cover.set_filename(path)
            except GLib.Error:
                pass
        return False


class TransferView(Gtk.Box):
    def __init__(self, settings, show_toast_cb):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.settings = settings
        self._show_toast = show_toast_cb
        self._games: list[Game] = []
        self._game_rows: list[TransferGameRow] = []
        self._dest_path: Path | None = None

        self._build_ui()

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        # --- Sección de destino ---
        dest_group = Adw.PreferencesGroup(
            title="Destino",
            description="Elegí el disco, USB, SD o carpeta donde copiar los juegos.",
        )
        dest_group.set_margin_start(12)
        dest_group.set_margin_end(12)
        dest_group.set_margin_top(12)

        self.dest_list = Gtk.ListBox()
        self.dest_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.dest_list.add_css_class("boxed-list")
        self.dest_list.connect("row-selected", self._on_dest_row_selected)
        dest_group.add(self.dest_list)

        dest_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                                margin_start=12, margin_end=12, margin_bottom=8, margin_top=8)
        refresh_drives_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_drives_btn.set_tooltip_text("Volver a detectar unidades")
        refresh_drives_btn.connect("clicked", lambda *_: self._refresh_drives())
        add_folder_btn = Gtk.Button(label="Agregar carpeta")
        add_folder_btn.connect("clicked", self._on_add_folder)
        dest_buttons.append(refresh_drives_btn)
        dest_buttons.append(add_folder_btn)

        self.dest_space_label = Gtk.Label(
            label="Elegí un destino para ver el espacio disponible.", xalign=0
        )
        self.dest_space_label.add_css_class("dim-label")
        self.dest_space_label.set_margin_start(12)
        self.dest_space_label.set_margin_end(12)
        self.dest_space_label.set_margin_bottom(8)

        self.append(dest_group)
        self.append(dest_buttons)
        self.append(self.dest_space_label)
        self.append(Gtk.Separator(margin_top=4, margin_bottom=4))

        # --- Sección de juegos ---
        games_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                                margin_start=12, margin_end=12, margin_top=4, margin_bottom=4)
        games_label = Gtk.Label(label="Juegos", xalign=0)
        games_label.add_css_class("heading")
        games_label.set_hexpand(True)
        select_all_btn = Gtk.Button(label="Seleccionar todos")
        select_all_btn.connect("clicked", lambda *_: self._set_all_selected(True))
        deselect_all_btn = Gtk.Button(label="Deseleccionar todos")
        deselect_all_btn.connect("clicked", lambda *_: self._set_all_selected(False))
        games_header.append(games_label)
        games_header.append(select_all_btn)
        games_header.append(deselect_all_btn)
        self.append(games_header)

        self.game_list = Gtk.ListBox()
        self.game_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.game_list.add_css_class("boxed-list")

        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self.game_list)
        scroller.set_vexpand(True)
        scroller.set_margin_start(12)
        scroller.set_margin_end(12)
        scroller.set_margin_bottom(8)
        self.append(scroller)

        # --- Progreso + acción ---
        self.transfer_progress = Gtk.ProgressBar(visible=False, show_text=True)
        self.transfer_progress.set_margin_start(12)
        self.transfer_progress.set_margin_end(12)
        self.append(self.transfer_progress)

        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                              margin_start=12, margin_end=12, margin_top=8, margin_bottom=12)
        self.transfer_button = Gtk.Button(label="Transferir seleccionados")
        self.transfer_button.add_css_class("suggested-action")
        self.transfer_button.connect("clicked", self._on_transfer_clicked)
        action_box.append(self.transfer_button)
        self.append(action_box)

        self._refresh_drives()

    # ------------------------------------------------------------ Destino --
    def _refresh_drives(self):
        """Vuelve a detectar las unidades removibles montadas y repuebla la
        lista, preservando las carpetas que el usuario agregó a mano
        (esas no se "detectan", así que no deben desaparecer al refrescar)."""
        manual_rows = [row for row in self._iter_dest_rows() if getattr(row, "is_manual", False)]
        selected_path = self._dest_path

        child = self.dest_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.dest_list.remove(child)
            child = nxt

        for drive in drives.list_removable_drives():
            subtitle = f"{drive.free_gb:.1f} GB libres de {drive.total_gb:.1f} GB · {drive.mount_point}"
            row = Adw.ActionRow(title=drive.name, subtitle=subtitle)
            row.dest_path = drive.mount_point
            row.is_manual = False
            row.add_prefix(Gtk.Image.new_from_icon_name("drive-removable-media-symbolic"))
            self.dest_list.append(row)

        for row in manual_rows:
            self.dest_list.append(row)

        # Si el destino elegido sigue en la lista (unidad que ya estaba
        # conectada o carpeta manual), lo re-seleccionamos.
        if selected_path is not None:
            for row in self._iter_dest_rows():
                if getattr(row, "dest_path", None) == selected_path:
                    self.dest_list.select_row(row)
                    break

    def _iter_dest_rows(self):
        row = self.dest_list.get_first_child()
        while row is not None:
            yield row
            row = row.get_next_sibling()

    def _on_dest_row_selected(self, listbox, row):
        self._dest_path = getattr(row, "dest_path", None) if row else None
        self._update_dest_space_label()

    def _update_dest_space_label(self):
        if self._dest_path is None:
            self.dest_space_label.set_label("Elegí un destino para ver el espacio disponible.")
            return
        try:
            usage = shutil.disk_usage(self._dest_path)
        except OSError:
            self.dest_space_label.set_label("No se pudo leer el espacio disponible en el destino.")
            return
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        self.dest_space_label.set_label(
            f"Espacio en destino: {free_gb:.1f} GB libres de {total_gb:.1f} GB"
        )

    def _on_add_folder(self, *_):
        dialog = Gtk.FileDialog(title="Elegí la carpeta destino")
        dialog.select_folder(self.get_root(), None, self._on_folder_picked)

    def _on_folder_picked(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if not folder:
            return
        path = Path(folder.get_path())
        row = Adw.ActionRow(title=path.name or str(path), subtitle=str(path))
        row.dest_path = path
        row.is_manual = True
        row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic"))
        self.dest_list.append(row)
        self.dest_list.select_row(row)

    # ------------------------------------------------------------- Juegos --
    def set_games(self, games: list[Game]):
        """Sincroniza la lista de juegos disponibles para transferir; se
        llama cada vez que la biblioteca se vuelve a escanear en la
        ventana principal."""
        self._games = games
        child = self.game_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.game_list.remove(child)
            child = nxt
        self._game_rows.clear()

        for game in games:
            row = TransferGameRow(game, self.settings.cover_region)
            self.game_list.append(row)
            self._game_rows.append(row)
            row.load_cover_async()

    def _set_all_selected(self, value: bool):
        for row in self._game_rows:
            row.check.set_active(value)

    # -------------------------------------------------------- Transferir --
    def _on_transfer_clicked(self, *_):
        if self._dest_path is None:
            self._show_toast("Elegí primero una unidad o carpeta destino.")
            return

        selected = [row.game for row in self._game_rows if row.check.get_active()]
        if not selected:
            self._show_toast("No hay juegos seleccionados.")
            return

        # Chequeo de espacio ANTES de arrancar: el tamaño de origen es una
        # cota superior razonable del tamaño final en WBFS (la conversión
        # solo achica al descartar padding, nunca agranda), así que sirve
        # para avisar de antemano sin tener que copiar/convertir primero.
        total_bytes = sum(g.size_bytes for g in selected)
        try:
            free_bytes = shutil.disk_usage(self._dest_path).free
        except OSError:
            free_bytes = None

        if free_bytes is not None and total_bytes > free_bytes:
            self._show_toast(
                f"No hay espacio suficiente en el destino: se necesitan "
                f"{_format_bytes(total_bytes)} y hay {_format_bytes(free_bytes)} libres "
                f"(faltan {_format_bytes(total_bytes - free_bytes)}). "
                "Liberá espacio o elegí menos juegos."
            )
            return

        dest_root = self._dest_path
        wit_binary = self.settings.wit_binary
        self.transfer_button.set_sensitive(False)
        self.transfer_progress.set_fraction(0)
        self.transfer_progress.set_text(f"0/{len(selected)} transferidos")
        self.transfer_progress.set_visible(True)

        def worker():
            ok_count = 0
            err_count = 0
            total = len(selected)
            for i, game in enumerate(selected, start=1):
                try:
                    library.send_to_wbfs_drive(game, dest_root, wit_binary)
                    ok_count += 1
                except Exception:
                    err_count += 1
                GLib.idle_add(self._update_progress, i, total)
            GLib.idle_add(self._on_transfer_done, ok_count, err_count)

        threading.Thread(target=worker, daemon=True).start()

    def _update_progress(self, done: int, total: int):
        self.transfer_progress.set_fraction(done / max(total, 1))
        self.transfer_progress.set_text(f"{done}/{total} transferidos")
        return False

    def _on_transfer_done(self, ok_count: int, err_count: int):
        self.transfer_button.set_sensitive(True)
        self.transfer_progress.set_visible(False)
        self._update_dest_space_label()
        if err_count:
            msg = f"Transferencia terminada: {ok_count} ok, {err_count} con error."
        else:
            msg = f"Transferencia terminada: {ok_count} juego(s) copiados ✓"
        self._show_toast(msg)
        return False
