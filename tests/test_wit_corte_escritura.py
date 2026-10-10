"""Cortar un `wit` que ESCRIBE por timeout o por una excepción: SIGKILL en
el acto, como al cancelar.

Medido con el `wit` real (v3.05a) convirtiendo un GameCube a ISO: con UN
SIGTERM a mitad de la copia imprime "PROGRAM WILL TERMINATE AFTER CURRENT
JOB HAS FINISHED", termina la copia, renombra el temporal al nombre final
y recién ahí sale (código 110). En la Kingston, con un solo SIGTERM, siguió
escribiendo minutos. `_terminate_process_group` le daba 5 s de gracia con
SIGTERM: 5 s más escribiendo en la unidad -y sin el limitador de caché,
que ya se había parado- después de que la app decidió cortar.

Las operaciones que solo LEEN (VERIFY, LIST, ISOSIZE) siguen con SIGTERM y
gracia: ahí no hay nada que escribir y terminar prolijo no cuesta nada.
"""
from __future__ import annotations

import sys
import time

from wiibackup_manager import wit_wrapper

# "wit" que, como el real, ante SIGTERM termina el trabajo en curso (acá
# tarda 2 s), renombra el temporal al nombre final y sale con 110. Anota en
# `despues` cuánto escribió después de la señal.
_WIT_QUE_TERMINA_EL_TRABAJO = r"""
import os, signal, sys, time
tmp, final, despues = sys.argv[1], sys.argv[2], sys.argv[3]
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT, 0o644)
pedido = {"terminar": False}
signal.signal(signal.SIGTERM, lambda *_: pedido.update(terminar=True))
while not pedido["terminar"]:
    os.write(fd, b"x" * 4096)
    time.sleep(0.01)
escrito = 0
fin = time.monotonic() + 2
while time.monotonic() < fin:
    escrito += os.write(fd, b"y" * 65536)
    time.sleep(0.01)
os.close(fd)
open(despues, "w").write(str(escrito))
os.replace(tmp, final)
sys.exit(110)
"""


def _correr(tmp_path, **kw):
    dest = tmp_path / "GZ2E01.iso"
    despues = tmp_path / "escrito_despues_del_corte.txt"
    args = [sys.executable, "-c", _WIT_QUE_TERMINA_EL_TRABAJO,
            str(tmp_path / ".GZ2E01.iso.AbCdEf.tmp"), str(dest), str(despues)]
    t0 = time.monotonic()
    try:
        resultado = wit_wrapper._run_with_progress(args, dest, kw.pop("cb", lambda _n: None),
                                                   **kw)
    except RuntimeError:
        resultado = None
    return resultado, time.monotonic() - t0, despues, dest


def test_el_timeout_de_una_copia_no_deja_a_wit_terminar_el_trabajo(tmp_path):
    resultado, duracion, despues, dest = _correr(
        tmp_path, inactivity_timeout=None, absolute_timeout=0.5)

    assert resultado is not None and resultado.returncode == 1
    assert not despues.exists(), (
        f"wit siguió escribiendo {despues.read_text()} bytes después del corte")
    assert duracion < 2.0
    assert not dest.exists()


def test_una_excepcion_durante_la_copia_tampoco(tmp_path):
    def cb_que_explota(_n):
        raise RuntimeError("falla en el callback")

    resultado, duracion, despues, dest = _correr(tmp_path, cb=cb_que_explota)

    assert resultado is None
    assert not despues.exists(), (
        f"wit siguió escribiendo {despues.read_text()} bytes después del corte")
    assert duracion < 2.5
    assert not dest.exists()


# ------------------------------------------- Solo lectura: sin cambios --
def test_una_operacion_de_solo_lectura_sigue_terminando_con_sigterm(tmp_path):
    """VERIFY/LIST no escriben: al vencer su timeout se les sigue pidiendo
    que terminen por las buenas antes del SIGKILL."""
    marca = tmp_path / "recibio_sigterm"
    guion = ("import signal, sys, time\n"
             f"signal.signal(signal.SIGTERM, lambda *_: (open({str(marca)!r}, 'w').close(), sys.exit(0)))\n"
             "time.sleep(60)\n")
    resultado = wit_wrapper._run_cancellable(sys.executable, "-c", guion, timeout=0.5)
    assert resultado.returncode == wit_wrapper.TIMEOUT_RETURNCODE
    assert marca.exists()


def test_la_copia_no_usa_la_terminacion_por_las_buenas(monkeypatch, tmp_path):
    """Guarda directa: el camino que escribe no pasa por SIGTERM."""
    llamadas = []
    monkeypatch.setattr(wit_wrapper, "_terminate_process_group",
                        lambda proc: llamadas.append(proc) or proc.kill())
    _correr(tmp_path, inactivity_timeout=None, absolute_timeout=0.5)
    assert llamadas == []
