"""Cerrar la app a mitad de un reemplazo devuelve el original a su lugar.

Lo medido en la Kingston: con "Cancelar operación y cerrar" a mitad de
un reemplazo de Brawl (RSBE01), `wit` moría en ~2 s pero el proceso de la
app terminaba antes de que el hilo de la copia devolviera el original:
quedaba como `.RSBE01.wbfs.respaldo-<pid>` (y `.wbf1`) con el `.tmp` a
medias. Lo que se fija acá:

- el guard devuelve el original ANTES de borrar el temporal (al revés,
  el original seguía oculto mientras se borraban varios GB);
- si el original no puede volver (USB desconectada), queda constancia en
  el historial en el momento, no cuando el hilo termine de cerrarse;
- dos `restore()` a la vez (el hilo de la copia y cualquier otro) no se
  pisan: cada par lo toma uno solo;
- el cierre espera a que lo cancelado termine, nunca más de 3 s, y sin
  trabar la ventana.
"""
from __future__ import annotations

import errno
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from wiibackup_manager import (atomicfs, library_ops, operations, oplog,
                               transfer_plan, wit_wrapper)
from wiibackup_manager.game_model import Game
from wiibackup_manager.operations import OperationKind, OperationManager
from wiibackup_manager.queue_manager import TransferQueue

ORIGINAL = {"RSBE01.wbfs": b"ORIGINAL parte 0", "RSBE01.wbf1": b"ORIGINAL parte 1"}


@pytest.fixture
def carpeta(tmp_path):
    c = tmp_path / "WII_USB" / "wbfs" / "RSBE01"
    c.mkdir(parents=True)
    for nombre, datos in ORIGINAL.items():
        (c / nombre).write_bytes(datos)
    return c


@pytest.fixture
def brawl(tmp_path):
    src = tmp_path / "brawl.wbfs"
    src.write_bytes(b"x" * 64)
    return Game(path=src, game_id="RSBE01", title="Super Smash Bros. Brawl",
                fmt="WBFS", size_bytes=8_000_000_000, identified_by="wbfs",
                console="wii")


def _contenido(c: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(c.iterdir())}


def _wit_cancelado(monkeypatch, vistos: dict):
    """`wit COPY` de mentira que deja su temporal a medias y se cancela,
    como el de verdad cuando le llega el SIGKILL."""
    monkeypatch.setattr(library_ops.wit_wrapper, "is_available", lambda _b: True)
    monkeypatch.setattr(library_ops.drives, "needs_wbfs_split", lambda _p: True)
    monkeypatch.setattr(library_ops.drives, "is_fat_filesystem", lambda _p: True)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)

    def convert(src, dest, fmt, binary, **kw):
        vistos.update(kw)
        dest = Path(dest)
        (dest.parent / f".{dest.name}.LtnLTy.tmp").write_bytes(b"juego nuevo a medias")
        raise wit_wrapper.OperationCancelled("cancelado")

    monkeypatch.setattr(library_ops.wit_wrapper, "convert", convert)


# --------------------------------------- Restaurar antes de borrar el .tmp --
def test_el_original_vuelve_antes_de_borrar_el_temporal(carpeta, brawl, monkeypatch):
    vistos: dict = {}
    _wit_cancelado(monkeypatch, vistos)
    eventos = []

    real_replace = atomicfs.os.replace

    def replace_anotado(origen, destino):
        if "respaldo" in Path(origen).name:
            eventos.append(("restaura", Path(destino).name))
        return real_replace(origen, destino)

    real_cleanup = wit_wrapper.cleanup_new_output_files

    def cleanup_anotado(dest, before):
        # Lo que importa: cuando se barre, el original YA está en su lugar.
        eventos.append(("barre", sorted(p.name for p in carpeta.iterdir()
                                        if not p.name.startswith("."))))
        return real_cleanup(dest, before)

    monkeypatch.setattr(atomicfs.os, "replace", replace_anotado)
    monkeypatch.setattr(library_ops.wit_wrapper, "cleanup_new_output_files",
                        cleanup_anotado)

    with pytest.raises(wit_wrapper.OperationCancelled):
        library_ops.send_to_wbfs_drive(brawl, carpeta.parent.parent, "wit",
                                       overwrite=True)

    # `wit_wrapper` no barre por su cuenta: lo hace el guard, después.
    assert vistos["cleanup_on_abort"] is False
    nombres = [e[0] for e in eventos]
    assert nombres == ["restaura", "restaura", "barre"]
    assert eventos[-1] == ("barre", ["RSBE01.wbf1", "RSBE01.wbfs"])
    assert _contenido(carpeta) == ORIGINAL


