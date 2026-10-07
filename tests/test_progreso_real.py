"""El progreso de una transferencia tiene que contar lo que llegó a la
UNIDAD, no lo que quedó en la caché del kernel.

El reporte: con un pendrive lento la barra saltaba a ~70% a ~700 MB/s
(los bytes estaban en RAM), después se quedaba quieta mientras la unidad
bajaba de verdad, y un 100% no significaba que se pudiera desenchufar.

`drives.DeviceWriteMeter` se prueba contra un /sys/block de mentira (un
directorio con el mismo formato). La cola se prueba con la unidad lenta
simulada de `test_cancelar_cola` y un medidor que lee lo que esa unidad
ya "bajó": así se puede comprobar, aviso por aviso, que la barra nunca
muestra más de lo que el disco recibió.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from test_cancelar_cola import (MIB, TAMANO, UnidadLenta, VELOCIDAD,  # noqa: F401
                                _esperar, _restos, sync_dispatch)
from wiibackup_manager import (drives, fsutil, library_ops, transfer_plan,
                               wit_wrapper)
from wiibackup_manager.operations import OperationManager
from wiibackup_manager.queue_manager import JobStatus, TransferQueue


# ============================================== DeviceWriteMeter --
def _stat(sys_block: Path, disco: str, sectores_escritos: int) -> None:
    """Una línea de /sys/block/<disco>/stat: 11+ campos, el séptimo son
    los sectores escritos."""
    carpeta = sys_block / disco
    carpeta.mkdir(parents=True, exist_ok=True)
    campos = [10, 0, 800, 5, 20, 0, sectores_escritos, 90, 0, 40, 95]
    (carpeta / "stat").write_text(" ".join(f"{c:8d}" for c in campos) + "\n")


def test_el_medidor_cuenta_desde_que_se_crea_en_sectores_de_512(tmp_path):
    _stat(tmp_path, "sdb", 1000)
    medidor = drives.DeviceWriteMeter(Path("/dev/sdb"), sys_block=tmp_path)
    assert medidor.written() == 0
    _stat(tmp_path, "sdb", 1000 + 2048)
    assert medidor.written() == 2048 * 512


def test_si_el_contador_desaparece_el_medidor_lo_dice(tmp_path):
    """La unidad se desconectó: no hay número, y quien lo usa vuelve a lo
    que escribió el programa."""
    _stat(tmp_path, "sdb", 10)
    medidor = drives.DeviceWriteMeter(Path("/dev/sdb"), sys_block=tmp_path)
    (tmp_path / "sdb" / "stat").unlink()
    assert medidor.written() is None


def test_sin_dispositivo_de_bloque_no_hay_medidor(tmp_path):
    assert drives.DeviceWriteMeter.for_path(
        tmp_path, resolve=lambda _p: None) is None
    # Un disco que no tiene contador tampoco.
    assert drives.DeviceWriteMeter.for_path(
        tmp_path, resolve=lambda _p: Path("/dev/sdz"),
        sys_block=tmp_path) is None


def test_el_medidor_entiende_el_subvolumen_de_btrfs(monkeypatch):
    """`findmnt` informa `/dev/nvme0n1p5[/home]` para un subvolumen."""
    monkeypatch.setattr(drives, "_block_device_for",
                        lambda _p: "/dev/nvme0n1p5[/home]")
    vistos = []
    monkeypatch.setattr(drives, "_whole_disk_path",
                        lambda d: vistos.append(d) or Path("/dev/nvme0n1"))
    assert drives._disk_for_meter("/home") == Path("/dev/nvme0n1")
    assert vistos == ["/dev/nvme0n1p5"]


# ================================================ Cola con medidor --
class MedidorDeLaUnidad:
    """Un `DeviceWriteMeter` que lee la unidad simulada: lo que ya bajó de
    todos los archivos, menos lo que había bajado al crearse."""

    def __init__(self, unidad: UnidadLenta, extra: int = 0):
        self.unidad = unidad
        self.extra = extra          # escrituras de "otro programa"
        self._base = self._total()

    def _total(self) -> int:
        return sum(self.unidad.bajado.values())

    def written(self):
        return self._total() - self._base + self.extra


@pytest.fixture
def unidad_medida(monkeypatch):
    unidad = UnidadLenta(VELOCIDAD)
    monkeypatch.setattr(os, "fsync", unidad.fsync)
    monkeypatch.setattr(os, "fdatasync", unidad.fsync)
    monkeypatch.setattr(fsutil, "sync_range", unidad.sync_range)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)
    medidores = []

    def para(_path, **_kw):
        medidor = MedidorDeLaUnidad(unidad)
        medidores.append(medidor)
        return medidor

    monkeypatch.setattr(drives.DeviceWriteMeter, "for_path", staticmethod(para))
    unidad.medidores = medidores
    return unidad


def _cola_que_anota(avisos: list) -> TransferQueue:
    def al_cambiar(job):
        avisos.append((time.monotonic(), job.progress, job.bytes_done,
                       job.speed_text, job.status))
    return TransferQueue(OperationManager(), dispatch=sync_dispatch,
                         on_job_changed=al_cambiar)


def _wit_que_escribe_en_cache(monkeypatch, tamano: int):
    """`wit COPY` de mentira: escribe el WBFS entero de una (a la caché,
    porque la unidad simulada solo cobra tiempo al bajar) e informa los
    bytes como lo hace el de verdad."""
    monkeypatch.setattr(library_ops.wit_wrapper, "is_available", lambda _b: True)
    monkeypatch.setattr(library_ops.drives, "needs_wbfs_split", lambda _p: False)
    monkeypatch.setattr(library_ops.drives, "is_fat_filesystem", lambda _p: True)

    def convert(src, dest, target_format, binary, bytes_progress_cb=None,
                cancel=None, **_kw):
        Path(dest).write_bytes(os.urandom(tamano))
        if bytes_progress_cb is not None:
            bytes_progress_cb(tamano)
        return subprocess.CompletedProcess(args=[], returncode=0,
                                           stdout="", stderr="")

    monkeypatch.setattr(library_ops.wit_wrapper, "convert", convert)


def _juego_wii(make_game, tamano):
    juego = make_game(name="juego.iso", game_id="RMCP01",
                      title="Mario Kart Wii", fmt="ISO", contenido=b"x")
    juego.size_bytes = tamano
    return juego


def test_la_barra_nunca_muestra_mas_de_lo_que_llego_a_la_unidad(
        make_game, tmp_path, unidad_medida, monkeypatch):
    """Copia de GameCube a la unidad lenta: en cada aviso a la interfaz,
    lo que se muestra como hecho no pasa de lo que el disco recibió."""
    juego = make_game(name="tp.iso", game_id="GZ2E01",
                      title="Twilight Princess", console="gc",
                      contenido=os.urandom(TAMANO // 2))
    destino = tmp_path / "usb"
    destino.mkdir()
    avisos = []
    cola = _cola_que_anota(avisos)

    vistos = []
    real = TransferQueue._report_progress

    def espia(self, job, escritos, ahora, medidor=None, **kw):
        real(self, job, escritos, ahora, medidor, **kw)
        vistos.append((job.bytes_done, medidor.written()))
    monkeypatch.setattr(TransferQueue, "_report_progress", espia)

    job = cola.add_jobs([juego], destino)[0]
    assert _esperar(lambda: job.is_final, 15)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg
    assert vistos, "no hubo avisos de progreso"
    for mostrado, en_unidad in vistos:
        assert mostrado <= en_unidad
    # Y la velocidad es la de la unidad (16 MiB/s), no la de la RAM.
    velocidades = [t for *_x, t, _s in avisos if t and "MB/s" in t]
    assert velocidades
    mbs = float(velocidades[-1].split()[0].replace(",", "."))
    assert mbs < 40, velocidades[-1]


def test_otro_programa_escribiendo_en_la_unidad_no_infla_la_barra(
        make_game, tmp_path, unidad_medida, monkeypatch):
    """El contador es del disco entero: si otro programa escribe 1 GB en
    la misma unidad, eso no es avance de esta copia. Lo que escribió la
    app es el tope."""
    def para(_path, **_kw):
        return MedidorDeLaUnidad(unidad_medida, extra=10 ** 9)
    monkeypatch.setattr(drives.DeviceWriteMeter, "for_path", staticmethod(para))

    juego = make_game(name="tp.iso", game_id="GZ2E01", title="TP",
                      console="gc", contenido=os.urandom(TAMANO // 4))
    destino = tmp_path / "usb"
    destino.mkdir()
    avisos = []
    cola = _cola_que_anota(avisos)
    job = cola.add_jobs([juego], destino)[0]
    assert _esperar(lambda: job.is_final, 15)
    cola.shutdown(wait=5)
    assert all(hecho <= TAMANO // 4 for _t, _p, hecho, _s, _e in avisos)


def test_wii_no_termina_hasta_que_la_unidad_tiene_todo(
        make_game, tmp_path, unidad_medida, monkeypatch):
    """`wit` termina en cuanto los datos llegan a la caché. Antes, la
    tarea quedaba "Completado" en ese momento; ahora espera a la unidad,
    mostrando "Escribiendo en la unidad" con un progreso que avanza."""
    _wit_que_escribe_en_cache(monkeypatch, TAMANO // 2)
    juego = _juego_wii(make_game, TAMANO // 2)
    destino = tmp_path / "usb"
    destino.mkdir()
    avisos = []
    cola = _cola_que_anota(avisos)
    job = cola.add_jobs([juego], destino)[0]
    assert _esperar(lambda: job.is_final, 15)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg
    copiado = transfer_plan.wbfs_dest_path(juego, destino)
    st = copiado.stat()
    # Todo en la unidad antes de darlo por copiado.
    assert unidad_medida.bajado[(st.st_dev, st.st_ino)] == TAMANO // 2
    escribiendo = [(p, t) for _t, p, _b, t, _e in avisos
                   if t and "Escribiendo en la unidad" in t]
    assert escribiendo, [a[3] for a in avisos]
    progresos = [p for p, _t in escribiendo]
    assert progresos == sorted(progresos) and progresos[-1] > progresos[0]


def test_cancelar_mientras_se_escribe_en_la_unidad_deja_todo_como_estaba(
        make_game, tmp_path, unidad_medida, monkeypatch):
    """Cancelar en la fase "Escribiendo en la unidad" (después de `wit`):
    la tarea queda Cancelada, el juego nuevo se borra y el que ya había en
    la unidad vuelve a su lugar (`DestinationGuard`)."""
    _wit_que_escribe_en_cache(monkeypatch, TAMANO)
    juego = _juego_wii(make_game, TAMANO)
    destino = tmp_path / "usb"
    viejo = transfer_plan.wbfs_dest_path(juego, destino)
    viejo.parent.mkdir(parents=True)
    viejo.write_bytes(b"el juego que el cliente ya tenia")

    avisos = []
    cola = _cola_que_anota(avisos)
    job = cola.add_jobs([juego], destino, overwrite=True)[0]
    assert _esperar(lambda: any(a[3] and "Escribiendo en la unidad" in a[3]
                                for a in avisos), 10)
    t0 = time.monotonic()
    cola.cancel_all()
    assert _esperar(lambda: job.is_final, 10)
    tardo = time.monotonic() - t0
    cola.shutdown(wait=5)

    assert job.status is JobStatus.CANCELLED, (job.status, job.error_msg)
    assert tardo < 1.5, f"tardó {tardo:.1f}s"
    assert viejo.read_bytes() == b"el juego que el cliente ya tenia"
    assert _restos(destino) == [viejo]


def test_sin_medidor_la_barra_sigue_lo_escrito_como_antes(
        make_game, tmp_path, monkeypatch):
    """Un destino que no es un dispositivo de bloque (una carpeta en un
    filesystem de red, un tmpfs): sin medidor, todo como antes."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)
    monkeypatch.setattr(drives.DeviceWriteMeter, "for_path",
                        staticmethod(lambda _p, **_k: None))
    contenido = os.urandom(MIB)
    juego = make_game(name="tp.iso", game_id="GZ2E01", title="TP",
                      console="gc", contenido=contenido)
    destino = tmp_path / "usb"
    destino.mkdir()
    cola = TransferQueue(OperationManager(), dispatch=sync_dispatch)
    job = cola.add_jobs([juego], destino)[0]
    assert _esperar(lambda: job.is_final, 10)
    cola.shutdown(wait=5)
    assert job.status is JobStatus.DONE
    assert transfer_plan.gc_dest_path(juego, destino).read_bytes() == contenido


