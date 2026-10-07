"""Grupo "Mi taller" de Ajustes → General: los datos con los que se firma
el Ticket de Entrega.

Se guardan en la configuración del usuario (`config.Settings`, en
~/.config) y nunca en el código: la app la usan talleres distintos y cada
uno imprime su propia marca. `pdf_export.ShopProfile.from_settings` los
lee de ahí al generar cada ticket.

Cómo se guarda
--------------
Igual que los interruptores de al lado: cada cambio se escribe a disco
sin un botón de "guardar". Los campos de texto actualizan la
configuración en memoria en cada tecla -un ticket generado un segundo
después ya los usa- pero la escritura a disco espera a que se deje de
tipear (`GUARDADO_DIFERIDO_MS`): reescribir config.json por cada letra no
aporta nada.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .. import config  # noqa: E402
from ..i18n import _  # noqa: E402

GUARDADO_DIFERIDO_MS = 600
TICKET_THEMES = (config.TICKET_THEME_DARK, config.TICKET_THEME_LIGHT)
LOGO_PREVIEW_SIZE = 40


class ShopSettingsGroup(Adw.PreferencesGroup):
    """Los campos de "Mi taller". `save()` escribe la configuración a
    disco (la ventana avisa si falla); este grupo solo decide CUÁNDO."""

    def __init__(self, settings: config.Settings, save):
        super().__init__(
            title=_("Mi taller"),
            description=_("Datos que se imprimen en el Ticket de Entrega."),
        )
        self.settings = settings
        self._save = save
        self._pending_save = 0

        self.name_row = self._entry(_("Nombre del taller"), "shop_name")
        self.slogan_row = self._entry(_("Eslogan"), "shop_slogan")
        self.location_row = self._entry(_("Ubicación"), "shop_location")
        self.whatsapp_row = self._entry(
            _("WhatsApp (con código de país, solo números)"), "shop_whatsapp",
            config.clean_whatsapp)
        self.whatsapp_row.set_input_purpose(Gtk.InputPurpose.PHONE)

        # ------------------------------------------------------- Logo --
        self.logo_row = Adw.ActionRow(title=_("Logo"))
        self.logo_row.set_use_markup(False)
        self.logo_preview = Gtk.Picture(
            content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True,
            valign=Gtk.Align.CENTER)
        self.logo_preview.set_size_request(LOGO_PREVIEW_SIZE * 2,
                                           LOGO_PREVIEW_SIZE)
        self.logo_preview.add_css_class("card")
        self.logo_row.add_prefix(self.logo_preview)
        self.clear_logo_btn = Gtk.Button(
            icon_name="edit-delete-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text=_("Quitar el logo"))
        self.clear_logo_btn.add_css_class("flat")
        self.clear_logo_btn.connect("clicked", self._clear_logo)
        pick_btn = Gtk.Button(label=_("Elegir…"), valign=Gtk.Align.CENTER,
                              tooltip_text=_("Elegir imagen"))
        pick_btn.connect("clicked", self._pick_logo)
        self.logo_row.add_suffix(self.clear_logo_btn)
        self.logo_row.add_suffix(pick_btn)
        self.add(self.logo_row)
        self._refresh_logo()

        # ---------------------------------------------- Color de acento --
        color_row = Adw.ActionRow(title=_("Color de acento"))
        self.color_btn = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        self.reset_color_btn = Gtk.Button(
            icon_name="edit-undo-symbolic", valign=Gtk.Align.CENTER,
            tooltip_text=_("Restablecer el color por defecto"))
        self.reset_color_btn.add_css_class("flat")
        self.reset_color_btn.connect("clicked", self._reset_color)
        color_row.add_suffix(self.reset_color_btn)
        color_row.add_suffix(self.color_btn)
        self.add(color_row)
        self._show_color(settings.shop_accent_color)
        self.color_btn.connect("notify::rgba", self._on_color_changed)

        # -------------------------------------------- Tema del ticket --
        self.theme_row = Adw.ComboRow(title=_("Tema del ticket"))
        self.theme_row.set_model(Gtk.StringList.new(
            [_("Oscuro (marca)"), _("Claro (para imprimir)")]))
        try:
            self.theme_row.set_selected(TICKET_THEMES.index(settings.ticket_theme))
        except ValueError:
            self.theme_row.set_selected(0)
        self.theme_row.connect("notify::selected", self._on_theme_changed)
        self.add(self.theme_row)

        # Lo que quedó tipeado y todavía no se escribió no se pierde al
        # cerrar la ventana.
        self.connect("unrealize", lambda *_a: self.flush())

    # ----------------------------------------------------------- Guardar --
    def _entry(self, title: str, attr: str, transform=None) -> Adw.EntryRow:
        row = Adw.EntryRow(title=title)
        row.set_text(getattr(self.settings, attr))

        def on_changed(r):
            texto = r.get_text()
            setattr(self.settings, attr, transform(texto) if transform else texto)
            self._schedule_save()

        row.connect("changed", on_changed)
        self.add(row)
        return row

    def _schedule_save(self):
        if self._pending_save:
            GLib.source_remove(self._pending_save)
        self._pending_save = GLib.timeout_add(GUARDADO_DIFERIDO_MS,
                                              self._save_now)

    def _save_now(self):
        self._pending_save = 0
        self._save()
        return GLib.SOURCE_REMOVE

    def flush(self):
        """Escribe ya lo que esté pendiente de guardar."""
        if self._pending_save:
            GLib.source_remove(self._pending_save)
            self._save_now()

    # -------------------------------------------------------------- Logo --
    def _refresh_logo(self):
        ruta = self.settings.shop_logo_path
        self.logo_row.set_subtitle(
            ruta or _("Sin logo: se imprime el nombre del taller"))
        self.logo_preview.set_filename(ruta or None)
        self.logo_preview.set_visible(bool(ruta))
        self.clear_logo_btn.set_sensitive(bool(ruta))

    def _pick_logo(self, *_args):
        dialog = Gtk.FileDialog(title=_("Elegí el logo del taller"))
        filtro = Gtk.FileFilter()
        filtro.set_name(_("Imagen PNG"))
        filtro.add_mime_type("image/png")
        filtro.add_pattern("*.png")
        filtros = Gio.ListStore.new(Gtk.FileFilter)
        filtros.append(filtro)
        dialog.set_filters(filtros)
        dialog.open(self.get_root(), None, self._on_logo_picked)

    def _on_logo_picked(self, dialog, result):
        try:
            archivo = dialog.open_finish(result)
        except Exception:
            # Cancelado: igual que el resto de los selectores de la app.
            return
        if archivo and archivo.get_path():
            self.set_logo(archivo.get_path())

    def set_logo(self, ruta: str):
        self.settings.shop_logo_path = ruta
        self._refresh_logo()
        self._save()

    def _clear_logo(self, *_args):
        self.set_logo("")

    # ------------------------------------------------------------- Color --
    def _show_color(self, valor: str):
        rgba = Gdk.RGBA()
        if not rgba.parse(valor):
            rgba.parse(config.DEFAULT_ACCENT_COLOR)
        self.color_btn.set_rgba(rgba)
        self.reset_color_btn.set_sensitive(
            valor.upper() != config.DEFAULT_ACCENT_COLOR.upper())

    def _on_color_changed(self, button, _param):
        c = button.get_rgba()
        valor = "#{:02X}{:02X}{:02X}".format(
            round(c.red * 255), round(c.green * 255), round(c.blue * 255))
        if valor == self.settings.shop_accent_color.upper():
            return
        self.settings.shop_accent_color = valor
        self.reset_color_btn.set_sensitive(
            valor != config.DEFAULT_ACCENT_COLOR.upper())
        self._save()

    def _reset_color(self, *_args):
        # Cambiar el botón dispara `_on_color_changed`, que guarda.
        self._show_color(config.DEFAULT_ACCENT_COLOR)

    # -------------------------------------------------------------- Tema --
    def _on_theme_changed(self, row, _param):
        idx = row.get_selected()
        if 0 <= idx < len(TICKET_THEMES):
            self.settings.ticket_theme = TICKET_THEMES[idx]
            self._save()
