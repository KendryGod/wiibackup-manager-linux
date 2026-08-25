"""Mover archivos a la papelera del sistema en vez de borrarlos.

Eliminar un juego solía ser un `Path.unlink()`: irreversible, y sobre
archivos de varios GB que tardan horas en volver a copiarse desde el disco
original. Ahora pasa por la papelera del escritorio, así que un borrado por
error se deshace desde Archivos (Nautilus) como cualquier otro archivo.

La papelera NO se implementa acá a mano. `Gio.File.trash()` ya hace todo lo
que pide la spec de freedesktop -mover el archivo, escribir el `.trashinfo`
con la ruta original y la fecha para poder restaurarlo, y elegir el
directorio de papelera correcto según dónde viva el archivo-. Reimplementar
eso sería garantizar que la papelera de esta app no sea la del sistema.

Ese "según dónde viva el archivo" es el detalle que importa acá, porque la
biblioteca suele estar en un disco externo: la papelera no es una sola.
Para lo que está en el disco del usuario es `~/.local/share/Trash`, pero un
archivo de otro sistema de archivos no se puede mover ahí (sería copiar
gigas de una unidad a otra, no un rename), así que la spec manda usar una
papelera en la raíz de esa misma unidad -`$topdir/.Trash-$uid`-. GIO la
crea sola cuando puede.

Cuando NO puede, no hay papelera posible para ese archivo. Los casos
reales: unidades montadas de solo lectura, y sistemas de archivos donde no
se puede crear el directorio de papelera en la raíz. Ahí `can_trash()`
devuelve False y el que llama avisa que el borrado va a ser definitivo,
en vez de prometer una papelera que no existe.

Sobre FAT32/exFAT: la papelera sí funciona -la spec no pide permisos de
Unix ni enlaces simbólicos, solo poder crear un directorio-, así que un
pendrive con juegos no es de por sí un caso perdido. Por eso la decisión se
toma preguntándole a GIO archivo por archivo (`access::can-trash`) y no
adivinando a partir del tipo de sistema de archivos.
"""
from __future__ import annotations

from pathlib import Path

from gi.repository import Gio, GLib


class TrashUnsupported(OSError):
    """No hay papelera disponible para ese archivo (unidad de solo
    lectura, o sin lugar donde crear el directorio de papelera).

    Hereda de OSError a propósito: para quien llama es un borrado que no
    se pudo hacer, y los `except OSError` que ya rodean las operaciones de
    archivo lo atrapan sin cambios."""


def can_trash(path) -> bool:
    """Si ese archivo se puede mandar a la papelera.

    Es la misma pregunta que se hace Archivos (Nautilus) para decidir si
    ofrece "Mover a la papelera" o "Eliminar permanentemente": el atributo
    estándar `access::can-trash`, que GIO responde mirando la unidad donde
    está el archivo, no su extensión ni su tamaño.

    Ante cualquier duda devuelve False (archivo ya borrado, unidad
    desconectada a mitad, permisos raros). Un False de más solo hace que
    se avise "esto se borra definitivamente"; un True de más prometería
    una papelera que después no está."""
    gfile = Gio.File.new_for_path(str(path))
    try:
        info = gfile.query_info(
            Gio.FILE_ATTRIBUTE_ACCESS_CAN_TRASH,
            Gio.FileQueryInfoFlags.NONE,
            None,
        )
    except GLib.Error:
        return False
    if not info.has_attribute(Gio.FILE_ATTRIBUTE_ACCESS_CAN_TRASH):
        return False
    return info.get_attribute_boolean(Gio.FILE_ATTRIBUTE_ACCESS_CAN_TRASH)


def send_to_trash(path) -> None:
    """Manda el archivo a la papelera del sistema.

    Levanta `TrashUnsupported` si esa unidad no tiene papelera posible, y
    `OSError` con el motivo de GIO para cualquier otro fallo. Nunca cae en
    borrar el archivo de todas formas: quedarse sin papelera cambia lo que
    el usuario aceptó al confirmar, así que esa decisión vuelve a la
    interfaz, que pregunta de nuevo."""
    gfile = Gio.File.new_for_path(str(path))
    try:
        gfile.trash(None)
    except GLib.Error as e:
        if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_SUPPORTED):
            raise TrashUnsupported(
                f"la unidad donde está '{Path(path).name}' no tiene papelera"
            ) from e
        raise OSError(e.message) from e


def delete_permanently(path) -> None:
    """Borrado definitivo, para cuando no hay papelera y el usuario lo
    confirmó sabiendo que no se puede deshacer."""
    Path(path).unlink()
