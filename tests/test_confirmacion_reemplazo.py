"""La confirmación de reemplazo dice qué archivo reemplaza a cuál.

Con GameCube el juego puede estar en la otra extensión (`game.ciso`) y
llegar como `game.iso`: la confirmación nombraba solo el que ya estaba, y
quien la leía no sabía que iba a quedar un `.iso`. Cuando cambia la
extensión dice "va a reemplazar game.ciso por game.iso"; si no cambia,
el mensaje de siempre.
"""
from __future__ import annotations

import gettext
import types
from pathlib import Path

import pytest

from wiibackup_manager import transfer_plan
from wiibackup_manager.game_model import Game

CARPETA = "Zelda OoT MQ [D43E01]"
LOCALE = Path(__file__).resolve().parent.parent / "data" / "locale"
MENSAJE_DE_SIEMPRE = ("Ya existe un archivo en:\n{dest}\n\nEnviar '{title}' lo va a "
                      "reemplazar. Esta acción no se puede deshacer.")


def _juego(tmp_path, ext):
    src = tmp_path / "origen" / f"zelda{ext}"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"x")
    return Game(path=src, game_id="D43E01", title="Zelda OoT MQ",
                fmt=ext.lstrip(".").upper(), size_bytes=1, identified_by="wit",
                console="gc")


@pytest.fixture
def usb(tmp_path):
    raiz = tmp_path / "USB"
    carpeta = raiz / "games" / CARPETA
    carpeta.mkdir(parents=True)
    return raiz, carpeta


def _pregunta_transferir(monkeypatch, raiz, juego):
    from wiibackup_manager.widgets import transfer_view
    preguntas = []
    monkeypatch.setattr(transfer_view.gtk_helpers, "confirm_overwrite",
                        lambda parent, body, accion: preguntas.append(body))
    vista = types.SimpleNamespace(
        _dest_path=raiz, _selected_games=lambda: [juego],
        _show_toast=lambda _m: None, get_root=lambda: None,
        _enqueue=lambda *a, **k: pytest.fail("encoló sin preguntar"))
    transfer_view.TransferView._on_transfer_clicked(vista)
    [cuerpo] = preguntas
    return cuerpo


def _pregunta_biblioteca(monkeypatch, raiz, juego):
    from wiibackup_manager import window
    preguntas = []
    monkeypatch.setattr(window.gtk_helpers, "confirm_overwrite",
                        lambda parent, body, accion: preguntas.append(body))
    ventana = types.SimpleNamespace(
        settings=types.SimpleNamespace(wit_binary="wit"),
        _show_toast=lambda _m: None,
        _start_send=lambda *a, **k: pytest.fail("envió sin preguntar"))
    dialogo = types.SimpleNamespace(
        select_folder_finish=lambda _r: types.SimpleNamespace(get_path=lambda: str(raiz)))
    window.WiiBackupWindow._on_batch_send_folder_chosen(ventana, dialogo, None, [juego])
    [cuerpo] = preguntas
    return cuerpo


@pytest.mark.parametrize("preguntar", [_pregunta_transferir, _pregunta_biblioteca],
                         ids=["transferir", "biblioteca"])
def test_con_otra_extension_dice_que_archivo_reemplaza_a_cual(
        usb, tmp_path, monkeypatch, preguntar):
    raiz, carpeta = usb
    (carpeta / "game.ciso").write_bytes(b"viejo")

    cuerpo = preguntar(monkeypatch, raiz, _juego(tmp_path, ".iso"))

    assert "va a reemplazar game.ciso por game.iso" in cuerpo
    assert str(carpeta / "game.ciso") in cuerpo


@pytest.mark.parametrize("preguntar", [_pregunta_transferir, _pregunta_biblioteca],
                         ids=["transferir", "biblioteca"])
def test_con_la_misma_extension_el_mensaje_de_siempre(
        usb, tmp_path, monkeypatch, preguntar):
    raiz, carpeta = usb
    (carpeta / "game.iso").write_bytes(b"viejo")

    cuerpo = preguntar(monkeypatch, raiz, _juego(tmp_path, ".iso"))

    assert cuerpo == MENSAJE_DE_SIEMPRE.format(dest=carpeta / "game.iso",
                                               title="Zelda OoT MQ")


def test_en_ingles(usb, tmp_path, monkeypatch):
    from wiibackup_manager.widgets import gtk_helpers
    catalogo = gettext.translation("wiibackup-manager", localedir=str(LOCALE),
                                   languages=["en"])
    monkeypatch.setattr(gtk_helpers, "_", catalogo.gettext)
    raiz, carpeta = usb
    (carpeta / "game.ciso").write_bytes(b"viejo")
    juego = _juego(tmp_path, ".iso")

    cuerpo = gtk_helpers.send_replace_body(
        transfer_plan.existing_destination(juego, raiz),
        transfer_plan.game_dest_path(juego, raiz), juego.title)

    assert "will replace game.ciso with game.iso" in cuerpo
