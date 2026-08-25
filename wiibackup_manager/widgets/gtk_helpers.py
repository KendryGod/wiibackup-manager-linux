"""Utilidades chicas compartidas por los diálogos de GTK."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import gi

gi.require_version("Adw", "1")
from gi.repository import Adw, Gio  # noqa: E402

from ..i18n import _


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


def confirm_overwrite(parent, body: str, on_overwrite: Callable[[], None]) -> None:
    """Diálogo de confirmación antes de pisar un archivo que ya existe.

    Compartido por las dos operaciones que pueden reemplazar un archivo
    del usuario sin vuelta atrás (convertir ISO<->WBFS y enviar un juego a
    una unidad WBFS), para que las dos pregunten igual en vez de que una
    sobrescriba en silencio. `on_overwrite` se llama solo si el usuario
    elige "Sobrescribir".
    """
    dialog = Adw.AlertDialog(
        heading=_("¿Sobrescribir el archivo existente?"),
        body=body,
    )
    dialog.add_response("cancel", _("Cancelar"))
    dialog.add_response("overwrite", _("Sobrescribir"))
    dialog.set_response_appearance("overwrite", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.connect(
        "response",
        lambda _d, response: on_overwrite() if response == "overwrite" else None,
    )
    dialog.present(parent)


def widget_is_alive(widget) -> bool:
    """True si `widget` sigue montado en una jerarquía con ventana.

    Sirve para los callbacks que llegan de un hilo de fondo (carátulas y
    metadata de GameTDB, que se reenvían al hilo de GTK con
    `GLib.idle_add`): entre que la descarga arranca y termina, la fila
    puede haber desaparecido de la lista -por un reordenamiento, un
    filtro, un rescan- o el panel de detalle puede haberse cerrado. Tocar
    las propiedades de un widget ya sacado de la jerarquía (y, en el caso
    de un Adw.Dialog cerrado, ya dispuesto por GTK) va desde "no se ve
    nada" hasta tirar la app entera con un error fatal de GTK.

    Un widget que todavía no se agregó a ninguna ventana también da
    False, que es lo correcto acá: todos los callbacks que usan esto
    corren por `GLib.idle_add`, o sea después de que la fila ya se agregó
    a la lista (o el diálogo ya se presentó), nunca antes.
    """
    try:
        return widget.get_root() is not None
    except Exception:
        # El objeto de C ya no está: PyGObject puede levantar cualquier
        # cosa al tocarlo. Sea lo que sea, el widget no está vivo.
        return False
