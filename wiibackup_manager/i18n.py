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
    from .i18n import _, N_

    _("Eliminar")                       # cadena suelta
    _("Eliminado: {name}").format(...)  # con datos
    N_("Pendiente")                     # se traduce en otro momento

`N_` es para el caso en que la cadena se ESCRIBE en un lugar y se MUESTRA
en otro: el valor de un enum, por ejemplo, que se define al importar el
módulo -cuando todavía no hay ninguna pantalla- y se traduce recién al
dibujar la fila. Ver más abajo, en su propia docstring.

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


def N_(message: str) -> str:
    """Marca una cadena para el catálogo SIN traducirla todavía.

    Devuelve el texto tal cual: es una función identidad. Lo único que
    hace es ser visible para `xgettext`, que arma el catálogo leyendo el
    fuente y no puede saber qué cadena termina adentro de un `_()` si esa
    cadena se escribió en otro lado.

    El caso que la trajo son los valores de los enums que se muestran al
    usuario (`OperationKind`, `queue_manager.JobStatus`). Ahí la cadena se
    define al importar el módulo -cuando todavía no hay ninguna ventana ni
    idioma que valga- y se traduce recién al mostrarla, con `_(self.value)`
    en la propiedad `.label`. Para `xgettext` eso es invisible: lo único
    que ve es `_(self.value)`, una variable, así que esos textos no
    llegaban nunca a la plantilla y `.label` devolvía el español aunque la
    app estuviera en inglés. Envolviendo el valor en `N_` la cadena entra
    al catálogo, y `.label` la encuentra.

    Para que esto funcione, `tools/update-translations.sh` le pasa
    `--keyword=N_` a `xgettext`; agregar otro marcador quiere decir
    agregarlo también ahí."""
    return message


def current_language() -> str:
    """Idioma del catálogo cargado ('es' si no hay ninguno), para el
    diálogo Acerca de y para poder verificarlo en las pruebas."""
    info = getattr(_translation, "info", lambda: {})()
    return (info.get("language") or "es").split("_")[0]
