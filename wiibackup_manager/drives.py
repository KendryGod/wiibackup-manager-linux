"""Detección de discos/USB/SD removibles montados en el sistema.

En Fedora/GNOME (udisks2 + gvfs) las unidades removibles se auto-montan en
/run/media/$USER/<etiqueta> (algunas otras distros usan /media/$USER/). No
hace falta hablar con udisks2 por D-Bus para esto: alcanza con listar esos
directorios y medir el espacio libre de cada punto de montaje.
"""
from __future__ import annotations

import os
import shutil
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
