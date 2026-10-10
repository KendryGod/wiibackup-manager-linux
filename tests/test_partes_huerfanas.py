"""Partes de un WBFS dividido que quedan de más al reemplazar o restaurar.

Un USB Loader lee `RSBE01.wbfs` + `RSBE01.wbf1` + `RSBE01.wbf2`… como UN
solo juego: una parte que sobra de otra copia se pega al final y el juego
queda inservible. Dos caminos la dejaban:

(i) Copia directa (un WBFS que entra entero) sobre un juego que estaba
    dividido: se reemplazaba `RSBE01.wbfs` y la `RSBE01.wbf1` vieja
    quedaba. Pasa con unidades que traen el juego dividido por otra
    herramienta (partes de 2 GB) y se lo vuelve a copiar entero.
(ii) Recovery Manager restaurando un respaldo de UNA parte después de una
    copia cortada que ya había dejado DOS con nombre final. `wit` escribe
    en `.tmp` y renombra a los nombres finales al terminar; si la unidad
    se va mientras se baja lo escrito (antes del `commit`), quedan las
    partes nuevas con nombre final y el original apartado.
"""
from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path

import pytest

from wiibackup_manager import (atomicfs, drives, fileops, library_ops,
                               recovery_service, transfer_plan, wit_wrapper)
from wiibackup_manager.game_model import Game
from wiibackup_manager.operations import OperationManager


@pytest.fixture
def carpeta(tmp_path):
    c = tmp_path / "WII_USB" / "wbfs" / "RSBE01"
    c.mkdir(parents=True)
    return c


def _contenido(c: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(c.iterdir())}


def _juego(tmp_path, fmt="WBFS", datos=b"juego nuevo entero"):
    src = tmp_path / f"brawl.{fmt.lower()}"
    src.write_bytes(datos)
    return Game(path=src, game_id="RSBE01", title="Super Smash Bros. Brawl",
                fmt=fmt, size_bytes=len(datos), identified_by="wbfs",
                console="wii")


# ------------------------------------------ (i) copia directa sobre dividido --
def test_copia_directa_sobre_un_juego_dividido_no_deja_la_parte_vieja(
        carpeta, tmp_path, monkeypatch):
    (carpeta / "RSBE01.wbfs").write_bytes(b"viejo parte 0")
    (carpeta / "RSBE01.wbf1").write_bytes(b"viejo parte 1")
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b: False)
    monkeypatch.setattr(drives, "needs_wbfs_split", lambda _p: True)
    juego = _juego(tmp_path)

    library_ops.send_to_wbfs_drive(juego, carpeta.parent.parent, overwrite=True)

    assert _contenido(carpeta) == {"RSBE01.wbfs": b"juego nuevo entero"}


def test_copia_directa_que_falla_deja_el_juego_dividido_entero(
        carpeta, tmp_path, monkeypatch):
    (carpeta / "RSBE01.wbfs").write_bytes(b"viejo parte 0")
    (carpeta / "RSBE01.wbf1").write_bytes(b"viejo parte 1")
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b: False)
    monkeypatch.setattr(drives, "needs_wbfs_split", lambda _p: True)

    def copia_que_falla(src, dest, cb, cancel=None):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(library_ops, "_copy_with_progress", copia_que_falla)

    with pytest.raises(OSError):
        library_ops.send_to_wbfs_drive(_juego(tmp_path), carpeta.parent.parent,
                                       overwrite=True)

    assert _contenido(carpeta) == {"RSBE01.wbfs": b"viejo parte 0",
                                   "RSBE01.wbf1": b"viejo parte 1"}


