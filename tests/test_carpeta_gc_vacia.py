"""Que no quede la carpeta de un juego vacía en la unidad del cliente:
`games/<Título [ID6]>/` (GameCube) o `wbfs/<ID6>/` (Wii).

Una copia crea la carpeta del juego y escribe adentro un temporal
(`.game.iso.parcial-*`, o el `.RSBE01.wbfs.XXXX.tmp` de `wit`). Si la copia
se cancela o falla, el temporal se borra, pero la carpeta quedaba vacía;
lo mismo al eliminar desde el Recovery Manager el temporal de una copia
que se cortó.

La carpeta se borra solo si quedó vacía POR ESTO: nunca una que ya
estaba (vacía o no, como la del juego que se está reemplazando), ni una
con cualquier otro archivo adentro -oculto incluido-, ni las raíces
`games/` y `wbfs/`, ni nada fuera de esas estructuras.
"""
from __future__ import annotations

import errno
import os
import subprocess
import time
from pathlib import Path

import pytest

from test_cancelar_cola import (TAMANO, UnidadLenta, VELOCIDAD,  # noqa: F401
                                _esperar, sync_dispatch)
from wiibackup_manager import (fsutil, library_ops, recovery_service,
                               transfer_plan)
from wiibackup_manager.operations import OperationManager
from wiibackup_manager.queue_manager import JobStatus, TransferQueue


@pytest.fixture
def unidad_lenta(monkeypatch):
    unidad = UnidadLenta(VELOCIDAD)
    monkeypatch.setattr(os, "fsync", unidad.fsync)
    monkeypatch.setattr(os, "fdatasync", unidad.fsync)
    monkeypatch.setattr(fsutil, "sync_range", unidad.sync_range)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)
    return unidad


def _tp(make_game, contenido=None):
    return make_game(name="tp.iso", game_id="GZ2E01",
                     title="Twilight Princess", console="gc",
                     contenido=contenido if contenido is not None
                     else os.urandom(TAMANO))


def _carpeta(juego, raiz):
    return transfer_plan.gc_dest_path(juego, raiz).parent


def _cancelar_a_mitad(juego, raiz):
    cola = TransferQueue(OperationManager(), dispatch=sync_dispatch)
    job = cola.add_jobs([juego], raiz)[0]
    assert _esperar(lambda: job.status is JobStatus.RUNNING, 5)
    time.sleep(0.5)
    cola.cancel_all()
    assert _esperar(lambda: job.is_final, 10)
    cola.shutdown(wait=5)
    return job


# ============================================================ Cancelar --
def test_cancelar_una_copia_gc_no_deja_la_carpeta_vacia(
        make_game, tmp_path, unidad_lenta):
    raiz = tmp_path / "usb"
    raiz.mkdir()
    juego = _tp(make_game)
    job = _cancelar_a_mitad(juego, raiz)

    assert job.status is JobStatus.CANCELLED
    assert not _carpeta(juego, raiz).exists()
    # `games/` en sí no se toca, aunque haya quedado vacía.
    assert (raiz / "games").is_dir()


def test_una_carpeta_que_ya_existia_vacia_no_se_borra(
        make_game, tmp_path, unidad_lenta):
    raiz = tmp_path / "usb"
    juego = _tp(make_game)
    carpeta = _carpeta(juego, raiz)
    carpeta.mkdir(parents=True)

    assert _cancelar_a_mitad(juego, raiz).status is JobStatus.CANCELLED
    assert carpeta.is_dir()


def test_el_disco_2_que_falla_no_toca_la_carpeta_del_disco_1(
        make_game, tmp_path, monkeypatch):
    """Un juego de dos discos comparte carpeta: si falla la copia del
    disco 2, el `game.iso` del disco 1 y su carpeta quedan como estaban."""
    raiz = tmp_path / "usb"
    disco2 = make_game(name="tp2.iso", game_id="GZ2E01",
                       title="Twilight Princess", console="gc",
                       contenido=b"disco 2", disc_number=1)
    carpeta = _carpeta(disco2, raiz)
    carpeta.mkdir(parents=True)
    (carpeta / "game.iso").write_bytes(b"disco 1")

    def falla(_fd):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(os, "fsync", falla)
    with pytest.raises(OSError):
        library_ops.send_to_wbfs_drive(disco2, raiz)
    assert [p.name for p in carpeta.iterdir()] == ["game.iso"]


# ============================================================== Fallar --
def test_una_copia_gc_que_falla_no_deja_la_carpeta_vacia(
        make_game, tmp_path, monkeypatch):
    raiz = tmp_path / "usb"
    juego = _tp(make_game, contenido=b"datos de gc")

    def falla(_fd):
        raise OSError(errno.EIO, "Input/output error")
    monkeypatch.setattr(os, "fsync", falla)
    with pytest.raises(OSError):
        library_ops.send_to_wbfs_drive(juego, raiz)

    assert not _carpeta(juego, raiz).exists()
    assert (raiz / "games").is_dir()


