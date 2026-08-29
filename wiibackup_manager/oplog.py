"""Historial persistente de operaciones.

Guarda el resultado final de cada operación que el usuario dispara
(convertir, transferir, importar, eliminar, renombrar, verificar) en
~/.config/wiibackup-manager/history.json, para poder revisar después qué
se hizo y qué falló.

Se registra el RESULTADO de cada operación, no sus pasos internos: una
transferencia de 20 juegos es una entrada con su resumen, no 20 entradas
ni una por cada bloque copiado. El escaneo de la biblioteca queda afuera a
propósito (ver `OperationLog.record`): no es una acción que el usuario
pida, corre solo al arrancar y después de cada operación, y llenaría el
historial de ruido.

Como config.py, este módulo no importa nada de GTK: se puede probar sin
levantar una ventana. La interfaz se entera de las entradas nuevas
registrando un listener.
"""
from __future__ import annotations

import json
import sys
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from . import config
from .i18n import _

# Estados posibles de una operación terminada.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_PARTIAL = "partial"
STATUS_CANCELLED = "cancelled"

# La clave (STATUS_OK = "ok", …) es lo que se guarda en history.json y no
# se traduce nunca: cambiar de idioma no puede volver ilegible un historial
# ya escrito. Traducible es solo la etiqueta que se muestra.
STATUS_LABELS = {
    STATUS_OK: _("Completada"),
    STATUS_ERROR: _("Con error"),
    STATUS_PARTIAL: _("Terminada con errores"),
    STATUS_CANCELLED: _("Cancelada"),
}

_VALID_STATUSES = frozenset(STATUS_LABELS)

# Tope de entradas guardadas. El historial es para consultar qué pasó
# hace poco, no un registro de auditoría eterno: sin tope el archivo
# crecería sin límite y habría que leerlo y reescribirlo entero en cada
# operación. Con 500 entradas el archivo queda en el orden de 100 KB.
MAX_ENTRIES = 500


# Nombre (sin traducir, mismo criterio que `OperationKind.value`) de la
# entrada que avisa que un respaldo temporal quedó ocupando espacio.
ORPHANED_BACKUP_OPERATION = "Respaldo temporal no eliminado"


@dataclass(frozen=True)
class LogEntry:
    """Una operación terminada.

    `timestamp` es ISO 8601 con offset horario local: sin el offset, un
    historial guardado en verano y leído en invierno mostraría horas
    corridas, y no habría forma de saber a qué huso corresponde.
    """

    timestamp: str
    # Nombre de la operación SIN traducir, siempre en español ("Convirtiendo",
    # "Eliminando", …): es lo que se guarda en el archivo. La traducción se
    # aplica al mostrarlo, así un historial escrito con el sistema en un
    # idioma se lee entero en el idioma actual, sin entradas mezcladas.
    operation: str
    target: str             # el juego, o un resumen ("12 juegos")
    status: str             # uno de STATUS_*
    detail: str = ""        # motivo del error / resumen, vacío si no hay nada que aclarar

    @property
    def status_label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)

    def when(self) -> Optional[datetime]:
        """El timestamp como datetime CON zona horaria, o None si el
        archivo traía algo que no se puede interpretar.

        La app siempre escribe el timestamp con offset, pero el historial
        se lee de forma tolerante (puede estar editado a mano, o venir de
        una versión anterior), y ahí puede aparecer uno sin zona. Mezclar
        los dos rompe el orden entero: comparar un datetime "naive" contra
        uno "aware" levanta TypeError, y el `sorted` de `entries()` se
        lleva puesta la carga del historial completo. A los que vienen sin
        zona se les asume la local, que es de donde salieron."""
        try:
            moment = datetime.fromisoformat(self.timestamp)
        except (ValueError, TypeError):
            return None
        if moment.tzinfo is None:
            return moment.astimezone()
        return moment

    def when_text(self) -> str:
        """Fecha y hora para mostrar: '2026-08-23 14:35'."""
        moment = self.when()
        return moment.strftime("%Y-%m-%d %H:%M") if moment else self.timestamp


def record_orphaned_backup(op_log: "Optional[OperationLog]", target: str,
                           detail: str) -> None:
    """Anota que una operación terminó bien pero dejó un respaldo temporal
    sin borrar.

    Va como entrada propia y no dentro del detalle de la operación que lo
    dejó: la operación se completó -su entrada dice "Completada" y está
    bien que lo diga- pero quedó espacio ocupado por un archivo oculto que
    el usuario no va a encontrar solo. Con entrada propia el problema es
    visible al mirar el historial en vez de estar escondido al final del
    detalle de otra cosa.

    `STATUS_PARTIAL` ("Terminada con errores") y no `STATUS_ERROR`: nada
    de lo que el usuario pidió falló. Tolera `op_log=None` para que quien
    llama no tenga que preguntar (mismo criterio que `golden_configs._log`)."""
    if op_log is None:
        return
    op_log.record(ORPHANED_BACKUP_OPERATION, target, STATUS_PARTIAL, detail)


def _coerce_entry(raw) -> Optional[LogEntry]:
    """Convierte un elemento del JSON en LogEntry, o None si no sirve.

    Igual que `Settings.load`, la validación es tolerante y por entrada:
    una línea corrupta (historial editado a mano, versión vieja del
    archivo) se descarta sola sin llevarse puesto el resto del historial.
    """
    if not isinstance(raw, dict):
        return None
    values = {}
    for field_name in ("timestamp", "operation", "target", "status", "detail"):
        value = raw.get(field_name, "")
        if not isinstance(value, str):
            return None
        values[field_name] = value
    if not values["timestamp"] or not values["operation"]:
        return None
    if values["status"] not in _VALID_STATUSES:
        return None
    return LogEntry(**values)