# ------------------------------------ (ii) restaurar con partes nuevas de más --
def test_restaurar_un_original_de_una_parte_borra_las_partes_nuevas(
        carpeta, tmp_path, monkeypatch):
    """El camino real: reemplazo con `wit` (que deja el juego nuevo en dos
    partes con nombre final), la unidad se desenchufa mientras se baja lo
    escrito, el original no puede volver; al reconectar, el Recovery
    Manager lo restaura."""
    (carpeta / "RSBE01.wbfs").write_bytes(b"ORIGINAL de una sola parte")
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b: True)
    monkeypatch.setattr(drives, "needs_wbfs_split", lambda _p: True)
    monkeypatch.setattr(drives, "is_fat_filesystem", lambda _p: True)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)

    def wit_que_termina(src, dest, fmt, binary, **_kw):
        dest = Path(dest)
        dest.write_bytes(b"nuevo parte 0")
        dest.with_suffix(".wbf1").write_bytes(b"nuevo parte 1")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(wit_wrapper, "convert", wit_que_termina)
    # Desenchufada, la unidad no acepta ni renombres ni borrados.
    unidad_ida = {"si": False}
    real_replace, real_unlink = atomicfs.os.replace, os.unlink

    def replace(a, b, *k, **kw):
        if unidad_ida["si"]:
            raise OSError(errno.EIO, "Input/output error")
        return real_replace(a, b, *k, **kw)

    def unlink(p, *k, **kw):
        if unidad_ida["si"]:
            raise OSError(errno.EIO, "Input/output error")
        return real_unlink(p, *k, **kw)

    def flush_y_se_desenchufa(paths, cancel=None, on_progress=None):
        unidad_ida["si"] = True
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(atomicfs.os, "replace", replace)
    monkeypatch.setattr(os, "unlink", unlink)
    monkeypatch.setattr(fileops, "flush_and_drop_cache", flush_y_se_desenchufa)

    with pytest.raises(library_ops.RollbackFailedError):
        library_ops.send_to_wbfs_drive(_juego(tmp_path, fmt="ISO"),
                                       carpeta.parent.parent, overwrite=True)

    # La unidad vuelve: el juego nuevo a medias con nombre final, y el
    # original apartado.
    unidad_ida["si"] = False
    assert sorted(p.name for p in carpeta.iterdir() if not p.name.startswith(".")) \
        == ["RSBE01.wbf1", "RSBE01.wbfs"]
    restos = recovery_service.scan([carpeta.parent.parent], ops=OperationManager(),
                                   min_age=0)
    [respaldo] = [r for r in restos
                  if r.kind is recovery_service.LeftoverKind.BACKUP]

    recovery_service.restore(respaldo)

    assert _contenido(carpeta) == {"RSBE01.wbfs": b"ORIGINAL de una sola parte"}


def test_restaurar_un_original_dividido_borra_solo_lo_que_sobra(carpeta):
    """El original tenía dos partes y lo cortado dejó tres: vuelven las dos
    del original y se va la tercera, que no es de ningún juego."""
    pid = 999_999_999      # un PID que no corre: resto abandonado
    for i, nombre in enumerate(("RSBE01.wbfs", "RSBE01.wbf1")):
        (carpeta / f".{nombre}.{library_ops.MARCA_RESPALDO}-{pid}").write_bytes(
            f"ORIGINAL parte {i}".encode())
    for nombre in ("RSBE01.wbfs", "RSBE01.wbf1", "RSBE01.wbf2"):
        (carpeta / nombre).write_bytes(b"cortado")
    restos = recovery_service.scan([carpeta.parent.parent], ops=OperationManager())
    [respaldo] = restos

    recovery_service.restore(respaldo)

    assert _contenido(carpeta) == {"RSBE01.wbfs": b"ORIGINAL parte 0",
                                   "RSBE01.wbf1": b"ORIGINAL parte 1"}


def test_si_restaurar_falla_no_se_borra_ninguna_parte(carpeta, monkeypatch):
    pid = 999_999_999
    (carpeta / f".RSBE01.wbfs.{library_ops.MARCA_RESPALDO}-{pid}").write_bytes(
        b"ORIGINAL")
    (carpeta / "RSBE01.wbfs").write_bytes(b"cortado 0")
    (carpeta / "RSBE01.wbf1").write_bytes(b"cortado 1")
    [respaldo] = recovery_service.scan([carpeta.parent.parent], ops=OperationManager())
    monkeypatch.setattr(atomicfs.os, "replace",
                        lambda a, b: (_ for _ in ()).throw(OSError(errno.EIO, "x")))

    with pytest.raises(recovery_service.RecoveryError):
        recovery_service.restore(respaldo)

    assert (carpeta / "RSBE01.wbf1").read_bytes() == b"cortado 1"
    assert any(p.name.startswith(".RSBE01.wbfs.respaldo") for p in carpeta.iterdir())

