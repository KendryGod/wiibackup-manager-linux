"""Una copia de GameCube se verifica comparando el SHA-1 del origen con el
de lo que quedó en la unidad.

`wit VERIFY` solo sabe revisar discos de Wii, así que los juegos de
GameCube se copiaban sin ninguna comprobación: la fila decía "sin
verificar". La copia de GameCube es byte a byte (Nintendont lee ISO y
CISO tal cual), así que el destino tiene que ser idéntico al origen:

- el SHA-1 del origen se calcula MIENTRAS se copia, con los mismos bytes
  que se leen para escribir (el origen no se lee dos veces);
- con la copia ya bajada a la unidad y sacada de la caché
  (`fileops.flush_and_drop_cache`), se relee el destino y se calcula el
  suyo, con progreso en la fila y cancelable;
- si no coinciden, la tarea queda como "Copiado, pero no verificó"; si
  coinciden, "verificado: igual al original (SHA-1)", y el historial
  guarda el SHA-1 (es el que publica Redump para cada disco).

Se elige SHA-1 por eso y porque, medido en esta máquina (i5-10310U, sin
SHA-NI), va a 736 MiB/s: casi lo mismo que blake2b (811) y el doble que
SHA-256 (365).
"""
from __future__ import annotations

import errno
import hashlib
import os
import types

import pytest

from wiibackup_manager import fileops, oplog, queue_manager, transfer_plan, wit_wrapper
from wiibackup_manager.operations import OperationManager
from wiibackup_manager.queue_manager import JobStatus, TransferQueue

MIB = 1024 * 1024
CONTENIDO = os.urandom(3 * MIB + 123)
SHA1 = hashlib.sha1(CONTENIDO).hexdigest()


def _sync(func, *args):
    func(*args)


def _esperar(job, timeout=10.0):
    import time
    t0 = time.monotonic()
    while not job.is_final and time.monotonic() - t0 < timeout:
        time.sleep(0.01)
    return job


@pytest.fixture
def juego(make_game, monkeypatch):
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)
    return make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                     console="gc", contenido=CONTENIDO)


@pytest.fixture
def destino(tmp_path):
    d = tmp_path / "usb"
    d.mkdir()
    return d


def _cola(tmp_path, al_cambiar=None):
    log = oplog.OperationLog(tmp_path / "history.json")
    return TransferQueue(OperationManager(log), on_job_changed=al_cambiar,
                         dispatch=_sync), log


def _copiar(cola, juego, destino, **kw):
    job = cola.add_jobs([juego], destino, **kw)[0]
    _esperar(job)
    cola.shutdown(wait=5)
    return job


# ------------------------------------------------------------ veredicto --
def test_verificada_compara_el_sha1_del_origen_con_el_del_destino(juego, destino, tmp_path):
    cola, log = _cola(tmp_path)

    job = _copiar(cola, juego, destino, verify_gamecube=True)

    assert job.status is JobStatus.DONE, job.error_msg
    assert job.verify_note == "verificado: igual al original (SHA-1)"
    assert list(job.phase_times) == ["prepare", "copy", "sync", "verify"]
    assert transfer_plan.game_dest_path(juego, destino).read_bytes() == CONTENIDO
    [entrada] = log.entries()
    assert entrada.status == oplog.STATUS_OK
    assert "verificado: igual al original (SHA-1)" in entrada.detail
    assert f"SHA-1 {SHA1}" in entrada.detail


def test_si_la_unidad_devuelve_otra_cosa_la_copia_no_verifica(juego, destino, tmp_path,
                                                             monkeypatch):
    """Un pendrive que acepta la escritura y devuelve otra cosa al leer (el
    caso de las memorias truchas): después de bajar la copia, un byte del
    medio cambia."""
    real = fileops.flush_and_drop_cache

    def _bajar_y_corromper(paths, cancel=None, on_progress=None):
        real(paths, cancel=cancel, on_progress=on_progress)
        for p in paths:
            with open(p, "r+b") as f:
                f.seek(2 * MIB)
                byte = f.read(1)
                f.seek(2 * MIB)
                f.write(bytes([byte[0] ^ 0xFF]))

    monkeypatch.setattr(queue_manager.fileops, "flush_and_drop_cache", _bajar_y_corromper)
    cola, log = _cola(tmp_path)

    job = _copiar(cola, juego, destino, verify_gamecube=True)

    assert job.status is JobStatus.CORRUPT, job.status
    assert "no es igual al original" in job.error_msg
    assert log.entries()[0].status == oplog.STATUS_PARTIAL
    # El historial guarda el SHA-1 del ORIGINAL, no el de la copia mala.
    assert f"SHA-1 {SHA1}" in log.entries()[0].detail
    # El archivo queda: es el usuario el que decide volver a copiarlo.
    assert transfer_plan.game_dest_path(juego, destino).exists()


