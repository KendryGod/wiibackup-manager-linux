from __future__ import annotations

import dataclasses

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .. import config, styles, wit_wrapper
from ..i18n import _
from . import gtk_helpers

# Lo que vale `Settings.wit_binary` por defecto: buscar `wit` en el PATH.
WIT_DEL_SISTEMA = config.Settings().wit_binary


def _estado_de_wit(binario: str) -> str:
    """El subtítulo de la fila de `wit`: si está, con su versión y dónde; si
    no está, o si lo que hay ahí no contesta como `wit`."""
    version = wit_wrapper.wit_version(binario)
    if version is not None:
        return (_("Estado: {status}").format(status=_("detectado ✓")) + " · "
                + _("{version} en {path}").format(
                    version=version, path=wit_wrapper.find_wit(binario)))
    if wit_wrapper.find_wit(binario) is not None:
        return _("Estado: {status}").format(status=_("no es `wit` ✗")) + " · " + binario
    estado = _("Estado: {status}").format(status=_("no encontrado ✗"))
    return estado if binario == WIT_DEL_SISTEMA else f"{estado} · {binario}"


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

        self._wit_row = Adw.ActionRow(title="wit (Wiimms ISO Tools)")
        self._wit_row.set_use_markup(False)
        self._wit_row.set_subtitle(_estado_de_wit(settings.wit_binary))
        # Elegir el archivo de `wit` (si no está en el PATH, o para usar
        # otra versión) y volver a buscarlo en el sistema.
        self._wit_pick_btn = Gtk.Button(icon_name="document-open-symbolic",
                                        valign=Gtk.Align.CENTER,
                                        tooltip_text=_("Elegir el programa `wit`"))
        self._wit_pick_btn.connect("clicked", self._pick_wit)
        self._wit_reset_btn = Gtk.Button(icon_name="edit-undo-symbolic",
                                         valign=Gtk.Align.CENTER,
                                         tooltip_text=_("Volver a buscar `wit` en el sistema"))
        self._wit_reset_btn.connect("clicked", lambda *_a: self._usar_wit(WIT_DEL_SISTEMA))
        self._wit_reset_btn.set_sensitive(settings.wit_binary != WIT_DEL_SISTEMA)
        self._wit_row.add_suffix(self._wit_pick_btn)
        self._wit_row.add_suffix(self._wit_reset_btn)
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

        # Al cerrar se guarda solo si algo cambió: guardar siempre pisaba
        # config.json con lo que hubiera en memoria aunque nadie hubiera
        # tocado nada.
        self._al_abrir = dataclasses.asdict(settings)
        self.connect("closed", self._on_closed)

    def _on_closed(self, *_args):
        if dataclasses.asdict(self.settings) != self._al_abrir:
            self.on_saved(self.settings)

    def _pick_library_folder(self, *_args):
        dialog = Gtk.FileDialog(title=_("Elegí la carpeta de tu biblioteca"))
        dialog.select_folder(self.get_root(), None, self._on_library_picked)

    def _on_library_picked(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if folder:
            path = gtk_helpers.local_path(folder)
            if path is None:
                self.add_toast(Adw.Toast(title=gtk_helpers.no_local_path_message()))
                return
            self.settings.library_path = str(path)
            self._library_row.set_subtitle(str(path))

    def _pick_wbfs_folder(self, *_args):
        dialog = Gtk.FileDialog(title=_("Elegí la carpeta/unidad WBFS"))
        dialog.select_folder(self.get_root(), None, self._on_wbfs_picked)

    def _on_wbfs_picked(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except Exception:
            return
        if folder:
            path = gtk_helpers.local_path(folder)
            if path is None:
                self.add_toast(Adw.Toast(title=gtk_helpers.no_local_path_message()))
                return
            self.settings.wbfs_drive_path = str(path)
            self._wbfs_row.set_subtitle(str(path))

    def _pick_wit(self, *_args):
        dialog = Gtk.FileDialog(title=_("Elegí el programa `wit`"))
        dialog.open(self.get_root(), None, self._on_wit_picked)

    def _on_wit_picked(self, dialog, result):
        try:
            archivo = dialog.open_finish(result)
        except Exception:
            return
        if archivo:
            path = gtk_helpers.local_path(archivo)
            if path is None:
                self.add_toast(Adw.Toast(title=gtk_helpers.no_local_path_message()))
                return
            self._usar_wit(str(path))

    def _usar_wit(self, binario: str) -> bool:
        """Usa `binario` como `wit` si contesta como tal, y lo muestra.
        Volver al del sistema (`WIT_DEL_SISTEMA`) se acepta siempre: es el
        valor por defecto, aunque ahora no haya ninguno instalado. Lo que no
        es `wit` no cambia nada y se avisa. El guardado lo hace la ventana al
        cerrarse el diálogo (`on_saved`)."""
        if binario != WIT_DEL_SISTEMA and wit_wrapper.wit_version(binario) is None:
            self.add_toast(Adw.Toast(title=_(
                "{path} no es `wit` (Wiimms ISO Tools): no se cambió nada.").format(
                    path=binario)))
            return False
        self.settings.wit_binary = binario
        self._wit_row.set_subtitle(_estado_de_wit(binario))
        self._wit_reset_btn.set_sensitive(binario != WIT_DEL_SISTEMA)
        return True

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
