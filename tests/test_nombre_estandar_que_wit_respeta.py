""""Renombrar a estándar" no arma nombres que `wit` reescribe.

`sanitize_filename` saca los caracteres que no van en un nombre de
archivo, pero deja los espacios que los rodeaban: "Ocarina of Time /
Master Quest" quedaba "Ocarina of Time  Master Quest", con dos. Con la
lista de títulos de GameTDB en inglés, 198 de 6720 nombres estándar
quedaban así: 197 con dos espacios (títulos con " : " o " / ", casi todos
los "Guitar Hero III Custom : ...") y uno con "@" ("Let's Sing @ Radio
Italia"). Convertir uno de esos juegos en la Biblioteca termina en el
aviso de "renombralo" (ver `test_wit_espacios_dobles.py`), así que el
nombre que propone la propia app no puede ser uno de esos.

Solo cambia el nombre en la Biblioteca. La carpeta de un juego de
GameCube en la unidad (`transfer_plan.gc_dest_path`) queda como estaba:
las unidades ya preparadas la tienen con dos espacios, y renombrarla haría
que la app no reconozca el juego que ya está copiado
(`test_wit_espacios_dobles.test_la_carpeta_de_d43e01_tiene_dos_espacios`).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from wiibackup_manager import game_model, wit_wrapper
from wiibackup_manager.game_model import Game


def _juego(titulo, game_id="D43E01", nombre="juego.ciso"):
    return Game(path=Path("/biblioteca") / nombre, game_id=game_id, title=titulo,
                fmt="CISO", size_bytes=1, identified_by="wit", console="gc")


def test_el_nombre_estandar_de_d43e01_tiene_un_solo_espacio():
    nombre = game_model.standard_filename(
        _juego("The Legend of Zelda: Ocarina of Time / Master Quest"))
    assert nombre == "The Legend of Zelda Ocarina of Time Master Quest [D43E01].ciso"


@pytest.mark.parametrize("titulo, game_id, esperado", [
    ("Guitar Hero III Custom : Modern Rock", "C3ME52",
     "Guitar Hero III Custom Modern Rock [C3ME52].wbfs"),
    ("Let's Sing @ Radio Italia", "S7LPKM", "Let's Sing Radio Italia [S7LPKM].wbfs"),
    ("Die drei ???: Das verfluchte Schloss", "R4DDUS",
     "Die drei Das verfluchte Schloss [R4DDUS].wbfs"),
])
def test_titulos_reales_que_armaban_nombres_que_wit_cambia(titulo, game_id, esperado):
    assert game_model.standard_filename(_juego(titulo, game_id, "x.wbfs")) == esperado


@pytest.mark.parametrize("titulo", [
    "A / B", "A : B", "A  B", "A  B", "Juego ~ Edición", "Juego; segunda parte",
    "100% Juego", "A ^ B", "A ` B", "A * B ? C", "A | B", "  espacios  en  los  bordes  ",
    # DEL: `sanitize_filename` saca los de control hasta 0x1f, no este.
    "A\x7fB",
])
def test_ningun_nombre_estandar_es_de_los_que_wit_reescribe(titulo):
    nombre = game_model.standard_filename(_juego(titulo))
    assert wit_wrapper.wit_respeta_el_nombre(nombre), repr(nombre)


def test_un_nombre_con_dos_espacios_se_ofrece_renombrar():
    """El que ya está en la Biblioteca con dos espacios: "Renombrar a
    estándar" lo ofrece, que es lo que destraba la conversión."""
    juego = _juego("The Legend of Zelda: Ocarina of Time / Master Quest",
                   nombre="The Legend of Zelda Ocarina of Time  Master Quest [D43E01].ciso")
    assert game_model.needs_rename(juego)


def test_los_nombres_que_ya_estaban_bien_no_cambian():
    for titulo, game_id, esperado in [
            ("Super Smash Bros. Brawl", "RSBE01", "Super Smash Bros. Brawl [RSBE01].ciso"),
            ("Mario & Sonic at the Olympic Games", "RWQE01",
             "Mario & Sonic at the Olympic Games [RWQE01].ciso"),
            ("Pokémon Battle Revolution", "RPBE01", "Pokémon Battle Revolution [RPBE01].ciso")]:
        assert game_model.same_filename(
            game_model.standard_filename(_juego(titulo, game_id)), esperado)
