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
# GameTDB no siempre sube la carátula bajo la región "EN": muchos títulos
# NTSC-U (p.ej. SMNE01, New Super Mario Bros. Wii) sólo existen bajo "US".
# Probamos la región pedida y después esta lista de respaldo, en orden.
COVER_FALLBACK_REGIONS = ["US", "EN", "DE", "FR", "JA", "KO"]
REQUEST_TIMEOUT = 5
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def cover_cache_path(game_id: str) -> Path:
    return config.COVERS_DIR / f"{game_id}.png"


def _is_valid_cached_cover(path: Path) -> bool:
    """True si el archivo cacheado es un PNG con contenido real.

    Una descarga interrumpida a medias (proceso matado, conexión cortada)
    puede dejar un archivo de 0 bytes o con datos truncados/no-PNG. Sin esta
    validación, get_cover_path lo trataría como "ya existe" para siempre y
    jamás reintentaría la descarga.
    """
    try:
        if path.stat().st_size == 0:
            return False
        with path.open("rb") as f:
            return f.read(8) == PNG_MAGIC
    except OSError:
        return False


def get_cover_path(game_id: str, region: str = "EN", force: bool = False) -> Optional[Path]:
    """Devuelve la ruta local de la carátula, descargándola si hace falta.

    Devuelve None si no se pudo obtener de ninguna región.
    """
    cache_path = cover_cache_path(game_id)
    if cache_path.exists():
        if not force and _is_valid_cached_cover(cache_path):
            return cache_path
        # Cache corrupta (0 bytes / no-PNG) o se pidió forzar: la borramos
        # para que el intento de descarga de abajo no la deje pisada a
        # medias si también falla.
        try:
            cache_path.unlink()
        except OSError:
            pass

    config.COVERS_DIR.mkdir(parents=True, exist_ok=True)

    regions_to_try = [region] + [r for r in COVER_FALLBACK_REGIONS if r != region]

    for r in regions_to_try:
        url = COVER_URL_TEMPLATE.format(region=r, game_id=game_id)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wiibackup-manager-linux"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                if resp.status == 200:
                    data = resp.read()
                    if data.startswith(PNG_MAGIC):
                        cache_path.write_bytes(data)
                        return cache_path
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError):
            continue

    return None
