"""Configuración persistente de WiiBackup Manager.

Guarda las preferencias del usuario (carpeta de biblioteca, carpeta de
destino WBFS, caché de carátulas) en ~/.config/wiibackup-manager/config.json
siguiendo el estándar XDG Base Directory.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, asdict, field, fields
from pathlib import Path
from typing import Optional

APP_ID = "com.gamefixsps.WiiBackupManager"

XDG_CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
XDG_CACHE_HOME = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))

CONFIG_DIR = XDG_CONFIG_HOME / "wiibackup-manager"
CONFIG_FILE = CONFIG_DIR / "config.json"
# El historial de operaciones va junto a la configuración y NO en la
# caché: la caché es todo contenido que se puede volver a bajar de
# GameTDB (carátulas, wiitdb.xml), así que borrarla tiene que ser
# inofensivo. El historial, en cambio, no se puede reconstruir de ningún
# lado si se pierde.
HISTORY_FILE = CONFIG_DIR / "history.json"
CACHE_DIR = XDG_CACHE_HOME / "wiibackup-manager"
COVERS_DIR = CACHE_DIR / "covers"


def write_text_atomic(path: Path, payload: str, encoding: str = "utf-8") -> None:
    """Escribe `payload` en `path` de forma atómica.

    Se escribe primero a un temporal en la MISMA carpeta (para que el
    rename sea dentro del mismo filesystem, condición para que sea
    atómico), se fuerza el flush a disco y recién ahí se reemplaza el
    archivo real con `os.replace`. Así el archivo nunca queda a medio
    escribir: o está el contenido viejo entero, o el nuevo entero.

    Antes esto era un `write_text()` directo en config.json: si la app o
    el sistema se cortaba en ese instante, el archivo quedaba truncado y
    el próximo arranque volvía a los valores por defecto sin decir nada,
    perdiendo las preferencias del usuario. El historial de operaciones y
    las listas exportadas (CSV/texto) usan la misma escritura por el mismo
    motivo: `encoding` está para eso, porque el CSV se guarda como
    utf-8-sig y no como utf-8 pelado.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Temporal con nombre único: dos guardados simultáneos no se pisan
    # el temporal entre sí (el último `os.replace` gana, entero).
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                     prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@dataclass
class Settings:
    library_path: str = str(Path.home() / "WiiGames")
    wbfs_drive_path: str = ""
    cover_region: str = "EN"
    wit_binary: str = "wit"
    auto_scan_on_start: bool = True
    # Apariencia: "system" (la del escritorio), "light" u "dark". Ver
    # `styles.apply_color_scheme`.
    color_scheme: str = "system"
    # Destinos guardados de la pestaña Transferir: [{"name": ..., "path": ...}].
    # Se guardan como lista de diccionarios y no como dataclass propia para
    # que el config.json siga siendo legible y editable a mano.
    dest_presets: list = field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        """Lee la configuración del disco, cayendo a los valores por defecto
        de cada campo que falte o venga con el tipo equivocado.

        La validación es por campo y no "todo o nada": si alguien editó el
        JSON a mano y dejó `auto_scan_on_start: "si"` (texto en vez de
        booleano), se pierde solo esa preferencia y no las otras cuatro.
        Sin esto el valor entraba tal cual al dataclass y reventaba recién
        más tarde, en cualquier lugar que lo usara."""
        defaults = cls()
        if not CONFIG_FILE.exists():
            return defaults

        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as e:
            print(f"[wiibackup-manager] config.json ilegible ({e}); "
                  "se usan los valores por defecto.", file=sys.stderr)
            return defaults

        if not isinstance(data, dict):
            print("[wiibackup-manager] config.json no contiene un objeto JSON; "
                  "se usan los valores por defecto.", file=sys.stderr)
            return defaults

        values = {}
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            expected = type(getattr(defaults, f.name))
            # `type(...) is` y no isinstance: en Python bool es subclase
            # de int, así que con isinstance un True se colaría en un campo
            # numérico (y al revés, un 1 pasaría por booleano).
            if type(value) is not expected:
                print(f"[wiibackup-manager] config.json: '{f.name}' tiene un valor "
                      f"inválido ({value!r}); se usa el valor por defecto "
                      f"({getattr(defaults, f.name)!r}).", file=sys.stderr)
                continue
            values[f.name] = value

        if "dest_presets" in values:
            values["dest_presets"] = clean_presets(values["dest_presets"])

        return cls(**values)

    def save(self) -> None:
        """Guarda la configuración de forma atómica (ver
        `write_text_atomic`, compartida con el historial y las listas exportadas)."""
        write_text_atomic(CONFIG_FILE, json.dumps(asdict(self), indent=2))


def try_save(settings: Settings) -> Optional[str]:
    """Guarda la configuración y devuelve el mensaje de error si no se
    pudo, o None si salió bien.

    Existe para que ningún callback de GTK llame a `settings.save()` a
    pelo: guardar puede fallar de verdad (disco lleno, permisos, la
    carpeta de configuración en un filesystem que se desconectó) y una
    excepción saliendo de un handler de señal no la agarra nadie."""
    try:
        settings.save()
        return None
    except OSError as e:
        return e.strerror or str(e)


def clean_presets(raw) -> list:
    """Deja solo los destinos guardados que tengan forma de tal.

    La validación por tipo de `Settings.load` alcanza para saber que
    `dest_presets` es una lista, pero no dice nada de lo que hay adentro:
    un config.json editado a mano puede traer strings sueltos o
    diccionarios a medias, y eso reventaría recién al dibujar la pestaña
    Transferir. Se descarta lo que no sirve y se conserva el resto, igual
    que hace `load` campo por campo.

    Los duplicados por ruta se colapsan quedándose con el último: dos
    accesos rápidos al mismo lugar no aportan nada y ensucian la lista."""
    presets = []
    vistos = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        path = item.get("path")
        if not isinstance(name, str) or not isinstance(path, str):
            continue
        name, path = name.strip(), path.strip()
        if not name or not path:
            continue
        if path in vistos:
            presets[vistos[path]] = {"name": name, "path": path}
            continue
        vistos[path] = len(presets)
        presets.append({"name": name, "path": path})
    return presets


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
