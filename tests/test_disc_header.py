"""Parseo del header de una ISO de Wii y validación del Game ID.

El Game ID importa más de lo que parece: sale del contenido de un archivo
que la app no controla y termina usado como componente de ruta al armar
'wbfs/<ID6>/<ID6>.wbfs'. Por eso se valida, y por eso se prueba.
"""
from __future__ import annotations

import pytest

from wiibackup_manager.disc_header import (
    is_valid_game_id,
    read_plain_iso_header,
    validate_game_id,
)


@pytest.mark.parametrize("game_id", ["RMCP01", "SMNE01", "RSBE01", "000000", "ZZZZZZ"])
def test_ids_validos(game_id):
    assert is_valid_game_id(game_id)
    assert validate_game_id(game_id) == game_id


@pytest.mark.parametrize("game_id", [
    "",              # vacío
    "RMCP0",         # cinco caracteres
    "RMCP011",       # siete
    "RMCP-1",        # guion
    "RM CP1",        # espacio
    "RMCP0Ñ",        # no ASCII
    "１２３４５６",   # dígitos Unicode de ancho completo, que isalnum() aceptaría
])
def test_ids_invalidos_se_rechazan(game_id):
    assert not is_valid_game_id(game_id)
    with pytest.raises(ValueError):
        validate_game_id(game_id)


@pytest.mark.parametrize("crudo,esperado", [("rmcp01", "RMCP01"), ("RmCp01", "RMCP01")])
def test_las_minusculas_se_aceptan_y_se_normalizan(crudo, esperado):
    """Un ID en minúsculas es el mismo ID: se acepta y se normaliza a
    mayúsculas, que es la forma con la que después se arma la ruta."""
    assert is_valid_game_id(crudo)
    assert validate_game_id(crudo) == esperado


@pytest.mark.parametrize("game_id", ["../../e", "a/b/c1", "RMC/01"])
def test_un_id_con_separador_de_ruta_no_pasa(game_id):
    """El caso que hace que esto sea una validación de seguridad y no un
    detalle cosmético: si un ID con '/' o '..' llegara a `wbfs_dest_path`,
    la ruta de destino se escaparía de la carpeta de la unidad."""
    assert not is_valid_game_id(game_id)
    with pytest.raises(ValueError):
        validate_game_id(game_id)


def test_lee_id_y_titulo_de_una_iso_plana(tmp_path, iso_bytes):
    iso = tmp_path / "juego.iso"
    iso.write_bytes(iso_bytes())
    info = read_plain_iso_header(iso)
    assert info is not None
    assert info.game_id == "RMCP01"
    assert info.title == "MARIO KART WII"


def test_sin_magic_word_no_es_una_iso_de_wii(tmp_path, iso_bytes):
    """Un archivo con extensión .iso que no es un disco de Wii (una ISO de
    PC, por ejemplo) no se puede identificar por header."""
    iso = tmp_path / "otra-cosa.iso"
    iso.write_bytes(iso_bytes(magic=False))
    assert read_plain_iso_header(iso) is None


def test_archivo_mas_corto_que_el_header_no_revienta(tmp_path):
    corto = tmp_path / "truncado.iso"
    corto.write_bytes(b"RMCP01")
    assert read_plain_iso_header(corto) is None


def test_archivo_inexistente_devuelve_none(tmp_path):
    assert read_plain_iso_header(tmp_path / "no-existe.iso") is None
