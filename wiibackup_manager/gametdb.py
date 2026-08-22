"""Integración con GameTDB (https://www.gametdb.com).

Dos usos:
1. Descargar la carátula (cover) de un juego a partir de su Game ID de 6
   caracteres, cacheándola en disco para no volver a pedirla.
2. (Opcional) Resolver el título completo de un juego cuando el header del
   disco no trae un nombre útil.

No requiere API key: las carátulas se sirven como imágenes estáticas en
art.gametdb.com y son de uso libre para proyectos como este.
"""
from __future__ import annotations

import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from . import config

COVER_URL_TEMPLATE = "https://art.gametdb.com/wii/cover/{region}/{game_id}.png"
COVER_URL_FALLBACK_REGION = "EN"
REQUEST_TIMEOUT = 8


def cover_cache_path(game_id: str) -> Path:
    return config.COVERS_DIR / f"{game_id}.png"


def get_cover_path(game_id: str, region: str = "EN", force: bool = False) -> Optional[Path]:
    """Devuelve la ruta local de la carátula, descargándola si hace falta.

    Devuelve None si no se pudo obtener de ninguna región.
    """
    cache_path = cover_cache_path(game_id)
    if cache_path.exists() and not force:
        return cache_path

    config.COVERS_DIR.mkdir(parents=True, exist_ok=True)

    regions_to_try = [region]
    if region != COVER_URL_FALLBACK_REGION:
        regions_to_try.append(COVER_URL_FALLBACK_REGION)

    for r in regions_to_try:
        url = COVER_URL_TEMPLATE.format(region=r, game_id=game_id)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wiibackup-manager-linux"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 200:
                    data = resp.read()
                    if data:
                        cache_path.write_bytes(data)
                        return cache_path
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
            continue

    return None
