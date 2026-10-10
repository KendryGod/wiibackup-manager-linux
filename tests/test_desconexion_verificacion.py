"""La unidad se desconecta después de la copia: al bajarla a disco
(`TransferQueue._sync_copy`) o al releerla (`TransferQueue._verify_copy`).

El destino es un punto de montaje fijo (`/mnt/usb`, creado a mano) que
queda como carpeta vacía que responde cuando la unidad se va: la única
señal es que dejó de ser punto de montaje. `_copy` ya la miraba
(`raiz_era_montaje`); estas dos fases no, y terminaban en ERROR o CORRUPT.

EIO solo no es desconexión (`drives._ERRNOS_SIN_DISPOSITIVO`): con la
unidad montada, un error de E/S sigue siendo un error, y una relectura que
no pasa sigue siendo CORRUPT.
"""
from __future__ import annotations

import errno
import time
from pathlib import Path

import pytest

from wiibackup_manager import (drives, fileops, library_ops, queue_manager,
                               transfer_plan)
from wiibackup_manager.operations import OperationManager
from wiibackup_manager.queue_manager import JobStatus, TransferQueue


def _esperar(job, timeout=10.0):
    limite = time.monotonic() + timeout
    while not job.is_final and time.monotonic() < limite:
        time.sleep(0.01)
    assert job.is_final


@pytest.fixture
def usb(tmp_path, monkeypatch):
    """`/mnt/usb`: punto de montaje hasta que `ida["si"]`; la carpeta sigue
    existiendo y respondiendo después."""
    raiz = tmp_path / "mnt_usb"
    raiz.mkdir()
    ida = {"si": False}
    real = drives.is_mount_point
    monkeypatch.setattr(drives, "is_mount_point",
                        lambda p: (not ida["si"]) if Path(p) == raiz else real(p))
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)
    monkeypatch.setattr(library_ops.wit_wrapper, "is_available", lambda _b: False)
    monkeypatch.setattr(queue_manager.wit_wrapper, "disc_console",
                        lambda _p, _b="wit": "wii")
    return raiz, ida


def _copiar_y_verificar(make_game, raiz):
    juego = make_game(name="juego.wbfs", game_id="RMCP01",
                      title="Mario Kart Wii", fmt="WBFS",
                      contenido=b"contenido wbfs")
    cola = TransferQueue(OperationManager(), dispatch=lambda f, *a: f(*a))
    job = cola.add_jobs([juego], raiz, verify_after_copy=True)[0]
    _esperar(job)
    cola.shutdown(wait=5)
    return job


# ------------------------------------------------------- N1: _sync_copy --
def test_desconexion_al_bajar_a_disco_es_unidad_desconectada(
        make_game, usb, monkeypatch):
    raiz, ida = usb

    def flush_y_se_va(paths, cancel=None, on_progress=None):
        ida["si"] = True
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(fileops, "flush_and_drop_cache", flush_y_se_va)
    job = _copiar_y_verificar(make_game, raiz)
    assert job.status is JobStatus.DEVICE_DISCONNECTED, job.error_msg


def test_eio_al_bajar_a_disco_con_la_unidad_montada_sigue_siendo_error(
        make_game, usb, monkeypatch):
    raiz, _ida = usb

    def flush_falla(paths, cancel=None, on_progress=None):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(fileops, "flush_and_drop_cache", flush_falla)
    job = _copiar_y_verificar(make_game, raiz)
    assert job.status is JobStatus.ERROR

