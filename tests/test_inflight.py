"""Pruebas de `inflight.py`, el registry compartido de pedidos en vuelo.

Estas pruebas absorben lo que antes se verificaba cuatro veces por vías
indirectas (hurgando los diccionarios internos de `gametdb` y
`oscwii_client`): aislamiento de callbacks, excepción del trabajo →
resultado de respaldo, deduplicación por clave y conteo de pedidos en
curso. Ahora que esa mecánica vive en un solo lugar, se prueba en un solo
lugar y más a fondo; lo que queda en cada módulo es su cableado propio
(qué es una clave suya, qué resultado vale la pena recordar).

Los tests que involucran concurrencia real usan `threading.Event` y no
`sleep`: bloquean el trabajo hasta que el test decide soltarlo, así el
orden de los eventos es determinista y no depende de la velocidad de la
máquina que corre la suite."""
from __future__ import annotations

import threading
import time

import pytest

from wiibackup_manager.inflight import Claim, InflightRegistry


# ------------------------------------------------------------- utilidades --
@pytest.fixture
def registry():
    reg = InflightRegistry(2, "test-inflight")
    yield reg
    reg.forget()


@pytest.fixture
def registry_con_memoria():
    reg = InflightRegistry(2, "test-inflight-mem", remember_results=True)
    yield reg
    reg.forget()


def _esperar(condicion, timeout: float = 5.0) -> bool:
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.01)
    return bool(condicion())


class TrabajoBloqueado:
    """Un trabajo que avisa cuando arrancó y se queda esperando permiso
    para terminar. Es lo que permite tener DOS pedidos anotados sobre el
    mismo job sin depender de que los hilos corran a cierta velocidad."""

    def __init__(self, resultado="resultado"):
        self.resultado = resultado
        self.arranco = threading.Event()
        self.seguir = threading.Event()
        self.llamadas = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.llamadas += 1
        self.arranco.set()
        assert self.seguir.wait(timeout=5), "el test no liberó el trabajo a tiempo"
        return self.resultado


# ==================================================================== join --
def test_join_el_primero_reclama_la_clave(registry):
    accion, valor = registry.join("k", lambda v: None)
    assert accion is Claim.CLAIMED
    assert valor is None
    assert registry.in_flight() == 1


def test_join_el_segundo_espera(registry):
    registry.join("k", lambda v: None)
    accion, valor = registry.join("k", lambda v: None)
    assert accion is Claim.WAITING
    assert valor is None
    # Sigue siendo UNA sola clave en curso, con dos anotados.
    assert registry.in_flight() == 1


def test_join_claves_distintas_no_se_mezclan(registry):
    assert registry.join("a", lambda v: None)[0] is Claim.CLAIMED
    assert registry.join("b", lambda v: None)[0] is Claim.CLAIMED
    assert registry.in_flight() == 2


def test_join_devuelve_el_resultado_recordado(registry_con_memoria):
    registry_con_memoria.join("k", lambda v: None)
    registry_con_memoria.run_now("k", lambda: "listo")

    accion, valor = registry_con_memoria.join("k", lambda v: None)
    assert accion is Claim.REMEMBERED
    assert valor == "listo"
    # Un resultado recordado no deja nada en curso ni anota el callback.
    assert registry_con_memoria.in_flight() == 0


def test_sin_memoria_nunca_recuerda(registry):
    registry.join("k", lambda v: None)
    registry.run_now("k", lambda: "listo")
    accion, _valor = registry.join("k", lambda v: None)
    assert accion is Claim.CLAIMED  # hay que rehacer el trabajo


# ================================================================= run_now --
def test_run_now_entrega_el_resultado_a_todos_los_anotados(registry):
    recibidos = []
    registry.join("k", recibidos.append)
    registry.join("k", recibidos.append)
    registry.join("k", recibidos.append)

    registry.run_now("k", lambda: "valor")

    assert recibidos == ["valor", "valor", "valor"]


def test_run_now_libera_la_clave(registry):
    registry.join("k", lambda v: None)
    registry.run_now("k", lambda: "valor")
    assert registry.in_flight() == 0


