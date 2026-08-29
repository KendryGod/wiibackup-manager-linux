"""Pruebas de `queue_manager.TransferQueue`.

La cola corre en un hilo de fondo (ver el docstring del módulo), así que
acá no se llama a `_copy` ni a ningún método privado directo: se encola de
verdad con `add_jobs` y se espera a que la tarea llegue a un estado final,
igual que hace `tools/manual_queue_e2e.py`. El único ajuste para probar
sin GTK es `dispatch=sync_dispatch`, que reemplaza `GLib.idle_add` por una
llamada directa -documentado como parámetro inyectable en
`TransferQueue.__init__`.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from wiibackup_manager import library
from wiibackup_manager.operations import OperationManager
from wiibackup_manager.queue_manager import JobStatus, TransferQueue


def sync_dispatch(func, *args) -> None:
    func(*args)


def hacer_cola() -> TransferQueue:
    return TransferQueue(OperationManager(), dispatch=sync_dispatch)


def esperar_final(job, timeout=10.0):
    t0 = time.monotonic()
    while not job.is_final and time.monotonic() - t0 < timeout:
        time.sleep(0.01)
    return job


# ------------------------------------------------------------ GameCube --
def test_copy_marca_error_sin_espacio_gc(make_game, tmp_path, monkeypatch):
    """El chequeo de espacio libre de `_copy` corre igual para GameCube que
    para Wii: si no entra, la tarea termina en Error y no llega a escribir
    nada en el destino."""
    monkeypatch.setattr(library, "free_space", lambda path: 100)

    juego = make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                      console="gc", contenido=b"x" * 4096)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    cola = hacer_cola()
    job = cola.add_jobs([juego], dest_root)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.ERROR, job.error_msg
    assert not library.gc_dest_path(juego, dest_root).exists()


def test_copy_gc_con_espacio_suficiente_copia(make_game, tmp_path, monkeypatch):
    """Con espacio de sobra, un juego de GameCube se copia entero a la
    estructura de Nintendont y la tarea llega a Completado."""
    monkeypatch.setattr(library, "free_space", lambda path: 10 ** 12)

    contenido = b"contenido de prueba gc" * 100
    juego = make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                      console="gc", contenido=contenido)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    cola = hacer_cola()
    job = cola.add_jobs([juego], dest_root)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg
    destino = library.gc_dest_path(juego, dest_root)
    assert destino.read_bytes() == contenido


def test_send_to_wbfs_drive_gc_no_llama_needs_wbfs_split(make_game, tmp_path, monkeypatch):
    """Recorriendo la cola de punta a punta (no solo `send_to_wbfs_drive`
    directo), una tarea de GameCube tampoco debería disparar
    `drives.needs_wbfs_split`: ese chequeo es cosa de Wii/`wit`."""
    def _no_deberia_llamarse(*_a, **_k):
        raise AssertionError(
            "needs_wbfs_split no debería evaluarse para un juego de GameCube")
    monkeypatch.setattr(library.drives, "needs_wbfs_split", _no_deberia_llamarse)

    juego = make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                      console="gc", contenido=b"contenido de prueba gc")
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    cola = hacer_cola()
    job = cola.add_jobs([juego], dest_root)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg


def test_send_to_wbfs_drive_gc_no_divide_aunque_el_destino_lo_pida(make_game, tmp_path,
                                                                    monkeypatch):
    """Aunque el destino "pida" dividir (FAT32 real o detectado como tal),
    una tarea de GameCube copia el archivo entero: a diferencia de Wii acá
    no hay `wit --split` de por medio, es una copia tal cual."""
    monkeypatch.setattr(library.drives, "needs_wbfs_split", lambda path: True)

    contenido = b"contenido de prueba gc"
    juego = make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                      console="gc", contenido=contenido)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    cola = hacer_cola()
    job = cola.add_jobs([juego], dest_root)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg
    destino = library.gc_dest_path(juego, dest_root)
    assert destino.read_bytes() == contenido
    assert not destino.with_suffix(".wbf1").exists()


# ------------------------------------------- RollbackFailedError hasta la UI --
def test_copy_con_rollback_fallido_distingue_los_dos_problemas_en_error_msg(
        make_game, tmp_path, monkeypatch):
    """El caso central que reportó la revisión de seguridad: `wit` falla
    Y ADEMÁS `DestinationGuard` no puede devolver el original a su lugar
    (acá, simulado para las dos partes de un WBFS dividido). El mensaje
    que llega a `job.error_msg` -lo que ve el usuario en la fila de la
    cola- tiene que nombrar los dos problemas, no solo "la conversión
    falló" como si el original se hubiera recuperado sin drama."""
    monkeypatch.setattr(library, "free_space", lambda path: 10 ** 12)
    monkeypatch.setattr(library.wit_wrapper, "is_available", lambda _binary: True)

    def _fake_convert(src, dest, target_format, binary, **kwargs):
        # Simula lo que deja `wit` cuando falla a mitad de camino: los
        # nombres finales ya tienen contenido nuevo (corrupto/parcial).
        Path(dest).write_bytes(b"nuevo-corrupto")
        Path(dest).with_suffix(".wbf1").write_bytes(b"nuevo-corrupto-1")
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="wit: fallo simulado")

    monkeypatch.setattr(library.wit_wrapper, "convert", _fake_convert)

    real_replace = os.replace

    def _falla_solo_al_restaurar(origen, destino):
        if Path(origen).name.startswith("."):
            raise OSError("no se pudo restaurar (simulado)")
        return real_replace(origen, destino)

    monkeypatch.setattr(library.os, "replace", _falla_solo_al_restaurar)

    # `fmt="ISO"` fuerza el camino de `wit` (una copia WBFS directa no
    # pasa por DestinationGuard ni por wit_wrapper.convert).
    juego = make_game(name="juego.iso", game_id="RMCP01", title="Mario Kart Wii",
                      fmt="ISO", contenido=b"contenido de origen")
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    # El destino ya tiene un WBFS dividido de una transferencia anterior:
    # es lo que hace que `DestinationGuard` se active (`enabled=True`) y
    # tenga algo que apartar/restaurar.
    destino = library.game_dest_path(juego, dest_root)
    destino.parent.mkdir(parents=True)
    destino.write_bytes(b"original-wbfs")
    destino.with_suffix(".wbf1").write_bytes(b"original-wbf1")

    cola = hacer_cola()
    job = cola.add_jobs([juego], dest_root, overwrite=True)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.ERROR
    # El motivo original (por qué falló la conversión)...
    assert "wit: fallo simulado" in job.error_msg
    # ...Y ADEMÁS que no se pudo restaurar -las dos cosas, no una sola.
    assert "restaurar" in job.error_msg.lower()

    # El original quedó SIN restaurar de verdad (no es solo el texto del
    # error): el respaldo sigue ahí, rescatable a mano.
    assert not destino.exists() or destino.read_bytes() != b"original-wbfs"
    respaldos = list(dest_root.rglob(".*.respaldo-*"))
    assert respaldos, "el respaldo temporal debería seguir existiendo"
