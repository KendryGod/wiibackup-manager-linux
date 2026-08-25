"""Pestaña 'Transferir': elegir un destino (USB/SD/carpeta) y copiar los
juegos seleccionados de la biblioteca hacia allí en formato WBFS."""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, GLib  # noqa: E402

from .. import config, drives, gametdb, library, oplog, wit_wrapper
from ..operations import OperationBusy, OperationKind, OperationOutcome
from ..library import Game
from ..i18n import _
from . import gtk_helpers
from .game_row import build_cover_widget


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
        """Igual que en GameRow: la carátula se pide al pool compartido de
        `gametdb` (nunca más de 6 descargas simultáneas en toda la app) y se
        aplica en el hilo principal de GTK vía GLib.idle_add.

        Antes esta vista lanzaba un `threading.Thread` por fila: con 300
        juegos eran 300 descargas de golpe contra GameTDB, mientras la
        Biblioteca hacía como mucho 6."""
        game_id, region = self.game.game_id, self._cover_region
        gametdb.fetch_cover_async(
            game_id, region,
            lambda path: GLib.idle_add(self._apply_cover,
                                        str(path) if path else None, game_id, region),
        )

    def _apply_cover(self, path: str | None, game_id: str | None = None,
                      region: str | None = None):
        # Igual que en GameRow: `set_games` reconstruye todas las filas
        # después de cada escaneo, así que una carátula que estaba en
        # vuelo puede llegar cuando esta fila ya no está en la lista.
        if not gtk_helpers.widget_is_alive(self):
            return False
        # Estas filas hoy se recrean (no se reusan como las de la
        # Biblioteca), así que el juego de la fila no cambia nunca; el
        # chequeo va igual para que el día que se reusen no aparezca acá
        # el mismo bug sutil que en GameRow.
        if game_id is not None and (game_id != self.game.game_id
                                     or region != self._cover_region):
            return False
        if path:
            try:
                self._cover.set_filename(path)
            except GLib.Error:
                pass
        return False


