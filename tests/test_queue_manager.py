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

import time

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
