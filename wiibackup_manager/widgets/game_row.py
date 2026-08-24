"""Fila de la lista de juegos: carátula + info + botón de acciones."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, GdkPixbuf, GLib, GObject  # noqa: E402

from .. import gametdb
from ..library import Game
from . import gtk_helpers

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
        # Subtítulo base (ID/formato/tamaño). Si GameTDB tiene un título
        # que aporte algo (ver `load_extra_info_async`) se le agrega una
        # línea ARRIBA de esta, para que el dato del título quede pegado al
        # título principal y no perdido después de los números.
        self._base_subtitle = f"{game.game_id} · {game.fmt} · {game.size_mb:,.0f} MB"
        # Línea de GameTDB ya aplicada, si llegó (ver `_apply_extra_title`).
        # Se guarda aparte del subtítulo armado para poder rehacerlo cuando
        # los datos del archivo cambian sin perderla. Ver `update_game`.
        self._extra_line: str | None = None
        self.set_subtitle(self._base_subtitle)
        self.set_subtitle_lines(2)

        # Clave con la que el ListBox ordena esta fila. La calcula la
        # ventana (es la que sabe qué criterio eligió el usuario) y se
        # guarda acá para no recalcularla en cada comparación: GTK llama a
        # su función de orden O(n log n) veces, y uno de los criterios es
        # la fecha del archivo, que implica un stat.
        self.sort_key = None

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

    def update_game(self, game: Game, cover_region: str | None = None) -> None:
        """Reapunta la fila a un `Game` nuevo del mismo archivo, sin
        reconstruir el widget.

        Lo usa la ventana después de cada escaneo: el archivo es el mismo
        pero los datos pueden haber cambiado (una conversión le cambia el
        formato y el tamaño, un renombrado el título). Reusar la fila en
        vez de tirarla y crear otra evita el parpadeo, conserva la casilla
        de selección y la carátula ya cargada, y es la diferencia entre
        que la lista se rehaga en un segundo o al instante.

        La carátula y la metadata solo se vuelven a pedir si cambió el
        juego (otro Game ID) o la región configurada: si no, lo que ya está
        en pantalla sigue siendo correcto."""
        region = cover_region or self.cover_region
        reload_art = (game.game_id != self.game.game_id
                      or region != self.cover_region)

        self.game = game
        self.cover_region = region
        self.set_title(GLib.markup_escape_text(game.title))
        self._base_subtitle = f"{game.game_id} · {game.fmt} · {game.size_mb:,.0f} MB"

        if reload_art:
            self._extra_line = None
            # La carátula que se ve es la del juego anterior: se limpia y
            # queda el placeholder hasta que llegue la nueva.
            self._cover.set_filename(None)
            self._refresh_subtitle()
            self.load_cover_async()
            self.load_extra_info_async()
        else:
            self._refresh_subtitle()

    def _refresh_subtitle(self):
        if self._extra_line:
            self.set_subtitle(f"{self._extra_line}\n{self._base_subtitle}")
        else:
            self.set_subtitle(self._base_subtitle)

    def load_cover_async(self):
        """Descarga (o toma de caché) la carátula usando el pool compartido
        de `gametdb` y la aplica en el hilo principal de GTK cuando termina.

        El pool vive en gametdb y no acá para que TODAS las vistas que
        muestran carátulas compartan el mismo límite de descargas
        simultáneas, y para que un rescan no vuelva a encolar carátulas que
        ya se están descargando."""
        gametdb.fetch_cover_async(
            self.game.game_id, self.cover_region,
            lambda path: GLib.idle_add(self._apply_cover, str(path) if path else None),
        )

    def _apply_cover(self, path: str | None):
        # La descarga pudo terminar después de que esta fila dejara de
        # existir (cambio de orden, rescan, biblioteca recargada): las
        # filas se reconstruyen enteras y la que pidió la carátula ya no
        # está en la lista. Ver `gtk_helpers.widget_is_alive`.
        if not gtk_helpers.widget_is_alive(self):
            return False
        if path:
            try:
                self._cover.set_filename(path)
            except GLib.Error:
                pass
        return False

    # ------------------------------------------------- Título de GameTDB --
    def load_extra_info_async(self):
        """Pide la metadata de GameTDB para mostrar el título original (o el
        traducido) abajo del título que ya muestra la fila.

        Igual que la carátula, va por el pool de `gametdb` y se aplica en el
        hilo de GTK: armar el índice de wiitdb.xml la primera vez implica
        bajar y parsear decenas de MB, que en el hilo principal congelaría
        la ventana entera."""
        gametdb.fetch_extra_info_async(
            self.game.game_id, self.cover_region,
            lambda info: GLib.idle_add(self._apply_extra_title, info),
        )

    def _apply_extra_title(self, info):
        """Agrega la línea del título de GameTDB si aporta algo.

        `title_to_show_next_to` devuelve None cuando el título de GameTDB
        es el mismo que la fila ya muestra (comparando sin mayúsculas ni
        puntuación), que es el caso más común: ahí la fila queda tal cual,
        sin una línea repetida."""
        if not gtk_helpers.widget_is_alive(self):
            # Misma carrera que en `_apply_cover`, pero más probable: armar
            # el índice de wiitdb.xml la primera vez tarda decenas de
            # segundos, tiempo de sobra para que el usuario reordene la
            # lista o dispare un rescan.
            return False
        if info is None:
            return False
        extra = info.title_to_show_next_to(self.game.title)
        if extra is None:
            return False
        label, title = extra
        self._extra_line = f"{label}: {GLib.markup_escape_text(title)}"
        self._refresh_subtitle()
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
