from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
from gi.repository import Adw, Gdk, Gio, Gtk  # noqa: E402

from .. import config, styles, wit_wrapper
from ..i18n import _


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, settings: config.Settings, on_saved):
        super().__init__()
        self.settings = settings
        self.on_saved = on_saved
        self.set_title(_("Preferencias"))

        page = Adw.PreferencesPage()
        self.add(page)

        group = Adw.PreferencesGroup(title=_("Biblioteca"))
        page.add(group)

        self._library_row = Adw.ActionRow(title=_("Carpeta de la biblioteca"))
        self._library_row.set_use_markup(False)
        self._library_row.set_subtitle(settings.library_path)
        pick_btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        pick_btn.connect("clicked", self._pick_library_folder)
        self._library_row.add_suffix(pick_btn)
        group.add(self._library_row)

        self._wbfs_row = Adw.ActionRow(title=_("Unidad/carpeta WBFS (USB Loader)"))
        self._wbfs_row.set_use_markup(False)
        self._wbfs_row.set_subtitle(settings.wbfs_drive_path or _("No configurada"))
        pick_wbfs_btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        pick_wbfs_btn.connect("clicked", self._pick_wbfs_folder)
        self._wbfs_row.add_suffix(pick_wbfs_btn)
        group.add(self._wbfs_row)

        group2 = Adw.PreferencesGroup(title=_("Motor de conversión"))
        page.add(group2)

        status = (_("detectado ✓") if wit_wrapper.is_available(settings.wit_binary)
                  else _("no encontrado ✗"))
        self._wit_row = Adw.ActionRow(title="wit (Wiimms ISO Tools)")
        self._wit_row.set_subtitle(_("Estado: {status}").format(status=status))
        group2.add(self._wit_row)

        group3 = Adw.PreferencesGroup(title=_("Carátulas"))
        page.add(group3)
        region_row = Adw.ComboRow(title=_("Región preferida"))
        region_model = Gtk.StringList.new(["EN", "US", "JA", "FR", "DE", "ES", "IT"])
        region_row.set_model(region_model)
        regions = ["EN", "US", "JA", "FR", "DE", "ES", "IT"]
        try:
            region_row.set_selected(regions.index(settings.cover_region))
        except ValueError:
            region_row.set_selected(0)
        region_row.connect("notify::selected", self._on_region_changed)
        self._region_row = region_row
        self._regions = regions
        group3.add(region_row)

        group4 = Adw.PreferencesGroup(title=_("Apariencia"))
        page.add(group4)
        self._schemes = [key for key, _label in styles.COLOR_SCHEME_LABELS]
        scheme_row = Adw.ComboRow(title=_("Tema"))
        scheme_row.set_subtitle(
            _("Solo tiene efecto con el tema estándar; un tema GTK de terceros "
              "manda sobre esto.")
        )
        scheme_row.set_model(
            Gtk.StringList.new([label for _key, label in styles.COLOR_SCHEME_LABELS])
        )
        try:
            scheme_row.set_selected(self._schemes.index(settings.color_scheme))
        except ValueError:
            scheme_row.set_selected(0)
        scheme_row.connect("notify::selected", self._on_scheme_changed)
        group4.add(scheme_row)

        self._build_shop_group(page)

        self.connect("closed", lambda *_a: self.on_saved(self.settings))

    # ------------------------------------------------------ Mi taller --
    # Los datos con los que se firma el Ticket de Entrega. Se guardan en la
    # configuración del usuario como el resto de las preferencias (al
    # cerrar el diálogo, ver `on_saved`) y nunca en el código: la app la
    # usan talleres distintos.
    _TICKET_THEMES = (config.TICKET_THEME_DARK, config.TICKET_THEME_LIGHT)

    def _build_shop_group(self, page):
        group = Adw.PreferencesGroup(
            title=_("Mi taller"),
            description=_("Datos que se imprimen en el Ticket de Entrega."),
        )
        page.add(group)

        def entry(title, attr, transform=None):
            row = Adw.EntryRow(title=title)
            row.set_text(getattr(self.settings, attr))

            def on_changed(r):
                texto = r.get_text()
                setattr(self.settings, attr,
                        transform(texto) if transform else texto)
            row.connect("changed", on_changed)
            group.add(row)
            return row

        entry(_("Nombre del taller"), "shop_name")
        entry(_("Eslogan"), "shop_slogan")
        entry(_("Ubicación"), "shop_location")
        whatsapp = entry(_("WhatsApp (con código de país, solo números)"),
                         "shop_whatsapp", config.clean_whatsapp)
        whatsapp.set_input_purpose(Gtk.InputPurpose.PHONE)

        self._logo_row = Adw.ActionRow(title=_("Logo"))
        self._logo_row.set_use_markup(False)
        self._update_logo_subtitle()
        clear_btn = Gtk.Button(icon_name="edit-clear-symbolic",
                               valign=Gtk.Align.CENTER,
                               tooltip_text=_("Quitar el logo"))
        clear_btn.add_css_class("flat")
        clear_btn.connect("clicked", self._clear_logo)
        pick_btn = Gtk.Button(icon_name="document-open-symbolic",
                              valign=Gtk.Align.CENTER,
                              tooltip_text=_("Elegir imagen"))
        pick_btn.connect("clicked", self._pick_logo)
        self._logo_row.add_suffix(clear_btn)
        self._logo_row.add_suffix(pick_btn)
        group.add(self._logo_row)

        color_row = Adw.ActionRow(title=_("Color de acento"))
        color_btn = Gtk.ColorDialogButton(
            dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        rgba = Gdk.RGBA()
        if not rgba.parse(self.settings.shop_accent_color):
            rgba.parse(config.DEFAULT_ACCENT_COLOR)
        color_btn.set_rgba(rgba)
        color_btn.connect("notify::rgba", self._on_accent_changed)
        color_row.add_suffix(color_btn)
        group.add(color_row)

        theme_row = Adw.ComboRow(title=_("Modo del ticket"))
        theme_row.set_model(Gtk.StringList.new(
            [_("Oscuro (marca)"), _("Claro (para imprimir)")]))
        try:
            theme_row.set_selected(
                self._TICKET_THEMES.index(self.settings.ticket_theme))
        except ValueError:
            theme_row.set_selected(0)
        theme_row.connect("notify::selected", self._on_ticket_theme_changed)
        group.add(theme_row)

    def _update_logo_subtitle(self):
        self._logo_row.set_subtitle(
            self.settings.shop_logo_path
            or _("Sin logo: se imprime el nombre del taller"))

    def _pick_logo(self, *_args):
        dialog = Gtk.FileDialog(title=_("Elegí el logo del taller"))
        filtro = Gtk.FileFilter()
        filtro.set_name(_("Imágenes"))
        filtro.add_mime_type("image/*")
        filtros = Gio.ListStore.new(Gtk.FileFilter)
        filtros.append(filtro)
        dialog.set_filters(filtros)
        dialog.open(self.get_root(), None, self._on_logo_picked)

    def _on_logo_picked(self, dialog, result):
        try:
            archivo = dialog.open_finish(result)
        except Exception:
            return
        if archivo and archivo.get_path():
            self.settings.shop_logo_path = archivo.get_path()
            self._update_logo_subtitle()

    def _clear_logo(self, *_args):
        self.settings.shop_logo_path = ""
        self._update_logo_subtitle()

    def _on_accent_changed(self, button, _param):
        c = button.get_rgba()
        self.settings.shop_accent_color = "#{:02X}{:02X}{:02X}".format(
            round(c.red * 255), round(c.green * 255), round(c.blue * 255))

    def _on_ticket_theme_changed(self, row, _param):
        idx = row.get_selected()
        if 0 <= idx < len(self._TICKET_THEMES):
            self.settings.ticket_theme = self._TICKET_THEMES[idx]

    def _pick_library_folder(self, *_args):
        dialog = Gtk.FileDialog(title=_("Elegí la carpeta de tu biblioteca"))
        dialog.select_folder(self.get_root(), None, self._on_library_picked)

    def _on_library_picked(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if folder:
            path = folder.get_path()
            self.settings.library_path = path
            self._library_row.set_subtitle(path)

    def _pick_wbfs_folder(self, *_args):
        dialog = Gtk.FileDialog(title=_("Elegí la carpeta/unidad WBFS"))
        dialog.select_folder(self.get_root(), None, self._on_wbfs_picked)

    def _on_wbfs_picked(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if folder:
            path = folder.get_path()
            self.settings.wbfs_drive_path = path
            self._wbfs_row.set_subtitle(path)

    def _on_region_changed(self, row, _param):
        idx = row.get_selected()
        if 0 <= idx < len(self._regions):
            self.settings.cover_region = self._regions[idx]

    def _on_scheme_changed(self, row, _param):
        idx = row.get_selected()
        if 0 <= idx < len(self._schemes):
            self.settings.color_scheme = self._schemes[idx]
            # Se aplica en el acto para que se vea el cambio mientras el
            # diálogo sigue abierto; el guardado en disco lo hace la
            # ventana al cerrarse (`on_saved`).
            styles.apply_color_scheme(self.settings.color_scheme)
