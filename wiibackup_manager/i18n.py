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
import sys
from pathlib import Path

DOMAIN = "wiibackup-manager"


def _candidate_dirs() -> list:
    """Directorios donde puede estar el catálogo, del más específico al
    más general."""
    paquete = Path(__file__).resolve().parent
    candidatos = [
        # Repo clonado sin instalar: <repo>/data/locale
        paquete.parent / "data" / "locale",
    ]
    # El prefijo deducido de dónde quedó instalado el paquete, que es el
    # único que distingue los tres casos: site-packages de ~/.local (pip
    # --user), de un venv, o del sistema. `sys.prefix` no sirve solo para
    # esto: con `pip install --user` sigue siendo /usr, así que un
    # catálogo viejo del sistema le ganaría al recién instalado.
    for padre in paquete.parents:
        if padre.name in ("site-packages", "dist-packages"):
            # …/<prefix>/lib/pythonX.Y/site-packages → <prefix>
            candidatos.append(padre.parent.parent.parent / "share" / "locale")
            break
    candidatos += [
        Path(sys.prefix) / "share" / "locale",
        Path.home() / ".local" / "share" / "locale",
        Path("/usr/local/share/locale"),
        Path("/usr/share/locale"),
    ]
    vistos = []
    for c in candidatos:
        if c not in vistos:
            vistos.append(c)
    return vistos


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