def test_el_origen_se_lee_una_sola_vez(juego, destino, tmp_path, monkeypatch):
    """El SHA-1 del origen sale de la misma lectura de la copia: lo único que
    se relee es el destino."""
    releidos = []
    real = fileops.hash_file

    def _espia(path, *a, **kw):
        releidos.append(path)
        return real(path, *a, **kw)

    monkeypatch.setattr(queue_manager.fileops, "hash_file", _espia)
    cola, _log = _cola(tmp_path)

    job = _copiar(cola, juego, destino, verify_gamecube=True)

    assert job.status is JobStatus.DONE, job.error_msg
    assert releidos == [transfer_plan.game_dest_path(juego, destino)]


# --------------------------------------------------------------- progreso --
def test_la_verificacion_muestra_su_progreso(juego, destino, tmp_path, monkeypatch):
    monkeypatch.setattr(queue_manager, "_PROGRESS_INTERVAL", 0)
    fotos = []
    cola, _log = _cola(tmp_path, al_cambiar=lambda j: fotos.append(
        (j.status, j.progress, j.speed_text)))

    job = _copiar(cola, juego, destino, verify_gamecube=True)

    assert job.status is JobStatus.DONE, job.error_msg
    verificando = [(p, t) for s, p, t in fotos if s is JobStatus.VERIFYING]
    progresos = [p for p, _t in verificando]
    assert progresos == sorted(progresos)
    assert progresos[0] == 0.0
    assert 0.6 < progresos[-1] <= 0.99
    assert any(t.startswith("66%") for _p, t in verificando), verificando


def test_el_progreso_de_la_verificacion_no_inunda_la_interfaz(juego, destino, tmp_path,
                                                             monkeypatch):
    """Como la copia: como mucho un aviso cada `_PROGRESS_INTERVAL`. Con el
    reloj quieto, después del primero no sale ninguno más."""
    monkeypatch.setattr(queue_manager, "time", types.SimpleNamespace(monotonic=lambda: 1000.0))
    fotos = []
    cola, _log = _cola(tmp_path, al_cambiar=lambda j: fotos.append((j.status, j.progress)))

    job = _copiar(cola, juego, destino, verify_gamecube=True)

    assert job.status is JobStatus.DONE, job.error_msg
    verificando = [p for s, p in fotos if s is JobStatus.VERIFYING]
    assert len(verificando) == 2, verificando     # el arranque y el primer bloque


