"""El ticket de una unidad que no se puede leer da un error claro.

`ticket_service._entradas` devolvía una lista vacía ante cualquier
`OSError`, y `collect_ticket_data` ponía la capacidad en 0 si no se podía
medir: con la unidad desconectada salía un PDF que decía "0 Wii · 0
GameCube · 0 Homebrew, 0 B, Desconocido". Ahora una carpeta que no existe
sigue contando como vacía (una unidad sin `games/` es normal), pero
cualquier otro error al leer -o que la unidad desaparezca mientras se la
lee- da `DriveUnreadable`, y la pestaña Transferir vuelve a mirar la
unidad antes de generar.
"""
from __future__ import annotations

import errno
import os
import shutil
import types
from pathlib import Path

import pytest

from wiibackup_manager import ticket_service


def _uso(total, libre):
    return lambda _p: types.SimpleNamespace(total=total, free=libre, used=total - libre)


def _unidad(tmp_path) -> Path:
    raiz = tmp_path / "USB"
    (raiz / "wbfs" / "RMCP01").mkdir(parents=True)
    (raiz / "wbfs" / "RMCP01" / "RMCP01.wbfs").write_bytes(b"\0")
    return raiz


def _leer(raiz, **kw):
    kw.setdefault("usage", _uso(100, 50))
    kw.setdefault("filesystem", lambda _p: "vfat")
    kw.setdefault("title_lookup", lambda _gid: None)
    return ticket_service.collect_ticket_data(raiz, **kw)


def test_una_unidad_sin_games_ni_apps_sigue_funcionando(tmp_path):
    datos = _leer(_unidad(tmp_path))
    assert datos.contents == ticket_service.DriveContents(1, 0, 0)


def test_una_carpeta_que_no_se_puede_leer_da_error_en_vez_de_contar_cero(tmp_path,
                                                                         monkeypatch):
    raiz = _unidad(tmp_path)
    (raiz / "games").mkdir()
    real = os.scandir

    def _scandir(ruta):
        if Path(ruta).name == "games":
            raise OSError(errno.EIO, "Input/output error", str(ruta))
        return real(ruta)

    monkeypatch.setattr(ticket_service.os, "scandir", _scandir)

    with pytest.raises(ticket_service.DriveUnreadable, match=str(raiz)):
        _leer(raiz)


def test_si_la_unidad_desaparece_mientras_se_la_lee_da_error(tmp_path):
    """Se desmonta a mitad: las carpetas que faltan después no cuentan
    como "vacías"."""
    raiz = _unidad(tmp_path)

    def _titulo(_gid):
        shutil.rmtree(raiz)          # se desmontó justo ahora
        return None

    with pytest.raises(ticket_service.DriveUnreadable):
        _leer(raiz, title_lookup=_titulo)


# ------------------------------------------------- Pestaña Transferir --
def test_si_la_unidad_ya_no_esta_no_se_genera_el_ticket(tmp_path, monkeypatch):
    from wiibackup_manager.widgets import transfer_view
    from wiibackup_manager.widgets.transfer_view import TransferView

    hilos, toasts, boton = [], [], []
    monkeypatch.setattr(transfer_view.threading, "Thread",
                        lambda *a, **k: hilos.append(k) or types.SimpleNamespace(
                            start=lambda: None))
    vista = types.SimpleNamespace(
        _dest_path=tmp_path / "WII_USB",          # se desconectó
        settings=types.SimpleNamespace(cover_region="EN"),
        ticket_button=types.SimpleNamespace(set_sensitive=boton.append),
        _show_toast=toasts.append,
        _update_ticket_button=lambda: boton.append("actualizado"))
    dialogo = types.SimpleNamespace(
        save_finish=lambda _r: types.SimpleNamespace(
            get_path=lambda: str(tmp_path / "ticket.pdf")))

    TransferView._on_ticket_file_chosen(vista, dialogo, None, "Cliente", "", None)

    assert hilos == []
    assert not (tmp_path / "ticket.pdf").exists()
    [aviso] = toasts
    assert "WII_USB" in aviso
    assert "actualizado" in boton
