"""Diálogo del Recovery Manager: qué se encontró y qué se puede hacer.

Lo que aparece acá ya pasó por todos los filtros de `recovery_service`
(dueño muerto, sin operación en curso encima). Este archivo es la parte
que decide cómo se le cuenta a una persona: cómo se nombra cada resto,
cuánto pesa, hace cuánto está ahí, y qué se le pregunta antes de tocar
nada.

Las dos preguntas que hace, y por qué
-------------------------------------
- **Eliminar siempre confirma.** Un resto puede ser un WBFS de 4 GB o el
  único respaldo del juego que el cliente tenía en la unidad. No hay
  papelera de por medio -`SetAside.discard` borra de verdad- así que la
  confirmación es la última chance, y por eso el tamaño va en el cuerpo
  del diálogo y no escondido en la fila de atrás.
- **Restaurar confirma solo si va a pisar algo.** Devolver un respaldo a
  un nombre que está libre no destruye nada. Pero si en ese nombre hay
  algo AHORA -lo que la operación interrumpida alcanzó a escribir- el
  `os.replace` lo reemplaza sin avisar, y eso sí hay que preguntarlo.

"Ignorar por ahora" no toca el disco: saca el resto de la lista de esta
sesión y nada más. Al próximo arranque vuelve a aparecer si sigue estando,
que es a propósito -es un problema real que sigue ocupando lugar, y la app
no tiene por qué acordarse de que un día alguien lo postergó.

Por qué las acciones corren en el hilo de GTK
---------------------------------------------
Porque ninguna mueve bytes. Restaurar es un `os.replace` dentro de la
misma carpeta (instantáneo, sin importar si el archivo pesa 4 GB) y
eliminar es un `unlink` o el `rmtree` de una staging a medio armar. Lo
único caro del Recovery Manager es medir los tamaños, y eso ya pasó en el
hilo de fondo del escaneo, antes de que este diálogo existiera.
"""
from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

from .. import recovery_service
from ..i18n import _, ngettext
from ..library import format_size

# Cortes para decir la antigüedad en la unidad que se entiende de un
# vistazo. No se muestran minutos y horas juntos: quien mira esto está
# decidiendo si algo es basura vieja o algo de recién, y para eso "hace 3
# días" alcanza y sobra.
_MINUTO = 60
_HORA = 60 * _MINUTO
_DIA = 24 * _HORA


