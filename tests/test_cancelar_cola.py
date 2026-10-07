"""Cancelar la cola de transferencia mientras la unidad destino es lenta.

El caso real: Twilight Princess (GameCube, ~1 GB) a un pendrive que
escribe a ~14.7 MB/s. En una máquina con RAM de sobra, `write()` deja todo
el archivo en la caché del kernel en un segundo, y lo que de verdad tarda
-más de un minuto- es bajarlo a la unidad. Si la cancelación solo se mira
entre bloques de la copia, cuando el usuario aprieta "Cancelar todo" ese
bucle ya terminó y el hilo está metido en un `fsync` que no se puede
interrumpir.

Para probarlo sin un pendrive se simula la unidad: escribir es gratis y
cada vez que algo espera a que los datos estén en el dispositivo (`fsync`,
o esperar un rango con `sync_file_range`) duerme lo que tardaría una
unidad de `VELOCIDAD` bytes/s en bajar lo que todavía faltaba.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

from wiibackup_manager import fsutil, transfer_plan
from wiibackup_manager.operations import OperationManager
from wiibackup_manager.queue_manager import JobStatus, TransferQueue

MIB = 1024 * 1024
VELOCIDAD = 16 * MIB          # bytes/s que "baja" la unidad simulada
TAMANO = 48 * MIB             # 3 s de escritura real a esa velocidad


def sync_dispatch(func, *args) -> None:
    func(*args)


class UnidadLenta:
    """Lleva, por archivo (dev, inodo), hasta qué byte ya "llegó" a la
    unidad. Esperar a un byte que todavía no llegó cuesta tiempo."""

    def __init__(self, velocidad: int):
        self.velocidad = velocidad
        self.bajado = {}
        self._lock = threading.Lock()
        self.esperas = 0

    def _esperar(self, fd: int, hasta: int) -> None:
        st = os.fstat(fd)
        clave = (st.st_dev, st.st_ino)
        with self._lock:
            hecho = self.bajado.get(clave, 0)
            falta = max(hasta - hecho, 0)
            self.bajado[clave] = max(hecho, hasta)
        if falta:
            self.esperas += 1
            time.sleep(falta / self.velocidad)

    def fsync(self, fd: int) -> None:
        self._esperar(fd, os.fstat(fd).st_size)

    def sync_range(self, fd: int, offset: int, nbytes: int, flags: int) -> None:
        if flags & (fsutil.SYNC_FILE_RANGE_WAIT_BEFORE
                    | fsutil.SYNC_FILE_RANGE_WAIT_AFTER):
            self._esperar(fd, offset + nbytes)


@pytest.fixture
def unidad_lenta(monkeypatch):
    unidad = UnidadLenta(VELOCIDAD)
    monkeypatch.setattr(os, "fsync", unidad.fsync)
    monkeypatch.setattr(os, "fdatasync", unidad.fsync)
    monkeypatch.setattr(fsutil, "sync_range", unidad.sync_range, raising=False)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)
    return unidad


def _esperar(condicion, timeout: float) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if condicion():
            return True
        time.sleep(0.01)
    return condicion()


def _restos(carpeta) -> list:
    return [p for p in carpeta.rglob("*") if p.is_file()]


def test_cancelar_todo_frena_una_copia_gc_a_una_unidad_lenta(
        make_game, tmp_path, unidad_lenta):
    """El reporte: con la barra en ~71% apretó "Cancelar todo" y la copia
    siguió. Tiene que quedar Cancelada en pocos segundos, sin haber
    esperado a que la unidad baje el archivo entero, y sin dejar nada en
    el destino -ni el juego ni un temporal a medio escribir-."""
    juego = make_game(name="tp.iso", game_id="GZ2E01",
                      title="Twilight Princess", console="gc",
                      contenido=os.urandom(TAMANO))
    destino = tmp_path / "usb"
    destino.mkdir()

    cola = TransferQueue(OperationManager(), dispatch=sync_dispatch)
    job = cola.add_jobs([juego], destino)[0]
    assert _esperar(lambda: job.status is JobStatus.RUNNING, 5)
    # Bien adentro de la copia: con el código viejo, a esta altura el
    # bucle ya terminó y el hilo está dormido en el `fsync`.
    time.sleep(0.6)

    t0 = time.monotonic()
    assert cola.cancel_all() == 1
    # La interfaz no queda muda: dice que se está cancelando en el acto.
    assert job.is_final or job.speed_text == "Cancelando…"

    assert _esperar(lambda: job.is_final, 10)
    tardo = time.monotonic() - t0
    cola.shutdown(wait=5)

    assert job.status is JobStatus.CANCELLED, (job.status, job.error_msg)
    # Mucho menos que los 3 s que tardaría la unidad en bajar todo.
    assert tardo < 1.5, f"tardó {tardo:.1f}s en cancelar"
    assert _restos(destino) == [], _restos(destino)


def test_cancelar_todo_frena_tambien_lo_que_esperaba_en_la_cola(
        make_game, tmp_path, unidad_lenta):
    """"Cancelar todo" es la cola entera: la que copia y las que esperan
    su turno."""
    juegos = [make_game(name=f"j{i}.iso", game_id=f"GZ2E0{i}",
                        title=f"Juego {i}", console="gc",
                        contenido=os.urandom(TAMANO // 2))
              for i in range(3)]
    destino = tmp_path / "usb"
    destino.mkdir()

    cola = TransferQueue(OperationManager(), dispatch=sync_dispatch)
    jobs = cola.add_jobs(juegos, destino)
    assert _esperar(lambda: jobs[0].status is JobStatus.RUNNING, 5)
    time.sleep(0.4)

    assert cola.cancel_all() == 3
    assert _esperar(lambda: all(j.is_final for j in jobs), 10)
    cola.shutdown(wait=5)

    assert [j.status for j in jobs] == [JobStatus.CANCELLED] * 3
    assert _restos(destino) == []


def test_una_copia_que_nadie_cancela_termina_entera(
        make_game, tmp_path, unidad_lenta):
    """El arreglo no puede romper el camino feliz: sin cancelar, el
    archivo llega entero y bajado a la unidad antes de darse por
    copiado."""
    contenido = os.urandom(TAMANO // 4)
    juego = make_game(name="tp.iso", game_id="GZ2E01",
                      title="Twilight Princess", console="gc",
                      contenido=contenido)
    destino = tmp_path / "usb"
    destino.mkdir()

    cola = TransferQueue(OperationManager(), dispatch=sync_dispatch)
    job = cola.add_jobs([juego], destino)[0]
    assert _esperar(lambda: job.is_final, 10)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg
    copiado = transfer_plan.gc_dest_path(juego, destino)
    assert copiado.read_bytes() == contenido
    st = copiado.stat()
    assert unidad_lenta.bajado[(st.st_dev, st.st_ino)] == len(contenido)


def test_la_sincronizacion_antes_de_verificar_se_puede_cancelar(
        tmp_path, unidad_lenta):
    """La fase "sincronización" baja a la unidad lo que `wit` dejó en la
    caché antes de verificar. Era un `fsync` de una sola vez: con un
    pendrive lento, minutos sin forma de cortarlo. Ahora va de a ventanas
    y mira el token entre una y otra."""
    from wiibackup_manager import fileops, wit_wrapper

    archivo = tmp_path / "juego.wbfs"
    archivo.write_bytes(os.urandom(TAMANO))
    token = wit_wrapper.CancellationToken()
    threading.Timer(0.3, token.cancel).start()

    t0 = time.monotonic()
    with pytest.raises(wit_wrapper.OperationCancelled):
        fileops.flush_and_drop_cache([archivo], cancel=token)
    assert time.monotonic() - t0 < 1.5


def test_sin_cancelar_la_sincronizacion_baja_todo(tmp_path, unidad_lenta):
    from wiibackup_manager import fileops

    archivo = tmp_path / "juego.wbfs"
    archivo.write_bytes(os.urandom(TAMANO // 4))
    fileops.flush_and_drop_cache([archivo])
    st = archivo.stat()
    assert unidad_lenta.bajado[(st.st_dev, st.st_ino)] == TAMANO // 4


def test_bounded_writeback_nunca_deja_mas_de_dos_ventanas_sin_bajar(
        tmp_path, unidad_lenta):
    """El tope de lo que queda en la caché es lo que acota cuánto tarda
    una cancelación."""
    ventana = MIB
    with open(tmp_path / "x", "wb") as f:
        bajada = fsutil.BoundedWriteback(f.fileno(), window=ventana)
        escrito = 0
        for _ in range(10):
            f.write(os.urandom(ventana // 2))
            f.flush()
            escrito += ventana // 2
            bajada.advance(escrito)
            assert escrito - bajada.confirmed <= 2 * ventana
        bajada.finish()
        assert bajada.confirmed == escrito
