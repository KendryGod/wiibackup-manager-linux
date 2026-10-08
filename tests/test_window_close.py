"""Cerrar la ventana con algo peligroso en curso: que pregunte primero.

Antes, `close-request` cortaba la cola y devolvía False: la ventana se
cerraba siempre, y lo que quedaba era confiar en que los `shutdown()` de
las tres vistas terminaran bien mientras el proceso se moría. Sobre la
unidad de un cliente que probablemente esté por desenchufar, eso no
alcanza.

Ahora el cierre se frena y se pregunta, pero SOLO si lo que corre es de lo
que no se puede cortar de golpe (`operations.INTERRUPT_UNSAFE_KINDS`). Un
escaneo de biblioteca no frena nada: no escribe, y vuelve a correr solo la
próxima vez que se abre la app.

Cómo se prueba
--------------
Con los métodos REALES de `WiiBackupWindow` atados a un `self` de mentira,
como en `test_queue_manager.py` y `test_recovery_service.py`: importar el
módulo no abre ninguna ventana, así que esto corre en cualquier terminal y
no solo bajo Xvfb.

Lo único simulado es el `present()` del diálogo, que necesitaría un
display y un padre de verdad. El `Adw.AlertDialog` en cambio es el de
verdad: se construye, se le leen los botones y se le emite la señal
`response` real, así que lo que se está probando es el diálogo que va a
ver el usuario y no una maqueta paralela que podría quedar desincronizada.
"""
from __future__ import annotations

import types

import pytest

from wiibackup_manager import operations
from wiibackup_manager.operations import OperationKind, OperationManager


# --------------------------------------------------------------- Harness --
class _VistaDeMentira:
    """Una de las tres vistas con trabajo de fondo. Solo anota que le
    pidieron cortar."""

    def __init__(self, nombre: str, eventos: list):
        self._nombre = nombre
        self._eventos = eventos

    def shutdown(self):
        self._eventos.append(f"shutdown:{self._nombre}")


class _TokenDeMentira:
    def __init__(self, eventos: list):
        self._eventos = eventos
        self.cancelled = False

    def cancel(self):
        self.cancelled = True
        self._eventos.append("cancel:token")


class _VentanaDeMentira:
    """Lo mínimo de `WiiBackupWindow` que tocan los métodos de cierre.

    `eventos` va anotando en orden todo lo que se le pide, que es lo que
    permite comprobar lo que importa: que la cancelación pase ANTES del
    cierre y no después."""

    def __init__(self, ops):
        self.eventos: list = []
        self.ops = ops
        self._close_confirmed = False
        self._cancel_token = _TokenDeMentira(self.eventos)
        self.transfer_view = _VistaDeMentira("transfer", self.eventos)
        self.homebrew_view = _VistaDeMentira("homebrew", self.eventos)
        self.memory_check_view = _VistaDeMentira("memory", self.eventos)
        self.dialogos: list = []
        self.toasts: list = []
        # La espera acotada antes de cerrar (`_close_after_cancel`), con un
        # reloj de mentira: cada vuelta del "temporizador" avanza el reloj
        # lo que avanzaría el de GLib, hasta que la función pida parar.
        self._closing = False
        self.reloj = 0.0
        self.vueltas = 0
        self._close_clock = lambda: self.reloj
        self._close_timer = self._temporizador

        from wiibackup_manager.window import WiiBackupWindow
        for nombre in ("_on_close_request", "_shutdown_views", "_confirm_close",
                       "_on_close_response", "_close_after_cancel"):
            setattr(self, nombre,
                    types.MethodType(getattr(WiiBackupWindow, nombre), self))

    def close(self):
        self.eventos.append("close")

    def _show_toast(self, mensaje):
        self.toasts.append(mensaje)

    def _temporizador(self, ms, funcion):
        while True:
            self.reloj += ms / 1000
            self.vueltas += 1
            if not funcion():
                return


@pytest.fixture
def sin_present(monkeypatch):
    """`AlertDialog.present()` necesita un display y un padre de verdad.
    Se lo intercepta y se guarda el diálogo, que es el real."""
    # Vía `window` y no `gi.repository` directo: ese módulo ya corrió el
    # `gi.require_version("Adw", "1")`, y además así se parchea el MISMO
    # Adw que usa el código bajo prueba.
    from wiibackup_manager.window import Adw
    presentados: list = []
    monkeypatch.setattr(Adw.AlertDialog, "present",
                        lambda self, parent=None: presentados.append(self))
    return presentados


@pytest.fixture
def ventana(sin_present):
    v = _VentanaDeMentira(OperationManager())
    v.dialogos = sin_present
    return v


# ------------------------------------------ La política de qué es peligroso --
def test_el_escaneo_de_biblioteca_no_cuenta_como_peligroso():
    """El caso que tiene que seguir cerrando sin molestar: el escaneo corre
    solo, no escribe nada y se rehace al abrir de nuevo."""
    ops = OperationManager()
    ops.start(OperationKind.SCANNING, resources=["/tmp/biblioteca"])
    assert ops.unsafe_to_interrupt() == []


