"""Datos convertidos en texto para una persona.

Tamaños, tiempos y la lista de juegos exportada. Vive aparte de la
biblioteca porque no depende de ella: `pdf_export`, el Ticket de Entrega y
el Recovery Manager formatean tamaños sin tener nada que ver con el
escaneo de juegos, y antes tenían que importar `library` entera para eso.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

from . import fsutil
from .i18n import _, ngettext


def format_size(n: int) -> str:
    """GB con un decimal, o MB si es menos de 1 GB (evita mostrar '0.0 GB'
    para tamaños chicos)."""
    gb = n / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{n / (1024 ** 2):.1f} MB"


def format_orphaned_backups(rutas) -> str:
    """Mensaje para el usuario sobre respaldos temporales que no se
    pudieron borrar, con cuánto espacio ocupan y dónde están.

    El "dónde" es lo que hace útil el aviso: son archivos ocultos (el
    nombre empieza con punto) de varios GB, así que sin la ruta el usuario
    ve la unidad más llena de lo que debería y no tiene forma de encontrar
    lo que la está ocupando."""
    rutas = [Path(r) for r in rutas]
    if not rutas:
        return ""
    total = sum(fsutil.path_size(r) for r in rutas)
    if len(rutas) == 1:
        return _("No se pudo eliminar un respaldo temporal de {size} en "
                 "{path}.").format(size=format_size(total), path=rutas[0])
    return _("No se pudieron eliminar {n} respaldos temporales ({size} en "
             "total): {paths}.").format(
                 n=len(rutas), size=format_size(total),
                 paths=", ".join(str(r) for r in rutas))


def format_eta(seconds: float) -> str:
    """Tiempo estimado restante en formato corto ('45s', '2m 15s', '1h 5m')."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s" if secs else f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m" if mins else f"{hours}h"


EXPORT_CSV = "csv"


EXPORT_TEXT = "text"


# Caracteres con los que Excel y LibreOffice arrancan a interpretar una
# celda como fórmula. El título de un juego sale del header de un archivo
# que la app no controla, así que uno llamado "=1+1" o
# "@SUM(1+1)*cmd|'/c calc'!A0" se ejecutaría al abrir la lista exportada
# en la computadora de un cliente. Tab y retorno de carro entran en la
# lista porque algunas versiones los tratan como separadores y corren la
# interpretación a la celda siguiente.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: str) -> str:
    """Neutraliza una celda que podría interpretarse como fórmula.

    Se le antepone un apóstrofe, que es la marca de "esto es texto" que
    entienden las hojas de cálculo: no se ve al abrir el archivo y la
    celda queda con el valor literal."""
    text = str(value)
    if text.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + text
    return text


def export_games(games, fmt: str = EXPORT_CSV) -> str:
    """Devuelve el contenido del archivo a exportar para `games`.

    `EXPORT_CSV` arma una planilla con Título, ID, Formato y Tamaño. El
    tamaño va dos veces, legible y en bytes: "4.3 GB" se lee de una pero
    se ordena mal en una planilla, y el número crudo ordena bien pero no
    se lee. Poner las dos columnas sale gratis y evita tener que elegir.

    `EXPORT_TEXT` arma una lista suelta ("Título — 4.3 GB", una por línea)
    para pegar en un chat, con el total al final."""
    if fmt == EXPORT_TEXT:
        lineas = [f"{game.title} — {format_size(game.size_bytes)}" for game in games]
        total = sum(game.size_bytes for game in games)
        noun = ngettext("juego", "juegos", len(games))
        lineas.append("")
        lineas.append(f"{len(games)} {noun} · {format_size(total)}")
        return "\n".join(lineas) + "\n"

    buffer = io.StringIO()
    # QUOTE_MINIMAL con la coma como separador: los títulos de Wii traen
    # comas y dos puntos ("Zelda: Skyward Sword"), y el módulo csv ya los
    # entrecomilla solo cuando hace falta.
    writer = csv.writer(buffer)
    writer.writerow([_("Título"), _("ID"), _("Formato"), _("Tamaño"),
                     _("Tamaño (bytes)")])
    for game in games:
        # Los tres campos de texto pasan por `_csv_safe`; los tamaños son
        # números que arma la app, no hace falta.
        writer.writerow([_csv_safe(game.title), _csv_safe(game.game_id),
                         _csv_safe(game.fmt),
                         format_size(game.size_bytes), game.size_bytes])
    return buffer.getvalue()
