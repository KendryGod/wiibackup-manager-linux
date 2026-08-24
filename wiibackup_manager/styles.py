"""Hoja de estilos propia de la app.

GTK/libadwaita ya trae clases para casi todo lo que usamos ("boxed-list",
"dim-label", "suggested-action", …). Acá van solo los estilos que la
plataforma no da hechos: por ahora, los colores de la barra de uso de
disco del destino en la pestaña Transferir.

Los colores salen de los nombres de color de libadwaita (@success_color,
@warning_color, @error_color) y no de valores fijos, para que sigan al
tema claro/oscuro del sistema en vez de quedar clavados en un verde que
se ve mal sobre fondo oscuro.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

# Estructura de nodos CSS de un Gtk.LevelBar continuo:
#     levelbar > trough > block.filled  (+ block.empty)
# La clase de estado va en el propio <levelbar> y no en el bloque porque es
# lo que la app puede tocar desde código (`add_css_class` sobre el widget).
APP_CSS = b"""
levelbar.disk-usage,
levelbar.disk-usage > trough,
levelbar.disk-usage > trough > block {
    min-height: 10px;
}
levelbar.disk-usage > trough,
levelbar.disk-usage > trough > block {
    border-radius: 5px;
}
levelbar.disk-usage.usage-ok > trough > block.filled {
    background-color: @success_color;
}
levelbar.disk-usage.usage-warn > trough > block.filled {
    background-color: @warning_color;
}
levelbar.disk-usage.usage-full > trough > block.filled {
    background-color: @error_color;
}
"""

# Por qué USER+1 y no PRIORITY_APPLICATION (que sería lo esperable para el
# CSS de una app): GTK carga ~/.config/gtk-4.0/gtk.css con
# PRIORITY_USER (800), y los temas de terceros que se instalan ahí (muy
# comunes en KDE y en escritorios personalizados) traen sus propias reglas
# genéricas para `levelbar > trough > block`. Con PRIORITY_APPLICATION
# (600) esas reglas le ganan a las de acá y la barra queda siempre del
# color de acento del tema, sin importar cuán llena esté la unidad —que es
# justo el dato que la barra tiene que comunicar.
#
# Pisar al tema del usuario es aceptable en este caso puntual porque el
# selector está acotado a la clase `.disk-usage`, que no existe fuera de
# esta app: no se le cambia el aspecto a ningún otro widget.
_CSS_PRIORITY = Gtk.STYLE_PROVIDER_PRIORITY_USER + 1

_loaded = False


def load_css() -> None:
    """Registra la hoja de estilos en el display por defecto. Idempotente:
    llamarla dos veces no apila dos proveedores con el mismo CSS."""
    global _loaded
    if _loaded:
        return
    display = Gdk.Display.get_default()
    if display is None:
        # Sin display (p. ej. corriendo en un entorno sin gráficos) no hay
        # dónde registrar el CSS; no es motivo para reventar.
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(APP_CSS)
    Gtk.StyleContext.add_provider_for_display(display, provider, _CSS_PRIORITY)
    _loaded = True


# Apariencia elegida en Preferencias -> esquema de color de libadwaita.
#
# Se usa AdwStyleManager y no GtkSettings:gtk-application-prefer-dark-theme,
# que es la propiedad vieja: libadwaita la ignora y avisa por consola
# ("Using GtkSettings:gtk-application-prefer-dark-theme with libadwaita is
# unsupported"). Ojo: ese aviso, si aparece, NO lo produce esta app sino la
# configuración del escritorio (~/.config/gtk-4.0/settings.ini con
# `gtk-application-prefer-dark-theme=1`), y libadwaita lo emite al
# inicializarse, antes de que la app pueda ejecutar una sola línea: no hay
# forma de silenciarlo desde acá, se saca borrando esa línea del
# settings.ini.
#
# Por defecto va "system": una app no tiene por qué decidir el aspecto del
# escritorio ajeno. Las otras dos opciones existen porque los temas GTK4 de
# terceros reemplazan la hoja de estilos de libadwaita, y ahí el esquema
# que libadwaita cree tener y lo que el tema realmente pinta pueden no
# coincidir; poder forzarlo a mano es la salida cuando eso pasa.
_COLOR_SCHEMES = {
    "system": Adw.ColorScheme.DEFAULT,
    "light": Adw.ColorScheme.FORCE_LIGHT,
    "dark": Adw.ColorScheme.FORCE_DARK,
}
DEFAULT_COLOR_SCHEME = "system"

COLOR_SCHEME_LABELS = [
    ("system", "Sistema"),
    ("light", "Claro"),
    ("dark", "Oscuro"),
]


def apply_color_scheme(name: str) -> None:
    """Aplica la apariencia elegida. Un valor desconocido (config.json
    editado a mano) cae en la del sistema en vez de reventar."""
    scheme = _COLOR_SCHEMES.get(name, _COLOR_SCHEMES[DEFAULT_COLOR_SCHEME])
    Adw.StyleManager.get_default().set_color_scheme(scheme)
