from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402

from .config import APP_ID
from .i18n import _
from .styles import load_css

# La libadwaita más vieja con la que arranca la ventana. La 1.5 trae
# `Adw.Dialog`, `Adw.AlertDialog`, `Adw.AboutDialog` y
# `Adw.PreferencesDialog`, y varios diálogos de la app heredan de ellas AL
# IMPORTARSE (`TicketDialog`, `PreferencesDialog`...): con una 1.4,
# `from .window import WiiBackupWindow` revienta con un AttributeError antes
# de que haya ventana. Por eso la ventana se importa recién en `do_activate`,
# después de mirar la versión.
ADW_MINIMA = (1, 5)


def version_de_libadwaita() -> tuple:
    return (Adw.get_major_version(), Adw.get_minor_version(), Adw.get_micro_version())


def problema_de_libadwaita(version: tuple | None = None) -> str | None:
    """El aviso para el usuario si la libadwaita del sistema es más vieja
    que `ADW_MINIMA`, o None si alcanza."""
    version = version_de_libadwaita() if version is None else version
    if tuple(version[:2]) >= ADW_MINIMA:
        return None
    return _("WiiBackup Manager necesita libadwaita {minima} o más nueva, y este "
             "sistema tiene la {actual}. Actualizá libadwaita (viene con GNOME 46 "
             "o más nuevo) y volvé a abrir la app.").format(
                 minima=".".join(map(str, ADW_MINIMA)),
                 actual=".".join(map(str, version)))


class WiiBackupApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self._window = None
        # Con lo que sale `main` si la app no pudo abrir (ver
        # `_avisar_y_salir`): `run` devuelve 0 aunque no haya habido ventana.
        self.exit_status = 0

    def do_activate(self):
        problema = problema_de_libadwaita()
        if problema is not None:
            print(f"[wiibackup-manager] {problema}", file=sys.stderr)
            self.exit_status = 1
            self._avisar_y_salir(problema)
            return
        # El CSS propio se carga acá y no en el import: necesita un
        # Gdk.Display abierto, que recién existe cuando la aplicación se
        # activa. `load_css` es idempotente, así que reactivar la app (por
        # ejemplo al lanzarla de nuevo desde el menú) no apila proveedores.
        load_css()
        if self._window is None:
            # Recién acá, con la versión ya comprobada: ver `ADW_MINIMA`.
            from .window import WiiBackupWindow
            self._window = WiiBackupWindow(self)
        self._window.present()

    def _avisar_y_salir(self, problema: str) -> None:
        """El aviso, en un diálogo de GTK puro y no de libadwaita, que es
        justo lo que falta: `Gtk.MessageDialog` existe en cualquier GTK 4. Va
        atado a la aplicación para que no se cierre antes de que se lea, y al
        cerrarlo la app termina (no le queda ninguna ventana)."""
        dialogo = Gtk.MessageDialog(
            message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.CLOSE,
            text=_("No se puede abrir WiiBackup Manager"), secondary_text=problema)
        dialogo.set_application(self)
        dialogo.connect("response", lambda d, _respuesta: d.destroy())
        dialogo.present()


def main() -> int:
    app = WiiBackupApp()
    estado = app.run(None)
    return estado or app.exit_status