def test_un_nombre_final_que_wit_llego_a_escribir_no_sobrevive(carpeta):
    """`wit` alcanzó a renombrar la primera parte al nombre final antes de
    morir: `os.replace` la pisa con el original, y lo que no es original
    (una parte nueva que el original no tenía) se barre."""
    dest = carpeta / "RSBE01.wbfs"
    with pytest.raises(RuntimeError):
        with library_ops.DestinationGuard(dest):
            dest.write_bytes(b"parcial de wit")
            (carpeta / "RSBE01.wbf2").write_bytes(b"parte nueva")
            (carpeta / ".RSBE01.wbfs.LtnLTy.tmp.1").write_bytes(b"temporal")
            raise RuntimeError("cortado")
    assert _contenido(carpeta) == ORIGINAL


def test_cancelado_sin_limpiar_deja_el_temporal_para_quien_llama(tmp_path):
    """`cleanup_on_abort=False`: `_run_with_progress` no borra lo que
    quedó a medias; lo hace el guard, después de restaurar."""
    dest = tmp_path / "RSBE01.wbfs"
    temporal = tmp_path / ".RSBE01.wbfs.ab12Cd.tmp"
    guion = (f"open({str(temporal)!r}, 'wb').write(b'x' * 1000)\n"
             "import time; time.sleep(60)\n")
    token = wit_wrapper.CancellationToken()
    threading.Timer(0.5, token.cancel).start()
    resultado = wit_wrapper._run_with_progress(
        [sys.executable, "-c", guion], dest, lambda _n: None, token,
        cleanup_on_abort=False)
    assert resultado.returncode == 1
    assert temporal.exists()


# ------------------------------------------ Restaurar falla: al historial --
def test_si_el_original_no_vuelve_queda_en_el_historial(carpeta, monkeypatch, tmp_path):
    log = oplog.OperationLog(tmp_path / "historial.json")
    dest = carpeta / "RSBE01.wbfs"
    real_replace = atomicfs.os.replace

    def replace_que_falla_al_devolver_wbf1(origen, destino):
        if Path(destino).name == "RSBE01.wbf1" and "respaldo" in Path(origen).name:
            raise OSError(errno.EIO, "Input/output error")
        return real_replace(origen, destino)

    monkeypatch.setattr(atomicfs.os, "replace", replace_que_falla_al_devolver_wbf1)
    temporal = carpeta / ".RSBE01.wbfs.LtnLTy.tmp"
    cancelado = wit_wrapper.OperationCancelled("cancelado")

    with pytest.raises(library_ops.RollbackFailedError) as info:
        with library_ops.DestinationGuard(dest, op_log=log):
            temporal.write_bytes(b"a medias")
            raise cancelado

    error = info.value
    assert error.original_error is cancelado
    [(original, respaldo)] = error.pending
    assert original.name == "RSBE01.wbf1"
    # El respaldo solo se tocó para intentar devolverlo: sigue intacto.
    assert respaldo.read_bytes() == ORIGINAL["RSBE01.wbf1"]
    # La otra parte sí volvió, y el temporal se barrió igual.
    assert (carpeta / "RSBE01.wbfs").read_bytes() == ORIGINAL["RSBE01.wbfs"]
    assert not temporal.exists()

    [entrada] = log.entries()
    assert entrada.operation == oplog.UNRESTORED_BACKUP_OPERATION
    assert entrada.status == oplog.STATUS_ERROR
    assert respaldo.name in entrada.detail
    assert entrada.target == str(dest)


def test_sin_historial_el_fallo_se_propaga_igual(carpeta, monkeypatch):
    monkeypatch.setattr(atomicfs.os, "replace",
                        lambda o, d: (_ for _ in ()).throw(OSError(errno.EIO, "x"))
                        if "respaldo" in Path(o).name else os.rename(o, d))
    with pytest.raises(library_ops.RollbackFailedError):
        with library_ops.DestinationGuard(carpeta / "RSBE01.wbfs"):
            raise RuntimeError("cortado")


# -------------------------------------- Dos restore() a la vez: uno solo --
def test_dos_restore_a_la_vez_no_se_pisan(tmp_path, monkeypatch):
    originales = []
    aside = atomicfs.SetAside("respaldo")
    for nombre in ORIGINAL:
        f = tmp_path / nombre
        f.write_bytes(ORIGINAL[nombre])
        aside.move_aside(f)
        originales.append(f)

    llamadas = []
    real_replace = atomicfs.os.replace

    def replace_lento(origen, destino):
        llamadas.append(Path(destino).name)
        time.sleep(0.05)    # que el otro hilo llegue mientras tanto
        return real_replace(origen, destino)

    monkeypatch.setattr(atomicfs.os, "replace", replace_lento)
    salida = {}
    largada = threading.Barrier(2)

    def restaurar(quien):
        largada.wait()
        salida[quien] = aside.restore()

    hilos = [threading.Thread(target=restaurar, args=(q,)) for q in ("cola", "cierre")]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(5)

    assert salida == {"cola": [], "cierre": []}, "uno de los dos reportó un fallo falso"
    assert sorted(llamadas) == sorted(ORIGINAL), "algún par se intentó dos veces"
    assert sorted(aside.restored) == sorted(originales)
    for f in originales:
        assert f.read_bytes() == ORIGINAL[f.name]
    assert not [p for p in tmp_path.iterdir() if "respaldo" in p.name]