class OperationLog:
    """Historial en memoria + archivo. Seguro entre hilos: las operaciones
    terminan en hilos de fondo y la interfaz lee desde el hilo de GTK."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = Path(path) if path is not None else config.HISTORY_FILE
        self._lock = threading.RLock()
        self._entries: list = []
        self._listeners: list = []
        self._load()

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------ Lectura --
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as e:
            print(f"[wiibackup-manager] history.json ilegible ({e}); "
                  "se empieza un historial nuevo.", file=sys.stderr)
            return
        if not isinstance(data, list):
            print("[wiibackup-manager] history.json no contiene una lista; "
                  "se empieza un historial nuevo.", file=sys.stderr)
            return

        entries = []
        for raw in data:
            entry = _coerce_entry(raw)
            if entry is not None:
                entries.append(entry)
        with self._lock:
            self._entries = entries[-MAX_ENTRIES:]

    def entries(self) -> list:
        """Todas las entradas, de la más reciente a la más vieja.

        El orden se calcula al leer y no se confía en el orden del
        archivo: una entrada agregada con el reloj del sistema corrido
        hacia atrás tiene que aparecer donde le corresponde por fecha.
        Las entradas con fecha ilegible van al final en vez de romper el
        orden del resto (por eso el primer elemento de la clave: separa
        "tiene fecha" de "no tiene" antes de comparar fechas, y así nunca
        se compara None contra un datetime).

        El desempate por posición no es un detalle: varias operaciones
        cortas seguidas (renombrar o borrar tres juegos) terminan dentro
        del mismo segundo y comparten timestamp. Sin desempate, el orden
        estable del sort las dejaba justo al revés —la más vieja arriba—
        dentro de ese segundo. Como las entradas se agregan al final de la
        lista, un índice mayor es una entrada más nueva.
        """
        with self._lock:
            entries = list(self._entries)

        def sort_key(pair):
            index, entry = pair
            moment = entry.when()
            return (moment is not None, moment, index)

        ordered = sorted(enumerate(entries), key=sort_key, reverse=True)
        return [entry for _index, entry in ordered]

    def is_empty(self) -> bool:
        with self._lock:
            return not self._entries

    # ------------------------------------------------------------ Escritura --
    def record(self, operation: str, target: str, status: str,
               detail: str = "") -> Optional[LogEntry]:
        """Agrega una entrada y la guarda. Devuelve la entrada, o None si
        no se registró nada.

        Un `status` desconocido se descarta en vez de guardarse: el
        historial se muestra traduciendo el estado a texto y a un ícono, y
        una entrada con un estado inventado no se podría mostrar bien."""
        if status not in _VALID_STATUSES:
            return None

        entry = LogEntry(
            timestamp=datetime.now().astimezone().isoformat(timespec="seconds"),
            operation=operation,
            target=target,
            status=status,
            detail=detail,
        )
        with self._lock:
            self._entries.append(entry)
            # Recortar por el principio: las que se caen son las más viejas.
            if len(self._entries) > MAX_ENTRIES:
                del self._entries[:-MAX_ENTRIES]
            self._save_locked()
        self._notify()
        return entry

    def clear(self) -> None:
        with self._lock:
            self._entries = []
            self._save_locked()
        self._notify()

    def _save_locked(self) -> None:
        """Guarda el historial. Se llama SIEMPRE con el lock tomado, para
        que dos operaciones que terminan a la vez no escriban dos veces
        el archivo con vistas distintas de la lista."""
        payload = json.dumps([asdict(e) for e in self._entries],
                             indent=2, ensure_ascii=False)
        try:
            # Misma escritura atómica que config.json: si la app se cierra
            # a mitad de la escritura, queda el historial viejo entero y no
            # un archivo truncado que después no se pueda leer.
            config.write_text_atomic(self._path, payload)
        except OSError as e:
            # No poder guardar el historial no puede hacer fallar la
            # operación que se acaba de completar: el archivo copiado ya
            # está en el disco, y perder su registro es mucho menos grave
            # que reventar el worker que lo copió.
            print(f"[wiibackup-manager] no se pudo guardar el historial ({e}).",
                  file=sys.stderr)

    # ------------------------------------------------------------ Exportar --
    def export_text(self) -> str:
        """El historial como texto plano, una línea por operación, de la
        más reciente a la más vieja. Texto y no JSON: lo que se exporta es
        para leer o mandar cuando algo falló, no para volver a importar."""
        lines = [_("Historial de operaciones — WiiBackup Manager"), ""]
        for entry in self.entries():
            line = (f"{entry.when_text()}  [{entry.status_label}]  "
                    f"{_(entry.operation)}: {entry.target}")
            if entry.detail:
                line += f"  — {entry.detail}"
            lines.append(line)
        if len(lines) == 2:
            lines.append(_("(sin operaciones registradas)"))
        return "\n".join(lines) + "\n"

    # ----------------------------------------------------------- Listeners --
    def add_listener(self, callback: Callable[[], None]) -> None:
        """Callback que se llama cada vez que el historial cambia. Ojo: se
        llama desde el hilo que registró la entrada, que puede no ser el de
        GTK; quien actualice widgets tiene que reenviarlo con
        `GLib.idle_add`."""
        with self._lock:
            self._listeners.append(callback)

    def _notify(self) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb()
            except Exception:
                # Un listener que falla (una vista que ya se destruyó) no
                # puede romper a los demás ni al worker que registró la
                # operación.
                pass
