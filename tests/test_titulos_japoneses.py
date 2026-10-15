"""Títulos japoneses y títulos ilegibles en la cabecera de un ISO.

`disc_header.read_plain_iso_header` decodificaba el título siempre en
ASCII con reemplazo: el nombre interno de un disco japonés (Shift-JIS)
salía como "�}���I�J�[�gWii", y "Renombrar a estándar [ID]" lo pasaba al
nombre del archivo. Ahora:

- con un ID de región japonesa (cuarto carácter "J") se decodifica en
  Shift-JIS (y CP932, que lo extiende), y el resultado tiene que ser
  imprimible;
- un título que igual queda ilegible (caracteres de reemplazo o no
  imprimibles) nunca llega a un nombre de archivo: el escaneo usa el
  título de la base de `wit` si lo hay, y si no, no se ofrece renombrar.

Probado solo con cabeceras sintéticas, no con un ISO japonés real.
"""
from __future__ import annotations

import types
from pathlib import Path

import pytest

from wiibackup_manager import game_model, library_ops, scanning, wit_wrapper
from wiibackup_manager.disc_header import DiscInfo, read_plain_iso_header
from wiibackup_manager.game_model import Game


def _iso(path: Path, game_id: bytes, titulo: bytes) -> Path:
    cabecera = bytearray(0x100)
    cabecera[0:6] = game_id
    cabecera[0x18:0x1C] = b"\x5d\x1c\x9e\xa3"
    cabecera[0x20:0x20 + len(titulo)] = titulo
    path.write_bytes(bytes(cabecera))
    return path


def _juego(path: Path, titulo: str, game_id: str = "RMCJ01") -> Game:
    return Game(path=path, game_id=game_id, title=titulo, fmt="ISO",
                size_bytes=path.stat().st_size, identified_by="iso")


def test_un_titulo_japones_se_lee_en_shift_jis(tmp_path):
    iso = _iso(tmp_path / "x.iso", b"RMCJ01", "マリオカートWii".encode("shift_jis"))

    info = read_plain_iso_header(iso)

    assert info.title == "マリオカートWii"
    juego = _juego(iso, info.title)
    assert game_model.title_is_usable(juego.title)
    assert game_model.standard_filename(juego) == "マリオカートWii [RMCJ01].iso"


def test_una_extension_de_cp932_tambien_se_lee(tmp_path):
    iso = _iso(tmp_path / "x.iso", b"RXXJ01", "①ゲーム".encode("cp932"))
    assert read_plain_iso_header(iso).title == "①ゲーム"


def test_un_titulo_ascii_de_otra_region_sigue_igual(tmp_path):
    iso = _iso(tmp_path / "x.iso", b"RMCE01", b"MARIO KART WII")
    assert read_plain_iso_header(iso).title == "MARIO KART WII"


@pytest.mark.parametrize("game_id, titulo", [
    (b"RMCE01", b"\xff\xfe\x80 roto"),        # no es ASCII
    (b"RMCJ01", b"\xff\xfe\xfd"),              # ni Shift-JIS ni CP932 imprimible
])
def test_un_titulo_ilegible_no_llega_al_nombre_de_archivo(tmp_path, game_id, titulo):
    iso = _iso(tmp_path / "Juego.iso", game_id, titulo)
    info = read_plain_iso_header(iso)
    juego = _juego(iso, info.title, info.game_id)

    # Ilegible a la vista ("�"), no caracteres de uso privado invisibles.
    assert "\ufffd" in info.title
    assert not game_model.title_is_usable(juego.title)
    assert not game_model.needs_rename(juego)
    with pytest.raises(library_ops.UnusableTitleError):
        library_ops.rename_to_standard(juego)
    assert [p.name for p in tmp_path.iterdir()] == ["Juego.iso"]


def test_el_escaneo_usa_el_titulo_de_wit_si_la_cabecera_es_ilegible(tmp_path, monkeypatch):
    iso = _iso(tmp_path / "Juego.iso", b"RMCE01", b"\xff\xfe roto")
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b="wit": True)
    monkeypatch.setattr(wit_wrapper, "identify",
                        lambda p, b="wit": DiscInfo("RMCE01", "Mario Kart Wii", "wit"))

    juego = scanning.identify_file(iso)

    assert juego.title == "Mario Kart Wii"
    assert game_model.needs_rename(juego)


def test_un_titulo_legible_de_wii_no_le_pregunta_nada_a_wit(tmp_path, monkeypatch):
    """El camino de siempre para Wii no cambia: un `wit` por ISO solo cuando
    la cabecera no alcanza."""
    iso = _iso(tmp_path / "Juego.iso", b"RMCE01", b"MARIO KART WII")
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b="wit": True)

    def _no(*_a, **_k):
        raise AssertionError("no tenía que llamar a wit")

    monkeypatch.setattr(wit_wrapper, "identify", _no)

    assert scanning.identify_file(iso).title == "MARIO KART WII"


def test_sin_wit_un_titulo_ilegible_no_se_ofrece_renombrar(tmp_path, monkeypatch):
    from wiibackup_manager.widgets.game_row import GameRow

    iso = _iso(tmp_path / "Juego.iso", b"RMCE01", b"\xff\xfe roto")
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b="wit": False)
    juego = scanning.identify_file(iso)

    menu = GameRow._build_menu(types.SimpleNamespace(game=juego))
    etiquetas = [menu.get_item_attribute_value(i, "label").get_string()
                 for i in range(menu.get_n_items())]
    assert "Renombrar a estándar [ID]" not in etiquetas


def test_la_ventana_avisa_en_vez_de_renombrar(tmp_path):
    from wiibackup_manager.window import WiiBackupWindow

    iso = _iso(tmp_path / "Juego.iso", b"RMCE01", b"\xff\xfe roto")
    juego = _juego(iso, read_plain_iso_header(iso).title, "RMCE01")
    toasts = []
    ventana = types.SimpleNamespace(_show_toast=toasts.append,
                                    _reject_if_busy=lambda *a, **k: False)

    WiiBackupWindow._on_rename_requested(ventana, types.SimpleNamespace(game=juego))
    WiiBackupWindow._start_batch_rename(ventana, [juego])

    assert [p.name for p in tmp_path.iterdir()] == ["Juego.iso"]
    assert len(toasts) == 2
    assert all("nombre de archivo" in t for t in toasts), toasts