@pytest.mark.parametrize("kind", sorted(operations.INTERRUPT_UNSAFE_KINDS,
                                        key=lambda k: k.name))
def test_las_operaciones_que_escriben_o_tardan_si_cuentan(kind):
    ops = OperationManager()
    ops.start(kind, resources=[f"/tmp/{kind.name}"])
    assert [op.kind for op in ops.unsafe_to_interrupt()] == [kind]


def test_lo_peligroso_se_ve_aunque_haya_algo_inocente_al_lado():
    """Un escaneo NO tapa una transferencia: lo que decide es si hay
    aunque sea una peligrosa, no qué proporción."""
    ops = OperationManager()
    ops.start(OperationKind.SCANNING, resources=["/tmp/biblioteca"])
    ops.start(OperationKind.TRANSFERRING, resources=["/run/media/usb"])
    assert [op.kind for op in ops.unsafe_to_interrupt()] == [
        OperationKind.TRANSFERRING]


# ------------------------------------------------- El cierre sin preguntar --
def test_cerrar_sin_nada_en_curso_no_muestra_ningun_dialogo(ventana):
    """El caso de siempre: no hay nada corriendo, la ventana se cierra y
    de paso les avisa a las vistas que corten."""
    assert ventana._on_close_request() is False, "frenó un cierre que era seguro"
    assert ventana.dialogos == [], "preguntó sin tener nada que preguntar"
    assert ventana.eventos == ["shutdown:transfer", "shutdown:homebrew",
                               "shutdown:memory"]


def test_cerrar_con_un_escaneo_en_curso_tampoco_pregunta(ventana):
    """Lo mismo, con la operación que el pedido nombró como segura."""
    ventana.ops.start(OperationKind.SCANNING, resources=["/tmp/biblioteca"])
    assert ventana._on_close_request() is False
    assert ventana.dialogos == []


# -------------------------------------------------- El cierre que pregunta --
@pytest.mark.parametrize("kind", sorted(operations.INTERRUPT_UNSAFE_KINDS,
                                        key=lambda k: k.name))
def test_una_operacion_peligrosa_frena_el_cierre_y_pregunta(ventana, kind):
    """Lo central: `close-request` devuelve True -que en GTK quiere decir
    "no cierres"- y aparece el diálogo. Se prueba con cada tipo peligroso,
    porque el pedido es que ninguno se cuele."""
    ventana.ops.start(kind, resources=[f"/tmp/{kind.name}"])

    assert ventana._on_close_request() is True, (
        f"{kind.name} dejó cerrar la ventana sin preguntar")
    # Y no cortó nada todavía: eso lo decide el usuario en el diálogo.
    assert ventana.eventos == []
    assert len(ventana.dialogos) == 1

    dialogo = ventana.dialogos[0]
    # Las dos salidas que pidió el pedido, y ninguna otra.
    assert dialogo.get_response_label("wait") == "Seguir esperando"
    assert dialogo.get_response_label("close") == "Cancelar operación y cerrar"
    # Dice QUÉ está corriendo, no un "hay algo en curso" genérico.
    assert kind.label in dialogo.get_body()
    # Escape y el botón por defecto son el que NO destruye nada.
    assert dialogo.get_close_response() == "wait"
    assert dialogo.get_default_response() == "wait"


def test_el_dialogo_nombra_todas_las_operaciones_en_curso(ventana):
    ventana.ops.start(OperationKind.TRANSFERRING, resources=["/run/media/usb"])
    ventana.ops.start(OperationKind.CHECKING_MEMORY, resources=["/run/media/sd"])

    ventana._on_close_request()

    cuerpo = ventana.dialogos[0].get_body()
    assert OperationKind.TRANSFERRING.label in cuerpo
    assert OperationKind.CHECKING_MEMORY.label in cuerpo


def test_el_dialogo_avisa_cuando_lo_que_corre_no_se_puede_cancelar(ventana):
    """Formatear va por `pkexec`: es un proceso de root que no es hijo
    nuestro, así que "Cancelar y cerrar" no lo para. El diálogo tiene que
    decirlo en vez de ofrecer una cancelación que no existe."""
    ventana.ops.start(OperationKind.FORMATTING, resources=["/dev/sdb"])
    ventana._on_close_request()

    cuerpo = ventana.dialogos[0].get_body()
    assert "no se puede cancelar" in cuerpo
    assert "sigue funcionando" in cuerpo


# ----------------------------------------------- Lo que hace cada respuesta --
def test_seguir_esperando_no_cierra_ni_corta_nada(ventana):
    ventana.ops.start(OperationKind.TRANSFERRING, resources=["/run/media/usb"])
    ventana._on_close_request()

    ventana.dialogos[0].emit("response", "wait")

    assert ventana.eventos == [], "canceló algo cuando le dijeron que esperara"
    assert ventana._close_confirmed is False
    # Y si el usuario vuelve a intentar cerrar, vuelve a preguntar.
    assert ventana._on_close_request() is True