def test_un_callback_que_falla_no_bloquea_a_los_demas(registry):
    """El caso real: una fila de la biblioteca que ya no existe cuando
    llega su resultado. No puede llevarse puestos a los otros que esperan
    lo mismo ni al worker del pool."""
    recibidos = []

    def cb_malo(_v):
        raise RuntimeError("boom")

    registry.join("k", cb_malo)
    registry.join("k", recibidos.append)
    registry.join("k", cb_malo)
    registry.join("k", recibidos.append)

    registry.run_now("k", lambda: "valor")

    assert recibidos == ["valor", "valor"]
    assert registry.in_flight() == 0


def test_un_trabajo_que_falla_resuelve_con_none_por_defecto(registry):
    recibidos = []
    registry.join("k", recibidos.append)

    def boom():
        raise RuntimeError("se rompió")

    registry.run_now("k", boom)

    assert recibidos == [None]
    assert registry.in_flight() == 0


def test_on_error_puede_mapear_la_excepcion_a_un_resultado(registry):
    """Lo que necesita `oscwii_client`: un fallo tiene que llegar como un
    resultado con el motivo adentro, no como None."""
    recibidos = []
    registry.join("k", recibidos.append)

    registry.run_now("k", lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                     on_error=lambda e: f"error: {e}")

    assert recibidos == ["error: boom"]


