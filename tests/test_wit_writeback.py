"""Cancelar una copia de `wit` corta en el acto: caché sin bajar acotada
(`_WritebackLimiter`), SIGKILL inmediato y `wit` atado a la vida de
quien lo lanza (PDEATHSIG).

Sin hardware: el "wit" es un proceso que escribe sin parar en un
temporal con el nombre que usa `wit` (e ignora SIGTERM, como `wit`, que
lo toma como "terminá la copia"), y la "unidad lenta" es un
`sync_range` falso que tarda lo que tardaría una USB de ~5 MB/s en bajar
lo que el archivo creció desde la última vez.

Lo que el kernel hace de verdad con una USB -no dejar morir a un proceso
hasta bajar lo que tiene en caché- no se puede simular acá; por eso el
tiempo de cancelar se calcula como lo que tarda en volver más lo que
quedaba pendiente a la velocidad de la unidad, que es exactamente lo que
se midió en la Kingston (0.9 s con 9.6 MB pendientes a ~14 MB/s).
"""
from __future__ import annotations

import errno
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from wiibackup_manager import fsutil, wit_wrapper

MB = 1_000_000
MiB = 1024 * 1024

# El "wit" simulado escribe a ~1 GB/s: más rápido que el `wit` real
# llenando la caché de una USB FAT32 (medido en la Kingston: 1.95 GB en
# 3.3 s, ~600 MB/s), y bastante más lento que escribir a lo bruto en un
# tmpfs (~4 GB/s), que no le pasa a ninguna copia de verdad. El ritmo es
# por cada escritura, sin "recuperar" el tiempo que pasó detenido: `wit`
# tampoco lo hace.
_ESCRITOR = (
    "import os, signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "fd = os.open(sys.argv[1], os.O_WRONLY | os.O_CREAT, 0o644)\n"
    "buf = b'x' * 262144\n"
    "n = 0\n"
    "while n < int(sys.argv[2]):\n"
    "    t = time.monotonic()\n"
    "    n += os.write(fd, buf)\n"
    "    resto = len(buf) / 1e9 - (time.monotonic() - t)\n"
    "    if resto > 0:\n"
    "        time.sleep(resto)\n"
    "time.sleep(60)\n"
)


def _lanzar_escritor(temporal: Path, total: int = 300 * MB) -> subprocess.Popen:
    return wit_wrapper._popen_wit(
        [sys.executable, "-c", _ESCRITOR, str(temporal), str(total)])


