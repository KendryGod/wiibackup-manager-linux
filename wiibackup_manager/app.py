from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio  # noqa: E402

from .config import APP_ID
from .styles import load_css
from .window import WiiBackupWindow


class WiiBackupApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self._window: WiiBackupWindow | None = None

    def do_activate(self):
        # El CSS propio se carga acá y no en el import: necesita un
        # Gdk.Display abierto, que recién existe cuando la aplicación se
        # activa. `load_css` es idempotente, así que reactivar la app (por
        # ejemplo al lanzarla de nuevo desde el menú) no apila proveedores.
        load_css()
        if self._window is None:
            self._window = WiiBackupWindow(self)
        self._window.present()


def main() -> int:
    app = WiiBackupApp()
    return app.run(None)
