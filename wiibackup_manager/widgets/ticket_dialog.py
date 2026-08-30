"""Diálogo que pide los dos datos que el Ticket de Entrega no puede
deducir solo: a nombre de quién va y qué aclaración lleva.

Todo lo demás del ticket -cuántos juegos, cuánto espacio, el formato de la
unidad- lo averigua `ticket_service` leyendo la unidad. Acá solo se
juntan los campos que escribe una persona, y se los pasa a quien sepa qué
hacer con ellos: este diálogo no genera nada, no conoce el PDF y no toca
la unidad."""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from ..i18n import _


class TicketDialog(Adw.Dialog):
    """Pide nombre de cliente y notas, y llama a `on_generate(nombre,
    notas)` si el usuario confirma.

    Los dos campos son OPCIONALES a propósito y el botón de generar nunca
    se apaga: el caso más común en el mostrador es entregar rápido y sin
    cargar nada, y un ticket sin nombre sigue siendo un comprobante útil
    de qué lleva la unidad. Ver `pdf_export._dibujar`, que arma la hoja sin
    dejar huecos cuando alguno de los dos falta."""

    def __init__(self, drive_label: str, on_generate):
        super().__init__()
        self.on_generate = on_generate
        self.set_title(_("Ticket de entrega"))
        self.set_content_width(460)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        cancel_btn = Gtk.Button(label=_("Cancelar"))
        cancel_btn.connect("clicked", lambda *_a: self.close())
        header.pack_start(cancel_btn)

        generate_btn = Gtk.Button(label=_("Generar"))
        generate_btn.add_css_class("suggested-action")
        generate_btn.connect("clicked", self._on_generate_clicked)
        header.pack_end(generate_btn)

        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(
            title=_("Datos del ticket"),
            description=_("Se genera un PDF con el contenido de «{drive}» "
                          "para enviarle al cliente.").format(drive=drive_label),
        )
        page.add(group)

        self.name_row = Adw.EntryRow(title=_("Nombre del cliente"))
        group.add(self.name_row)

        # Las notas van en un TextView y no en otra EntryRow porque son
        # texto libre de varias líneas ("incluye 2 controles", "revisar
        # lector en 6 meses"), y una fila de una línea invitaría a
        # escribir menos de lo que hace falta.
        notes_group = Adw.PreferencesGroup(title=_("Notas (opcional)"))
        page.add(notes_group)

        self.notes_view = Gtk.TextView()
        self.notes_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.notes_view.set_top_margin(8)
        self.notes_view.set_bottom_margin(8)
        self.notes_view.set_left_margin(8)
        self.notes_view.set_right_margin(8)

        scroller = Gtk.ScrolledWindow()
        scroller.set_min_content_height(110)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.notes_view)
        scroller.add_css_class("card")
        notes_group.add(scroller)

        toolbar.set_content(page)
        self.set_child(toolbar)

    def _notes_text(self) -> str:
        buffer = self.notes_view.get_buffer()
        return buffer.get_text(buffer.get_start_iter(),
                               buffer.get_end_iter(), False)

    def _on_generate_clicked(self, *_args):
        nombre = self.name_row.get_text()
        notas = self._notes_text()
        # Cerrar ANTES de avisar: lo que sigue abre un selector de archivo,
        # y dejar este diálogo abierto atrás apilaría dos ventanas modales.
        self.close()
        self.on_generate(nombre, notas)