def test_enviar_desde_la_biblioteca_tambien_espera_a_la_unidad(
        make_game, tmp_path, monkeypatch):
    """`send_to_wbfs_drive` lo usa también el envío desde la Biblioteca:
    vuelve con lo de `wit` ya bajado, aunque nadie mire el progreso."""
    unidad = UnidadLenta(VELOCIDAD * 8)
    monkeypatch.setattr(os, "fsync", unidad.fsync)
    monkeypatch.setattr(fsutil, "sync_range", unidad.sync_range)
    _wit_que_escribe_en_cache(monkeypatch, 4 * MIB)
    juego = _juego_wii(make_game, 4 * MIB)
    dest = library_ops.send_to_wbfs_drive(juego, tmp_path / "usb")
    st = dest.stat()
    assert unidad.bajado[(st.st_dev, st.st_ino)] == 4 * MIB


def test_sin_cancelar_el_flush_de_wit_no_falla_con_token(tmp_path, monkeypatch):
    """`flush_and_drop_cache` con un token que nadie cancela baja todo y
    no levanta nada."""
    from wiibackup_manager import fileops
    archivo = tmp_path / "a.wbfs"
    archivo.write_bytes(os.urandom(MIB))
    pulsos = []
    fileops.flush_and_drop_cache([archivo], cancel=wit_wrapper.CancellationToken(),
                                 on_progress=lambda: pulsos.append(1))
    assert pulsos