def test_un_trabajo_que_falla_igual_libera_la_clave(registry):
    """Si un fallo dejara la clave reservada, ningún pedido posterior de
    esa clave volvería a intentarse nunca."""
    registry.join("k", lambda v: None)
    registry.run_now("k", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert registry.in_flight() == 0
    assert registry.join("k", lambda v: None)[0] is Claim.CLAIMED


def test_run_now_sobre_una_clave_sin_anotados_no_falla(registry):
    """Puede pasar si alguien llamó a `forget()` en el medio: no hay a
    quién avisarle, pero no es motivo para reventar el worker."""
    registry.run_now("fantasma", lambda: "valor")


# ------------------------------------------------------- resultado recordado
def test_remember_when_true_recuerda(registry_con_memoria):
    registry_con_memoria.join("k", lambda v: None)
    registry_con_memoria.run_now("k", lambda: None, remember_when=lambda v: True)
    assert registry_con_memoria.join("k", lambda v: None)[0] is Claim.REMEMBERED


def test_remember_when_false_no_recuerda(registry_con_memoria):
    """El caso de la metadata sin internet: el None es temporal y hay que
    poder reintentarlo cuando vuelva la conexión."""
    registry_con_memoria.join("k", lambda v: None)
    registry_con_memoria.run_now("k", lambda: None, remember_when=lambda v: False)
    assert registry_con_memoria.join("k", lambda v: None)[0] is Claim.CLAIMED


def test_remember_when_recibe_el_valor_resuelto(registry_con_memoria):
    vistos = []
    registry_con_memoria.join("k", lambda v: None)
    registry_con_memoria.run_now("k", lambda: "el valor",
                                  remember_when=lambda v: vistos.append(v) or True)
    assert vistos == ["el valor"]


def test_remember_when_tambien_se_aplica_a_un_resultado_de_error(registry_con_memoria):
    registry_con_memoria.join("k", lambda v: None)
    registry_con_memoria.run_now(
        "k", lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        on_error=lambda e: "falló", remember_when=lambda v: v != "falló")
    assert registry_con_memoria.join("k", lambda v: None)[0] is Claim.CLAIMED


def test_sin_remember_when_recuerda_siempre(registry_con_memoria):
    registry_con_memoria.join("k", lambda v: None)
    registry_con_memoria.run_now("k", lambda: "valor")
    assert registry_con_memoria.join("k", lambda v: None)[0] is Claim.REMEMBERED


# ================================================================== submit --
def test_submit_resuelve_en_el_pool(registry):
    recibidos = []
    registry.submit("k", lambda: "valor", recibidos.append)
    assert _esperar(lambda: recibidos == ["valor"])
    assert registry.in_flight() == 0


def test_submit_dedupe_pedidos_concurrentes(registry):
    """El corazón del módulo: dos pedidos de la misma clave mientras el
    primero sigue en curso hacen UN solo trabajo, y los dos reciben el
    mismo resultado."""
    trabajo = TrabajoBloqueado("una sola vez")
    recibidos = []
    lock = threading.Lock()

    def anotar(v):
        with lock:
            recibidos.append(v)

    registry.submit("k", trabajo, anotar)
    assert trabajo.arranco.wait(timeout=5)
    assert registry.in_flight() == 1

    registry.submit("k", trabajo, anotar)
    registry.submit("k", trabajo, anotar)
    trabajo.seguir.set()

    assert _esperar(lambda: len(recibidos) == 3)
    assert trabajo.llamadas == 1
    assert recibidos == ["una sola vez"] * 3
    assert registry.in_flight() == 0


def test_submit_claves_distintas_corren_en_paralelo(registry):
    trabajo_a = TrabajoBloqueado("a")
    trabajo_b = TrabajoBloqueado("b")
    recibidos = {}
    lock = threading.Lock()

    def anotar(nombre):
        def _cb(v):
            with lock:
                recibidos[nombre] = v
        return _cb

    registry.submit("a", trabajo_a, anotar("a"))
    registry.submit("b", trabajo_b, anotar("b"))

    assert trabajo_a.arranco.wait(timeout=5)
    assert trabajo_b.arranco.wait(timeout=5)
    assert registry.in_flight() == 2

    trabajo_a.seguir.set()
    trabajo_b.seguir.set()

    assert _esperar(lambda: len(recibidos) == 2)
    assert recibidos == {"a": "a", "b": "b"}


def test_submit_con_resultado_recordado_contesta_en_el_mismo_hilo(registry_con_memoria):
    """No debe ocupar un worker: se resuelve en el acto, que es lo que
    hace barato un rescan de 300 filas."""
    registry_con_memoria.submit("k", lambda: "valor", lambda v: None)
    assert _esperar(lambda: registry_con_memoria.in_flight() == 0)

    hilo_del_callback = []
    registry_con_memoria.submit(
        "k",
        lambda: (_ for _ in ()).throw(AssertionError("no debería rehacer el trabajo")),
        lambda v: hilo_del_callback.append(threading.current_thread()))

    assert hilo_del_callback == [threading.current_thread()]


def test_submit_un_trabajo_que_falla_no_deja_a_nadie_esperando(registry):
    recibidos = []
    registry.submit("k", lambda: (_ for _ in ()).throw(RuntimeError("boom")),
                     recibidos.append)
    assert _esperar(lambda: recibidos == [None])
    assert registry.in_flight() == 0


def test_un_callback_puede_volver_a_pedir_sin_deadlock(registry):
    """Los callbacks se llaman fuera del lock, a propósito: una fila que
    se redibuja al recibir su resultado vuelve a pedir en el acto, y con
    el lock tomado eso sería un deadlock."""
    recibidos = []

    def reentrante(valor):
        recibidos.append(valor)
        if len(recibidos) == 1:
            registry.submit("otra", lambda: "segundo", recibidos.append)

    registry.submit("primera", lambda: "primero", reentrante)

    assert _esperar(lambda: recibidos == ["primero", "segundo"])
    assert registry.in_flight() == 0


# ================================================================== forget --
def test_forget_vacia_pedidos_y_resultados(registry_con_memoria):
    registry_con_memoria.join("en-curso", lambda v: None)
    registry_con_memoria.join("recordado", lambda v: None)
    registry_con_memoria.run_now("recordado", lambda: "valor")

    registry_con_memoria.forget()

    assert registry_con_memoria.in_flight() == 0
    assert registry_con_memoria.join("recordado", lambda v: None)[0] is Claim.CLAIMED


def test_forget_sobre_un_registry_sin_memoria_no_falla(registry):
    registry.join("k", lambda v: None)
    registry.forget()
    assert registry.in_flight() == 0
