from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio  # noqa: E402

from .config import APP_ID
from .window import WiiBackupWindow


class WiiBackupApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self._window: WiiBackupWindow | None = None

    def do_activate(self):
        if self._window is None:
            self._window = WiiBackupWindow(self)
        self._window.present()


def main() -> int:
    app = WiiBackupApp()
    return app.run(None)
