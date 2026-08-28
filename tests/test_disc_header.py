"""Parseo del header de una ISO de Wii y validación del Game ID.

El Game ID importa más de lo que parece: sale del contenido de un archivo
que la app no controla y termina usado como componente de ruta al armar
'wbfs/<ID6>/<ID6>.wbfs'. Por eso se valida, y por eso se prueba.
"""
from __future__ import annotations

import pytest

from wiibackup_manager.disc_header import (
    is_valid_game_id,
    read_ciso_disc_number,
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


# --------------------------------------------------------- GameCube --
def test_lee_id_y_consola_de_una_iso_de_gamecube(tmp_path, iso_bytes):
    iso = tmp_path / "juego.iso"
    iso.write_bytes(iso_bytes(game_id=b"GZ2E01", title=b"TWILIGHT PRINCESS",
                              console="gc"))
    info = read_plain_iso_header(iso)
    assert info is not None
    assert info.game_id == "GZ2E01"
    assert info.console == "gc"
    assert info.disc_number == 0


def test_una_iso_de_wii_sigue_marcada_como_wii(tmp_path, iso_bytes):
    iso = tmp_path / "juego.iso"
    iso.write_bytes(iso_bytes())
    info = read_plain_iso_header(iso)
    assert info.console == "wii"


def test_disco_2_de_gamecube_se_lee_del_offset_0x06(tmp_path, iso_bytes):
    iso = tmp_path / "disco2.iso"
    iso.write_bytes(iso_bytes(game_id=b"GZ2E01", console="gc", disc_number=1))
    info = read_plain_iso_header(iso)
    assert info.disc_number == 1


def test_numero_de_disco_disparatado_se_trata_como_disco_1(tmp_path, iso_bytes):
    """Un header corrupto (o un archivo que no es lo que dice ser) no puede
    terminar armando un nombre de archivo 'discN.ext' sin sentido."""
    iso = tmp_path / "raro.iso"
    iso.write_bytes(iso_bytes(console="gc", disc_number=250))
    info = read_plain_iso_header(iso)
    assert info.disc_number == 0


def test_ninguno_de_los_dos_magic_words_no_es_disco_valido(tmp_path, iso_bytes):
    iso = tmp_path / "otra-cosa.iso"
    iso.write_bytes(iso_bytes(magic=False))
    assert read_plain_iso_header(iso) is None


# ------------------------------------------------------------- CISO --
def _ciso_bytes(game_id=b"GZ2E01", disc_number=0, block_0_present=True,
                magic=b"CISO", block_size=0x8000):
    header = bytearray(0x8000)
    header[0:4] = magic
    header[4:8] = block_size.to_bytes(4, "little")
    header[8] = 1 if block_0_present else 0
    disc = bytearray(block_size)
    disc[0:6] = game_id
    disc[6] = disc_number
    disc[0x1C:0x20] = (0xC2339F3D).to_bytes(4, "big")
    return bytes(header) + bytes(disc)


def test_lee_el_numero_de_disco_de_un_ciso(tmp_path):
    ciso = tmp_path / "juego.ciso"
    ciso.write_bytes(_ciso_bytes(disc_number=1))
    assert read_ciso_disc_number(ciso) == 1


def test_ciso_disco_1_da_cero(tmp_path):
    ciso = tmp_path / "juego.ciso"
    ciso.write_bytes(_ciso_bytes(disc_number=0))
    assert read_ciso_disc_number(ciso) == 0


def test_ciso_sin_magic_da_cero(tmp_path):
    ciso = tmp_path / "no-es-ciso.ciso"
    ciso.write_bytes(_ciso_bytes(magic=b"NOPE", disc_number=1))
    assert read_ciso_disc_number(ciso) == 0


def test_ciso_con_bloque_0_ausente_da_cero(tmp_path):
    """Sin el bloque 0 no hay header de disco que leer con confianza: 0
    (disco 1) es el respaldo seguro, no una adivinanza."""
    ciso = tmp_path / "sin-bloque-0.ciso"
    ciso.write_bytes(_ciso_bytes(disc_number=1, block_0_present=False))
    assert read_ciso_disc_number(ciso) == 0


def test_ciso_truncado_no_revienta(tmp_path):
    ciso = tmp_path / "truncado.ciso"
    ciso.write_bytes(b"CISO" + b"\x00" * 10)
    assert read_ciso_disc_number(ciso) == 0


def test_ciso_inexistente_da_cero(tmp_path):
    assert read_ciso_disc_number(tmp_path / "no-existe.ciso") == 0
