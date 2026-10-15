"""Renombrar no se ofrece si el único cambio es la normalización Unicode.

`sanitize_filename` arma el nombre estándar en NFKD: "Pokémon" queda como
"e" + tilde combinante. Un archivo que ya se llamaba "Pokémon … .wbfs" con
la "é" compuesta (NFC, lo habitual) se ve idéntico, pero la comparación
por bytes decía que había que renombrarlo: figuraba siempre como
pendiente y la confirmación mostraba "X → X" con los dos nombres iguales.

Ahora "necesita renombrar", el menú de la fila, la confirmación en lote y
`rename_to_standard` ignoran las diferencias que son solo de forma
(NFC/NFD). Una diferencia real -incluido un carácter de compatibilidad
como una letra de ancho completo- sigue contando.
"""
from __future__ import annotations

import types
import unicodedata
from pathlib import Path

import pytest

from wiibackup_manager import game_model, library_ops
from wiibackup_manager.game_model import Game

TITULO = "Pokémon Battle Revolution"


def _juego(tmp_path: Path, nombre: str, titulo: str = TITULO) -> Game:
    p = tmp_path / nombre
    p.write_bytes(b"x")
    return Game(path=p, game_id="RPBE01", title=titulo, fmt="WBFS", size_bytes=1,
                identified_by="wit")


def _nfc(texto):
    return unicodedata.normalize("NFC", texto)


@pytest.mark.parametrize("forma", ["NFC", "NFD"])
def test_solo_la_normalizacion_no_es_un_renombrado(tmp_path, forma):
    nombre = unicodedata.normalize(forma, f"{TITULO} [RPBE01].wbfs")
    juego = _juego(tmp_path, nombre)

    assert not game_model.needs_rename(juego)
    assert library_ops.rename_to_standard(juego) == tmp_path / nombre
    assert [p.name for p in tmp_path.iterdir()] == [nombre]   # mismos bytes


def test_una_diferencia_real_sigue_contando(tmp_path):
    assert game_model.needs_rename(_juego(tmp_path, "pokemon.wbfs"))


def test_un_caracter_de_compatibilidad_es_una_diferencia_real(tmp_path):
    """Ancho completo ("Ｍａｒｉｏ") no es la misma palabra en NFC: NFKD sí la
    convierte, así que ahí renombrar cambia algo de verdad."""
    juego = _juego(tmp_path, "Ｍａｒｉｏ [RPBE01].wbfs", titulo="Ｍａｒｉｏ")
    assert game_model.needs_rename(juego)


def test_el_menu_no_ofrece_renombrar_si_solo_cambia_la_forma(tmp_path):
    from wiibackup_manager.widgets.game_row import GameRow

    def etiquetas(juego):
        menu = GameRow._build_menu(types.SimpleNamespace(game=juego))
        return [menu.get_item_attribute_value(i, "label").get_string()
                for i in range(menu.get_n_items())]

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert "Renombrar a estándar [ID]" not in etiquetas(
        _juego(tmp_path / "a", _nfc(f"{TITULO} [RPBE01].wbfs")))
    assert "Renombrar a estándar [ID]" in etiquetas(_juego(tmp_path / "b", "pokemon.wbfs"))


def test_la_ventana_no_renombra_ni_muestra_x_flecha_x(tmp_path):
    from wiibackup_manager.window import WiiBackupWindow

    nombre = _nfc(f"{TITULO} [RPBE01].wbfs")
    juego = _juego(tmp_path, nombre)
    toasts = []
    ventana = types.SimpleNamespace(_show_toast=toasts.append,
                                    _reject_if_busy=lambda *a, **k: False)

    WiiBackupWindow._on_rename_requested(ventana, types.SimpleNamespace(game=juego))
    WiiBackupWindow._start_batch_rename(ventana, [juego])

    assert [p.name for p in tmp_path.iterdir()] == [nombre]
    assert len(toasts) == 2 and all("nombre estándar" in t for t in toasts), toasts
