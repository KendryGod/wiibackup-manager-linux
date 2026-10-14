"""El ISO y el CISO de un mismo juego de GameCube muestran el mismo título.

Un `.iso` se identificaba leyendo su cabecera (`read_plain_iso_header`),
que trae el nombre interno del disco ("ZELDA OCARINA MULTI PACK"); un
`.ciso` no se puede leer así y lo identifica `wit`, que usa su base de
títulos ("The Legend of Zelda: Ocarina of Time / Master Quest"). Ahora el
ISO de GameCube toma el título de la misma base, por ID6, y cae al de la
cabecera si `wit` no está o no lo conoce.

Wii no cambia: un ISO de Wii sigue con el título de su cabecera y sin
llamar a `wit` (cambiarlo movería lo que hace "Renombrar a estándar [ID]"
con toda una biblioteca de Wii, y sumaría un `wit` por ISO al escaneo).
"""
from __future__ import annotations

import pytest

from wiibackup_manager import game_model, scanning, wit_wrapper
from wiibackup_manager.disc_header import DiscInfo

TITULO_WIT = "The Legend of Zelda: Ocarina of Time / Master Quest"
CABECERA = "ZELDA OCARINA MULTI PACK"


@pytest.fixture
def iso_gc(tmp_path, iso_bytes):
    p = tmp_path / "Zelda.iso"
    p.write_bytes(iso_bytes(game_id=b"D43E01", title=CABECERA.encode(), console="gc"))
    return p


def _wit(monkeypatch, responde):
    llamadas = []

    def identify(path, binary="wit"):
        llamadas.append(path.name)
        return responde(path)

    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b="wit": True)
    monkeypatch.setattr(wit_wrapper, "identify", identify)
    return llamadas


def test_iso_de_gamecube_con_id_conocido_toma_el_titulo_de_wit(iso_gc, monkeypatch):
    _wit(monkeypatch, lambda p: DiscInfo("D43E01", TITULO_WIT, "wit", "gc"))

    juego = scanning.identify_file(iso_gc)

    assert juego.title == TITULO_WIT
    assert (juego.game_id, juego.console, juego.fmt) == ("D43E01", "gc", "ISO")


def test_iso_y_ciso_del_mismo_juego_muestran_el_mismo_titulo(
        iso_gc, tmp_path, monkeypatch):
    _wit(monkeypatch, lambda p: DiscInfo("D43E01", TITULO_WIT, "wit", "gc"))
    ciso = tmp_path / "Zelda.ciso"
    ciso.write_bytes(b"CISO" + b"\0" * 64)

    assert scanning.identify_file(iso_gc).title == scanning.identify_file(ciso).title


@pytest.mark.parametrize("respuesta", [
    lambda p: DiscInfo("D43E01", CABECERA, "wit", "gc"),   # wit no lo conoce
    lambda p: None,                                         # wit no pudo leerlo
    lambda p: DiscInfo("GZ2E01", "Otro juego", "wit", "gc"),  # otro ID: no se usa
], ids=["id-desconocido", "wit-sin-respuesta", "otro-id"])
def test_si_wit_no_tiene_el_titulo_queda_el_de_la_cabecera(iso_gc, monkeypatch, respuesta):
    _wit(monkeypatch, respuesta)
    assert scanning.identify_file(iso_gc).title == CABECERA


def test_sin_wit_queda_el_de_la_cabecera(iso_gc, monkeypatch):
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b="wit": False)
    assert scanning.identify_file(iso_gc).title == CABECERA


def test_un_iso_de_wii_no_cambia_ni_llama_a_wit(tmp_path, iso_bytes, monkeypatch):
    p = tmp_path / "Brawl.iso"
    p.write_bytes(iso_bytes(game_id=b"RSBE01", title=b"SUPER SMASH BROS.BRAWL"))
    llamadas = _wit(monkeypatch, lambda p: DiscInfo("RSBE01", "Super Smash Bros. Brawl",
                                                    "wit", "wii"))

    juego = scanning.identify_file(p)

    assert juego.title == "SUPER SMASH BROS.BRAWL"
    assert llamadas == []


def test_renombrar_a_estandar_usa_el_mismo_titulo_que_el_ciso(iso_gc, monkeypatch):
    _wit(monkeypatch, lambda p: DiscInfo("D43E01", TITULO_WIT, "wit", "gc"))
    juego = scanning.identify_file(iso_gc)
    assert game_model.standard_filename(juego).startswith("The Legend of Zelda")
    assert game_model.standard_filename(juego).endswith("[D43E01].iso")