def test_cancelar_y_cerrar_corta_de_verdad_ANTES_de_cerrar(ventana):
    """La otra mitad del pedido: que "Cancelar operación y cerrar" no sea
    solo un cierre con otro nombre.

    Se comprueba el ORDEN, no solo que las llamadas ocurran: cerrar
    primero y cancelar después sería exactamente el problema que esto vino
    a arreglar."""
    ventana.ops.start(OperationKind.TRANSFERRING, resources=["/run/media/usb"])
    ventana._on_close_request()

    ventana.dialogos[0].emit("response", "close")

    assert ventana.eventos == [
        "shutdown:transfer", "shutdown:homebrew", "shutdown:memory",
        "cancel:token", "close",
    ], "el cierre no pasó después de cancelar todo"
    assert ventana._cancel_token.cancelled is True


def test_convertir_se_corta_por_el_token_y_no_por_las_vistas(ventana):
    """Convertir y verificar en lote corren por la barra de progreso de la
    ventana, no por ninguna de las tres vistas: sin el token no los cortaba
    nadie."""
    ventana.ops.start(OperationKind.CONVERTING, resources=["/tmp/juego.iso"])
    ventana._on_close_request()
    ventana.dialogos[0].emit("response", "close")

    assert ventana._cancel_token.cancelled is True
    assert "close" in ventana.eventos


def test_despues_de_confirmar_el_cierre_ya_no_vuelve_a_preguntar(ventana):
    """El `close()` que dispara el diálogo entra otra vez a
    `close-request`. Sin la bandera, volvería a preguntar y la ventana no
    se cerraría nunca."""
    ventana.ops.start(OperationKind.TRANSFERRING, resources=["/run/media/usb"])
    ventana._on_close_request()
    ventana.dialogos[0].emit("response", "close")
    assert ventana._close_confirmed is True

    # La segunda vuelta, la que dispara `self.close()`.
    ventana.eventos.clear()
    assert ventana._on_close_request() is False, "quedó preguntando para siempre"
    assert ventana.dialogos == [ventana.dialogos[0]], "mostró un segundo diálogo"
    # Y no vuelve a cortar lo que ya cortó.
    assert ventana.eventos == []


# ------------------------------------- Esperar lo cancelado, con un tope --
def test_cerrar_espera_a_que_lo_cancelado_termine_de_cerrarse(ventana):
    """El caso del reemplazo: cancelar mata `wit` en el acto, pero el
    original lo devuelve a su nombre el hilo de la copia un momento
    después. La ventana se cierra recién cuando la operación terminó."""
    op = ventana.ops.start(OperationKind.TRANSFERRING, resources=["/run/media/usb"])
    ventana._on_close_request()

    def temporizador(ms, funcion):
        while True:
            ventana.reloj += ms / 1000
            ventana.vueltas += 1
            if ventana.vueltas == 6:
                ventana.ops.finish(op)   # el hilo terminó de restaurar
            if not funcion():
                return

    ventana._close_timer = temporizador
    ventana.dialogos[0].emit("response", "close")

    assert ventana.eventos[-1] == "close"
    assert ventana.vueltas == 6
    assert ventana.reloj == pytest.approx(0.6)
    assert ventana.toasts == ["Cancelando antes de cerrar…"]


def test_cerrar_no_espera_mas_de_3_segundos(ventana):
    """Un `wit` que no muere (la USB se desconectó y quedó en estado D):
    la operación no termina nunca, y la ventana se cierra igual al
    vencer el plazo."""
    ventana.ops.start(OperationKind.TRANSFERRING, resources=["/run/media/usb"])
    ventana._on_close_request()
    ventana.dialogos[0].emit("response", "close")

    assert ventana.eventos[-1] == "close"
    assert ventana.reloj == pytest.approx(operations.CLOSE_WAIT_SECONDS)
    assert operations.CLOSE_WAIT_SECONDS <= 3.0


def test_mientras_espera_otro_intento_de_cerrar_no_pregunta_ni_cierra(ventana):
    op = ventana.ops.start(OperationKind.TRANSFERRING, resources=["/run/media/usb"])
    programadas = []
    ventana._close_timer = lambda ms, funcion: programadas.append(funcion)
    ventana._on_close_request()
    ventana.dialogos[0].emit("response", "close")
    assert "close" not in ventana.eventos

    assert ventana._on_close_request() is True
    assert len(ventana.dialogos) == 1, "volvió a preguntar mientras esperaba"
    assert "close" not in ventana.eventos

    ventana.ops.finish(op)
    assert programadas[0]() is False
    assert ventana.eventos[-1] == "close"


def test_lo_que_no_se_puede_cancelar_no_se_espera(ventana):
    """Formatear corre como root fuera de la app: esperarlo 3 segundos no
    cambia nada. Se cierra en el acto, como antes."""
    ventana.ops.start(OperationKind.FORMATTING, resources=["/dev/sdb"])
    ventana._on_close_request()
    ventana.dialogos[0].emit("response", "close")

    assert ventana.eventos[-1] == "close"
    assert ventana.vueltas == 0
    assert ventana.toasts == []