def test_una_copia_gc_que_sale_bien_deja_su_carpeta(make_game, tmp_path):
    raiz = tmp_path / "usb"
    juego = _tp(make_game, contenido=b"datos de gc")
    dest = library_ops.send_to_wbfs_drive(juego, raiz)
    assert dest.read_bytes() == b"datos de gc"


# ================================================ Recovery: eliminar --
def _parcial_abandonado(carpeta):
    parcial = carpeta / ".game.iso.parcial-ab12cd"
    carpeta.mkdir(parents=True, exist_ok=True)
    parcial.write_bytes(b"medio juego")
    viejo = time.time() - 3600
    os.utime(parcial, (viejo, viejo))
    return parcial


def _eliminar_desde_recovery(raiz):
    (resto,) = recovery_service.scan([raiz], ops=OperationManager(),
                                     open_files=lambda: set())
    recovery_service.delete(resto)


def test_eliminar_el_parcial_desde_recovery_borra_la_carpeta_vacia(tmp_path):
    raiz = tmp_path / "usb"
    carpeta = raiz / "games" / "Twilight Princess [GZ2E01]"
    _parcial_abandonado(carpeta)
    _eliminar_desde_recovery(raiz)
    assert not carpeta.exists()
    assert (raiz / "games").is_dir()


@pytest.mark.parametrize("otro", ["game.iso", "disc2.iso", ".oculto",
                                  "notas.txt"])
def test_con_cualquier_otro_archivo_la_carpeta_no_se_borra(tmp_path, otro):
    raiz = tmp_path / "usb"
    carpeta = raiz / "games" / "Twilight Princess [GZ2E01]"
    _parcial_abandonado(carpeta)
    (carpeta / otro).write_bytes(b"del usuario")
    _eliminar_desde_recovery(raiz)
    assert [p.name for p in carpeta.iterdir()] == [otro]


def test_eliminar_el_parcial_de_wii_desde_recovery_borra_la_carpeta(tmp_path):
    """El temporal de `wit` de una copia de Wii que se cortó, eliminado
    desde el Recovery Manager: `wbfs/<ID6>/` no queda vacía. `wbfs/` sí."""
    raiz = tmp_path / "usb"
    carpeta = raiz / "wbfs" / "RSBE01"
    carpeta.mkdir(parents=True)
    tmp = carpeta / ".RSBE01.wbfs.Ab12Cd.tmp"
    tmp.write_bytes(b"x")
    viejo = time.time() - 3600
    os.utime(tmp, (viejo, viejo))
    _eliminar_desde_recovery(raiz)
    assert not carpeta.exists()
    assert (raiz / "wbfs").is_dir()


def test_el_parcial_de_wii_con_el_juego_al_lado_no_borra_la_carpeta(tmp_path):
    raiz = tmp_path / "usb"
    carpeta = raiz / "wbfs" / "RSBE01"
    carpeta.mkdir(parents=True)
    (carpeta / "RSBE01.wbfs").write_bytes(b"juego del usuario")
    tmp = carpeta / ".RSBE01.wbfs.Ab12Cd.tmp"
    tmp.write_bytes(b"x")
    viejo = time.time() - 3600
    os.utime(tmp, (viejo, viejo))
    _eliminar_desde_recovery(raiz)
    assert [p.name for p in carpeta.iterdir()] == ["RSBE01.wbfs"]


# ================================================================ Wii --
def _mario_kart(make_game, contenido=None):
    """Un WBFS que entra entero: `send_to_wbfs_drive` lo copia directo (sin
    `wit`), por el mismo camino de `_copy_with_progress` que GameCube."""
    return make_game(name="mk.wbfs", game_id="RMCP01", title="Mario Kart Wii",
                     fmt="WBFS", console="wii",
                     contenido=contenido if contenido is not None
                     else os.urandom(TAMANO))


@pytest.fixture
def sin_wit_para_copiar(monkeypatch):
    monkeypatch.setattr(library_ops.wit_wrapper, "is_available", lambda _b: False)
    monkeypatch.setattr(library_ops.drives, "needs_wbfs_split", lambda _p: False)


def test_cancelar_una_copia_de_wii_nueva_no_deja_la_carpeta_vacia(
        make_game, tmp_path, unidad_lenta, sin_wit_para_copiar):
    raiz = tmp_path / "usb"
    raiz.mkdir()
    juego = _mario_kart(make_game)
    job = _cancelar_a_mitad(juego, raiz)

    assert job.status is JobStatus.CANCELLED
    assert not (raiz / "wbfs" / "RMCP01").exists()
    assert (raiz / "wbfs").is_dir()


def _wit_que_falla(monkeypatch):
    """`wit COPY` de mentira que deja su temporal a medias y sale con
    error, como un `wit` que se cae a mitad de convertir."""
    monkeypatch.setattr(library_ops.wit_wrapper, "is_available", lambda _b: True)
    monkeypatch.setattr(library_ops.drives, "needs_wbfs_split", lambda _p: True)
    monkeypatch.setattr(library_ops.drives, "is_fat_filesystem", lambda _p: True)

    def convert(src, dest, fmt, binary, **_kw):
        dest = Path(dest)
        (dest.parent / f".{dest.name}.LtnLTy.tmp").write_bytes(b"a medias")
        return subprocess.CompletedProcess([], 1, "", "wit: error simulado")

    monkeypatch.setattr(library_ops.wit_wrapper, "convert", convert)