def format_age(seconds: float) -> str:
    """Antigüedad en palabras: "hace 3 días", "hace 5 horas", "recién".

    El caso "recién" existe para los respaldos: un resto puede ser de hace
    dos minutos si la app se cerró mal recién, y decir "hace 0 minutos"
    quedaría raro."""
    if seconds < _MINUTO:
        return _("recién")
    if seconds < _HORA:
        n = int(seconds // _MINUTO)
        return ngettext("hace {n} minuto", "hace {n} minutos", n).format(n=n)
    if seconds < _DIA:
        n = int(seconds // _HORA)
        return ngettext("hace {n} hora", "hace {n} horas", n).format(n=n)
    n = int(seconds // _DIA)
    return ngettext("hace {n} día", "hace {n} días", n).format(n=n)


def summary_text(leftovers: list) -> str:
    """El texto del aviso de la ventana: cuántos son y cuánto ocupan.

    Vive acá y no en la ventana para que el aviso y el diálogo que abre
    hablen de lo mismo con las mismas palabras. El espacio va en el
    resumen porque es la razón por la que alguien va a apretar "Ver
    detalles": son GB que no sabía que tenía ocupados."""
    cuantos, bytes_totales = recovery_service.summary(leftovers)
    return ngettext(
        "Se encontró {n} resto de una operación interrumpida ({size}).",
        "Se encontraron {n} restos de operaciones interrumpidas ({size}).",
        cuantos).format(n=cuantos, size=format_size(bytes_totales))


class RecoveryDialog(Adw.Dialog):
    """Lista los restos encontrados, uno por grupo, con sus acciones.

    `on_resolved(leftover)` se llama cada vez que uno sale de la lista -se
    restauró, se eliminó o se ignoró-, para que la ventana actualice su
    aviso. El diálogo no sabe qué hace la ventana con eso; solo avisa.

    `ops` es el `OperationManager`: se lo vuelve a consultar antes de cada
    acción, no solo durante el escaneo. Ver `is_locked_by_operation`."""

    def __init__(self, leftovers: list, *, ops, show_toast, on_resolved):
        super().__init__()
        self._ops = ops
        self._show_toast = show_toast
        self._on_resolved = on_resolved
        self._grupos: dict = {}

        self.set_title(_("Operaciones interrumpidas"))
        self.set_content_width(600)
        self.set_content_height(520)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        self._page = Adw.PreferencesPage()
        self._intro = Adw.PreferencesGroup(
            description=_(
                "Son archivos que quedaron de operaciones que no llegaron a "
                "terminar (por ejemplo, si se apagó la PC en el medio). Los "
                "procesos que los dejaron ya no están corriendo, así que se "
                "pueden limpiar sin interrumpir nada."))
        self._page.add(self._intro)

        for leftover in leftovers:
            grupo = self._build_group(leftover)
            self._grupos[leftover.path] = grupo
            self._page.add(grupo)

        toolbar.set_content(self._page)
        self.set_child(toolbar)

    # ------------------------------------------------------------ Filas --
    def _build_group(self, leftover) -> Adw.PreferencesGroup:
        grupo = Adw.PreferencesGroup(title=leftover.kind.label,
                                     description=leftover.kind.description)

        fila = Adw.ActionRow(title=leftover.original.name)
        # Sin markup: los nombres de archivo del usuario pueden traer `&` o
        # `<`, y con markup activado (el default de las filas de Adwaita)
        # eso rompe el renderizado de la fila entera.
        fila.set_use_markup(False)
        fila.set_subtitle(self._subtitle(leftover))
        fila.set_subtitle_lines(0)
        grupo.add(fila)

        botones = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6,
                          valign=Gtk.Align.CENTER)

        if leftover.restorable:
            restaurar = Gtk.Button(label=_("Restaurar"))
            restaurar.set_tooltip_text(
                _("Devolver este archivo a su nombre original: {name}")
                .format(name=leftover.original.name))
            restaurar.connect("clicked", lambda *_a: self._on_restore(leftover))
            botones.append(restaurar)

        eliminar = Gtk.Button(label=_("Eliminar"))
        eliminar.add_css_class("destructive-action")
        eliminar.connect("clicked", lambda *_a: self._on_delete(leftover))
        botones.append(eliminar)

        ignorar = Gtk.Button(label=_("Ignorar por ahora"))
        ignorar.add_css_class("flat")
        ignorar.set_tooltip_text(
            _("No tocarlo. Vuelve a aparecer la próxima vez que abras la app."))
        ignorar.connect("clicked", lambda *_a: self._on_ignore(leftover))
        botones.append(ignorar)

        fila.add_suffix(botones)
        return grupo

    def _subtitle(self, leftover) -> str:
        """Tamaño, antigüedad y dónde está.

        La ruta completa va sí o sí: son archivos ocultos, y sin el
        "dónde" el usuario no puede ir a verlos por su cuenta antes de
        decidir -que es exactamente lo que uno quiere poder hacer antes de
        borrar algo de 4 GB."""
        partes = [format_size(leftover.size_bytes),
                  format_age(leftover.age_seconds())]
        if leftover.original_exists and leftover.restorable:
            partes.append(_("el nombre original está ocupado"))
        return " · ".join(partes) + "\n" + str(leftover.path)

    # --------------------------------------------------------- Acciones --
    def _sigue_libre(self, leftover) -> bool:
        """Revalida contra el `OperationManager` justo antes de tocar el
        disco: entre que se armó esta lista y el click pudo arrancar una
        transferencia sobre esa misma unidad."""
        if self._ops is None:
            return True
        if not recovery_service.is_locked_by_operation(self._ops, leftover):
            return True
        self._show_toast(
            _("Ahora hay una operación usando esa ubicación. Probá de nuevo "
              "cuando termine."))
        return False

    def _on_restore(self, leftover):
        if not self._sigue_libre(leftover):
            return
        if not leftover.original_exists:
            # El nombre original está libre: devolverlo no pisa nada.
            self._restaurar(leftover)
            return

        dialog = Adw.AlertDialog(
            heading=_("¿Reemplazar «{name}»?").format(
                name=leftover.original.name),
            body=_(
                "Ya hay un archivo con ese nombre: es lo que alcanzó a "
                "escribir la operación que se interrumpió, así que "
                "probablemente esté incompleto.\n\n"
                "Restaurar lo reemplaza por el respaldo ({size}) y no se "
                "puede deshacer.").format(size=format_size(leftover.size_bytes)))
        dialog.add_response("cancel", _("Cancelar"))
        dialog.add_response("restore", _("Reemplazar"))
        dialog.set_response_appearance("restore",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _d, r: self._restaurar(leftover) if r == "restore" else None)
        dialog.present(self)

    def _restaurar(self, leftover):
        try:
            recovery_service.restore(leftover)
        except recovery_service.RecoveryError as e:
            self._show_toast(str(e))
            return
        self._show_toast(
            _("«{name}» volvió a su lugar.").format(
                name=leftover.original.name))
        self._resolver(leftover)

    def _on_delete(self, leftover):
        if not self._sigue_libre(leftover):
            return

        cuerpo = _(
            "Se van a liberar {size}.\n\n{ruta}\n\nNo se puede deshacer."
        ).format(size=format_size(leftover.size_bytes), ruta=leftover.path)
        if leftover.restorable:
            # Es la copia entera de un archivo del usuario, no un temporal:
            # eliminarla es la única acción de este diálogo que puede
            # perder datos que todavía se podían recuperar.
            cuerpo = _(
                "Es una copia completa de «{name}». Si la eliminás, ya no "
                "vas a poder restaurarla.\n\n"
            ).format(name=leftover.original.name) + cuerpo

        dialog = Adw.AlertDialog(
            heading=_("¿Eliminar este resto?"), body=cuerpo)
        dialog.add_response("cancel", _("Cancelar"))
        dialog.add_response("delete", _("Eliminar"))
        dialog.set_response_appearance("delete",
                                       Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _d, r: self._eliminar(leftover) if r == "delete" else None)
        dialog.present(self)

    def _eliminar(self, leftover):
        try:
            recovery_service.delete(leftover)
        except recovery_service.RecoveryError as e:
            self._show_toast(str(e))
            return
        self._show_toast(
            _("Espacio liberado: {size}.").format(
                size=format_size(leftover.size_bytes)))
        self._resolver(leftover)

    def _on_ignore(self, leftover):
        self._resolver(leftover)

    def _resolver(self, leftover):
        """Saca el resto de la lista y, si no queda ninguno, cierra: un
        diálogo vacío no le dice nada a nadie."""
        grupo = self._grupos.pop(leftover.path, None)
        if grupo is not None:
            self._page.remove(grupo)
        self._on_resolved(leftover)
        if not self._grupos:
            self.close()
