"""Configuración común de las pruebas.

Lo importante que pasa acá: aislar el HOME. `config.py` calcula las rutas
de config.json, history.json y la caché a partir de XDG_CONFIG_HOME /
XDG_CACHE_HOME **en el momento del import**, así que hay que fijarlas
antes de que cualquier módulo de la app se importe. Sin esto, correr la
suite pisaría la configuración y el historial reales del que la corre.
"""
from __future__ import annotations

import os
import tempfile

# Antes de cualquier `from wiibackup_manager import ...` (ver arriba).
_SANDBOX = tempfile.mkdtemp(prefix="wbm-tests-")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_SANDBOX, "config")
os.environ["XDG_CACHE_HOME"] = os.path.join(_SANDBOX, "cache")
# Idioma fijo: las pruebas comparan textos en español (el idioma del
# código fuente), y si la máquina que corre la suite está en inglés el
# catálogo los traduciría y las comparaciones fallarían.
os.environ["LANGUAGE"] = "es"

import pytest  # noqa: E402

from wiibackup_manager.game_model import Game  # noqa: E402


@pytest.fixture
def make_game(tmp_path):
    """Crea un archivo real y devuelve el Game que lo describe.

    Archivo real y no un mock: casi todo lo que se prueba acá (renombrar,
    detectar colisiones, escanear) toca el filesystem de verdad, que es
    justamente donde estaban los bugs de pérdida de datos."""
    def _make(name="Juego.iso", game_id="RMCP01", title="Mario Kart Wii",
              size=1024, fmt=None, contenido=None, console="wii", disc_number=0):
        path = tmp_path / name
        path.write_bytes(contenido if contenido is not None else b"\0" * size)
        return Game(
            path=path,
            game_id=game_id,
            title=title,
            fmt=fmt or path.suffix.lstrip(".").upper(),
            size_bytes=path.stat().st_size,
            identified_by="iso",
            console=console,
            disc_number=disc_number,
        )
    return _make


@pytest.fixture
def iso_bytes():
    """Bytes de una ISO de Wii (o GameCube, con `console="gc"`) plana
    mínima pero válida: el header real que parsea
    `disc_header.read_plain_iso_header`. `disc_number` es el byte crudo
    del offset 0x06 (0 = disco 1, 1 = disco 2, ...)."""
    def _make(game_id=b"RMCP01", title=b"MARIO KART WII", magic=True,
              console="wii", disc_number=0):
        buf = bytearray(0x100)
        buf[0:6] = game_id
        buf[6] = disc_number
        if magic:
            if console == "gc":
                buf[0x1C:0x20] = (0xC2339F3D).to_bytes(4, "big")
            else:
                buf[0x18:0x1C] = (0x5D1C9EA3).to_bytes(4, "big")
        buf[0x20:0x20 + len(title)] = title
        return bytes(buf)
    return _make


@pytest.fixture(autouse=True)
def sin_traducciones_pegadas():
    """Que ningún test deje un módulo hablando en otro idioma.

    Casi todos los módulos hacen `from .i18n import _`, o sea que se
    quedan con una REFERENCIA a la función de traducción. Un test que
    parchea el catálogo (hoy solo `test_smoke_gui`, al arrancar la app en
    inglés) tiene que devolver esa referencia a su lugar en CADA módulo
    que se la llevó, y es fácil que se le escape uno: la lista se queda
    vieja, o un módulo se importa por primera vez con el parche ya puesto
    y `monkeypatch` termina "restaurándolo" al inglés.

    El síntoma es horrible de diagnosticar: otro test, en otro archivo,
    falla comparando un texto en español, y solo cuando la suite sale en
    cierto orden. Este chequeo lo convierte en un fallo inmediato que
    además dice qué módulo quedó pegado y quién lo dejó así.

    Corre después de CADA test porque el culpable es el test anterior, no
    el que falla. Cuesta recorrer unos 40 módulos: nada, comparado con la
    tarde que se pierde persiguiendo el fallo intermitente."""
    yield
    import sys

    from wiibackup_manager import i18n

    pegados = sorted(
        nombre for nombre, modulo in sys.modules.items()
        if nombre.startswith("wiibackup_manager")
        and getattr(modulo, "_", None) is not None
        and getattr(modulo, "_") is not i18n._
    )
    assert not pegados, (
        "estos módulos quedaron con una traducción pegada después del test: "
        + ", ".join(pegados)
        + " -- ver `_poner_el_catalogo_en_ingles` en tests/test_smoke_gui.py")
