"""Pestaña 'Log': historial de operaciones terminadas.

Muestra lo que guarda `oplog.OperationLog` (la más reciente arriba), con
botones para limpiar el historial o exportarlo a un archivo de texto. La
vista no decide qué se registra ni cuándo: solo lee el historial y se
vuelve a dibujar cuando cambia.
"""
from __future__ import annotations

from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, GLib, Gio  # noqa: E402

from .. import oplog

# Ícono y clase de color por estado. Los nombres son íconos simbólicos
# estándar del tema, presentes en cualquier escritorio con Adwaita.
_STATUS_ICONS = {
    oplog.STATUS_OK: ("emblem-ok-symbolic", "success"),
    oplog.STATUS_ERROR: ("dialog-error-symbolic", "error"),
    oplog.STATUS_PARTIAL: ("dialog-warning-symbolic", "warning"),
    oplog.STATUS_CANCELLED: ("process-stop-symbolic", "dim-label"),
}


class LogView(Gtk.Box):
    def __init__(self, log: oplog.OperationLog, show_toast_cb):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._log = log
        self._show_toast = show_toast_cb

        self._build_ui()
        self.refresh()

        # El historial se escribe desde los hilos de fondo que terminan
        # cada operación, así que el refresco se reenvía al hilo de GTK.
        self._log.add_listener(lambda: GLib.idle_add(self.refresh))

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          margin_start=12, margin_end=12, margin_top=12, margin_bottom=8)
        title = Gtk.Label(label="Historial de operaciones", xalign=0)
        title.add_css_class("heading")
        title.set_hexpand(True)
        header.append(title)

        self._export_button = Gtk.Button(label="Exportar")
        self._export_button.set_tooltip_text("Guardar el historial en un archivo de texto")
        self._export_button.connect("clicked", self._on_export_clicked)
        header.append(self._export_button)

        self._clear_button = Gtk.Button(label="Limpiar")
        self._clear_button.add_css_class("destructive-action")
        self._clear_button.set_tooltip_text("Borrar todas las entradas del historial")
        self._clear_button.connect("clicked", self._on_clear_clicked)
        header.append(self._clear_button)

        self.append(header)

        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.add_css_class("boxed-list")

        scroller = Gtk.ScrolledWindow()
        scroller.set_child(self.list_box)
        scroller.set_vexpand(True)
        scroller.set_margin_start(12)
        scroller.set_margin_end(12)
        scroller.set_margin_bottom(12)

        self.status_page = Adw.StatusPage(
            title="Todavía no hay operaciones registradas",
            description="Acá van a aparecer las conversiones, transferencias, "
                        "importaciones y eliminaciones a medida que las hagas.",
            icon_name="document-open-recent-symbolic",
        )

        self.stack = Gtk.Stack()
        self.stack.add_named(scroller, "list")
        self.stack.add_named(self.status_page, "empty")
        self.stack.set_vexpand(True)
        self.append(self.stack)

    # ----------------------------------------------------------- Refresco --
    def refresh(self):
        """Vuelve a dibujar la lista entera desde el historial.

        Redibujar todo y no ir agregando filas de a una: las entradas
        pueden llegar mientras esta pestaña ni se está viendo, y el
        historial tiene un tope de 500 entradas, así que rehacerlo es
        barato y evita que la lista y el archivo se desincronicen."""
        child = self.list_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.list_box.remove(child)
            child = nxt

        entries = self._log.entries()
        for entry in entries:
            self.list_box.append(self._build_row(entry))

        self.stack.set_visible_child_name("list" if entries else "empty")
        # Sin entradas no hay nada que limpiar ni que exportar.
        has_entries = bool(entries)
        self._clear_button.set_sensitive(has_entries)
        self._export_button.set_sensitive(has_entries)
        return False  # para GLib.idle_add

    def _build_row(self, entry: oplog.LogEntry) -> Adw.ActionRow:
        icon_name, css_class = _STATUS_ICONS.get(
            entry.status, ("dialog-question-symbolic", "dim-label")
        )

        row = Adw.ActionRow()
        # markup_escape_text porque title/subtitle de Adw.ActionRow se
        # interpretan como markup de Pango, y acá entra tanto el título de
        # un juego como el texto de error que devolvió `wit`.
        row.set_title(GLib.markup_escape_text(f"{entry.operation}: {entry.target}"))

        subtitle = f"{entry.when_text()} · {entry.status_label}"
        if entry.detail:
            subtitle += f"\n{entry.detail}"
        row.set_subtitle(GLib.markup_escape_text(subtitle))
        # 0 = sin límite: el motivo de un error de `wit` puede ser largo y
        # es justo el dato por el que alguien abre esta pestaña.
        row.set_subtitle_lines(0)

        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.add_css_class(css_class)
        icon.set_valign(Gtk.Align.CENTER)
        row.add_prefix(icon)
        return row

    # ------------------------------------------------------------ Limpiar --
    def _on_clear_clicked(self, *_):
        dialog = Adw.AlertDialog(
            heading="¿Limpiar el historial?",
            body="Se van a borrar todas las entradas registradas. "
                 "Esta acción no se puede deshacer.\n\n"
                 "No afecta a los juegos ni a los archivos: solo se borra el "
                 "registro de lo que se hizo.",
        )
        dialog.add_response("cancel", "Cancelar")
        dialog.add_response("clear", "Limpiar")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_clear_confirmed)
        dialog.present(self.get_root())

    def _on_clear_confirmed(self, dialog, response):
        if response != "clear":
            return
        self._log.clear()
        self._show_toast("Historial de operaciones limpiado.")

    # ----------------------------------------------------------- Exportar --
    def _on_export_clicked(self, *_):
        dialog = Gtk.FileDialog(title="Exportar el historial")
        dialog.set_initial_name("historial-wiibackup-manager.txt")
        dialog.save(self.get_root(), None, self._on_export_path_chosen)

    def _on_export_path_chosen(self, dialog, result):
        try:
            gfile = dialog.save_finish(result)
        except GLib.Error:
            # El usuario canceló el selector: no es un error que haya que
            # informar.
            return
        if gfile is None:
            return
        path = gfile.get_path()
        if not path:
            self._show_toast("No se pudo exportar: destino inválido.")
            return
        try:
            Path(path).write_text(self._log.export_text(), encoding="utf-8")
        except OSError as e:
            self._show_toast(f"No se pudo exportar el historial: {e}")
            return
        self._show_toast(f"Historial exportado a {Path(path).name}")
