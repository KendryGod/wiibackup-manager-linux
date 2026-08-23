"""Panel de detalle de un juego: carátula grande + datos conocidos del
archivo (ID, formato, tamaño) más lo que GameTDB tenga disponible (género,
jugadores, fecha de lanzamiento, publisher, developer). Si GameTDB no trae
alguno de esos datos para el juego, esa fila simplemente no se muestra: no
se inventa ni se rellena con un placeholder."""
from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, GLib  # noqa: E402

from .. import gametdb
from ..library import Game
from .game_row import build_cover_widget

COVER_WIDTH = 220
COVER_HEIGHT = 308


class GameDetailDialog(Adw.Dialog):
    def __init__(self, game: Game, cover_region: str = "EN"):
        super().__init__()
        self.game = game
        self.set_content_width(420)
        self.set_content_height(560)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16,
                         margin_start=20, margin_end=20, margin_top=4, margin_bottom=24)

        cover_widget, self._cover = build_cover_widget(COVER_WIDTH, COVER_HEIGHT)
        cover_widget.set_halign(Gtk.Align.CENTER)
        outer.append(cover_widget)

        title_label = Gtk.Label(label=game.title, wrap=True, justify=Gtk.Justification.CENTER)
        title_label.add_css_class("title-2")
        title_label.set_halign(Gtk.Align.CENTER)
        outer.append(title_label)

        self.info_group = Adw.PreferencesGroup()
        outer.append(self.info_group)

        self._add_row("ID de juego", game.game_id)
        self._add_row("Formato", game.fmt)
        self._add_row("Tamaño", f"{game.size_mb:,.0f} MB")

        self._extra_status_row = Adw.ActionRow(title="Buscando información adicional en GameTDB…")
        spinner = Gtk.Spinner(spinning=True)
        self._extra_status_row.add_suffix(spinner)
        self.info_group.add(self._extra_status_row)

        scroller = Gtk.ScrolledWindow()
        scroller.set_child(outer)
        scroller.set_vexpand(True)
        toolbar_view.set_content(scroller)

        self.set_child(toolbar_view)

        self._load_cover_async(cover_region)
        self._load_extra_info_async()

    def _add_row(self, label: str, value: str):
        row = Adw.ActionRow(title=label, subtitle=value)
        self.info_group.add(row)
        return row

    # ------------------------------------------------------------ Carátula --
    def _load_cover_async(self, cover_region: str):
        # Mismo pool compartido que la Biblioteca y Transferir: si la
        # carátula de este juego ya se está descargando para una fila, este
        # panel se cuelga de esa descarga en vez de pedirla de nuevo.
        gametdb.fetch_cover_async(
            self.game.game_id, cover_region,
            lambda path: GLib.idle_add(self._apply_cover, str(path) if path else None),
        )

    def _apply_cover(self, path: str | None):
        if path:
            try:
                self._cover.set_filename(path)
            except GLib.Error:
                pass
        return False

    # --------------------------------------------------- Metadata extra --
    def _load_extra_info_async(self):
        def worker():
            info = gametdb.get_game_extra_info(self.game.game_id)
            GLib.idle_add(self._apply_extra_info, info)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_extra_info(self, info: gametdb.GameExtraInfo | None):
        self.info_group.remove(self._extra_status_row)

        if info is None:
            placeholder = Adw.ActionRow(
                title="No se encontró información adicional en GameTDB para este juego."
            )
            placeholder.add_css_class("dim-label")
            self.info_group.add(placeholder)
            return False

        if info.genre:
            self._add_row("Género", info.genre)
        if info.players:
            self._add_row("Jugadores", info.players)
        if info.release_date:
            self._add_row("Fecha de lanzamiento", info.release_date)
        if info.publisher:
            self._add_row("Publisher", info.publisher)
        if info.developer:
            self._add_row("Developer", info.developer)
        return False
