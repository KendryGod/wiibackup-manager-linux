"""Un título ilegible nunca forma una ruta en la unidad.

`transfer_plan.gc_dest_path` armaba la carpeta de Nintendont con el título
tal cual (`games/<título> [ID6]/`): un ISO de GameCube con un título
ilegible -con U+FFFD- y sin título de `wit` creaba `games/�� [ID6]/` en la
unidad del cliente. Ahora, si el título no sirve para un nombre de archivo,
la carpeta es `<ID6> [<ID6>]`, que `_gc_game_folder` sigue reconociendo por
el `[ID6]` del final.

Y en el origen: `read_plain_iso_header` ya no devuelve un título con U+FFFD,
cae al ID6 (como con un título vacío) y marca que no se pudo leer, para que
el escaneo siga pidiéndole el título a la base de `wit`. Un título de `wit`
que tampoco sirve no reemplaza al que había.
"""
from __future__ import annotations

from pathlib import Path

from wiibackup_manager import scanning, transfer_plan, wit_wrapper
from wiibackup_manager.disc_header import DiscInfo, read_plain_iso_header
from wiibackup_manager.game_model import Game


def _gc(titulo: str, disco: int = 0, nombre: str = "x.iso") -> Game:
    return Game(path=Path(nombre), game_id="GZ2E01", title=titulo, fmt="ISO",
                size_bytes=1, identified_by="iso", console="gc", disc_number=disco)


def test_un_titulo_ilegible_no_va_a_la_carpeta_de_gamecube(tmp_path):
    dest = transfer_plan.gc_dest_path(_gc("��"), tmp_path)

    assert "�" not in str(dest)
    assert dest == tmp_path / "games" / "GZ2E01 [GZ2E01]" / "game.iso"


def test_esa_carpeta_se_reconoce_por_el_id(tmp_path):
    """El segundo disco, aunque llegue con un título legible, va a la
    carpeta que ya armó el primero."""
    primero = transfer_plan.gc_dest_path(_gc("��"), tmp_path)
    primero.parent.mkdir(parents=True)
    primero.write_bytes(b"disco 1")

    segundo = transfer_plan.gc_dest_path(_gc("Twilight Princess", disco=1), tmp_path)

    assert segundo == primero.parent / "disc2.iso"


def test_un_titulo_legible_sigue_igual(tmp_path):
    assert transfer_plan.gc_dest_path(_gc("Zelda"), tmp_path).parent.name == \
        "Zelda [GZ2E01]"


def _iso(path: Path, game_id: bytes, titulo: bytes, gamecube=False) -> Path:
    cabecera = bytearray(0x100)
    cabecera[0:6] = game_id
    if gamecube:
        cabecera[0x1C:0x20] = b"\xc2\x33\x9f\x3d"
    else:
        cabecera[0x18:0x1C] = b"\x5d\x1c\x9e\xa3"
    cabecera[0x20:0x20 + len(titulo)] = titulo
    path.write_bytes(bytes(cabecera))
    return path


def test_la_cabecera_ilegible_cae_al_id6(tmp_path):
    info = read_plain_iso_header(_iso(tmp_path / "x.iso", b"RMCE01", b"\xff\xfe roto"))

    assert info.title == "RMCE01" and info.title_unreadable


def test_una_cabecera_legible_no_se_marca(tmp_path):
    info = read_plain_iso_header(_iso(tmp_path / "x.iso", b"RMCE01", b"MARIO KART WII"))

    assert info.title == "MARIO KART WII" and not info.title_unreadable


def test_un_titulo_de_wit_ilegible_no_reemplaza_al_de_la_cabecera(tmp_path, monkeypatch):
    """Con G1, `wit` devuelve los bytes que no son UTF-8 como U+FFFD: ese
    título no se usa."""
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b="wit": True)
    monkeypatch.setattr(wit_wrapper, "identify",
                        lambda p, b="wit": DiscInfo("GZ2E01", "Zel�a", "wit", "gc"))
    legible = _iso(tmp_path / "a.iso", b"GZ2E01", b"ZELDA", gamecube=True)
    ilegible = _iso(tmp_path / "b.iso", b"GZ2E01", b"\xff\xfe", gamecube=True)

    assert scanning.identify_file(legible).title == "ZELDA"
    assert scanning.identify_file(ilegible).title == "GZ2E01"


def test_de_punta_a_punta_un_iso_ilegible_sin_wit(tmp_path, monkeypatch):
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b="wit": False)
    juego = scanning.identify_file(
        _iso(tmp_path / "x.iso", b"GZ2E01", b"\xff\xfe", gamecube=True))

    dest = transfer_plan.game_dest_path(juego, tmp_path / "usb")

    assert "�" not in str(dest)
    assert dest.parent.name == "GZ2E01 [GZ2E01]"
