"""Traducción de la interfaz (gettext).

El idioma original del código es el español: los `msgid` son las cadenas
en español tal como están escritas en el fuente. Eso tiene una
consecuencia útil: si no hay ninguna traducción instalada, o el idioma del
sistema no es uno de los soportados, gettext devuelve el `msgid` y la app
queda en español sin ningún caso especial. El fallback es el idioma
original, no un archivo que haya que mantener.

Cómo se elige el idioma
-----------------------
No lo elige la app: lo elige gettext leyendo LANGUAGE, LC_ALL, LC_MESSAGES
y LANG, en ese orden, que es la configuración regional del escritorio. Un
usuario con el sistema en inglés abre la app en inglés sin tocar nada, y
`LANGUAGE=en wiibackup-manager` la fuerza para una corrida suelta.

Dónde se buscan las traducciones
--------------------------------
En orden: el árbol del repo clonado (para poder correr la app sin
instalarla), y después los prefijos donde el instalador deja los .mo
-`~/.local/share/locale` con `pip install --user`, `/usr/share/locale`
con una instalación de sistema-. Se prueba cuál tiene realmente el
catálogo en vez de asumir uno: la misma app corre desde el repo, desde
~/.local y desde /usr según cómo se la haya instalado.

Uso
---
    from .i18n import _

    _("Eliminar")                       # cadena suelta
    _("Eliminado: {name}").format(...)  # con datos

Nunca `_(f"...")`: la f-string se interpola ANTES de buscar la traducción,
así que el msgid tendría adentro el nombre del archivo del usuario y no
coincidiría con ninguna entrada del catálogo. Por eso las cadenas con
datos usan `.format()` con marcadores nombrados, que además dejan que la
traducción reordene los datos si en su idioma el orden es otro.
"""
from __future__ import annotations

import gettext
import locale

from .fsutil import installed_data_dirs

DOMAIN = "wiibackup-manager"


def _candidate_dirs() -> list:
    """Directorios donde puede estar el catálogo, del más específico al
    más general.

    La búsqueda en sí (repo clonado / venv / pip --user / sistema) vive en
    `fsutil.installed_data_dirs`, compartida con la carpeta de configs
    maestras de `golden_configs`: acá solo se dice qué rutas buscar."""
    return installed_data_dirs("data/locale", "locale")


def _load() -> gettext.NullTranslations:
    """El catálogo del primer directorio que tenga uno para el idioma del
    sistema. Si ninguno lo tiene, devuelve el catálogo vacío de gettext,
    que hace que `_()` devuelva el español del fuente."""
    for localedir in _candidate_dirs():
        if gettext.find(DOMAIN, str(localedir)) is not None:
            return gettext.translation(DOMAIN, str(localedir), fallback=True)
    return gettext.NullTranslations()


# La configuración regional se aplica una sola vez, al importar: afecta
# cómo GTK formatea números y fechas. Si el sistema tiene un locale que no
# está generado, `setlocale` levanta y se sigue con el locale "C" en vez
# de no abrir la app por eso.
try:
    locale.setlocale(locale.LC_ALL, "")
except locale.Error:
    pass

_translation = _load()

_ = _translation.gettext
ngettext = _translation.ngettext


def current_language() -> str:
    """Idioma del catálogo cargado ('es' si no hay ninguno), para el
    diálogo Acerca de y para poder verificarlo en las pruebas."""
    info = getattr(_translation, "info", lambda: {})()
    return (info.get("language") or "es").split("_")[0]
