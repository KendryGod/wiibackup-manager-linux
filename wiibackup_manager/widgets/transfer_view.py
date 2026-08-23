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

from .. import drives, gametdb, library, wit_wrapper
from ..operations import OperationBusy, OperationKind
from ..library import Game
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

        self._build_ui()
        GLib.timeout_add_seconds(3, self._poll_drives)

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

        self.eject_button = Gtk.Button(label="Expulsar unidad")
        self.eject_button.set_tooltip_text(
            "Desmontar de forma segura la unidad seleccionada antes de desconectarla."
        )
        self.eject_button.set_sensitive(False)
        self.eject_button.connect("clicked", self._on_eject_clicked)
        dest_buttons.append(self.eject_button)

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

        self.cancel_button = Gtk.Button(label="Cancelar", visible=False)
        self.cancel_button.connect("clicked", self._on_cancel_clicked)
        action_box.append(self.cancel_button)

        self.append(action_box)

        self._refresh_drives()
        self._update_operation_ui()

    def _update_operation_ui(self):
        """Apaga el botón de transferir mientras haya cualquier operación en
        curso (propia o de la Biblioteca): son los mismos archivos."""
        busy_label = self.ops.busy_label()
        self.transfer_button.set_sensitive(busy_label is None)
        self.transfer_button.set_tooltip_text(
            f"Hay una operación en curso: {busy_label}. Esperá a que termine."
            if busy_label else None
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

        auto_drives = drives.list_removable_drives()
        for drive in auto_drives:
            subtitle = f"{drive.free_gb:.1f} GB libres de {drive.total_gb:.1f} GB · {drive.mount_point}"
            row = Adw.ActionRow(title=drive.name, subtitle=subtitle)
            row.dest_path = drive.mount_point
            row.is_manual = False
            row.add_prefix(Gtk.Image.new_from_icon_name("drive-removable-media-symbolic"))
            self.dest_list.append(row)
        self._known_auto_mounts = {d.mount_point for d in auto_drives}

        for row in manual_rows:
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
            self._show_toast("El destino elegido ya no está disponible y se quitó de la lista.")

    def _poll_drives(self):
        """Sondeo periódico (mismo patrón que la detección de la Biblioteca
        desconectada): solo repuebla la lista de destinos cuando algo
        cambió de verdad, para no reconstruirla sin necesidad cada pocos
        segundos."""
        current_auto = {d.mount_point for d in drives.list_removable_drives()}
        manual_rows = [row for row in self._iter_dest_rows() if getattr(row, "is_manual", False)]
        manual_became_invalid = any(not self._is_dest_valid(row.dest_path) for row in manual_rows)
        if current_auto != self._known_auto_mounts or manual_became_invalid:
            self._refresh_drives()
        return True  # seguir sondeando

    def _iter_dest_rows(self):
        row = self.dest_list.get_first_child()
        while row is not None:
            yield row
            row = row.get_next_sibling()

    def _on_dest_row_selected(self, listbox, row):
        self._dest_path = getattr(row, "dest_path", None) if row else None
        self._update_dest_space_label()
        self._update_eject_button()

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

    def _update_eject_button(self):
        if self._dest_path is not None and drives.is_mount_point(self._dest_path):
            self.eject_button.set_sensitive(True)
            self.eject_button.set_tooltip_text(
                "Desmontar de forma segura la unidad seleccionada antes de desconectarla."
            )
        else:
            self.eject_button.set_sensitive(False)
            self.eject_button.set_tooltip_text(
                "El destino elegido no es una unidad montada (es una carpeta local): "
                "no hay nada que expulsar."
            )

    def _on_eject_clicked(self, *_):
        if self._dest_path is None or not drives.is_mount_point(self._dest_path):
            return
        dest_path = self._dest_path
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

    def _on_add_folder(self, *_):
        dialog = Gtk.FileDialog(title="Elegí la carpeta destino")
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
                f"{library.format_size(total_bytes)} y hay {library.format_size(free_bytes)} libres "
                f"(faltan {library.format_size(total_bytes - free_bytes)}). "
                "Liberá espacio o elegí menos juegos."
            )
            return

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
                    f"Ya existe un archivo en:\n{dest}\n\n"
                    f"Enviar '{selected[0].title}' lo va a reemplazar. "
                    "Esta acción no se puede deshacer.",
                    lambda: self._start_transfer(selected, dest_root, overwrite=True),
                )
                return

        self._start_transfer(selected, dest_root)

    def _start_transfer(self, selected: list[Game], dest_root: Path, overwrite: bool = False):
        try:
            op = self.ops.start(OperationKind.TRANSFERRING, [g.path for g in selected])
        except OperationBusy as e:
            self._show_toast(f"No se puede ahora: {e.detail}.")
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
        self.transfer_progress.set_text(f"0/{len(selected)} transferidos")
        self.transfer_progress.set_visible(True)

        total = len(selected)
        total_bytes = sum(g.size_bytes for g in selected)

        def worker():
            ok_count = 0
            err_count = 0
            skipped_count = 0
            bytes_done = 0
            start_time = time.monotonic()
            cancelled = False
            for i, game in enumerate(selected, start=1):
                if cancel.cancelled:
                    cancelled = True
                    break
                base_bytes_done = bytes_done

                def on_game_progress(current: int, _base=base_bytes_done, _game=game):
                    # Tope al 97% del tamaño esperado de ESTE juego: es una
                    # estimación por tamaño de archivo, y wit puede seguir
                    # cerrando/renombrando un instante más después de
                    # escribir el último byte. Sin este margen la barra
                    # llegaría al 100% y se quedaría ahí "clavada" un rato
                    # antes de que el juego realmente termine.
                    est = min(current, int(_game.size_bytes * 0.97))
                    GLib.idle_add(self._update_progress, i, total, _game.title,
                                  _base + est, total_bytes, start_time)

                GLib.idle_add(self._update_progress, i, total, game.title,
                              bytes_done, total_bytes, start_time)
                try:
                    library.send_to_wbfs_drive(game, dest_root, wit_binary,
                                                bytes_progress_cb=on_game_progress,
                                                overwrite=overwrite, cancel=cancel)
                    ok_count += 1
                except wit_wrapper.OperationCancelled:
                    # Cancelado a mitad de ESTE juego: no es un error, y no
                    # se sigue con los que faltaban.
                    cancelled = True
                    break
                except library.DestinationExistsError:
                    # El juego ya está en la unidad: ni éxito ni error, se
                    # cuenta aparte y se informa en el resumen final.
                    skipped_count += 1
                except Exception:
                    if cancel.cancelled:
                        # El fallo es consecuencia de haber matado a `wit`
                        # al cancelar, no un error real de la copia.
                        cancelled = True
                        break
                    # Un juego que falla no frena el resto: se cuenta como
                    # error y se sigue con el siguiente de la selección.
                    err_count += 1
                bytes_done += game.size_bytes
            GLib.idle_add(self._on_transfer_done, ok_count, err_count, cancelled,
                          skipped_count, op)

        threading.Thread(target=worker, daemon=True).start()

    def _on_cancel_clicked(self, *_):
        # Mata el `wit` (o corta la copia) que esté corriendo ahora mismo,
        # no solo evita que arranque el próximo juego.
        self._cancel_token.cancel()
        self.cancel_button.set_sensitive(False)
        self._show_toast("Cancelando la transferencia…")

    def _update_progress(self, done: int, total: int, title: str,
                          bytes_done: int, total_bytes: int, start_time: float):
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
        if bytes_done > 0 and elapsed > 1:
            speed = bytes_done / elapsed
            remaining = max(total_bytes - bytes_done, 0)
            eta_text = f" · ~{library.format_eta(remaining / speed)} restantes" if speed > 0 else ""
        elif total > 1:
            eta_text = " · calculando tiempo restante…"
        else:
            eta_text = ""
        self.transfer_progress.set_text(f"{done}/{total} · {title}{eta_text}")
        return False

    def _on_transfer_done(self, ok_count: int, err_count: int, cancelled: bool = False,
                           skipped_count: int = 0, op=None):
        self.ops.finish(op)
        self.transfer_button.set_sensitive(True)
        self.cancel_button.set_visible(False)
        self.transfer_progress.set_visible(False)
        self._update_dest_space_label()
        skipped_text = f", {skipped_count} ya estaban en el destino" if skipped_count else ""
        if cancelled:
            msg = (f"Transferencia cancelada: {ok_count} ok, {err_count} con error"
                   f"{skipped_text} antes de cancelar.")
        elif err_count or skipped_count:
            msg = f"Transferencia terminada: {ok_count} ok, {err_count} con error{skipped_text}."
        else:
            msg = f"Transferencia terminada: {ok_count} juego(s) copiados ✓"
        self._show_toast(msg)
        return False
