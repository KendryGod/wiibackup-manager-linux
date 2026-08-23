"""Fila de la lista de juegos: carátula + info + botón de acciones."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, GdkPixbuf, GLib, GObject  # noqa: E402

from .. import gametdb
from ..library import Game

COVER_WIDTH = 120
COVER_HEIGHT = 168  # proporción típica de carátula frontal de Wii (~0.71)
COVER_PLACEHOLDER_ICON = "media-optical-symbolic"


def build_cover_widget(width: int = COVER_WIDTH, height: int = COVER_HEIGHT):
    """Arma el widget de carátula: tamaño fijo con un ícono de disco de
    placeholder de fondo y la carátula real superpuesta cuando termina de
    cargar. Como el tamaño se fija de entrada (no depende de si hay imagen
    cargada), la fila nunca cambia de tamaño ni "salta" al terminar la
    descarga, y si la descarga falla el placeholder queda visible en vez de
    un hueco vacío.

    Devuelve (widget_a_insertar, picture_para_setear_la_carátula).
    """
    overlay = Gtk.Overlay()
    overlay.set_size_request(width, height)
    overlay.add_css_class("card")

    placeholder = Gtk.Image.new_from_icon_name(COVER_PLACEHOLDER_ICON)
    placeholder.set_pixel_size(int(min(width, height) * 0.4))
    placeholder.add_css_class("dim-label")
    overlay.set_child(placeholder)

    picture = Gtk.Picture()
    picture.set_content_fit(Gtk.ContentFit.COVER)
    picture.set_can_shrink(True)
    picture.set_halign(Gtk.Align.FILL)
    picture.set_valign(Gtk.Align.FILL)
    overlay.add_overlay(picture)

    return overlay, picture


# Pool compartido por todas las filas: antes cada GameRow disparaba su propio
# threading.Thread sin límite, así que una biblioteca de 50 juegos lanzaba 50
# descargas simultáneas contra GameTDB (saturando la conexión y haciendo que
# el servidor cuelgue/rechace pedidos). Con un pool acotado, una carátula
# lenta o colgada no bloquea a las demás: como mucho ocupa uno de los
# workers, y el resto sigue descargando en paralelo.
_COVER_DOWNLOAD_WORKERS = 6
_cover_executor = ThreadPoolExecutor(
    max_workers=_COVER_DOWNLOAD_WORKERS, thread_name_prefix="cover-dl"
)


class GameRow(Adw.ActionRow):
    __gsignals__ = {
        "convert-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "rename-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "verify-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "delete-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "selection-toggled": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "detail-requested": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, game: Game, cover_region: str = "EN"):
        super().__init__()
        self.game = game
        self.cover_region = cover_region

        self.set_title(GLib.markup_escape_text(game.title))
        subtitle = f"{game.game_id} · {game.fmt} · {game.size_mb:,.0f} MB"
        self.set_subtitle(subtitle)

        # Casilla de selección múltiple: oculta por defecto, se muestra al
        # activar el modo selección desde la ventana principal.
        self.select_check = Gtk.CheckButton(visible=False)
        self.select_check.set_valign(Gtk.Align.CENTER)
        self.select_check.connect(
            "toggled", lambda cb: self.emit("selection-toggled", cb.get_active())
        )
        self.add_prefix(self.select_check)

        self._cover_widget, self._cover = build_cover_widget()
        self.add_prefix(self._cover_widget)

        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("view-more-symbolic")
        menu_button.set_valign(Gtk.Align.CENTER)
        menu_button.set_menu_model(self._build_menu())
        self.add_suffix(menu_button)

        action_group = Gio_SimpleActionGroup(self)
        self.insert_action_group("row", action_group)

        # Clickear la fila (fuera del menú de tres puntos y, si el modo
        # selección está activo, fuera de la casilla) abre el panel de
        # detalle. set_activatable_widget (en set_selection_mode) redirige
        # el click a la casilla en vez de esto mientras dura la selección.
        self.set_activatable(True)
        self.connect("activated", self._on_activated)

    def _on_activated(self, *_):
        if not self.select_check.get_visible():
            self.emit("detail-requested")

    def _build_menu(self):
        from gi.repository import Gio
        menu = Gio.Menu()
        menu.append("Renombrar a estándar [ID]", "row.rename")
        menu.append("Convertir ISO ↔ WBFS", "row.convert")
        menu.append("Verificar integridad", "row.verify")
        menu.append("Eliminar", "row.delete")
        return menu

    def set_selection_mode(self, enabled: bool):
        """Muestra u oculta la casilla de selección. Con el modo activo,
        clickear la fila alterna la casilla en vez de no hacer nada."""
        self.select_check.set_visible(enabled)
        if enabled:
            self.set_activatable_widget(self.select_check)
        else:
            self.set_activatable_widget(None)
            self.select_check.set_active(False)

    def is_selected(self) -> bool:
        return self.select_check.get_active()

    def load_cover_async(self):
        """Descarga (o toma de caché) la carátula usando el pool compartido
        de workers y la aplica en el hilo principal de GTK cuando termina."""

        def worker():
            path = gametdb.get_cover_path(self.game.game_id, self.cover_region)
            GLib.idle_add(self._apply_cover, str(path) if path else None)

        _cover_executor.submit(worker)

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