def test_una_conversion_de_wii_nueva_que_falla_no_deja_la_carpeta(
        make_game, tmp_path, monkeypatch):
    raiz = tmp_path / "usb"
    _wit_que_falla(monkeypatch)
    juego = make_game(name="brawl.iso", game_id="RSBE01",
                      title="Super Smash Bros. Brawl", fmt="ISO",
                      contenido=b"iso")
    with pytest.raises(RuntimeError):
        library_ops.send_to_wbfs_drive(juego, raiz)
    assert not (raiz / "wbfs" / "RSBE01").exists()
    assert (raiz / "wbfs").is_dir()


def test_reemplazar_un_juego_de_wii_que_falla_deja_la_carpeta_intacta(
        make_game, tmp_path, monkeypatch):
    """La carpeta del juego que se estaba reemplazando ya existía: no es
    de esta operación, y adentro vuelve a quedar el original."""
    raiz = tmp_path / "usb"
    carpeta = raiz / "wbfs" / "RSBE01"
    carpeta.mkdir(parents=True)
    original = {"RSBE01.wbfs": b"ORIGINAL 0", "RSBE01.wbf1": b"ORIGINAL 1"}
    for nombre, datos in original.items():
        (carpeta / nombre).write_bytes(datos)
    _wit_que_falla(monkeypatch)
    juego = make_game(name="brawl.iso", game_id="RSBE01",
                      title="Super Smash Bros. Brawl", fmt="ISO",
                      contenido=b"iso")
    with pytest.raises(RuntimeError):
        library_ops.send_to_wbfs_drive(juego, raiz, overwrite=True)
    assert {p.name: p.read_bytes() for p in carpeta.iterdir()} == original


def test_cancelar_el_reemplazo_de_un_wii_existente_deja_la_carpeta(
        make_game, tmp_path, unidad_lenta, sin_wit_para_copiar):
    raiz = tmp_path / "usb"
    carpeta = raiz / "wbfs" / "RMCP01"
    carpeta.mkdir(parents=True)
    (carpeta / "RMCP01.wbfs").write_bytes(b"el que ya tenia")
    juego = _mario_kart(make_game)

    cola = TransferQueue(OperationManager(), dispatch=sync_dispatch)
    job = cola.add_jobs([juego], raiz, overwrite=True)[0]
    assert _esperar(lambda: job.status is JobStatus.RUNNING, 5)
    time.sleep(0.5)
    cola.cancel_all()
    assert _esperar(lambda: job.is_final, 10)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.CANCELLED
    assert [p.name for p in carpeta.iterdir()] == ["RMCP01.wbfs"]
    assert (carpeta / "RMCP01.wbfs").read_bytes() == b"el que ya tenia"


# ======================================================== La primitiva --
def test_nunca_borra_las_raices_ni_nada_fuera_de_la_estructura(tmp_path):
    raiz = tmp_path / "usb"
    games = raiz / "games"
    wbfs = raiz / "wbfs"
    afuera = raiz / "otra" / "Juego [GZ2E01]"
    afuera_id6 = raiz / "otra" / "RSBE01"
    for carpeta in (games, wbfs, afuera, afuera_id6):
        carpeta.mkdir(parents=True)
    for carpeta in (games, wbfs, afuera, afuera_id6):
        assert not library_ops.remove_dir_if_empty(carpeta), carpeta
        assert carpeta.is_dir()


@pytest.mark.parametrize("nombre", ["Mario Kart [RMCP01]", "rsbe01", "RSBE0",
                                    "RSBE011", "RSBE-1"])
def test_en_wbfs_solo_se_borran_carpetas_con_nombre_de_id6(tmp_path, nombre):
    """`wbfs/<ID6>/` es lo que arma la app (`transfer_plan.wbfs_dest_path`);
    cualquier otro nombre ahí adentro no es una carpeta suya."""
    carpeta = tmp_path / "usb" / "wbfs" / nombre
    carpeta.mkdir(parents=True)
    assert not library_ops.remove_dir_if_empty(carpeta)
    assert carpeta.is_dir()


def test_una_carpeta_wbfs_id6_vacia_se_borra(tmp_path):
    carpeta = tmp_path / "usb" / "wbfs" / "RSBE01"
    carpeta.mkdir(parents=True)
    assert library_ops.remove_dir_if_empty(carpeta)
    assert not carpeta.exists()
    assert (tmp_path / "usb" / "wbfs").is_dir()


def test_una_carpeta_con_un_oculto_no_se_borra(tmp_path):
    carpeta = tmp_path / "games" / "Juego [GZ2E01]"
    carpeta.mkdir(parents=True)
    (carpeta / ".parcial-de-otra-copia").write_bytes(b"x")
    assert not library_ops.remove_dir_if_empty(carpeta)
    assert carpeta.is_dir()