# ------------------------------------------------------------- cancelar --
def test_cancelar_mientras_verifica_deja_la_copia_y_lo_dice(juego, destino, tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(queue_manager, "_PROGRESS_INTERVAL", 0)

    def _al_cambiar(j):
        if j.status is JobStatus.VERIFYING and j.progress > 0:
            j.cancel_token.cancel()

    cola, log = _cola(tmp_path, al_cambiar=_al_cambiar)

    job = _copiar(cola, juego, destino, verify_gamecube=True)

    assert job.status is JobStatus.DONE, job.error_msg
    assert job.verify_note == "verificación cancelada"
    assert log.entries()[0].status == oplog.STATUS_PARTIAL
    assert transfer_plan.game_dest_path(juego, destino).read_bytes() == CONTENIDO


# ---------------------------------------------------------------- fallas --
def test_un_error_al_releer_no_marca_la_copia_como_mala(juego, destino, tmp_path,
                                                        monkeypatch):
    def _falla(*_a, **_k):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(queue_manager.fileops, "hash_file", _falla)
    cola, log = _cola(tmp_path)

    job = _copiar(cola, juego, destino, verify_gamecube=True)

    assert job.status is JobStatus.DONE, job.error_msg
    assert job.verify_note.startswith("no se pudo verificar")
    assert log.entries()[0].status == oplog.STATUS_PARTIAL


def test_si_la_unidad_se_va_mientras_verifica(juego, destino, tmp_path, monkeypatch):
    def _falla(*_a, **_k):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(queue_manager.fileops, "hash_file", _falla)
    monkeypatch.setattr(queue_manager.drives, "device_is_gone", lambda **_k: True)
    cola, _log = _cola(tmp_path)

    job = _copiar(cola, juego, destino, verify_gamecube=True)

    assert job.status is JobStatus.DEVICE_DISCONNECTED, job.status


@pytest.mark.parametrize("se_fue, estado", [(True, JobStatus.DEVICE_DISCONNECTED),
                                             (False, JobStatus.DONE)])
def test_si_la_copia_desaparece_antes_de_releerla(juego, destino, tmp_path, monkeypatch,
                                                 se_fue, estado):
    """El tamaño se mide adentro del mismo `try` que la relectura: si la
    unidad ya no está, la tarea se cierra igual por `_finish_job`."""
    def _no_esta(_p):
        raise FileNotFoundError(errno.ENOENT, "No such file or directory")

    monkeypatch.setattr(queue_manager, "os",
                        types.SimpleNamespace(path=types.SimpleNamespace(getsize=_no_esta)))
    monkeypatch.setattr(queue_manager.drives, "device_is_gone", lambda **_k: se_fue)
    cola, _log = _cola(tmp_path)

    job = _copiar(cola, juego, destino, verify_gamecube=True)

    assert job.status is estado, (job.status, job.error_msg)
    if estado is JobStatus.DONE:
        assert job.verify_note.startswith("no se pudo verificar")


# --------------------------------------------------------- sin verificar --
def _sin_releer(monkeypatch):
    def _no(*_a, **_k):
        raise AssertionError("no se tenía que releer nada")

    monkeypatch.setattr(queue_manager.fileops, "hash_file", _no)
    monkeypatch.setattr(queue_manager.fileops, "flush_and_drop_cache", _no)


def test_apagada_no_relee_nada(juego, destino, tmp_path, monkeypatch):
    _sin_releer(monkeypatch)
    cola, _log = _cola(tmp_path)

    job = _copiar(cola, juego, destino, verify_gamecube=False)

    assert job.status is JobStatus.DONE, job.error_msg
    assert job.verify_note == ""
    assert list(job.phase_times) == ["prepare", "copy"]


def test_un_wii_no_se_compara_por_hash(make_game, destino, tmp_path, monkeypatch):
    """Los juegos de Wii se verifican con `wit VERIFY` (su propio switch):
    `wit` convierte el formato al copiar, así que origen y destino no son
    los mismos bytes."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)
    monkeypatch.setattr(queue_manager.library_ops.wit_wrapper, "is_available",
                        lambda _b: False)
    _sin_releer(monkeypatch)
    wii = make_game(name="juego.wbfs", game_id="RMCP01", title="Mario Kart Wii",
                    fmt="WBFS", contenido=b"wbfs")
    cola, _log = _cola(tmp_path)

    job = _copiar(cola, wii, destino, verify_gamecube=True)

    assert job.status is JobStatus.DONE, job.error_msg
    assert job.verify_note == ""


# --------------------------------------------------------------- fileops --
def test_hash_file_da_el_sha1_y_avisa_lo_leido(tmp_path):
    p = tmp_path / "x.iso"
    p.write_bytes(CONTENIDO)
    leido = []

    assert fileops.hash_file(p, progress_cb=leido.append) == SHA1
    assert leido == [MIB, 2 * MIB, 3 * MIB, 3 * MIB + 123]


def test_hash_file_se_puede_cancelar(tmp_path):
    p = tmp_path / "x.iso"
    p.write_bytes(CONTENIDO)
    token = wit_wrapper.CancellationToken()
    leido = []

    def _avance(n):
        leido.append(n)
        token.cancel()

    with pytest.raises(wit_wrapper.OperationCancelled):
        fileops.hash_file(p, cancel=token, progress_cb=_avance)
    assert leido == [MIB]


def test_la_copia_calcula_el_sha1_de_lo_que_lee(tmp_path):
    origen = tmp_path / "origen.iso"
    origen.write_bytes(CONTENIDO)
    h = hashlib.sha1()

    fileops._copy_with_progress(origen, tmp_path / "copia.iso", lambda _n: None,
                                digest=h)

    assert h.hexdigest() == SHA1
    assert (tmp_path / "copia.iso").read_bytes() == CONTENIDO