def test_dos_restore_del_guard_a_la_vez_no_levantan_rollback(carpeta, monkeypatch):
    """El mismo caso por la puerta que usa la app: sin el reparto, el
    segundo `_restore` no encontraba el respaldo y levantaba
    `RollbackFailedError` por un original que estaba en su lugar."""
    guard = library_ops.DestinationGuard(carpeta / "RSBE01.wbfs").__enter__()
    real_replace = atomicfs.os.replace
    monkeypatch.setattr(atomicfs.os, "replace",
                        lambda o, d: (time.sleep(0.05), real_replace(o, d))[1])
    errores = []
    largada = threading.Barrier(2)

    def restaurar():
        largada.wait()
        try:
            guard._restore()
        except library_ops.RollbackFailedError as e:
            errores.append(e)

    hilos = [threading.Thread(target=restaurar) for _ in range(2)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(5)
    assert errores == []
    assert _contenido(carpeta) == ORIGINAL


# --------------------------------------------- El cierre: tope de 3 s --
def _temporizador_real(ms, funcion):
    """`GLib.timeout_add` de mentira, con el reloj de verdad."""
    while funcion():
        time.sleep(ms / 1000)


def test_el_cierre_respeta_el_tope_con_un_wit_que_no_muere(brawl, carpeta, monkeypatch):
    """Una copia cuyo `wit` no termina de morir (en estado D con la USB
    desconectada): la cola cancela, la operación no termina nunca, y el
    cierre igual llega a los 3 s."""
    soltar = threading.Event()
    arranco = threading.Event()

    def send_que_no_vuelve(*_a, **_kw):
        arranco.set()
        soltar.wait(30)     # ni SIGKILL lo saca: está en el kernel
        raise wit_wrapper.OperationCancelled("cancelado")

    monkeypatch.setattr(library_ops, "send_to_wbfs_drive", send_que_no_vuelve)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)
    ops = OperationManager()
    cola = TransferQueue(ops, dispatch=lambda f, *a: f(*a))
    cola.add_jobs([brawl], carpeta.parent.parent, overwrite=True)
    assert arranco.wait(5)
    try:
        cola.shutdown()
        cerrado = []
        t0 = time.monotonic()
        operations.close_when_settled(ops, lambda: cerrado.append(time.monotonic() - t0),
                                      _temporizador_real)
        assert cerrado, "no cerró nunca"
        assert 2.9 <= cerrado[0] <= 3.3, f"cerró a los {cerrado[0]:.2f} s"
    finally:
        soltar.set()


def test_el_cierre_no_espera_de_mas_si_lo_cancelado_termina_antes():
    ops = OperationManager()
    op = ops.start(OperationKind.TRANSFERRING, resources=["/run/media/usb"])
    threading.Timer(0.4, ops.finish, args=(op,)).start()
    cerrado = []
    t0 = time.monotonic()
    operations.close_when_settled(ops, lambda: cerrado.append(time.monotonic() - t0),
                                  _temporizador_real)
    assert 0.35 <= cerrado[0] <= 0.8


def test_el_cierre_no_deja_a_wit_vivo_ni_detenido(tmp_path):
    """Lo que cancela el cierre es el SIGKILL de siempre: aunque el "wit"
    esté detenido por el limitador en ese momento, muere, y nunca queda
    detenido esperando."""
    dest = tmp_path / "RSBE01.wbfs"
    guion = ("import os, signal, sys, time\n"
             "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
             f"fd = os.open({str(tmp_path / '.RSBE01.wbfs.ab12Cd.tmp')!r}, "
             "os.O_WRONLY | os.O_CREAT)\n"
             "while True:\n    os.write(fd, b'x' * 65536); time.sleep(0.001)\n")
    token = wit_wrapper.CancellationToken()
    pids = []
    original = wit_wrapper._WritebackLimiter

    class _Anotado(original):
        def __init__(self, proc, *a, **kw):
            super().__init__(proc, *a, **kw)
            pids.append(proc.pid)
            self._pause()   # detenido justo cuando llega el cierre

    resultado = {}
    hilo = threading.Thread(target=lambda: resultado.setdefault(
        "r", wit_wrapper._run_with_progress(
            [sys.executable, "-c", guion], dest, lambda _n: None, token)))
    import unittest.mock as mock
    with mock.patch.object(wit_wrapper, "_WritebackLimiter", _Anotado):
        hilo.start()
        time.sleep(0.5)
        t0 = time.monotonic()
        token.cancel()
        hilo.join(5)
    assert not hilo.is_alive()
    assert time.monotonic() - t0 < 1.0
    assert resultado["r"].returncode == 1
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)

