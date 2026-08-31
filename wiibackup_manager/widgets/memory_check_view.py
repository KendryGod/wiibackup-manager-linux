"""Página 'Verificar Memoria': confirmar con `f3` que un pendrive o una SD
tienen de verdad la capacidad que dicen, y -si pasan la prueba- ofrecer
formatearlos en FAT32 ahí mismo.

Por qué las dos cosas en la misma página
----------------------------------------
Porque es un solo trabajo del mundo real: llega la memoria de un cliente,
se verifica que no sea trucha, y queda lista para usar. La verificación
deja la memoria llena de los archivos de prueba de `f3` (se borran solos al
terminar, ver `f3_wrapper.cleanup_test_files`) y casi siempre lo que sigue
es formatearla, así que el botón está donde el usuario ya está mirando.

Encadenar ACÁ sí corresponde, a diferencia de la integración con Modo
Fábrica: no es una cadena automática de operaciones destructivas: la
verificación no borra nada, el formateo aparece como una OPCIÓN cuando
termina, y en el medio hay una decisión explícita del usuario -el mismo
diálogo de confirmación con la palabra escrita a mano que pide Modo
Fábrica.

El formateo que se ofrece acá es de propósito general -como GUIFormat en
Windows-: FAT32 y nada más. NO arma las carpetas apps/games/wbfs; eso es
específico de Wii y sigue siendo cosa de Modo Fábrica. Lo que sí es
exactamente el mismo son los blindajes: se llama a `drives.format_fat32`,
la misma función que usa Modo Fábrica por debajo (lista blanca de
removibles, chequeo de montajes críticos, re-verificación de identidad
justo antes de `mkfs.vfat`). La lista blanca no se afloja ni un poco por
ser un formateo "simple": el disco tiene que aparecer en
`drives.candidate_for_mount_point()` o el botón directamente no se muestra.

Igual que el resto de la app, esta vista solo dibuja y reacciona a clicks:
lo que sabe verificar es `f3_wrapper`, lo que sabe formatear es `drives`, y
todo lo que tarda corre en un hilo de fondo y vuelve al hilo de GTK con
`GLib.idle_add`.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from .. import drives, f3_wrapper, oplog, wit_wrapper
from ..i18n import _
from ..operations import OperationBusy, OperationKind, OperationOutcome
from . import gtk_helpers

# Cada cuánto, como máximo, se repinta la barra de progreso. `f3` escribe
# su línea de avance varias veces por segundo y cada una llegaría como un
# `GLib.idle_add`: a 4 por segundo la barra ya se ve fluida y el hilo de
# GTK queda libre para todo lo demás.
_PROGRESS_MIN_INTERVAL = 0.25

# Cada cuánto se vuelve a mirar qué unidades hay montadas, para que una
# memoria recién conectada aparezca sola en el desplegable (mismo criterio
# que el selector de destino de la Homebrew Store).
_POLL_SECONDS = 3


class MemoryCheckView(Gtk.Box):
    def __init__(self, show_toast_cb, ops=None, op_log=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._show_toast = show_toast_cb
        if ops is None:
            from ..operations import OperationManager
            ops = OperationManager()
        self.ops = ops
        self.op_log = op_log

        self._drives: list = []
        self._busy = False          # verificación en curso
        self._formatting = False    # formateo en curso
        self._cancel: Optional[wit_wrapper.CancellationToken] = None
        self._last_progress_at = 0.0
        # A QUÉ punto de montaje corresponde el veredicto que se está
        # mostrando: si el usuario cambia de unidad en el desplegable, el
        # resultado (y con él la opción de formatear) deja de aplicar.
        self._result_mount: Optional[Path] = None

        self._build_ui()
        self._refresh_drives()
        GLib.timeout_add_seconds(_POLL_SECONDS, self._poll_drives)

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        self._missing_banner = Adw.Banner(revealed=False)
        self.append(self._missing_banner)

        scroller = Gtk.ScrolledWindow(vexpand=True)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroller.set_child(content)
        self.append(scroller)

        # --- Elegir unidad y verificar ---
        group = Adw.PreferencesGroup(
            title=_("Verificar memoria"),
            description=_(
                "Escribe archivos de prueba en todo el espacio libre y los "
                "vuelve a leer para confirmar que la memoria tiene de verdad "
                "la capacidad que dice tener. Es la única forma de detectar "
                "un pendrive o una SD truchos, y tarda: contá alrededor de "
                "una hora por cada 30 GB. Al terminar borra sus archivos de "
                "prueba y no toca nada de lo que ya haya en la memoria "
                "-aunque conviene hacerla vacía, porque solo se verifica el "
                "espacio que esté libre."),
        )
        group.set_margin_start(12)
        group.set_margin_end(12)
        group.set_margin_top(12)

        self._drive_model = Gtk.StringList.new([])
        self._drive_row = Adw.ComboRow(title=_("Unidad"))
        self._drive_row.set_use_markup(False)
        self._drive_row.set_model(self._drive_model)
        self._drive_row.connect("notify::selected", self._on_drive_changed)
        group.add(self._drive_row)
        content.append(group)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          margin_start=12, margin_end=12, margin_top=8)
        self._refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        self._refresh_btn.set_tooltip_text(_("Volver a buscar memorias conectadas"))
        self._refresh_btn.connect("clicked", lambda *_a: self._refresh_drives())
        buttons.append(self._refresh_btn)

        self._check_btn = Gtk.Button(label=_("Verificar memoria…"))
        self._check_btn.add_css_class("suggested-action")
        self._check_btn.set_sensitive(False)
        self._check_btn.connect("clicked", self._on_check_clicked)
        buttons.append(self._check_btn)

        self._cancel_btn = Gtk.Button(label=_("Cancelar"))
        self._cancel_btn.set_visible(False)
        self._cancel_btn.connect("clicked", self._on_cancel_clicked)
        buttons.append(self._cancel_btn)
        content.append(buttons)

        self._empty_label = Gtk.Label(
            label=_("No hay ninguna memoria conectada. Conectá un pendrive o "
                    "una tarjeta SD y tocá el botón de actualizar."),
            wrap=True, xalign=0)
        self._empty_label.add_css_class("dim-label")
        self._empty_label.set_margin_start(12)
        self._empty_label.set_margin_end(12)
        self._empty_label.set_margin_top(8)
        content.append(self._empty_label)

        self._progress = Gtk.ProgressBar(visible=False, show_text=True)
        self._progress.set_margin_start(12)
        self._progress.set_margin_end(12)
        self._progress.set_margin_top(12)
        content.append(self._progress)

        # --- Resultado ---
        self._result_group = Adw.PreferencesGroup(visible=False)
        self._result_group.set_margin_start(12)
        self._result_group.set_margin_end(12)
        self._result_group.set_margin_top(12)
        self._result_row = Adw.ActionRow()
        self._result_row.set_use_markup(False)
        self._result_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        self._result_row.add_prefix(self._result_icon)
        self._result_group.add(self._result_row)
        content.append(self._result_group)

        # --- Formatear (aparece solo si la memoria pasó la prueba) ---
        self._format_group = Adw.PreferencesGroup(
            title=_("Formatear en FAT32"),
            description=_(
                "Formateo de propósito general: deja la memoria vacía y en "
                "FAT32, que lo lee cualquier cosa (Windows, teléfonos, "
                "cámaras, consolas). No arma las carpetas de Wii -para eso "
                "está Modo Fábrica. Borra TODO el contenido actual."),
            visible=False,
        )
        self._format_group.set_margin_start(12)
        self._format_group.set_margin_end(12)
        self._format_group.set_margin_top(12)
        self._format_group.set_margin_bottom(12)

        self._label_row = Adw.EntryRow(title=_("Etiqueta del volumen (opcional)"))
        self._label_row.set_show_apply_button(False)
        self._format_group.add(self._label_row)

        self._format_btn_row = Adw.ActionRow(
            title=_("Formatear esta memoria"),
            subtitle=_("Pide confirmación antes de tocar nada."))
        self._format_btn_row.set_use_markup(False)
        self._format_btn = Gtk.Button(label=_("Formatear en FAT32"), valign=Gtk.Align.CENTER)
        self._format_btn.add_css_class("destructive-action")
        self._format_btn.connect("clicked", self._on_format_clicked)
        self._format_btn_row.add_suffix(self._format_btn)
        self._format_btn_row.set_activatable_widget(self._format_btn)
        self._format_group.add(self._format_btn_row)
        content.append(self._format_group)

        self._update_missing_banner()

    def _update_missing_banner(self):
        """Aviso -y botón de verificar apagado- si falta instalar f3. Se
        vuelve a mirar en cada refresco: si el usuario lo instala con la
        app abierta, no hace falta reiniciarla."""
        faltan = f3_wrapper.missing_binaries()
        self._missing_banner.set_revealed(bool(faltan))
        if faltan:
            self._missing_banner.set_title(
                _("Falta instalar {binaries}, que es lo que hace la "
                  "verificación. En Fedora: sudo dnf install f3")
                .format(binaries=", ".join(faltan)))
        return not faltan

    # ------------------------------------------------------------ Unidades --
    def _refresh_drives(self):
        """Repuebla el desplegable con las unidades removibles MONTADAS: la
        verificación escribe archivos en el punto de montaje, así que acá
        importa lo que esté montado y no el disco crudo (ese lo resuelve
        `drives.candidate_for_mount_point` recién a la hora de formatear).

        Conserva la unidad elegida si sigue estando: el refresco automático
        cada pocos segundos no tiene por qué mover la selección debajo del
        mouse."""
        anterior = self._selected_drive()
        self._drives = drives.list_removable_drives()
        while self._drive_model.get_n_items():
            self._drive_model.remove(0)
        for unidad in self._drives:
            self._drive_model.append(self._drive_label(unidad))

        hay = bool(self._drives)
        self._empty_label.set_visible(not hay)
        self._drive_row.set_visible(hay)
        if hay:
            indice = 0
            if anterior is not None:
                for i, unidad in enumerate(self._drives):
                    if unidad.mount_point == anterior.mount_point:
                        indice = i
                        break
            self._drive_row.set_selected(indice)
        self._update_drive_subtitle()
        self._sync_result_visibility()
        self._update_sensitivity()

    @staticmethod
    def _drive_label(unidad) -> str:
        """Lo que se ve en el desplegable: corto, porque una fila de
        `Adw.ComboRow` no tiene mucho ancho y el resto (espacio libre y
        punto de montaje) se muestra abajo, en el subtítulo de la fila."""
        return _("{name} ({total:.1f} GB)").format(
            name=unidad.name, total=unidad.total_gb)

    @staticmethod
    def _drive_detail(unidad) -> str:
        """La descripción completa, para el subtítulo y para los diálogos:
        el espacio libre importa porque es EXACTAMENTE lo que se va a
        verificar."""
        return _("{free:.1f} GB libres de {total:.1f} GB — {mount}").format(
            free=unidad.free_gb, total=unidad.total_gb,
            mount=unidad.mount_point)

    def _update_drive_subtitle(self):
        unidad = self._selected_drive()
        self._drive_row.set_subtitle(
            self._drive_detail(unidad) if unidad is not None else "")

    def _poll_drives(self) -> bool:
        """Detecta unidades conectadas/desconectadas sin que el usuario
        toque el botón de actualizar. No hace nada mientras hay una
        operación en curso: repoblar el desplegable ahí solo puede
        confundir (y la unidad en uso no se va a ir a ningún lado)."""
        if not self._busy and not self._formatting:
            actuales = {d.mount_point for d in drives.list_removable_drives()}
            if actuales != {d.mount_point for d in self._drives}:
                self._refresh_drives()
        return True

    def _selected_drive(self):
        idx = self._drive_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._drives):
            return None
        return self._drives[idx]

    def _on_drive_changed(self, *_a):
        self._update_drive_subtitle()
        self._sync_result_visibility()
        self._update_sensitivity()

    def _sync_result_visibility(self):
        """El veredicto vale para la unidad que se verificó, no para
        cualquiera: si la que está elegida no es esa, se esconde -y con él
        la opción de formatear.

        Se llama tanto al cambiar de unidad como al repoblar la lista, y
        no solo desde el `notify::selected`, porque hay casos donde el
        índice elegido no cambia pero la unidad SÍ: después de formatear
        (la unidad vuelve montada con otro nombre) o si se desconecta la
        memoria verificada y quedó otra en el mismo lugar de la lista. En
        los dos, el veredicto viejo estaría describiendo a otra memoria."""
        unidad = self._selected_drive()
        if (self._result_mount is not None
                and (unidad is None or unidad.mount_point != self._result_mount)):
            self._hide_result()

    def _update_sensitivity(self):
        libre = not self._busy and not self._formatting
        hay_f3 = self._update_missing_banner()
        self._check_btn.set_sensitive(
            libre and hay_f3 and self._selected_drive() is not None)
        self._refresh_btn.set_sensitive(libre)
        self._drive_row.set_sensitive(libre)
        self._format_btn.set_sensitive(libre)

    # -------------------------------------------------------- Verificación --
    def _on_check_clicked(self, *_a):
        unidad = self._selected_drive()
        if unidad is None:
            return
        dialog = Adw.AlertDialog(
            heading=_("¿Verificar esta memoria?"),
            body=_("Se va a llenar el espacio libre de:\n{drive}\n{detail}\n\ncon "
                   "archivos de prueba y después se van a volver a leer uno "
                   "por uno. Los archivos de prueba se borran al terminar y "
                   "no se toca nada de lo que ya haya adentro.\n\nPuede "
                   "tardar horas según el tamaño y la velocidad de la "
                   "memoria. Se puede cancelar en cualquier momento.")
            .format(drive=unidad.name, detail=self._drive_detail(unidad)),
        )
        dialog.add_response("cancel", _("Cancelar"))
        dialog.add_response("check", _("Verificar"))
        dialog.set_response_appearance("check", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("check")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda _d, r: self._start_check(unidad) if r == "check" else None)
        dialog.present(self.get_root())

    def _start_check(self, unidad):
        """Lanza f3write + f3read en un hilo de fondo.

        Declara ante el `OperationManager` los mismos recursos que una
        transferencia (`drives.resources_for_mount_point`: el punto de
        montaje MÁS el disco físico detrás). Con eso, verificar una memoria
        y formatearla/escribirle desde otra parte de la app se bloquean
        mutuamente: llenar la memoria de archivos de prueba mientras se le
        copia un juego encima sería justo lo que el `OperationManager`
        existe para impedir."""
        try:
            op = self.ops.start(
                OperationKind.CHECKING_MEMORY,
                resources=drives.resources_for_mount_point(unidad.mount_point))
        except OperationBusy as e:
            self._show_toast(
                _("No se puede verificar ahora: {detail}.").format(detail=e.detail))
            return

        self._busy = True
        self._cancel = wit_wrapper.CancellationToken()
        self._last_progress_at = 0.0
        self._hide_result()
        self._cancel_btn.set_visible(True)
        self._cancel_btn.set_sensitive(True)
        self._progress.set_visible(True)
        self._progress.set_fraction(0.0)
        self._progress.set_text(_("Preparando…"))
        self._update_sensitivity()

        cancel = self._cancel
        mount_point = unidad.mount_point

        def on_progress(progress):
            # El hilo lector de f3 llama acá muchas veces por segundo: se
            # deja pasar como mucho una cada `_PROGRESS_MIN_INTERVAL` para
            # no inundar el hilo de GTK de `idle_add`.
            ahora = time.monotonic()
            if ahora - self._last_progress_at < _PROGRESS_MIN_INTERVAL:
                return
            self._last_progress_at = ahora
            GLib.idle_add(self._on_check_progress, progress)

        def worker():
            try:
                result = f3_wrapper.check_memory(
                    mount_point, progress_cb=on_progress, cancel=cancel)
            except f3_wrapper.F3NotFoundError as e:
                result = f3_wrapper.CheckResult(ok=False, error=str(e))
            except Exception as e:  # noqa: BLE001
                if drives.device_is_gone(mount_point=mount_point,
                                         known_dir=mount_point, exc=e):
                    # Sacar la memoria a mitad de la prueba es de lo más
                    # fácil que puede pasar acá: `f3` la está llenando y
                    # tarda horas. No es un fallo de la memoria -no hay
                    # veredicto- y decir "no se pudo completar la
                    # verificación" a secas invita a desconfiar del
                    # pendrive cuando el problema fue el cable.
                    result = f3_wrapper.CheckResult(
                        ok=False, disconnected=True,
                        error=drives.disconnected_message())
                else:
                    result = f3_wrapper.CheckResult(ok=False, error=str(e))
            self.ops.finish(op, self._outcome_for(unidad, result))
            GLib.idle_add(self._on_check_done, unidad, result)

        threading.Thread(target=worker, daemon=True, name="f3-check").start()

    @staticmethod
    def _outcome_for(unidad, result) -> OperationOutcome:
        """Lo que queda en el historial. Una memoria que MIENTE se registra
        como error, no como "completada": la operación terminó, pero el
        resultado es que esa memoria no sirve, y eso es lo que hay que poder
        encontrar en el historial tres semanas después."""
        if result.cancelled:
            return OperationOutcome(oplog.STATUS_CANCELLED, unidad.name)
        if result.disconnected:
            return OperationOutcome(oplog.STATUS_DISCONNECTED, unidad.name,
                                    result.error)
        if result.error:
            return OperationOutcome(oplog.STATUS_ERROR, unidad.name, result.error)
        if result.ok:
            return OperationOutcome(
                oplog.STATUS_OK, unidad.name,
                _("{gb:.1f} GB verificados sin errores").format(gb=result.ok_gb))
        return OperationOutcome(
            oplog.STATUS_ERROR, unidad.name,
            _("La memoria no es confiable: {lost:.1f} GB de {total:.1f} GB no "
              "volvieron bien").format(lost=result.lost_gb,
                                       total=result.announced_gb))

    def _on_check_progress(self, progress) -> bool:
        if not self._busy:
            return False
        self._progress.set_fraction(progress.fraction)
        texto = (_("Escribiendo datos de prueba…")
                 if progress.phase == f3_wrapper.PHASE_WRITE
                 else _("Leyendo y comparando…"))
        partes = [texto, f"{int(progress.fraction * 100)}%"]
        if progress.speed:
            partes.append(progress.speed)
        if progress.eta:
            partes.append(_("faltan {eta}").format(eta=progress.eta))
        self._progress.set_text(" · ".join(partes))
        return False

    def _on_check_done(self, unidad, result) -> bool:
        self._busy = False
        self._cancel = None
        self._cancel_btn.set_visible(False)
        self._progress.set_visible(False)
        self._update_sensitivity()

        if result.cancelled:
            self._show_toast(_("Verificación cancelada."))
            return False

        self._result_mount = unidad.mount_point
        self._show_result(unidad, result)
        return False

    def _on_cancel_clicked(self, *_a):
        # Apagar el botón evita el doble click; el estado final lo pone el
        # propio hilo al notar la cancelación (mismo patrón que la cola de
        # transferencias).
        self._cancel_btn.set_sensitive(False)
        self._progress.set_text(_("Cancelando…"))
        if self._cancel is not None:
            self._cancel.cancel()

    def shutdown(self):
        """Corta la verificación en curso al cerrar la ventana, igual que
        `TransferView.shutdown` y `HomebrewStoreView.shutdown`.

        Acá importa más que en los otros dos: `f3write` se lanza en su
        propia sesión de procesos, así que sin esto sobreviviría al cierre
        de la app y seguiría llenando la memoria del cliente de archivos de
        prueba, sin ninguna ventana que lo muestre ni que permita pararlo.
        `CancellationToken.cancel()` le manda SIGTERM al grupo entero y
        vuelve en el acto, sin trabar el cierre."""
        if self._cancel is not None:
            self._cancel.cancel()

    # ------------------------------------------------------------ Veredicto --
    def _hide_result(self):
        self._result_mount = None
        self._result_group.set_visible(False)
        self._format_group.set_visible(False)

    def _show_result(self, unidad, result):
        """Pinta el veredicto y, SOLO si la memoria pasó limpia, encadena
        la opción de formatear.

        El encadenado es de interfaz, no de operaciones: acá no se lanza
        ningún formateo, se muestra un botón. Y no se muestra ni siquiera
        eso si el disco detrás de la memoria no está en la lista blanca de
        removibles (`drives.candidate_for_mount_point` devuelve None), que
        es la misma lista blanca de Modo Fábrica y no se afloja por este
        camino."""
        self._result_group.set_visible(True)
        self._format_group.set_visible(False)

        if result.disconnected:
            # Título propio y en amarillo: no hay veredicto sobre la
            # memoria, así que "No se pudo completar la verificación" con
            # un ícono de error la deja bajo sospecha sin motivo.
            self._set_result_row("drive-removable-media-symbolic", "warning",
                                 _("Se desconectó la memoria"),
                                 _("{detail} Volvé a conectarla y empezá la "
                                   "verificación de nuevo.").format(
                                       detail=result.error))
            return

        if result.error:
            self._set_result_row("dialog-error-symbolic", "error",
                                 _("No se pudo completar la verificación"),
                                 result.error)
            return

        if not result.ok:
            detalle = _(
                "De los {total:.1f} GB que dice tener, solo volvieron bien "
                "{ok:.1f} GB: se perdieron {lost:.1f} GB (corruptos "
                "{corrupt:.1f} GB, cambiados {changed:.1f} GB, sobrescritos "
                "{over:.1f} GB). No la uses para guardar nada que importe."
            ).format(total=result.announced_gb, ok=result.ok_gb,
                     lost=result.lost_gb,
                     corrupt=result.corrupted_bytes / (1024 ** 3),
                     changed=result.changed_bytes / (1024 ** 3),
                     over=result.overwritten_bytes / (1024 ** 3))
            self._set_result_row("dialog-warning-symbolic", "warning",
                                 _("La memoria NO es confiable"), detalle)
            self._show_toast(_("'{name}' no pasó la verificación.")
                             .format(name=unidad.name))
            return

        velocidades = []
        if result.write_speed:
            velocidades.append(_("escritura {speed}").format(speed=result.write_speed))
        if result.read_speed:
            velocidades.append(_("lectura {speed}").format(speed=result.read_speed))
        detalle = _("Se escribieron y se volvieron a leer {gb:.1f} GB sin un "
                    "solo error.").format(gb=result.ok_gb)
        if velocidades:
            detalle += " " + _("Velocidad: {speeds}.").format(
                speeds=", ".join(velocidades))
        self._set_result_row("emblem-ok-symbolic", "success",
                             _("La memoria es real"), detalle)
        self._show_toast(_("'{name}' pasó la verificación: {gb:.1f} GB reales.")
                         .format(name=unidad.name, gb=result.ok_gb))

        device = drives.candidate_for_mount_point(unidad.mount_point)
        if device is None:
            self._format_btn_row.set_title(_("No se puede formatear desde acá"))
            self._format_btn_row.set_subtitle(
                _("El sistema no marca este disco como removible, así que la "
                  "app no lo formatea: es la misma protección que impide "
                  "formatear un disco interno por error."))
            self._format_btn.set_visible(False)
            self._label_row.set_visible(False)
        else:
            self._format_btn_row.set_title(_("Formatear {drive}")
                                           .format(drive=device.display_name))
            self._format_btn_row.set_subtitle(
                _("Pide confirmación antes de tocar nada."))
            self._format_btn.set_visible(True)
            self._label_row.set_visible(True)
        self._format_group.set_visible(True)

    def _set_result_row(self, icon_name: str, css_class: str, title: str,
                        subtitle: str):
        self._result_icon.set_from_icon_name(icon_name)
        for clase in ("success", "warning", "error"):
            self._result_icon.remove_css_class(clase)
        self._result_icon.add_css_class(css_class)
        self._result_row.set_title(title)
        self._result_row.set_subtitle(subtitle)

    # ------------------------------------------------------------ Formateo --
    def _on_format_clicked(self, *_a):
        if self._result_mount is None:
            return
        # Se vuelve a resolver el dispositivo AHORA, no se guarda el de
        # cuando terminó la verificación: entre una cosa y la otra pudieron
        # pasar horas, y el `size_bytes`/`identity` que quedan acá son los
        # que `drives.verify_still_safe` va a comparar contra el kernel
        # justo antes de formatear.
        device = drives.candidate_for_mount_point(self._result_mount)
        if device is None:
            self._show_toast(
                _("Esa unidad ya no está conectada (o el sistema dejó de "
                  "marcarla como removible)."))
            self._refresh_drives()
            self._hide_result()
            return

        etiqueta = drives.normalize_fat_label(self._label_row.get_text())
        cuerpo = _("Vas a formatear:\n{drive}\n\nSe borra TODO su contenido "
                   "actual, sin posibilidad de recuperarlo. Para confirmar, "
                   "escribí {{word}} abajo.").format(drive=device.display_name)
        if etiqueta:
            cuerpo += "\n\n" + _("Etiqueta: {label}").format(label=etiqueta)
        gtk_helpers.confirm_whole_disk_format(
            self.get_root(),
            heading=_("¿Formatear esta memoria en FAT32?"),
            body=cuerpo,
            confirm_label=_("Formatear"),
            on_confirm=lambda: self._start_format(device, etiqueta),
        )

    def _start_format(self, device, etiqueta: str):
        """Corre `drives.format_fat32` en un hilo de fondo: los mismos
        blindajes que Modo Fábrica, sin la estructura de carpetas de Wii.

        Declara el DISCO ENTERO como recurso (no el punto de montaje, que
        el propio formateo desmonta), igual que Modo Fábrica: es lo que
        hace que choque con una transferencia o una instalación de homebrew
        sobre ese mismo disco."""
        try:
            op = self.ops.start(OperationKind.FORMATTING,
                                resources=[device.path])
        except OperationBusy as e:
            self._show_toast(
                _("No se puede formatear ahora: {detail}.").format(detail=e.detail))
            return

        self._formatting = True
        self._update_sensitivity()
        self._progress.set_visible(True)
        self._progress.set_fraction(0.0)
        self._progress.set_text(_("Formateando…"))
        GLib.timeout_add(150, self._pulse_progress)

        def worker():
            try:
                punto = drives.format_fat32(device, label=etiqueta or None)
            except Exception as e:  # noqa: BLE001
                resultado = OperationOutcome(oplog.STATUS_ERROR,
                                             str(device.path), str(e))
                self.ops.finish(op, resultado)
                GLib.idle_add(self._on_format_done, False, str(e))
            else:
                self.ops.finish(op, OperationOutcome(
                    oplog.STATUS_OK, str(device.path),
                    _("FAT32 en {path}").format(path=punto)))
                GLib.idle_add(self._on_format_done, True, str(punto))

        threading.Thread(target=worker, daemon=True, name="fat32-format").start()

    def _pulse_progress(self) -> bool:
        if not self._formatting:
            return False
        self._progress.pulse()
        return True

    def _on_format_done(self, ok: bool, detail: str) -> bool:
        self._formatting = False
        self._progress.set_visible(False)
        if ok:
            self._show_toast(
                _("Memoria formateada en FAT32 y montada en {path}.")
                .format(path=detail))
            # El veredicto sigue siendo cierto (la memoria es real), pero
            # ya no hay nada que formatear: se esconde la opción para que
            # un segundo click no vuelva a formatear lo recién formateado.
            self._format_group.set_visible(False)
            self._label_row.set_text("")
        else:
            self._show_toast(
                _("No se pudo formatear: {error}").format(error=detail))
        self._refresh_drives()
        self._update_sensitivity()
        return False
