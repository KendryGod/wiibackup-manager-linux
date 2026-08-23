"""Panel de detalle de un juego: carátula grande + datos conocidos del
archivo (ID, formato, tamaño) más lo que GameTDB tenga disponible (título
original, género, jugadores, fecha de lanzamiento, publisher, developer,
controles compatibles y sinopsis). Si GameTDB no trae alguno de esos datos
para el juego, esa fila (o esa sección entera, en el caso de la sinopsis)
simplemente no se muestra: no se inventa ni se rellena con un
placeholder."""
from __future__ import annotations

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
        self._cover_region = cover_region
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

        # Sinopsis: arranca oculta y solo se muestra si GameTDB tiene una
        # para este juego (la tiene ~el 80% de la base).
        self.synopsis_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                                     visible=False)
        synopsis_heading = Gtk.Label(label="Sinopsis", xalign=0)
        synopsis_heading.add_css_class("heading")
        self.synopsis_box.append(synopsis_heading)

        # `use_markup` queda en False (el valor por defecto): la sinopsis
        # es texto que baja de GameTDB, y no puede traer etiquetas Pango
        # que terminen interpretadas como formato.
        self._synopsis_label = Gtk.Label(xalign=0, yalign=0, wrap=True,
                                          margin_start=8, margin_end=8,
                                          margin_top=8, margin_bottom=8)
        self._synopsis_label.set_selectable(True)

        # Scroll propio y acotado en vez de dejar crecer el panel: hay
        # sinopsis de más de 7000 caracteres en la base, y sin tope el
        # resto de los datos quedaría a varias pantallas de distancia.
        # Con `propagate_natural_height` una sinopsis corta no deja hueco
        # vacío ni muestra barra: el marco se ajusta a lo que ocupa.
        synopsis_scroller = Gtk.ScrolledWindow()
        synopsis_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        synopsis_scroller.set_propagate_natural_height(True)
        synopsis_scroller.set_max_content_height(260)
        synopsis_scroller.set_child(self._synopsis_label)

        synopsis_frame = Gtk.Frame()
        synopsis_frame.set_child(synopsis_scroller)
        self.synopsis_box.append(synopsis_frame)

        outer.append(self.synopsis_box)

        scroller = Gtk.ScrolledWindow()
        scroller.set_child(outer)
        scroller.set_vexpand(True)
        toolbar_view.set_content(scroller)

        self.set_child(toolbar_view)

        self._load_cover_async(cover_region)
        self._load_extra_info_async()

    def _add_row(self, label: str, value: str, wrap: bool = False):
        row = Adw.ActionRow(title=label, subtitle=value)
        if wrap:
            # 0 = sin límite de líneas. La lista de controles de un juego
            # como Mario Kart Wii son seis accesorios y no entra en una
            # sola línea: sin esto se cortaría con puntos suspensivos.
            row.set_subtitle_lines(0)
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
        # Mismo worker compartido que usan las filas de la Biblioteca: si
        # el índice de wiitdb.xml ya se armó (o se está armando) para la
        # lista, este panel lo reusa en vez de volver a bajar y parsear los
        # 30+ MB del volcado.
        gametdb.fetch_extra_info_async(
            self.game.game_id, self._cover_region,
            lambda info: GLib.idle_add(self._apply_extra_info, info),
        )

    def _apply_extra_info(self, info: gametdb.GameExtraInfo | None):
        self.info_group.remove(self._extra_status_row)

        if info is None:
            placeholder = Adw.ActionRow(
                title="No se encontró información adicional en GameTDB para este juego."
            )
            placeholder.add_css_class("dim-label")
            self.info_group.add(placeholder)
            return False

        # El título de GameTDB va primero: es el dato que identifica al
        # juego, y solo aparece si dice algo distinto del título que el
        # panel ya muestra arriba de la carátula.
        extra_title = info.title_to_show_next_to(self.game.title)
        if extra_title is not None:
            self._add_row(*extra_title)
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
        if info.controls:
            self._add_row(
                "Controles compatibles",
                " · ".join(control.describe() for control in info.controls),
                wrap=True,
            )
        if info.synopsis:
            self._synopsis_label.set_label(info.synopsis)
            self.synopsis_box.set_visible(True)
        return False
