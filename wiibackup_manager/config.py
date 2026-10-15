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
from datetime import datetime
from pathlib import Path
from typing import ClassVar, Optional

from .packs import clean_packs

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


# La carpeta de la biblioteca del primer arranque: la única que la app crea
# sola (ver `ensure_library_dir`).
DEFAULT_LIBRARY_PATH = str(Path.home() / "WiiGames")

TICKET_THEME_DARK = "dark"
TICKET_THEME_LIGHT = "light"
TICKET_THEMES = (TICKET_THEME_DARK, TICKET_THEME_LIGHT)
# Morado medido sobre el flyer de referencia de la primera marca que usó
# el ticket; cada taller lo cambia por el suyo en Ajustes.
DEFAULT_ACCENT_COLOR = "#AB85F4"


def clean_whatsapp(raw: str) -> str:
    """Solo los dígitos de `raw`. La gente escribe el número como lo ve en
    una tarjeta ("+504 8888-0000") y wa.me no acepta ni el "+" ni los
    separadores."""
    return "".join(ch for ch in raw if ch.isascii() and ch.isdigit())


def parse_hex_color(value: str) -> Optional[tuple]:
    """"#RRGGBB" -> (r, g, b) en 0..1, o None si no tiene esa forma."""
    value = (value or "").strip().lstrip("#")
    if len(value) != 6:
        return None
    try:
        return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))
    except ValueError:
        return None


@dataclass
class Settings:
    library_path: str = DEFAULT_LIBRARY_PATH
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
    # Si se inyecta `--psel=-UPDATE` al convertir con `wit` (descarta la
    # partición de actualización del disco, que USB Loader GX/Nintendont
    # no usan): ahorra espacio en el destino a costa de que ese juego ya
    # no pueda actualizarse desde el propio disco. Activado por defecto
    # porque es lo que casi todo el mundo quiere en un USB Loader.
    scrub_update: bool = True
    # Si después de copiar un juego a la unidad se lo relee con
    # `wit VERIFY` para confirmar que lo que quedó escrito está bien, y no
    # solo que la copia terminó sin dar error. DESACTIVADO por defecto, al
    # revés que `scrub_update`: verificar vuelve a leer TODO lo que se
    # acaba de escribir, así que puede más que duplicar lo que tarda una
    # tanda. Eso tiene que ser una decisión de quien la corre, no una
    # sorpresa -alguien que copia 40 juegos y de golpe tarda el doble sin
    # haberlo pedido va a pensar que la app se colgó.
    verify_after_copy: bool = False
    # "Mi taller": los datos con los que se firma el Ticket de Entrega. Van
    # acá, en la configuración de cada usuario, y no en el código: la app
    # la usan talleres distintos y cada uno imprime su propia marca. Vacíos
    # por defecto -el ticket sale con un encabezado neutro-.
    shop_name: str = ""
    shop_slogan: str = ""
    shop_location: str = ""
    # Número internacional SOLO con dígitos (código de país incluido, sin
    # "+"), que es lo que pide https://wa.me/<número>. Ver `clean_whatsapp`.
    shop_whatsapp: str = ""
    shop_logo_path: str = ""
    shop_accent_color: str = DEFAULT_ACCENT_COLOR
    # "dark" (con la marca, para mandar por WhatsApp) o "light" (fondo
    # blanco, para imprimir sin gastar tinta). Ver `TICKET_THEMES`.
    ticket_theme: str = TICKET_THEME_DARK
    # Packs de taller de la pestaña Transferir: {"Pack Mario": ["RMCE01",
    # ...]}. Un objeto de listas de ID6, como `dest_presets`, para que el
    # config.json siga siendo legible y editable a mano. Una config de
    # 0.2.2 no la trae: queda vacía. Ver packs.py.
    packs: dict = field(default_factory=dict)

    # Lo que `load` tuvo que hacer con un config.json que no se podía usar:
    # si lo encontró dañado, y dónde lo dejó guardado aparte (None si no lo
    # pudo apartar). No son preferencias -`ClassVar` las deja afuera de los
    # campos, y con eso de `asdict` y del archivo-: son para que la ventana
    # avise.
    config_was_damaged: ClassVar[bool] = False
    damaged_copy: ClassVar[Optional[Path]] = None

    @classmethod
    def load(cls) -> "Settings":
        """Lee la configuración del disco, cayendo a los valores por defecto
        de cada campo que falte o venga con el tipo equivocado.

        La validación es por campo y no "todo o nada": si alguien editó el
        JSON a mano y dejó `auto_scan_on_start: "si"` (texto en vez de
        booleano), se pierde solo esa preferencia y no las otras cuatro.
        Sin esto el valor entraba tal cual al dataclass y reventaba recién
        más tarde, en cualquier lugar que lo usara.

        Si el archivo entero no se puede usar -no se puede leer, está
        vacío, no es JSON o no es un objeto- se aparta como
        `config.json.corrupto-<fecha>` ANTES de seguir con los valores por
        defecto (ver `_damaged`). Si no, el primer guardado lo pisaba y con
        él se iban los destinos guardados y los datos de "Mi taller"."""
        defaults = cls()
        if not CONFIG_FILE.exists():
            return defaults

        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError) as e:
            return cls._damaged(f"ilegible ({e})")

        if not isinstance(data, dict):
            return cls._damaged("no contiene un objeto JSON")

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

        if "library_path" in values:
            biblioteca = _valid_library_path(values["library_path"])
            if biblioteca is None:
                print(f"[wiibackup-manager] config.json: 'library_path' no es una "
                      f"ruta absoluta ({values['library_path']!r}); se usa el valor "
                      f"por defecto ({defaults.library_path!r}).", file=sys.stderr)
                del values["library_path"]
            else:
                values["library_path"] = biblioteca
        if "dest_presets" in values:
            values["dest_presets"] = clean_presets(values["dest_presets"])
        if "packs" in values:
            values["packs"] = clean_packs(values["packs"])
        if "shop_whatsapp" in values:
            values["shop_whatsapp"] = clean_whatsapp(values["shop_whatsapp"])
        if values.get("shop_accent_color") is not None \
                and parse_hex_color(values["shop_accent_color"]) is None:
            del values["shop_accent_color"]
        if values.get("ticket_theme") not in (None, *TICKET_THEMES):
            del values["ticket_theme"]

        return cls(**values)

    @classmethod
    def _damaged(cls, motivo: str) -> "Settings":
        """Los valores por defecto, con el config.json que no se pudo usar
        apartado para que el primer guardado no lo pise."""
        settings = cls()
        settings.config_was_damaged = True
        settings.damaged_copy = _set_aside_damaged_config()
        destino = (f"se guardó aparte como {settings.damaged_copy.name}"
                   if settings.damaged_copy is not None
                   else "no se pudo guardar aparte")
        print(f"[wiibackup-manager] config.json {motivo}: {destino}; se usan "
              "los valores por defecto.", file=sys.stderr)
        return settings

    def save(self) -> None:
        """Guarda la configuración de forma atómica (ver
        `write_text_atomic`, compartida con el historial y las listas exportadas)."""
        write_text_atomic(CONFIG_FILE, json.dumps(asdict(self), indent=2))


