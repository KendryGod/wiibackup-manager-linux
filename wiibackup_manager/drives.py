"""Detección de discos/USB/SD removibles montados en el sistema.

En Fedora/GNOME (udisks2 + gvfs) las unidades removibles se auto-montan en
/run/media/$USER/<etiqueta> (algunas otras distros usan /media/$USER/). No
hace falta hablar con udisks2 por D-Bus para esto: alcanza con listar esos
directorios y medir el espacio libre de cada punto de montaje.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MOUNT_ROOTS = ("/run/media", "/media")


@dataclass
class DriveInfo:
    name: str
    mount_point: Path
    free_bytes: int
    total_bytes: int

    @property
    def free_gb(self) -> float:
        return self.free_bytes / (1024 ** 3)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024 ** 3)


def list_removable_drives() -> list[DriveInfo]:
    """Escanea /run/media/$USER y /media/$USER en busca de unidades montadas."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    drives: list[DriveInfo] = []
    seen: set[str] = set()

    for root in MOUNT_ROOTS:
        base = Path(root) / user
        if not base.is_dir():
            continue
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            # os.path.ismount filtra subcarpetas que no son, ellas mismas,
            # un punto de montaje (por ejemplo si alguien creó una carpeta
            # vacía ahí a mano).
            if not entry.is_dir() or not os.path.ismount(entry):
                continue
            key = str(entry.resolve())
            if key in seen:
                continue
            seen.add(key)
            try:
                usage = shutil.disk_usage(entry)
            except OSError:
                continue
            drives.append(DriveInfo(
                name=entry.name,
                mount_point=entry,
                free_bytes=usage.free,
                total_bytes=usage.total,
            ))

    return drives


def is_mount_point(path: Path) -> bool:
    """True si `path` es en sí mismo un punto de montaje (y no, por ejemplo,
    una carpeta cualquiera del disco interno), condición para que tenga
    sentido ofrecer expulsarlo."""
    try:
        return os.path.ismount(path)
    except OSError:
        return False


def _block_device_for(path: Path) -> str | None:
    """Dispositivo de bloque (ej. /dev/sdb1) montado en `path`, vía findmnt."""
    try:
        result = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "--target", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def eject_mount_point(path: Path) -> tuple[bool, str]:
    """Desmonta de forma segura `path` para que la unidad se pueda
    desconectar físicamente. Usa udisksctl (unmount + power-off, lo que
    además apaga eléctricamente la unidad removible cuando el hardware lo
    soporta) y si no está disponible cae a `gio mount -u`."""
    if not is_mount_point(path):
        return False, f"'{path}' no es un punto de montaje: no hace falta expulsarlo."

    device = _block_device_for(path)
    if device:
        try:
            result = subprocess.run(
                ["udisksctl", "unmount", "-b", device],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            # power-off es mejor esfuerzo: no todos los lectores de tarjetas
            # SD lo soportan, pero cuando funciona deja la unidad lista para
            # desconectar sin más riesgo que el de un USB ya desmontado.
            subprocess.run(["udisksctl", "power-off", "-b", device],
                            capture_output=True, text=True, timeout=30)
            return True, "Unidad expulsada de forma segura."
        if result is not None:
            stderr = result.stderr.strip()
            if "not authorized" not in stderr.lower():
                return False, stderr or "No se pudo desmontar la unidad."

    try:
        result = subprocess.run(
            ["gio", "mount", "-u", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"No se pudo expulsar la unidad: {e}"

    if result.returncode == 0:
        return True, "Unidad expulsada de forma segura."
    return False, result.stderr.strip() or "No se pudo desmontar la unidad."
