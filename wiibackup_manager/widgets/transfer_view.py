"""Pestaña 'Transferir': elegir un destino (USB/SD/carpeta) y copiar los
juegos seleccionados de la biblioteca hacia allí en formato WBFS."""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk, GLib  # noqa: E402

from .. import config, drives, formatting, gametdb, transfer_plan
from ..game_model import Game
from ..queue_manager import JobStatus, TransferJob, TransferQueue
from ..i18n import _, ngettext
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
            game_id, region, console=self.game.console,
            on_done=lambda path: GLib.idle_add(self._apply_cover,
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


# Cómo se ve cada estado de la cola. En un solo lugar y no repartido en
# `if`s por todo `JobRow.refresh`: el día que se agregue un estado, lo que
# falta se ve de una.
#
# El icono va a la izquierda y la clase CSS pinta la fila entera. Ninguna
# de las dos cosas es decorativa: son las que dejan barrer una cola de 40
# filas y encontrar la que falló sin leer subtítulo por subtítulo (y las
# que hacen que eso funcione también para quien no distingue el rojo del
# verde, porque la forma del icono cambia además del color).
_JOB_APPEARANCE = {
    JobStatus.PENDING: ("content-loading-symbolic", "dim-label"),
    JobStatus.RUNNING: ("folder-download-symbolic", None),
    JobStatus.VERIFYING: ("view-refresh-symbolic", None),
    JobStatus.DONE: ("emblem-ok-symbolic", "success"),
    JobStatus.SKIPPED: ("object-select-symbolic", "dim-label"),
    JobStatus.ERROR: ("dialog-error-symbolic", "error"),
    # Amarillo y no rojo, y con un ícono propio: el archivo está en la
    # unidad -no es un error de copia- pero no sirve. Que se distinga de
    # un vistazo del rojo de ERROR es justamente el punto.
    JobStatus.CORRUPT: ("dialog-warning-symbolic", "warning"),
    JobStatus.CANCELLED: ("process-stop-symbolic", "dim-label"),
}

_JOB_CSS_CLASSES = ("success", "error", "warning", "dim-label")


class JobRow(Adw.ActionRow):
    """Una tarea de la cola, dibujada.

    Es deliberadamente tonta: no sabe copiar, no sabe cancelar y no decide
    nada. Lee un `TransferJob` y se pinta; el botón "X" le avisa a la cola,
    que es la única que cambia estados. Esa separación es lo que evita el
    bug clásico de este tipo de listas -la fila que se pinta "cancelado" al
    apretar el botón mientras `wit` sigue escribiendo por atrás.

    Todo lo que muestra sale de campos que ya vienen calculados por
    `queue_manager` (`speed_text` incluido): la fila no hace cuentas de
    velocidad ni de tiempo restante, porque los datos para hacerlas (bytes
    escritos, cuándo arrancó de verdad) viven en el hilo de la cola."""

    def __init__(self, job: TransferJob, on_cancel):
        super().__init__()
        self.job = job
        self._on_cancel = on_cancel

        self.set_title(GLib.markup_escape_text(job.game.title))
        self.set_title_lines(1)
        self.set_subtitle_lines(2)

        self._icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
        self.add_prefix(self._icon)

        # Ancho fijo: si la barra se estirara con el ancho disponible, cada
        # fila tendría una barra de un largo distinto según lo largo que
        # fuera el título del juego, y comparar el avance de una tarea con
        # la de abajo se volvería imposible.
        self.progress = Gtk.ProgressBar(valign=Gtk.Align.CENTER,
                                         width_request=160)
        self.add_suffix(self.progress)

        self.cancel_button = Gtk.Button(icon_name="window-close-symbolic",
                                         valign=Gtk.Align.CENTER)
        self.cancel_button.add_css_class("flat")
        self.cancel_button.add_css_class("destructive-action")
        self.cancel_button.set_tooltip_text(_("Cancelar esta transferencia"))
        self.cancel_button.connect("clicked", self._on_cancel_clicked)
        self.add_suffix(self.cancel_button)

        self.refresh()

    def _on_cancel_clicked(self, *_args):
        # Se apaga en el acto pero NO se cambia el estado de la fila: matar
        # el `wit` en curso lleva un instante, y quien manda a repintar es
        # la cola cuando la tarea realmente muere. Apagarlo evita el doble
        # click, que es lo único que hay que resolver acá.
        self.cancel_button.set_sensitive(False)
        self._on_cancel(self.job)

    def refresh(self):
        """Vuelve a pintar la fila con el estado actual de la tarea. La
        llama la vista desde el callback de la cola, o sea siempre en el
        hilo de GTK."""
        job = self.job
        icon, css = _JOB_APPEARANCE.get(job.status, ("content-loading-symbolic", None))
        self._icon.set_from_icon_name(icon)
        for clase in _JOB_CSS_CLASSES:
            self.remove_css_class(clase)
        if css:
            self.add_css_class(css)

        self.set_subtitle(GLib.markup_escape_text(self._subtitle()))

        # La barra solo tiene sentido mientras hay algo que medir. En una
        # tarea que falló o se canceló, una barra a medio llenar es ruido:
        # lo que hay que leer ahí es el motivo, no cuánto había avanzado.
        en_curso = job.status in (JobStatus.PENDING, JobStatus.RUNNING,
                                  JobStatus.VERIFYING)
        self.progress.set_visible(en_curso or job.status is JobStatus.DONE)
        self.progress.set_fraction(1.0 if job.status is JobStatus.DONE
                                    else job.progress)
        # `is_final` es de la cola, no de la fila: una tarea terminada no se
        # puede cancelar, y el botón desaparece en vez de quedar gris para
        # no dejar 40 botones muertos en pantalla.
        self.cancel_button.set_visible(not job.is_final)

    def _subtitle(self) -> str:
        job = self.job
        if job.status is JobStatus.ERROR:
            # El motivo primero y completo: es el único estado donde el
            # texto importa más que el resto de la fila.
            return _("Error: {detail}").format(
                detail=job.error_msg or _("falló la copia"))
        if job.status is JobStatus.CORRUPT:
            # Mismo trato que ERROR -el motivo primero y entero- porque
            # acá también lo único que importa es qué pasó.
            return job.error_msg or job.status.label
        if job.status is JobStatus.DONE:
            texto = _("{status} · {size} en {elapsed}").format(
                status=job.status.label,
                size=formatting.format_size(job.output_bytes),
                elapsed=formatting.format_eta(job.elapsed))
            # La nota de verificación va al final y solo si la hay: sin
            # el switch prendido la fila se ve exactamente como antes.
            return f"{texto} · {job.verify_note}" if job.verify_note else texto
        if job.status is JobStatus.VERIFYING:
            return " · ".join([job.status.label, job.speed_text]) \
                if job.speed_text else job.status.label
        if job.status is JobStatus.RUNNING:
            partes = [job.status.label, f"{int(job.progress * 100)}%"]
            if job.speed_text:
                partes.append(job.speed_text)
            return " · ".join(partes)
        if job.status is JobStatus.PENDING and job.speed_text:
            # Acá `speed_text` no trae velocidad sino el motivo de la espera
            # ("En espera: Convirtiendo"), que es justo lo que hay que
            # mostrar para que la fila quieta no parezca colgada.
            return job.speed_text
        return job.status.label


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
        # La cola: el motor de las transferencias. Esta vista no copia
        # nada, solo encola y dibuja lo que la cola le va contando (ver
        # queue_manager.py). Los dos callbacks llegan ya en el hilo de GTK.
        self.queue = TransferQueue(self.ops,
                                    on_job_changed=self._on_job_changed,
                                    on_queue_idle=self._on_queue_idle)
        # Fila por id de tarea. Es un dict y no una lista porque el callback
        # llega con un `TransferJob` suelto y hay que encontrar su fila en
        # tiempo constante: con una cola de 200 juegos y un aviso de
        # progreso cada 100 ms, buscar linealmente sería recorrer la lista
        # entera diez veces por segundo.
        self._job_rows: dict[int, JobRow] = {}
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

        # El ticket va acá, al lado de "Expulsar", porque este es el
        # momento en que existe: la unidad ya está preparada y el paso
        # siguiente es desconectarla y entregarla. Es lo último que se
        # hace antes de sacar el pendrive, así que es donde se busca.
        self.ticket_button = Gtk.Button(label=_("Ticket de entrega"))
        self.ticket_button.set_tooltip_text(
            _("Generar un PDF con el contenido de la unidad para enviarle "
              "al cliente.")
        )
        self.ticket_button.set_sensitive(False)
        self.ticket_button.connect("clicked", self._on_ticket_clicked)
        dest_buttons.append(self.ticket_button)

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

        # --- Cola de transferencia ---
        #
        # Reemplaza a la barra de progreso única que había acá. Una sola
        # barra para N juegos obligaba a promediar tamaños distintos (un
        # número que no describe nada de lo que está pasando) y dejaba un
        # único "Cancelar" que mataba el lote entero. Ahora cada tarea es
        # una fila con su barra y su "X".
        #
        # La sección arranca escondida y aparece con la primera tarea: una
        # caja vacía anunciando "Cola de transferencia" le comería espacio a
        # la lista de juegos, que es lo que el usuario mira antes de
        # transferir nada.
        self.queue_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                                  visible=False)
        self.queue_box.append(Gtk.Separator(margin_top=4))

        queue_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                                margin_start=12, margin_end=12, margin_top=4)
        self.queue_label = Gtk.Label(label=_("Cola de transferencia"), xalign=0)
        self.queue_label.add_css_class("heading")
        self.queue_label.set_hexpand(True)
        queue_header.append(self.queue_label)

        self.clear_done_button = Gtk.Button(label=_("Limpiar terminadas"))
        self.clear_done_button.set_tooltip_text(
            _("Sacar de la lista las tareas ya completadas, canceladas o con "
              "error. No toca lo que está copiando."))
        self.clear_done_button.connect("clicked", self._on_clear_finished)
        queue_header.append(self.clear_done_button)

        self.cancel_all_button = Gtk.Button(label=_("Cancelar todo"))
        self.cancel_all_button.add_css_class("destructive-action")
        self.cancel_all_button.connect("clicked", self._on_cancel_all)
        queue_header.append(self.cancel_all_button)
        self.queue_box.append(queue_header)

        self.queue_list = Gtk.ListBox()
        self.queue_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.queue_list.add_css_class("boxed-list")

        # Alto acotado (no `vexpand`): la cola no puede crecer hasta tapar
        # la lista de juegos, y con 40 tareas encoladas el usuario tiene que
        # seguir pudiendo elegir más. Scrollea adentro suyo.
        queue_scroller = Gtk.ScrolledWindow()
        queue_scroller.set_child(self.queue_list)
        queue_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        queue_scroller.set_min_content_height(120)
        queue_scroller.set_max_content_height(240)
        queue_scroller.set_propagate_natural_height(True)
        queue_scroller.set_margin_start(12)
        queue_scroller.set_margin_end(12)
        self.queue_box.append(queue_scroller)
        self.append(self.queue_box)

        # --- Acción ---
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                              margin_start=12, margin_end=12, margin_top=8, margin_bottom=12)
        self.transfer_button = Gtk.Button(label=_("Transferir seleccionados"))
        self.transfer_button.add_css_class("suggested-action")
        self.transfer_button.connect("clicked", self._on_transfer_clicked)
        action_box.append(self.transfer_button)

        # Nota: acá NO va un "Cancelar" global. Cancelar de a una es el
        # botón de cada fila, y cancelar todo vive junto a la cola, que es
        # lo que ese botón afecta.
        self.append(action_box)

        self._refresh_drives()
        self._update_operation_ui()

    def _update_operation_ui(self):
        """Refresca lo que depende de qué está pasando en el sistema.

        Antes esto apagaba el botón "Transferir seleccionados" cuando había
        una operación en conflicto. Con la cola eso dejó de tener sentido, y
        no por comodidad: **encolar nunca puede fallar**. Si hay una
        conversión andando sobre uno de los juegos elegidos, la tarea entra
        igual y se queda esperando su turno diciendo por qué (ver
        `TransferQueue._acquire_operation`). Un botón gris obligaba al
        usuario a quedarse mirando la pantalla para reintentar; la cola
        espera sola, que es exactamente para lo que sirve una cola.

        Lo que sí sigue dependiendo del estado global es expulsar la unidad.

        Se llama desde tres lados: al construir la vista, cada vez que el
        `OperationManager` avisa un cambio (reenviado con `GLib.idle_add`,
        porque ese aviso puede venir de cualquier hilo) y desde el callback
        de la cola."""
        self._update_dest_buttons()
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
            self._update_dest_buttons()
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
        self._update_dest_buttons()

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

    def _update_dest_buttons(self):
        """Refresca los botones que dependen del destino elegido. Los dos
        se actualizan juntos porque miran lo mismo -qué hay seleccionado y
        si está ocupado-, y separarlos hacía que cada lugar que cambia la
        selección tuviera que acordarse de llamar a los dos."""
        self._update_eject_button()
        self._update_ticket_button()

    def _update_ticket_button(self):
        """El ticket se habilita con cualquier destino elegido que exista,
        sea una unidad montada o una carpeta local: el resumen se arma
        leyendo la estructura de carpetas, que es igual en los dos casos
        (una carpeta de pruebas en el disco tiene el mismo `wbfs/` que un
        pendrive).

        A diferencia de expulsar, NO se apaga cuando la unidad está
        ocupada: generar el ticket solo lee, así que no puede romper una
        transferencia en curso. Lo peor que pasa es que cuente un juego de
        menos si se lo pide antes de que la cola termine, y eso se
        soluciona volviéndolo a generar."""
        listo = self._dest_path is not None and self._dest_path.is_dir()
        self.ticket_button.set_sensitive(listo)
        if listo:
            self.ticket_button.set_tooltip_text(
                _("Generar un PDF con el contenido de la unidad para "
                  "enviarle al cliente.")
            )
        else:
            self.ticket_button.set_tooltip_text(
                _("Elegí primero un destino para poder generar su ticket.")
            )

    def _on_ticket_clicked(self, *_args):
        """Paso 1 de 3: pedirle a la persona el nombre del cliente y las
        notas. Los otros dos pasos son elegir dónde guardar el PDF y
        generarlo."""
        if self._dest_path is None:
            return
        from .ticket_dialog import TicketDialog

        dialog = TicketDialog(self._dest_path.name or str(self._dest_path),
                              self._on_ticket_details)
        dialog.present(self)

    def _on_ticket_details(self, client_name: str, notes: str):
        """Paso 2: dónde guardar el PDF. El nombre propuesto lo arma el
        servicio (lleva cliente y fecha), así que la vista no decide cómo
        se llama el archivo, solo lo ofrece."""
        from .. import ticket_service

        dialog = Gtk.FileDialog(title=_("Guardar el ticket de entrega"))
        dialog.set_initial_name(ticket_service.suggested_filename(client_name))
        dialog.set_initial_folder(gtk_helpers.safe_initial_folder())
        dialog.save(self.get_root(), None,
                    lambda d, r: self._on_ticket_file_chosen(d, r, client_name,
                                                              notes))

    def _on_ticket_file_chosen(self, dialog, result, client_name: str,
                               notes: str):
        try:
            archivo = dialog.save_finish(result)
        except Exception:
            # El usuario canceló el selector: no es un error que valga un
            # aviso, igual que en el resto de los selectores de la app.
            return
        if not archivo or self._dest_path is None:
            return

        destino = Path(archivo.get_path())
        origen = self._dest_path
        self.ticket_button.set_sensitive(False)

        def worker():
            """Paso 3, fuera del hilo de GTK: contar el contenido de la
            unidad implica recorrer carpetas y preguntarle el filesystem a
            `findmnt` (un subproceso con timeout), y las dos cosas pueden
            tardar lo suficiente como para congelar la ventana."""
            try:
                from .. import pdf_export, ticket_service
                datos = ticket_service.collect_ticket_data(
                    origen, client_name=client_name, notes=notes)
                pdf_export.render_ticket(datos, destino)
                GLib.idle_add(self._on_ticket_done, destino, None)
            except Exception as e:  # noqa: BLE001 - se le muestra al usuario
                GLib.idle_add(self._on_ticket_done, destino, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_ticket_done(self, destino: Path, error: str | None):
        self._update_ticket_button()
        if error is not None:
            self._show_toast(
                _("No se pudo generar el ticket: {error}").format(error=error))
            return False

        # Abrirlo con el visor del sistema es el punto: el ticket existe
        # para mandarlo por WhatsApp, y el paso siguiente es verlo y
        # compartirlo. Si no hay visor de PDF instalado, el archivo igual
        # quedó bien guardado y el aviso dice dónde.
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(destino)))
        launcher.launch(self.get_root(), None,
                        lambda l, r: self._on_ticket_opened(l, r, destino))
        return False

    def _on_ticket_opened(self, launcher, result, destino: Path):
        try:
            launcher.launch_finish(result)
        except Exception:
            self._show_toast(
                _("Ticket guardado en {ruta} (no se pudo abrir el visor de "
                  "PDF).").format(ruta=destino))
            return
        self._show_toast(_("Ticket guardado en {ruta}").format(ruta=destino))

    def _update_eject_button(self):
        if self._dest_path is not None and drives.is_mount_point(self._dest_path):
            # Una unidad con trabajo encolado no se expulsa, aunque en este
            # instante no se esté escribiendo un solo byte.
            #
            # El `OperationManager` solo conoce la tarea que está copiando
            # AHORA: entre una tarea y la siguiente hay un pestañeo en el
            # que no hay ninguna operación declarada, y ese hueco alcanza
            # para que un click desmonte el pendrive justo antes de que la
            # próxima tarea empiece a escribirle. `is_writing_to` mira la
            # cola entera -lo que está copiando y lo que espera- y tapa el
            # hueco. El gestor sigue siendo la otra mitad de la guarda: es
            # el que cubre lo que escribe la Biblioteca, que la cola no ve.
            ocupada = (self.queue.is_writing_to(self._dest_path)
                       or self.ops.is_resource_busy(self._dest_path) is not None)
            self.eject_button.set_sensitive(not ocupada)
            self.eject_button.set_tooltip_text(
                _("No se puede expulsar mientras haya transferencias en curso "
                  "o en cola hacia esta unidad.") if ocupada else
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
        # Revalidar al apretar, no solo al pintar el botón: entre que el
        # botón quedó habilitado y el click puede haber arrancado una tarea
        # de la cola.
        if self.queue.is_writing_to(dest_path):
            self._show_toast(
                _("No se puede expulsar ahora: la cola todavía tiene "
                  "transferencias hacia esa unidad. Cancelalas o esperá a que "
                  "terminen.")
            )
            return
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
            self._update_dest_buttons()
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

        dest_root = self._dest_path

        # Con un solo juego se pregunta antes de pisar un destino que ya
        # existe, igual que al convertir. En lote no: la cola marca esos
        # como "Ya estaba en el destino" y sigue, que es mejor que frenar
        # 40 juegos con un diálogo por cada uno.
        #
        # `dest.exists()` es una lectura de metadatos sobre una ruta que el
        # usuario acaba de elegir: es lo bastante barata como para hacerla
        # en el hilo de GTK sin que se note.
        if len(selected) == 1:
            try:
                dest = transfer_plan.game_dest_path(selected[0], dest_root)
            except ValueError:
                dest = None
            if dest is not None and dest.exists():
                gtk_helpers.confirm_overwrite(
                    self.get_root(),
                    _("Ya existe un archivo en:\n{dest}\n\n"
                      "Enviar '{title}' lo va a reemplazar. "
                      "Esta acción no se puede deshacer.")
                    .format(dest=dest, title=selected[0].title),
                    lambda: self._enqueue(selected, dest_root, overwrite=True),
                )
                return

        self._enqueue(selected, dest_root)

    def _enqueue(self, selected: list[Game], dest_root: Path, overwrite: bool = False):
        """Manda los juegos elegidos a la cola. Vuelve enseguida.

        Lo único que pasa en el hilo de GTK es descartar duplicados y
        mostrar el aviso; medir los tamaños (que le pregunta a `wit` por
        cada juego) va a un hilo de fondo, y encolar lo hace ese mismo hilo
        al terminar. Es la diferencia entre poder seguir usando la ventana
        mientras se planifica un lote de 200 juegos y no poder."""
        nuevos = self._descartar_encolados(selected, dest_root)
        if not nuevos:
            self._show_toast(
                _("Esos juegos ya están en la cola o ya se copiaron a este "
                  "destino. Usá 'Limpiar terminadas' para volver a enviarlos."))
            return
        if len(nuevos) < len(selected):
            self._show_toast(
                _("{n} juego(s) ya estaban en la cola o ya se copiaron: se "
                  "encolaron solo los que faltaban.")
                .format(n=len(selected) - len(nuevos)))

        wit_binary = self.settings.wit_binary

        def worker():
            # `plan_transfer_fast` y no `plan_transfer`: mide todos los
            # juegos en paralelo (ver transfer_plan.py). Lo que se gana acá es que
            # las filas de la cola aparezcan casi en el acto en vez de
            # después de N invocaciones de `wit` encadenadas.
            #
            # Si la medición fallara entera, `add_jobs` acepta `Game`
            # pelados y la cola mide cada uno justo antes de copiarlo: se
            # pierde la barra de progreso fina de esa tarea, no la
            # transferencia.
            try:
                items = transfer_plan.plan_transfer_fast(nuevos, wit_binary)
            except Exception:
                items = nuevos
            # `add_jobs` es thread-safe y las filas las crea el callback
            # (que sí llega por `GLib.idle_add`), así que se puede llamar
            # derecho desde acá.
            self.queue.add_jobs(items, dest_root, wit_binary=wit_binary,
                                overwrite=overwrite,
                                scrub_update=self.settings.scrub_update,
                                verify_after_copy=self.settings.verify_after_copy)

        threading.Thread(target=worker, daemon=True,
                         name="transfer-plan").start()

    # Estados que NO bloquean volver a encolar el mismo juego: el que falló
    # y el que se canceló son justamente los que uno quiere reintentar sin
    # tener que limpiar la cola primero.
    _REINTENTABLES = (JobStatus.ERROR, JobStatus.CANCELLED)

    def _descartar_encolados(self, selected: list[Game], dest_root: Path) -> list[Game]:
        """Saca los juegos que esta cola ya resolvió (o está por resolver)
        hacia el mismo destino.

        Sin esto, tocar "Transferir seleccionados" dos veces -o volver a la
        pestaña y tocarlo de nuevo sin destildar nada- encola el lote entero
        otra vez y la cola lo copia dos veces, obedientemente. Antes lo
        tapaba el botón apagado mientras duraba la transferencia; ahora el
        botón no se apaga nunca, así que la regla tiene que estar escrita
        acá.

        La regla no es "está en la cola" sino "ya se ocupó de esto", y la
        diferencia importa: en un lote largo, para cuando el usuario toca el
        botón de nuevo hay juegos que YA terminaron, y con la regla ingenua
        volverían a encolarse los 20 primeros. Lo que sí se deja reintentar
        es lo que salió mal (error o cancelado), que es el caso donde volver
        a tocar el botón es exactamente lo que uno quiere que pase.

        Para forzar una recopia de algo ya completado está "Limpiar
        terminadas": vacía esta memoria y el juego vuelve a ser encolable."""
        resueltos = {(job.game.path, job.dest_root)
                     for job in self.queue.jobs
                     if job.status not in self._REINTENTABLES}
        return [g for g in selected if (g.path, dest_root) not in resueltos]

    # ------------------------------------------------------ Avisos de la cola --
    def _on_job_changed(self, job: TransferJob):
        """Único punto por el que la cola toca la interfaz. Llega siempre en
        el hilo de GTK: lo garantiza `queue_manager` envolviendo cada aviso
        en `GLib.idle_add`.

        Crea la fila la primera vez que ve una tarea y la refresca después.
        Que sea el mismo camino para las dos cosas evita el desfase clásico:
        una fila creada al encolar y actualizada por otro lado que se pierde
        el primer cambio de estado si llegó antes de que la fila existiera."""
        if not gtk_helpers.widget_is_alive(self):
            return
        row = self._job_rows.get(job.id)
        if row is None:
            row = JobRow(job, self._on_job_cancel_requested)
            self._job_rows[job.id] = row
            self.queue_list.append(row)
        else:
            row.refresh()

        self._update_queue_header()
        # Una tarea que termina cambia el espacio libre del destino y puede
        # liberar la unidad para expulsarla.
        if job.is_final:
            self._update_dest_space_label()
        self._update_dest_buttons()

    def _on_job_cancel_requested(self, job: TransferJob):
        if not self.queue.cancel_job(job.id):
            # Terminó entre que se dibujó el botón y el click. No es un
            # error: la fila ya muestra el estado final.
            return
        self._show_toast(_("Cancelando '{title}'…").format(title=job.game.title))

    def _on_queue_idle(self, summary):
        """La cola se quedó sin trabajo: un solo resumen de la tanda.

        Un toast por juego terminado sería insoportable con 40 juegos, y no
        hace falta: el avance de cada uno ya se ve en su fila."""
        if not gtk_helpers.widget_is_alive(self):
            return
        if summary.batch_id != self.queue.batch_id:
            # El aviso salió del hilo de la cola y llegó acá por
            # `GLib.idle_add`; en el medio el usuario encoló una tanda
            # nueva, así que este resumen ya no describe lo que está
            # pasando -mostrarlo diría "Cola terminada" encima de una
            # copia que recién arranca. Se descarta entero: la tanda nueva
            # ya está mandando sus propios avisos por `_on_job_changed`,
            # que son los que mantienen al día el encabezado, el espacio
            # libre y el botón de expulsar.
            return
        self._update_dest_space_label()
        self._update_dest_buttons()
        self._update_queue_header()

        partes = [_("{n} copiados").format(n=summary.done)]
        if summary.skipped:
            partes.append(_("{n} ya estaban en el destino").format(n=summary.skipped))
        if summary.errors:
            partes.append(_("{n} con error").format(n=summary.errors))
        if summary.corrupt:
            partes.append(ngettext("{n} no pasó la verificación",
                                   "{n} no pasaron la verificación",
                                   summary.corrupt).format(n=summary.corrupt))
        if summary.cancelled:
            partes.append(_("{n} cancelados").format(n=summary.cancelled))
        # El detalle de CADA error queda en su fila (y en el historial): acá
        # va la cuenta, y "revisá la cola" para saber dónde mirar.
        cola = (_(" · revisá la cola para ver el detalle")
                if (summary.errors or summary.corrupt) else "")
        self._show_toast(_("Cola terminada: {detail}.{tail}").format(
            detail=", ".join(partes), tail=cola))

    def _update_queue_header(self):
        """Muestra u oculta la sección de la cola y actualiza su encabezado.

        El recuento va en el título ("Cola de transferencia — 3 en curso, 12
        terminadas") en vez de en una etiqueta aparte para no sumar otra
        línea de texto a una pantalla que ya tiene bastante."""
        jobs = self.queue.jobs
        self.queue_box.set_visible(bool(jobs))
        if not jobs:
            return
        pendientes = sum(1 for j in jobs if not j.is_final)
        terminadas = len(jobs) - pendientes
        partes = []
        if pendientes:
            partes.append(_("{n} en cola").format(n=pendientes))
        if terminadas:
            partes.append(_("{n} terminadas").format(n=terminadas))
        self.queue_label.set_label(
            _("Cola de transferencia — {detail}").format(detail=", ".join(partes))
            if partes else _("Cola de transferencia"))
        self.cancel_all_button.set_sensitive(bool(pendientes))
        self.clear_done_button.set_sensitive(bool(terminadas))

    def _on_clear_finished(self, *_args):
        for job in self.queue.clear_finished():
            row = self._job_rows.pop(job.id, None)
            if row is not None:
                self.queue_list.remove(row)
        self._update_queue_header()

    def _on_cancel_all(self, *_args):
        frenadas = self.queue.cancel_all()
        if frenadas:
            self._show_toast(_("Cancelando {n} transferencia(s)…").format(n=frenadas))

    def shutdown(self):
        """Corta la cola al cerrar la ventana: cancela lo que esté copiando
        y mata el `wit` en curso. El hilo de la cola es daemon, así que sin
        esto el proceso igual terminaría, pero dejando un `wit` a medio
        escribir sobre una unidad que el usuario está por desenchufar."""
        self.queue.shutdown()