def _valid_library_path(value: str) -> Optional[str]:
    """`value` como ruta absoluta (con `~` expandido), o None si no lo es.

    Una ruta vacía o relativa no es una biblioteca: se resolvía contra la
    carpeta desde donde se lanzó la app, y el escaneo recorría eso."""
    texto = value.strip()
    if not texto:
        return None
    ruta = Path(texto).expanduser()
    return str(ruta) if ruta.is_absolute() else None


def _set_aside_damaged_config() -> Optional[Path]:
    """Renombra el config.json que no se pudo usar a
    `config.json.corrupto-<fecha>` (con un sufijo si ese nombre ya está) y
    devuelve dónde quedó, o None si no se pudo.

    Un rename y no una copia: alcanza con que el directorio admita
    escritura -lo mismo que necesita después `save`-, aunque el archivo
    mismo no se pueda leer."""
    base = f"{CONFIG_FILE.name}.corrupto-{datetime.now():%Y-%m-%d-%H%M%S}"
    destino = CONFIG_FILE.with_name(base)
    numero = 2
    while destino.exists():
        destino = CONFIG_FILE.with_name(f"{base}-{numero}")
        numero += 1
    try:
        os.rename(CONFIG_FILE, destino)
    except OSError:
        return None
    return destino


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


def ensure_library_dir(settings: Settings) -> bool:
    """Deja lista la carpeta de la biblioteca y dice si se puede usar.

    Solo se crea sola la carpeta POR DEFECTO (`DEFAULT_LIBRARY_PATH`, la
    del primer arranque). Una que eligió el usuario y no está es casi
    siempre una unidad sin montar: un USB (`/run/media/...`, donde además
    no hay permiso para crear nada), o un disco en fstab con su punto de
    montaje en una carpeta propia, como `~/Externo`. Crearla ahí dejaba la
    biblioteca en el disco interno, debajo del punto de montaje: no
    aparecía el aviso de "unidad no disponible", y lo que se agregara
    quedaba escondido cuando se montaba el disco."""
    biblioteca = Path(settings.library_path)
    if biblioteca.is_dir():
        return True
    if biblioteca != Path(DEFAULT_LIBRARY_PATH):
        return False
    try:
        biblioteca.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return True


def ensure_dirs(settings: Settings) -> None:
    ensure_library_dir(settings)
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def library_path_available(settings: Settings) -> bool:
    """True si library_path existe como carpeta accesible ahora mismo."""
    return Path(settings.library_path).is_dir()
