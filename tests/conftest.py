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

from wiibackup_manager.library import Game  # noqa: E402


@pytest.fixture
def make_game(tmp_path):
    """Crea un archivo real y devuelve el Game que lo describe.

    Archivo real y no un mock: casi todo lo que se prueba acá (renombrar,
    detectar colisiones, escanear) toca el filesystem de verdad, que es
    justamente donde estaban los bugs de pérdida de datos."""
    def _make(name="Juego.iso", game_id="RMCP01", title="Mario Kart Wii",
              size=1024, fmt=None, contenido=None):
        path = tmp_path / name
        path.write_bytes(contenido if contenido is not None else b"\0" * size)
        return Game(
            path=path,
            game_id=game_id,
            title=title,
            fmt=fmt or path.suffix.lstrip(".").upper(),
            size_bytes=path.stat().st_size,
            identified_by="iso",
        )
    return _make


@pytest.fixture
def iso_bytes():
    """Bytes de una ISO de Wii plana mínima pero válida: el header real
    que parsea `disc_header.read_plain_iso_header`."""
    def _make(game_id=b"RMCP01", title=b"MARIO KART WII", magic=True):
        buf = bytearray(0x100)
        buf[0:6] = game_id
        if magic:
            buf[0x18:0x1C] = (0x5D1C9EA3).to_bytes(4, "big")
        buf[0x20:0x20 + len(title)] = title
        return bytes(buf)
    return _make
