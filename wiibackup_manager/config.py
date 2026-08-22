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
    Path(settings.library_path).mkdir(parents=True, exist_ok=True)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