def _estado(pid: int) -> str:
    """'R', 'S', 'T' (detenido), 'Z'... o '' si ya no existe."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return ""


def _matar(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait(timeout=5)


class _UnidadLenta:
    """`sync_range` falso: tarda lo que tardaría una unidad de `rate`
    bytes/s en bajar lo que el archivo creció desde el último llamado.
    Anota qué archivos le pidieron bajar."""

    def __init__(self, rate: float):
        self.rate = rate
        self.bajado: dict[int, int] = {}
        self.rutas: set[str] = set()
        self.llamadas = 0

    def __call__(self, fd, offset, nbytes, flags):
        self.llamadas += 1
        self.rutas.add(os.readlink(f"/proc/self/fd/{fd}"))
        tam = os.fstat(fd).st_size
        nuevo = tam - self.bajado.get(fd, 0)
        self.bajado[fd] = tam
        if nuevo > 0:
            time.sleep(nuevo / self.rate)


class _Proc:
    pid = os.getpid()
    returncode = None


# ------------------------------------------------- Ventana adaptativa --
def test_ventana_es_un_segundo_de_copia_con_piso_y_techo(tmp_path):
    lim = wit_wrapper._WritebackLimiter(_Proc(), tmp_path / "X.wbfs", set())
    assert lim.window == wit_wrapper.WRITEBACK_MIN_WINDOW == 4 * MiB
    lim.rate = 5 * MB
    assert lim.window == 5 * MB
    lim.rate = 14 * MB
    assert lim.window == 14 * MB
    lim.rate = 1 * MB           # unidad lentísima: el piso
    assert lim.window == 4 * MiB
    lim.rate = 2_000 * MB       # disco rápido: el techo
    assert lim.window == wit_wrapper.WRITEBACK_MAX_WINDOW


def test_usb_lenta_simulada_5_mbps_acota_lo_pendiente(tmp_path):
    """Con una unidad de 5 MB/s la ventana se ajusta a ~1 s de copia y lo
    que queda sin bajar nunca pasa de lo que se baja en menos de 2 s."""
    dest = tmp_path / "RSBE01.wbfs"
    unidad = _UnidadLenta(5 * MB)
    proc = _lanzar_escritor(tmp_path / ".RSBE01.wbfs.ab12Cd.tmp")
    lim = wit_wrapper._WritebackLimiter(proc, dest, set(), sync=unidad)
    try:
        lim.start()
        time.sleep(4)
        assert lim.rate is not None
        assert 2.5 * MB <= lim.rate <= 5.5 * MB
        assert 4 * MiB <= lim.window <= 6 * MB
        assert lim.max_pending / unidad.rate < 2.0, (
            f"quedaron {lim.max_pending / MB:.1f} MB sin bajar")
        # Avanzó: la unidad simulada fue bajando de a ventanas.
        assert lim.flushed >= 10 * MB
    finally:
        lim.stop()
        _matar(proc)


# --------------------------------------------------- Pausa y reanuda --
def test_detiene_a_wit_cuando_se_adelanta_y_lo_reanuda(tmp_path):
    dest = tmp_path / "RSBE01.wbfs"
    proc = _lanzar_escritor(tmp_path / ".RSBE01.wbfs.ab12Cd.tmp")
    lim = wit_wrapper._WritebackLimiter(proc, dest, set(), sync=_UnidadLenta(8 * MB))
    estados = []
    try:
        lim.start()
        fin = time.monotonic() + 3
        while time.monotonic() < fin:
            estados.append(_estado(proc.pid))
            time.sleep(0.005)
        detenido = estados.index("T")
        assert any(e != "T" for e in estados[detenido:]), "nunca lo reanudó"
        assert lim.pauses >= 2
    finally:
        lim.stop()
        try:
            assert _estado(proc.pid) != "T"
        finally:
            _matar(proc)


def test_stop_reanuda_a_un_wit_detenido(tmp_path):
    dest = tmp_path / "RSBE01.wbfs"
    proc = _lanzar_escritor(tmp_path / ".RSBE01.wbfs.ab12Cd.tmp")
    # Unidad lentísima: en cuanto se pasa de la ventana queda detenido.
    lim = wit_wrapper._WritebackLimiter(proc, dest, set(), sync=_UnidadLenta(0.2 * MB))
    try:
        lim.start()
        fin = time.monotonic() + 3
        while _estado(proc.pid) != "T" and time.monotonic() < fin:
            time.sleep(0.005)
        assert _estado(proc.pid) == "T"
        lim.stop(timeout=0.1)
        time.sleep(0.05)
        assert _estado(proc.pid) not in ("T", "")
    finally:
        _matar(proc)


def test_si_la_unidad_falla_wit_no_queda_detenido(tmp_path):
    """Un error de E/S al bajar (USB desenchufada) apaga el limitador y
    reanuda a `wit`: el error lo va a encontrar `wit` por su cuenta."""
    dest = tmp_path / "RSBE01.wbfs"
    proc = _lanzar_escritor(tmp_path / ".RSBE01.wbfs.ab12Cd.tmp")

    def unidad_que_no_responde(fd, offset, nbytes, flags):
        raise OSError(errno.EIO, "Input/output error")

    lim = wit_wrapper._WritebackLimiter(proc, dest, set(), sync=unidad_que_no_responde)
    try:
        lim.start()
        fin = time.monotonic() + 5
        while any(h.is_alive() for h in lim._threads) and time.monotonic() < fin:
            time.sleep(0.01)
        assert not any(h.is_alive() for h in lim._threads)
        assert not lim.paused
        time.sleep(0.05)
        assert _estado(proc.pid) not in ("T", "")
    finally:
        lim.stop()
        _matar(proc)


# ------------------------------------------- Solo los temporales nuevos --
def test_reconoce_solo_la_forma_de_los_temporales_de_wit(tmp_path):
    dest = tmp_path / "RSBE01.wbfs"
    es = lambda nombre: wit_wrapper._is_wit_temp(tmp_path / nombre, dest)  # noqa: E731
    assert es(".RSBE01.wbfs.9zxK9A.tmp")
    assert es(".RSBE01.wbfs.9zxK9A.tmp.1")
    assert not es(".RSBE01.wbfs.respaldo-4821")
    assert not es(".RSBE01.wbfs.respaldo-4821.tmp.x")
    assert not es("RSBE01.wbfs")
    assert not es(".RSBE02.wbfs.9zxK9A.tmp")


def test_nunca_toca_el_respaldo_ni_los_temporales_de_antes(tmp_path):
    dest = tmp_path / "RSBE01.wbfs"
    respaldo = tmp_path / ".RSBE01.wbfs.respaldo-4821"
    otro_respaldo = tmp_path / ".RSBE01.wbfs.respaldo-999"
    viejo = tmp_path / ".RSBE01.wbfs.viejo1.tmp"
    for f in (respaldo, otro_respaldo, viejo):
        f.write_bytes(b"juego del cliente" * 1000)
    protegidos = wit_wrapper.output_files(dest) | {respaldo}
    unidad = _UnidadLenta(20 * MB)
    nuevo = tmp_path / ".RSBE01.wbfs.nuevo1.tmp"
    proc = _lanzar_escritor(nuevo, total=60 * MB)
    lim = wit_wrapper._WritebackLimiter(proc, dest, protegidos, sync=unidad)
    try:
        lim.start()
        time.sleep(1.5)
    finally:
        lim.stop()
        _matar(proc)
    assert unidad.rutas == {str(nuevo)}
    for f in (respaldo, otro_respaldo, viejo):
        assert f.read_bytes() == b"juego del cliente" * 1000


# ------------------------------------------------- Cancelar en < 2 s --
@pytest.fixture
def usb_5mbps(monkeypatch):
    """La unidad simulada para todo `_run_with_progress`, y el limitador
    que se use, para poder mirar lo que quedó pendiente."""
    unidad = _UnidadLenta(5 * MB)
    monkeypatch.setattr(fsutil, "sync_range", unidad)
    creados = []
    original = wit_wrapper._WritebackLimiter

    class _Anotado(original):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            creados.append(self)

    monkeypatch.setattr(wit_wrapper, "_WritebackLimiter", _Anotado)
    return unidad, creados


def test_cancelar_tarda_menos_de_2_s_con_un_wit_simulado(tmp_path, usb_5mbps):
    """Desde el botón hasta que `wit` no existe y lo que dejó en caché
    está bajado (o descartado): menos de 2 s, en una USB de 5 MB/s."""
    unidad, creados = usb_5mbps
    dest = tmp_path / "RSBE01.wbfs"
    temporal = tmp_path / ".RSBE01.wbfs.ab12Cd.tmp"
    token = wit_wrapper.CancellationToken()
    resultado = {}

    def correr():
        resultado["r"] = wit_wrapper._run_with_progress(
            [sys.executable, "-c", _ESCRITOR, str(temporal), str(300 * MB)],
            dest, lambda _n: None, token)

    hilo = threading.Thread(target=correr)
    hilo.start()
    time.sleep(3)
    lim = creados[0]
    pendiente = lim.written() - lim.flushed
    t0 = time.monotonic()
    token.cancel()
    vuelve = time.monotonic() - t0
    hilo.join(timeout=10)
    muerto = time.monotonic() - t0
    assert not hilo.is_alive()
    assert vuelve < 0.1, "el botón Cancelar no puede trabar la ventana"
    total = muerto + pendiente / unidad.rate
    assert total < 2.0, (f"cancelar tardaría {total:.2f} s "
                         f"({pendiente / MB:.1f} MB pendientes)")
    assert resultado["r"].returncode == 1
    assert not temporal.exists()
    assert _estado(lim.proc.pid) == ""


def test_una_excepcion_no_deja_a_wit_detenido_ni_vivo(tmp_path, usb_5mbps):
    _unidad, creados = usb_5mbps
    dest = tmp_path / "RSBE01.wbfs"
    temporal = tmp_path / ".RSBE01.wbfs.ab12Cd.tmp"

    def progreso_que_explota(_n):
        if creados and creados[0].pauses:
            raise RuntimeError("falla en el callback")

    with pytest.raises(RuntimeError):
        wit_wrapper._run_with_progress(
            [sys.executable, "-c", _ESCRITOR, str(temporal), str(300 * MB)],
            dest, progreso_que_explota)
    lim = creados[0]
    assert not lim.paused
    assert lim.proc.returncode is not None
    assert _estado(lim.proc.pid) == ""
    assert not temporal.exists()


# ------------------------------------------------- SIGKILL y PDEATHSIG --
def test_cancelar_manda_sigkill_en_el_acto_sin_otro_hilo(monkeypatch):
    """`wit` toma SIGTERM como "terminá la copia". El SIGKILL sale en el
    mismo llamado: no depende de un hilo que quizás nunca llega a correr
    (la app que se cierra en ese momento)."""
    proc = wit_wrapper._popen_wit([
        sys.executable, "-c",
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n"])
    time.sleep(0.2)
    hilos = []
    monkeypatch.setattr(wit_wrapper.threading, "Thread",
                        lambda *a, **kw: hilos.append(kw) or pytest.fail("lanzó un hilo"))
    t = wit_wrapper.CancellationToken()
    try:
        assert t.attach(proc)
        t.cancel()
        assert proc.wait(timeout=0.5) == -signal.SIGKILL
        assert hilos == []
    finally:
        _matar(proc)


def test_pdeathsig_mata_a_wit_si_termina_el_hilo_que_lo_lanzo():
    lanzado = {}

    def hilo_que_no_espera():
        lanzado["p"] = wit_wrapper._popen_wit(
            [sys.executable, "-c", "import time; time.sleep(60)"])

    h = threading.Thread(target=hilo_que_no_espera)
    h.start()
    h.join()
    proc = lanzado["p"]
    try:
        assert proc.wait(timeout=3) == -signal.SIGKILL
    finally:
        _matar(proc)


def test_pdeathsig_no_mata_a_wit_mientras_el_hilo_lo_espera():
    """El caso real: `_run_with_progress` y `_run_cancellable` se quedan
    esperando a `wit` en el mismo hilo que lo lanzó."""
    lanzado = {}
    soltar = threading.Event()

    def hilo_que_espera():
        lanzado["p"] = wit_wrapper._popen_wit(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        soltar.wait(10)

    h = threading.Thread(target=hilo_que_espera)
    h.start()
    try:
        time.sleep(0.5)
        assert lanzado["p"].poll() is None
    finally:
        soltar.set()
        h.join()
        _matar(lanzado["p"])
