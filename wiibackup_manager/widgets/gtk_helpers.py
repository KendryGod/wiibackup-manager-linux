"""Utilidades chicas compartidas por los diálogos de GTK."""
from __future__ import annotations

from pathlib import Path

from gi.repository import Gio


def safe_initial_folder(preferred: str | Path | None = None) -> Gio.File:
    """Gio.File para usar como carpeta inicial de un Gtk.FileDialog.

    GTK recuerda en dconf la última carpeta que usó cualquier selector de
    archivos (org.gtk.Settings.FileChooser, compartido entre apps). Si esa
    carpeta ya no existe -por ejemplo una unidad USB que se desconectó- el
    diálogo nativo intenta navegar ahí solo y falla con un error feo ("No
    se pudo encontrar «...»"). Fijar `initial_folder` explícitamente evita
    que use esa ubicación recordada: se usa `preferred` si sigue siendo una
    carpeta accesible, y si no, cae en silencio a la carpeta home del
    usuario (que siempre existe).
    """
    if preferred is not None:
        candidate = Path(preferred)
        try:
            if candidate.is_dir():
                return Gio.File.new_for_path(str(candidate))
        except OSError:
            pass
    return Gio.File.new_for_path(str(Path.home()))