class TransferView(Gtk.Box):
    def __init__(self, settings, show_toast_cb, ops=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.settings = settings
        self._show_toast = show_toast_cb
        # Gestor de operaciones compartido con la ventana principal: es el
        # que evita, por ejemplo, transferir un juego que se está
        # convirtiendo o borrando desde la Biblioteca. Ver operations.py.
        if ops is None:
            from ..operations import OperationManager
            ops = OperationManager()
        self.ops = ops
        self.ops.add_listener(lambda: GLib.idle_add(self._update_operation_ui))
        self._games: list[Game] = []
        self._game_rows: list[TransferGameRow] = []
        self._dest_path: Path | None = None
        # Token de cancelación de la transferencia en curso: además de la
        # bandera "no sigas con el próximo juego", guarda el proceso de
        # `wit` que está corriendo para poder matarlo al cancelar (ver
        # `wit_wrapper.CancellationToken`).
        self._cancel_token = wit_wrapper.CancellationToken()
        # Snapshot de los puntos de montaje detectados en el último refresco,
        # para que el sondeo periódico solo repueble la lista cuando algo
        # cambió de verdad (unidad nueva, expulsada, o carpeta manual que
        # dejó de existir) en vez de reconstruir todo cada 3 segundos.
        self._known_auto_mounts: set[Path] = set()
        # Disponibilidad de cada destino guardado en el último refresco, para
        # que el sondeo detecte cuando uno se conecta o se va. Ver
        # `_preset_availability`.
        self._known_preset_state: dict = {}

        self._build_ui()
        GLib.timeout_add_seconds(3, self._poll_drives)

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        # --- Sección de destino ---
        dest_group = Adw.PreferencesGroup(
            title=_("Destino"),
            description=_("Elegí el disco, USB, SD o carpeta donde copiar los juegos."),
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
        refresh_drives_btn.set_tooltip_text(_("Volver a detectar unidades"))
        refresh_drives_btn.connect("clicked", lambda *_a: self._refresh_drives())
        add_folder_btn = Gtk.Button(label=_("Agregar carpeta"))
        add_folder_btn.connect("clicked", self._on_add_folder)
        dest_buttons.append(refresh_drives_btn)
        dest_buttons.append(add_folder_btn)

        # Guardar el destino elegido con un nombre corto ("HDD principal",
        # "SD cliente") para no volver a navegar carpetas la próxima vez.
        self.save_preset_btn = Gtk.Button(label=_("Guardar destino"))
        self.save_preset_btn.set_tooltip_text(
            _("Guardar el destino elegido como acceso rápido, con un nombre.")
        )
        self.save_preset_btn.connect("clicked", self._on_save_preset)
        dest_buttons.append(self.save_preset_btn)

        self.eject_button = Gtk.Button(label=_("Expulsar unidad"))
        self.eject_button.set_tooltip_text(
            _("Desmontar de forma segura la unidad seleccionada antes de "
              "desconectarla.")
        )
        self.eject_button.set_sensitive(False)
        self.eject_button.connect("clicked", self._on_eject_clicked)
        dest_buttons.append(self.eject_button)

        # Espacio del destino: texto + barra de color. La barra es un
        # agregado al texto, no un reemplazo: el número exacto ("12.3 GB
        # libres de 465.8 GB") es el dato que sirve para decidir si entran
        # los juegos elegidos, y la barra es la lectura de un vistazo de
        # cuán llena está la unidad.
        self.dest_space_label = Gtk.Label(
            label=_("Elegí un destino para ver el espacio disponible."), xalign=0
        )
        self.dest_space_label.add_css_class("dim-label")

        self.dest_space_bar = Gtk.LevelBar()
        self.dest_space_bar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        self.dest_space_bar.set_min_value(0.0)
        self.dest_space_bar.set_max_value(1.0)
        self.dest_space_bar.add_css_class("disk-usage")
        # GTK trae de fábrica los cortes "low"/"high"/"full", que le ponen
        # sus propias clases al bloque lleno y lo pintan según el tema. Acá
        # el color lo decide `_usage_css_class` con los umbrales de disco
        # que nos interesan (70% / 90%), así que se sacan para que no haya
        # dos reglas peleando por el mismo bloque.
        for offset in ("low", "high", "full"):
            self.dest_space_bar.remove_offset_value(offset)
        self.dest_space_bar.set_visible(False)

        dest_space_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        dest_space_box.set_margin_start(12)
        dest_space_box.set_margin_end(12)
        dest_space_box.set_margin_bottom(8)
        dest_space_box.append(self.dest_space_label)
        dest_space_box.append(self.dest_space_bar)

        self.append(dest_group)
        self.append(dest_buttons)
        self.append(dest_space_box)
        self.append(Gtk.Separator(margin_top=4, margin_bottom=4))

        # --- Sección de juegos ---
        games_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                                margin_start=12, margin_end=12, margin_top=4, margin_bottom=4)
        games_label = Gtk.Label(label=_("Juegos"), xalign=0)
        games_label.add_css_class("heading")
        games_label.set_hexpand(True)
        select_all_btn = Gtk.Button(label=_("Seleccionar todos"))
        select_all_btn.connect("clicked", lambda *_a: self._set_all_selected(True))
        deselect_all_btn = Gtk.Button(label=_("Deseleccionar todos"))
        deselect_all_btn.connect("clicked", lambda *_a: self._set_all_selected(False))
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
        self.transfer_button = Gtk.Button(label=_("Transferir seleccionados"))
        self.transfer_button.add_css_class("suggested-action")
        self.transfer_button.connect("clicked", self._on_transfer_clicked)
        action_box.append(self.transfer_button)

        self.cancel_button = Gtk.Button(label=_("Cancelar"), visible=False)
        self.cancel_button.connect("clicked", self._on_cancel_clicked)
        action_box.append(self.cancel_button)

        self.append(action_box)

        self._refresh_drives()
        self._update_operation_ui()

    def _update_operation_ui(self):
        """Apaga el botón de transferir solo si ESTA transferencia no puede
        arrancar ahora, no porque haya cualquier cosa en curso.

        Antes miraba `busy_label()`: una verificación suelta de un juego
        que ni siquiera está en la selección dejaba el botón gris. Ahora se
        le pregunta al gestor por la operación concreta, con los archivos
        elegidos, igual que hacen los botones de la Biblioteca. Una
        conversión o una importación siguen apagándolo: comparten la barra
        de progreso con la transferencia y no pueden convivir (ver
        `_SHARED_PROGRESS_KINDS` en operations.py).

        El `is_busy()` de arranque evita resolver las rutas de toda la
        selección en el caso normal: esto se recalcula en cada casilla que
        el usuario toca."""
        blocker = None
        if self.ops.is_busy():
            blocker = self.ops.conflict_for(
                OperationKind.TRANSFERRING,
                read=[game.path for game in self._selected_games()],
                resources=[self._dest_path] if self._dest_path is not None else [],
            )
        self.transfer_button.set_sensitive(blocker is None)
        self.transfer_button.set_tooltip_text(
            _("Hay una operación en curso: {op}. Esperá a que termine.")
            .format(op=blocker.label) if blocker else None
        )
        return False

    # ------------------------------------------------------------ Destino --
    @staticmethod
    def _is_dest_valid(path: Path) -> bool:
        """True si `path` todavía existe y es una carpeta legible. Cubre
        tanto la carpeta agregada a mano que quedó huérfana (la unidad que
        la contenía se expulsó/desconectó) como una carpeta local borrada
        después de agregarla."""
        try:
            return path.is_dir() and os.access(path, os.R_OK)
        except OSError:
            return False

    def _refresh_drives(self):
        """Vuelve a detectar las unidades removibles montadas y repuebla la
        lista, preservando las carpetas que el usuario agregó a mano (esas
        no se "detectan" solas) pero solo si siguen siendo accesibles: una
        carpeta manual cuya unidad ya se expulsó desaparece de la lista en
        vez de quedar mostrada con un error de espacio genérico."""
        manual_rows = [
            row for row in self._iter_dest_rows()
            if getattr(row, "is_manual", False) and self._is_dest_valid(row.dest_path)
        ]
        selected_path = self._dest_path

        child = self.dest_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.dest_list.remove(child)
            child = nxt

        # Primero los destinos guardados: son los que el usuario eligió
        # tener a mano, y una unidad que además esté guardada se muestra
        # con SU nombre ("SD cliente") en vez del genérico del sistema, sin
        # aparecer dos veces en la lista.
        preset_paths = set()
        for preset in list(self.settings.dest_presets):
            self.dest_list.append(self._build_preset_row(preset))
            preset_paths.add(Path(preset["path"]))

        auto_drives = drives.list_removable_drives()
        for drive in auto_drives:
            if drive.mount_point in preset_paths:
                continue
            subtitle = _("{free:.1f} GB libres de {total:.1f} GB · {path}").format(
                free=drive.free_gb, total=drive.total_gb, path=drive.mount_point)
            row = Adw.ActionRow(title=drive.name, subtitle=subtitle)
            row.dest_path = drive.mount_point
            row.is_manual = False
            row.add_prefix(Gtk.Image.new_from_icon_name("drive-removable-media-symbolic"))
            self.dest_list.append(row)
        self._known_auto_mounts = {d.mount_point for d in auto_drives}
        self._known_preset_state = self._preset_availability()

        for row in manual_rows:
            if row.dest_path in preset_paths:
                continue
            self.dest_list.append(row)

        # Si el destino elegido sigue en la lista (unidad que ya estaba
        # conectada o carpeta manual todavía válida), lo re-seleccionamos.
        reselected = False
        if selected_path is not None:
            for row in self._iter_dest_rows():
                if getattr(row, "dest_path", None) == selected_path:
                    self.dest_list.select_row(row)
                    reselected = True
                    break

        if selected_path is not None and not reselected:
            # El destino elegido ya no está disponible (unidad expulsada o
            # desconectada, o carpeta manual borrada): se saca de la
            # selección en vez de dejarlo mostrado con un error genérico.
            self._dest_path = None
            self.dest_list.unselect_all()
            self._update_dest_space_label()
            self._update_eject_button()
            self._show_toast(_("El destino elegido ya no está disponible y se quitó de la lista."))

    def _build_preset_row(self, preset: dict):
        """Fila de un destino guardado. Si la ruta no está disponible ahora
        (la SD que se sacó, el disco que no se conectó), la fila se muestra
        igual pero apagada y diciendo por qué: borrarla sola sería peor,
        porque el acceso rápido justamente existe para cuando esa unidad
        vuelva."""
        path = Path(preset["path"])
        row = Adw.ActionRow(title=preset["name"])
        row.dest_path = path
        row.is_manual = False
        row.is_preset = True
        row.preset_name = preset["name"]
        row.add_prefix(Gtk.Image.new_from_icon_name("starred-symbolic"))

        if self._is_dest_valid(path):
            try:
                usage = shutil.disk_usage(path)
                libres = usage.free / (1024 ** 3)
                total = usage.total / (1024 ** 3)
                row.set_subtitle(
                    _("{free:.1f} GB libres de {total:.1f} GB · {path}").format(
                        free=libres, total=total, path=path))
            except OSError:
                row.set_subtitle(str(path))
        else:
            row.set_subtitle(_("No disponible ahora · {path}").format(path=path))
            # Apagada: así no se puede elegir un destino que no está, que
            # es lo que terminaba en un error feo al tocar Transferir.
            row.set_sensitive(False)
            row.add_css_class("dim-label")

        quitar = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
        quitar.add_css_class("flat")
        quitar.set_tooltip_text(_("Quitar este acceso rápido"))
        quitar.connect("clicked", lambda *_a: self._on_delete_preset(preset))
        # El botón va fuera del estado de la fila: un destino que no está
        # disponible es justamente el que uno quiere poder borrar.
        quitar.set_sensitive(True)
        row.add_suffix(quitar)
        return row

    def _on_save_preset(self, *_args):
        if self._dest_path is None:
            self._show_toast(_("Elegí primero un destino de la lista para guardarlo."))
            return
        path = self._dest_path

        dialog = Adw.AlertDialog(
            heading=_("Guardar este destino"),
            body=_("Se va a guardar como acceso rápido:\n{path}").format(path=path),
        )
        entry = Gtk.Entry(text=path.name or str(path),
                           placeholder_text="Ej.: HDD principal, SD cliente")
        entry.set_margin_start(12); entry.set_margin_end(12)
        entry.set_margin_top(8); entry.set_margin_bottom(4)
        dialog.set_extra_child(entry)
        dialog.add_response("cancel", _("Cancelar"))
        dialog.add_response("save", _("Guardar"))
        dialog.set_default_response("save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_save_preset_response, entry, path)
        dialog.present(self.get_root())

    def _on_save_preset_response(self, dialog, response, entry, path: Path):
        if response != "save":
            return
        nombre = entry.get_text().strip() or (path.name or str(path))
        presets = [p for p in self.settings.dest_presets if p.get("path") != str(path)]
        presets.append({"name": nombre, "path": str(path)})
        self.settings.dest_presets = presets
        self._save_settings()
        self._refresh_drives()
        self._show_toast(_("Destino guardado como '{name}'.").format(name=nombre))

    def _on_delete_preset(self, preset: dict):
        self.settings.dest_presets = [
            p for p in self.settings.dest_presets if p.get("path") != preset.get("path")
        ]
        self._save_settings()
        self._refresh_drives()
        self._show_toast(_("Se quitó el acceso rápido '{name}'.")
                         .format(name=preset.get("name", "")))

    def _save_settings(self):
        error = config.try_save(self.settings)
        if error:
            # No se pierde lo que ya está en pantalla: la lista en memoria
            # queda igual, solo no sobrevive al próximo arranque.
            self._show_toast(_("No se pudo guardar la configuración: {error}").format(error=error))

    def _poll_drives(self):
        """Sondeo periódico (mismo patrón que la detección de la Biblioteca
        desconectada): solo repuebla la lista de destinos cuando algo
        cambió de verdad, para no reconstruirla sin necesidad cada pocos
        segundos."""
        current_auto = {d.mount_point for d in drives.list_removable_drives()}
        manual_rows = [row for row in self._iter_dest_rows() if getattr(row, "is_manual", False)]
        manual_became_invalid = any(not self._is_dest_valid(row.dest_path) for row in manual_rows)
        # Un destino guardado que se conecta (o se desconecta) también tiene
        # que reflejarse solo: si no, la SD que el usuario acaba de enchufar
        # sigue mostrándose como "No disponible" hasta que toque Actualizar.
        preset_state = self._preset_availability()
        presets_cambiaron = preset_state != self._known_preset_state
        if current_auto != self._known_auto_mounts or manual_became_invalid or presets_cambiaron:
            self._refresh_drives()
        return True  # seguir sondeando

    def _preset_availability(self) -> dict:
        return {p["path"]: self._is_dest_valid(Path(p["path"]))
                for p in self.settings.dest_presets}

    def _iter_dest_rows(self):
        row = self.dest_list.get_first_child()
        while row is not None:
            yield row
            row = row.get_next_sibling()

    def _on_dest_row_selected(self, listbox, row):
        self._dest_path = getattr(row, "dest_path", None) if row else None
        self._update_dest_space_label()
        self._update_eject_button()

    # Umbrales de la barra de uso de disco del destino. Debajo de WARN la
    # barra va verde, entre WARN y FULL amarilla, y de FULL para arriba
    # roja: con un disco Wii de doble capa pesando ~8 GB, un 90% ocupado en
    # una unidad chica ya significa que probablemente no entre el próximo
    # juego.
    _USAGE_WARN = 0.70
    _USAGE_FULL = 0.90

    @classmethod
    def _usage_css_class(cls, ratio: float) -> str:
        if ratio >= cls._USAGE_FULL:
            return "usage-full"
        if ratio >= cls._USAGE_WARN:
            return "usage-warn"
        return "usage-ok"

    def _set_usage_bar(self, ratio: float | None):
        """Actualiza la barra de uso, o la esconde si no hay un destino
        legible del que sacar el dato (`ratio is None`). Esconderla es a
        propósito: una barra en cero se leería como "el disco está vacío",
        que es justo lo contrario de "no sé cuánto hay"."""
        for css_class in ("usage-ok", "usage-warn", "usage-full"):
            self.dest_space_bar.remove_css_class(css_class)
        if ratio is None:
            self.dest_space_bar.set_visible(False)
            return
        ratio = min(max(ratio, 0.0), 1.0)
        self.dest_space_bar.add_css_class(self._usage_css_class(ratio))
        self.dest_space_bar.set_value(ratio)
        self.dest_space_bar.set_tooltip_text(
            _("{percent:.0f}% del destino ocupado").format(percent=ratio * 100))
        self.dest_space_bar.set_visible(True)

    def _update_dest_space_label(self):
        if self._dest_path is None:
            self.dest_space_label.set_label(_("Elegí un destino para ver el espacio disponible."))
            self._set_usage_bar(None)
            return
        try:
            usage = shutil.disk_usage(self._dest_path)
        except OSError:
            self.dest_space_label.set_label(_("No se pudo leer el espacio disponible en el destino."))
            self._set_usage_bar(None)
            return
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        # `usage.used + usage.free` no da `usage.total`: en ext4 y familia
        # hay bloques reservados para root que están en total y no en
        # ninguno de los otros dos. El porcentaje que le importa al usuario
        # es "cuánto del disco no puedo usar", así que se calcula sobre lo
        # que NO está libre y no sobre `usage.used`.
        ratio = (usage.total - usage.free) / usage.total if usage.total else None
        percent_text = (_(" · {percent:.0f}% usado").format(percent=ratio * 100)
                        if ratio is not None else "")
        self.dest_space_label.set_label(
            _("Espacio en destino: {free:.1f} GB libres de {total:.1f} GB{percent}")
            .format(free=free_gb, total=total_gb, percent=percent_text)
        )
        self._set_usage_bar(ratio)

    def _update_eject_button(self):
        if self._dest_path is not None and drives.is_mount_point(self._dest_path):
            self.eject_button.set_sensitive(True)
            self.eject_button.set_tooltip_text(
                _("Desmontar de forma segura la unidad seleccionada antes de "
                  "desconectarla.")
            )
        else:
            self.eject_button.set_sensitive(False)
            self.eject_button.set_tooltip_text(
                _("El destino elegido no es una unidad montada (es una carpeta "
                  "local): no hay nada que expulsar.")
            )

    def _on_eject_clicked(self, *_args):
        if self._dest_path is None or not drives.is_mount_point(self._dest_path):
            return
        dest_path = self._dest_path

        # Desmontar una unidad a la que se le está escribiendo deja el
        # archivo cortado por la mitad y puede romper el filesystem del
        # pendrive. El gestor sabe qué unidades están ocupadas porque cada
        # transferencia declara su destino como recurso.
        ocupada = self.ops.is_resource_busy(dest_path)
        if ocupada is not None:
            self._show_toast(
                _("No se puede expulsar ahora: hay una operación en curso sobre "
                  "esa unidad ({op}). Esperá a que termine o cancelala.")
                .format(op=ocupada.label)
            )
            return
        self.eject_button.set_sensitive(False)

        def worker():
            ok, message = drives.eject_mount_point(dest_path)
            GLib.idle_add(self._on_eject_done, ok, message, dest_path)

        threading.Thread(target=worker, daemon=True).start()

    def _on_eject_done(self, ok: bool, message: str, dest_path: Path):
        self._show_toast(message)
        if ok:
            # La unidad ya no está montada: si seguía elegida como destino,
            # sacarla de la selección y refrescar la lista de unidades.
            if self._dest_path == dest_path:
                self.dest_list.unselect_all()
            self._refresh_drives()
        else:
            self._update_eject_button()
        return False

    def _on_add_folder(self, *_args):
        dialog = Gtk.FileDialog(title=_("Elegí la carpeta destino"))
        dialog.set_initial_folder(gtk_helpers.safe_initial_folder())
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
            # Tildar o destildar cambia qué archivos tocaría la
            # transferencia, y con eso si choca o no con lo que esté
            # corriendo: hay que revisar el botón.
            row.check.connect("toggled", lambda *_a: self._update_operation_ui())
            self.game_list.append(row)
            self._game_rows.append(row)
            row.load_cover_async()
        self._update_operation_ui()

    def _selected_games(self) -> list[Game]:
        return [row.game for row in self._game_rows if row.check.get_active()]

    def _set_all_selected(self, value: bool):
        for row in self._game_rows:
            row.check.set_active(value)

    # -------------------------------------------------------- Transferir --
    def _on_transfer_clicked(self, *_args):
        if self._dest_path is None:
            self._show_toast(_("Elegí primero una unidad o carpeta destino."))
            return

        selected = self._selected_games()
        if not selected:
            self._show_toast(_("No hay juegos seleccionados."))
            return

        # El chequeo de espacio NO se hace acá: calcular cuánto va a ocupar
        # cada juego implica preguntarle a `wit` por cada uno, y con un
        # lote grande (o un archivo dañado) eso congelaría la ventana. Lo
        # hace el worker antes de copiar nada, mostrando "Calculando…".
        dest_root = self._dest_path

        # Con un solo juego (flujo individual) se pregunta antes de pisar
        # un destino que ya existe, igual que al convertir. En lote no
        # tiene sentido preguntar por cada uno: el worker los omite y los
        # informa aparte en el resumen final.
        if len(selected) == 1:
            try:
                dest = library.wbfs_dest_path(selected[0], dest_root)
            except ValueError:
                dest = None
            if dest is not None and dest.exists():
                gtk_helpers.confirm_overwrite(
                    self.get_root(),
                    _("Ya existe un archivo en:\n{dest}\n\n"
                      "Enviar '{title}' lo va a reemplazar. "
                      "Esta acción no se puede deshacer.")
                    .format(dest=dest, title=selected[0].title),
                    lambda: self._start_transfer(selected, dest_root, overwrite=True),
                )
                return

        self._start_transfer(selected, dest_root)

    def _start_transfer(self, selected: list[Game], dest_root: Path, overwrite: bool = False):
        # Se declara el destino, no solo el origen: los archivos que se van
        # a escribir y la unidad entera como recurso ocupado. Eso es lo que
        # hace que "Expulsar unidad" se niegue mientras dure la copia, en
        # vez de desmontar el pendrive abajo de `wit`.
        try:
            op = self.ops.start(
                OperationKind.TRANSFERRING,
                read=[g.path for g in selected],
                write=library.wbfs_dest_paths(selected, dest_root),
                resources=[dest_root],
            )
        except OperationBusy as e:
            self._show_toast(_("No se puede ahora: {detail}.").format(detail=e.detail))
            return

        wit_binary = self.settings.wit_binary
        # Token nuevo por transferencia: no arrastra el estado de una
        # cancelación anterior.
        cancel = wit_wrapper.CancellationToken()
        self._cancel_token = cancel
        self.transfer_button.set_sensitive(False)
        self.cancel_button.set_visible(True)
        self.cancel_button.set_sensitive(True)
        self.transfer_progress.set_fraction(0)
        self.transfer_progress.set_text(
            _("0/{total} transferidos").format(total=len(selected)))
        self.transfer_progress.set_visible(True)

        total = len(selected)
        # El total en bytes lo calcula el worker (`plan_transfer`): es el
        # tamaño de SALIDA, no el de los archivos de origen.
        # Igual que en la Biblioteca: el título si es uno solo, el recuento
        # si es un lote. Se calcula acá (no en el worker) porque `selected`
        # es la lista con la que arrancó la transferencia.
        target = (selected[0].title if len(selected) == 1
                  else f"{len(selected)} juegos")

        def worker():
            # Paso previo, ya fuera del hilo de GTK: cuánto va a ocupar
            # cada juego en el destino y si entra todo.
            GLib.idle_add(self._show_planning)
            plan = library.plan_transfer(selected, wit_binary)
            total_bytes_salida = sum(item.output_bytes for item in plan)
            libres_ahora = library.free_space(dest_root)
            if libres_ahora is not None and total_bytes_salida > libres_ahora:
                GLib.idle_add(
                    self._on_transfer_done, 0, len(plan), False, 0, op, target,
                    [f"No entra en el destino: se necesitan "
                     f"{library.format_size(total_bytes_salida)} y hay "
                     f"{library.format_size(libres_ahora)} libres (faltan "
                     f"{library.format_size(total_bytes_salida - libres_ahora)}). "
                     "Liberá espacio o elegí menos juegos."])
                return

            ok_count = 0
            err_count = 0
            skipped_count = 0
            # Motivos concretos, no solo la cuenta: "1 con error" no le
            # dice a nadie qué pasó ni con cuál juego.
            error_msgs: list[str] = []
            # Tres contadores en vez de uno: antes se sumaba el tamaño del
            # juego al progreso pasara lo que pasara, así que un juego que
            # fallaba o que ya estaba en el destino contaba como copiado y
            # la velocidad (y con ella el tiempo restante) salía inflada.
            # `bytes_written` es lo único que se escribió de verdad y es lo
            # que se usa para medir velocidad; la barra avanza con todo lo
            # ya resuelto, que es lo que el usuario entiende por "cuánto
            # falta del lote".
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
                    # Tope al 97% del tamaño esperado de ESTE juego: es una
                    # estimación por tamaño de archivo, y wit puede seguir
                    # cerrando/renombrando un instante más después de
                    # escribir el último byte. Sin este margen la barra
                    # llegaría al 100% y se quedaría ahí "clavada" un rato
                    # antes de que el juego realmente termine.
                    est = min(current, int(_item.output_bytes * 0.97))
                    GLib.idle_add(self._update_progress, i, total, _item.game.title,
                                  _base + est, _written + est, total_bytes_salida,
                                  start_time)

                GLib.idle_add(self._update_progress, i, total, game.title,
                              bytes_written + bytes_failed + bytes_skipped,
                              bytes_written, total_bytes_salida, start_time)
                # El espacio libre cambia a medida que se copia: revisar
                # antes de CADA juego evita quedarse sin lugar a mitad de
                # la escritura y dejar un archivo cortado en la unidad.
                necesario = item.output_bytes
                libres = library.free_space(dest_root)
                if libres is not None and necesario > libres:
                    err_count += 1
                    bytes_failed += item.output_bytes
                    error_msgs.append(
                        f"{game.title}: no entra en el destino "
                        f"(necesita {library.format_size(necesario)}, "
                        f"quedan {library.format_size(libres)})"
                    )
                    continue

                try:
                    library.send_to_wbfs_drive(game, dest_root, wit_binary,
                                                bytes_progress_cb=on_game_progress,
                                                overwrite=overwrite, cancel=cancel)
                    ok_count += 1
                    bytes_written += item.output_bytes
                except wit_wrapper.OperationCancelled:
                    # Cancelado a mitad de ESTE juego: no es un error, y no
                    # se sigue con los que faltaban.
                    cancelled = True
                    break
                except library.DestinationExistsError:
                    # El juego ya está en la unidad: ni éxito ni error, se
                    # cuenta aparte y se informa en el resumen final.
                    skipped_count += 1
                    bytes_skipped += item.output_bytes
                except Exception as e:
                    if cancel.cancelled:
                        # El fallo es consecuencia de haber matado a `wit`
                        # al cancelar, no un error real de la copia.
                        cancelled = True
                        break
                    # Un juego que falla no frena el resto: se cuenta como
                    # error y se sigue con el siguiente de la selección.
                    err_count += 1
                    bytes_failed += item.output_bytes
                    error_msgs.append(f"{game.title}: {e}")
            GLib.idle_add(self._on_transfer_done, ok_count, err_count, cancelled,
                          skipped_count, op, target, error_msgs)

        threading.Thread(target=worker, daemon=True).start()

    def _on_cancel_clicked(self, *_args):
        # Mata el `wit` (o corta la copia) que esté corriendo ahora mismo,
        # no solo evita que arranque el próximo juego.
        self._cancel_token.cancel()
        self.cancel_button.set_sensitive(False)
        self._show_toast(_("Cancelando la transferencia…"))

    def _show_planning(self):
        """Mientras el worker calcula cuánto ocupa cada juego, la ventana
        tiene que seguir viva y decir qué está haciendo."""
        self.transfer_progress.set_visible(True)
        self.transfer_progress.set_fraction(0)
        self.transfer_progress.set_text(_("Calculando espacio necesario…"))
        return False

    def _update_progress(self, done: int, total: int, title: str,
                          bytes_done: int, bytes_written: int,
                          total_bytes: int, start_time: float):
        # Fracción por bytes reales (no por "juegos completados"): con un
        # solo juego grande, `done` no cambia hasta que termina, así que
        # basarse solo en eso deja la barra clavada en 0% durante toda la
        # copia/conversión. Con total_bytes en 0 (no debería pasar, pero
        # por las dudas) cae al cálculo viejo por cantidad de juegos.
        if total_bytes > 0:
            fraction = min(bytes_done / total_bytes, 0.99)
        else:
            fraction = (done - 1) / max(total, 1)
        self.transfer_progress.set_fraction(fraction)
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
        self.transfer_progress.set_text(
            _("{done}/{total} · {title}{eta}").format(
                done=done, total=total, title=title, eta=eta_text))
        return False

    def _on_transfer_done(self, ok_count: int, err_count: int, cancelled: bool = False,
                           skipped_count: int = 0, op=None, target: str = "",
                           error_msgs: list | None = None):
        # El resultado que se le pasa a `finish` es lo que queda anotado en
        # la pestaña Log. Cancelado gana sobre el resto (lo pidió el
        # usuario); un lote con parte copiada y parte fallada queda como
        # "parcial", que no es lo mismo que no haber copiado nada.
        error_msgs = error_msgs or []
        detail_parts = [f"{ok_count} ok"]
        if skipped_count:
            detail_parts.append(
                _("{n} ya estaban en el destino").format(n=skipped_count))
        if err_count:
            detalle = "; ".join(error_msgs[:3])
            mas = f" (+{len(error_msgs) - 3} más)" if len(error_msgs) > 3 else ""
            detail_parts.append(
                _("{n} con error: {detail}{more}").format(
                    n=err_count, detail=detalle, more=mas)
                if detalle else _("{n} con error").format(n=err_count))
        if cancelled:
            status = oplog.STATUS_CANCELLED
        elif not err_count:
            status = oplog.STATUS_OK
        elif ok_count:
            status = oplog.STATUS_PARTIAL
        else:
            status = oplog.STATUS_ERROR
        self.ops.finish(op, OperationOutcome(status=status, target=target,
                                              detail=" · ".join(detail_parts)))
        self.transfer_button.set_sensitive(True)
        self.cancel_button.set_visible(False)
        self.transfer_progress.set_visible(False)
        self._update_dest_space_label()
        skipped_text = (_(", {n} ya estaban en el destino").format(n=skipped_count)
                        if skipped_count else "")
        if cancelled:
            msg = _("Transferencia cancelada: {ok} ok, {err} con error"
                    "{skipped} antes de cancelar.").format(
                        ok=ok_count, err=err_count, skipped=skipped_text)
        elif err_count or skipped_count:
            motivo = f" ({'; '.join(error_msgs[:2])})" if error_msgs else ""
            msg = _("Transferencia terminada: {ok} ok, {err} con error"
                    "{skipped}.{reason}").format(
                        ok=ok_count, err=err_count, skipped=skipped_text,
                        reason=motivo)
        else:
            msg = _("Transferencia terminada: {ok} juego(s) copiados ✓").format(
                ok=ok_count)
        self._show_toast(msg)
        return False
