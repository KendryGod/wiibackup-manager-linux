"""Configuración persistente de WiiBackup Manager.

Guarda las preferencias del usuario (carpeta de biblioteca, carpeta de
destino WBFS, caché de carátulas) en ~/.config/wiibackup-manager/config.json
siguiendo el estándar XDG Base Directory.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path

APP_ID = "com.gamefixsps.WiiBackupManager"

XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
XDG_CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))

CONFIG_DIR = XDG_CONFIG_HOME / "wiibackup-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_DIR = XDG_CACHE_HOME / "wiibackup-manager"
COVERS_DIR = CACHE_DIR / "covers"


@dataclass
class Settings:
    library_path: str = str(Path.home() / "WiiGames")
    wbfs_drive_path: str = ""
    cover_region: str = "EN"
    wit_binary: str = "wit"
    auto_scan_on_start: bool = True

    @classmethod
    def load(cls) -> "Settings":
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
                return cls(**known)
            except (json.JSONDecodeError, OSError):
                pass
        return cls()

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def ensure_dirs(settings: Settings) -> None:
    try:
        Path(settings.library_path).mkdir(parents=True, exist_ok=True)
    except OSError:
        # library_path puede apuntar a un punto de montaje externo que no
        # está conectado en este momento (ej. /run/media/usuario/DISCO/...).
        # En ese caso mkdir(parents=True) fallaría al intentar crear
        # directorios padre donde el usuario no tiene permiso de escritura
        # (/run/media/usuario). No hay nada que crear ahí: la app debe
        # abrir igual con la biblioteca vacía y esperar a que el usuario
        # conecte la unidad.
        pass
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def library_path_available(settings: Settings) -> bool:
    """True si library_path existe como carpeta accesible ahora mismo."""
    return Path(settings.library_path).is_dir()
