"""Fila de la lista de juegos: carátula + info + botón de acciones."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, GdkPixbuf, GLib, GObject  # noqa: E402

from .. import gametdb
from ..library import Game

COVER_WIDTH = 48
COVER_HEIGHT = 67  # proporción típica de carátula frontal de Wii (~0.71)


class GameRow(Adw.ActionRow):
    __gsignals__ = {
        "convert-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "rename-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "verify-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "delete-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, game: Game, cover_region: str = "EN"):
        super().__init__()
        self.game = game
        self.cover_region = cover_region

        self.set_title(GLib.markup_escape_text(game.title))
        subtitle = f"{game.game_id} · {game.fmt} · {game.size_mb:,.0f} MB"
        self.set_subtitle(subtitle)

        self._cover = Gtk.Picture()
        self._cover.set_size_request(COVER_WIDTH, COVER_HEIGHT)
        self._cover.set_content_fit(Gtk.ContentFit.COVER)
        self._cover.add_css_class("card")
        self._cover.add_css_class("dim-label")
        self.add_prefix(self._cover)

        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("view-more-symbolic")
        menu_button.set_valign(Gtk.Align.CENTER)
        menu_button.set_menu_model(self._build_menu())
        self.add_suffix(menu_button)

        action_group = Gio_SimpleActionGroup(self)
        self.insert_action_group("row", action_group)

    def _build_menu(self):
        from gi.repository import Gio
        menu = Gio.Menu()
        menu.append("Renombrar a estándar [ID]", "row.rename")
        menu.append("Convertir ISO ↔ WBFS", "row.convert")
        menu.append("Verificar integridad", "row.verify")
        menu.append("Eliminar", "row.delete")
        return menu

    def load_cover_async(self):
        """Descarga (o toma de caché) la carátula en un hilo aparte y la
        aplica en el hilo principal de GTK cuando termina."""
        import threading

        def worker():
            path = gametdb.get_cover_path(self.game.game_id, self.cover_region)
            GLib.idle_add(self._apply_cover, str(path) if path else None)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_cover(self, path: str | None):
        if path:
            try:
                self._cover.set_filename(path)
            except GLib.Error:
                pass
        return False


def Gio_SimpleActionGroup(row: GameRow):
    """Crea el grupo de acciones 'row.*' conectado a las señales de la fila."""
    from gi.repository import Gio

    group = Gio.SimpleActionGroup()

    def make_action(name: str, signal: str):
        action = Gio.SimpleAction.new(name, None)
        action.connect("activate", lambda *_: row.emit(signal))
        group.add_action(action)

    make_action("rename", "rename-requested")
    make_action("convert", "convert-requested")
    make_action("verify", "verify-requested")
    make_action("delete", "delete-requested")
    return group
