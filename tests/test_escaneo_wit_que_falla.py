"""Un `wit` que falla con UN archivo no tumba el escaneo de la biblioteca.

`identify_file` y `_titulo_de_wit` solo atrapaban `WitNotFoundError`:
cualquier otra excepción de `wit_wrapper.identify` -un `UnicodeDecodeError`
como el de un título Shift-JIS, un `OSError`, un corte por tiempo, lo que
sea- salía del escaneo y la ventana mostraba "No se pudo escanear la
biblioteca" sin listar ningún juego. Ahora ese archivo cae a lo que se sabe
sin `wit` (el título de la cabecera, o sin identificar) y el resto se lista
igual. Una cancelación sí se sigue propagando.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wiibackup_manager import scanning, wit_wrapper
from wiibackup_manager.disc_header import DiscInfo

FALLAS = [
    UnicodeDecodeError("utf-8", b"\x83", 0, 1, "invalid start byte"),
    OSError(5, "Input/output error"),
    subprocess.TimeoutExpired(["wit", "LIST"], 90),
    RuntimeError("wit se cayó"),
]


def _iso(path: Path, game_id: bytes, nombre: bytes, gamecube=False) -> None:
    cabecera = bytearray(0x100)
    cabecera[0:6] = game_id
    if gamecube:
        cabecera[0x1C:0x20] = b"\xc2\x33\x9f\x3d"
    else:
        cabecera[0x18:0x1C] = b"\x5d\x1c\x9e\xa3"
    cabecera[0x20:0x20 + len(nombre)] = nombre
    path.write_bytes(bytes(cabecera))


@pytest.fixture
def biblioteca(tmp_path):
    raiz = tmp_path / "biblioteca"
    raiz.mkdir()
    (raiz / "Bueno.wbfs").write_bytes(b"wbfs")
    (raiz / "Roto.wbfs").write_bytes(b"wbfs")
    _iso(raiz / "Zelda.iso", b"GZ2E01", b"ZELDA CABECERA", gamecube=True)
    _iso(raiz / "Mario.iso", b"RMCE01", b"MARIO KART WII")
    return raiz


def _wit(monkeypatch, falla):
    def identify(path, binary="wit"):
        nombre = Path(path).name
        if nombre in ("Roto.wbfs", "Zelda.iso"):
            raise falla
        return DiscInfo("RSBE01", "Super Smash Bros. Brawl", "wit", "wii")

    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b="wit": True)
    monkeypatch.setattr(wit_wrapper, "identify", identify)


@pytest.mark.parametrize("falla", FALLAS, ids=lambda e: type(e).__name__)
def test_el_resto_de_la_biblioteca_se_lista_igual(biblioteca, monkeypatch, falla):
    _wit(monkeypatch, falla)

    juegos = {g.path.name: g for g in scanning.scan_library(biblioteca)}

    assert sorted(juegos) == ["Bueno.wbfs", "Mario.iso", "Roto.wbfs", "Zelda.iso"]
    assert juegos["Bueno.wbfs"].game_id == "RSBE01"
    assert juegos["Roto.wbfs"].identified_by == "unknown"          # sin wit no se sabe
    assert juegos["Zelda.iso"].title == "ZELDA CABECERA"           # el de la cabecera
    assert juegos["Zelda.iso"].game_id == "GZ2E01"
    assert juegos["Mario.iso"].title == "MARIO KART WII"


@pytest.mark.parametrize("archivo", ["Roto.wbfs", "Zelda.iso"])
def test_una_cancelacion_si_se_propaga(biblioteca, monkeypatch, archivo):
    def identify(path, binary="wit"):
        if Path(path).name == archivo:
            raise wit_wrapper.OperationCancelled("cancelado")
        return None

    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b="wit": True)
    monkeypatch.setattr(wit_wrapper, "identify", identify)

    with pytest.raises(wit_wrapper.OperationCancelled):
        scanning.scan_library(biblioteca)
