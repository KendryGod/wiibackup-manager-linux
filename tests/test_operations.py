"""Reglas de conflicto entre operaciones.

Este es el módulo que decide qué combinaciones NO pueden correr a la vez
(borrar un juego mientras `wit` lo convierte, expulsar el pendrive a mitad
de una copia). Es lógica pura y sin GTK a propósito, justamente para poder
probarla así.
"""
from __future__ import annotations

import pytest

from wiibackup_manager.operations import (
    OperationBusy,
    OperationKind,
    OperationManager,
)


@pytest.fixture
def ops():
    return OperationManager()


def test_arranca_libre(ops):
    assert not ops.is_busy()
    assert ops.busy_label() is None


def test_start_y_finish(ops, tmp_path):
    op = ops.start(OperationKind.CONVERTING, read=[tmp_path / "a.iso"])
    assert ops.is_busy()
    assert ops.busy_label() == "Convirtiendo"
    ops.finish(op)
    assert not ops.is_busy()


def test_finish_tolera_none_y_llamadas_repetidas(ops, tmp_path):
    """Los workers llaman a finish en un `finally` y otra vez al terminar:
    la segunda no puede romper nada."""
    ops.finish(None)
    op = ops.start(OperationKind.VERIFYING, read=[tmp_path / "a.iso"])
    ops.finish(op)
    ops.finish(op)
    assert not ops.is_busy()


# --------------------------------------------- Regla 1: tipos exclusivos --
def test_dos_escaneos_a_la_vez_no(ops, tmp_path):
    ops.start(OperationKind.SCANNING, resources=[tmp_path])
    with pytest.raises(OperationBusy):
        ops.start(OperationKind.SCANNING, resources=[tmp_path / "otra"])


# ------------------------------------------ Regla 1b: barra de progreso --
def test_dos_operaciones_que_usan_la_barra_no_conviven(ops, tmp_path):
    ops.start(OperationKind.CONVERTING, write=[tmp_path / "a.wbfs"])
    with pytest.raises(OperationBusy):
        ops.start(OperationKind.TRANSFERRING, read=[tmp_path / "b.iso"])


def test_verificar_un_juego_suelto_no_reserva_la_barra(ops, tmp_path):
    """Verificar o borrar UN juego mientras se convierte OTRO es algo que
    se hace todo el tiempo preparando varias unidades seguidas."""
    ops.start(OperationKind.CONVERTING, write=[tmp_path / "a.wbfs"])
    ops.start(OperationKind.VERIFYING, read=[tmp_path / "distinto.iso"])
    assert len(ops.active_operations()) == 2


def test_un_lote_de_verificar_si_reserva_la_barra(ops, tmp_path):
    ops.start(OperationKind.CONVERTING, write=[tmp_path / "a.wbfs"])
    with pytest.raises(OperationBusy):
        ops.start(OperationKind.VERIFYING, read=[tmp_path / "b.iso"],
                  uses_progress_bar=True)


# ------------------------------------------------- Regla 2: recursos --
def test_dos_operaciones_no_ocupan_el_mismo_lugar(ops, tmp_path):
    ops.start(OperationKind.TRANSFERRING, resources=[tmp_path])
    with pytest.raises(OperationBusy):
        ops.start(OperationKind.IMPORTING, resources=[tmp_path])


def test_un_recurso_dentro_de_otro_tambien_choca(ops, tmp_path):
    """La biblioteca guardada DENTRO del pendrive que se está escribiendo."""
    adentro = tmp_path / "sub" / "biblioteca"
    adentro.mkdir(parents=True)
    ops.start(OperationKind.TRANSFERRING, resources=[tmp_path])
    with pytest.raises(OperationBusy):
        ops.start(OperationKind.SCANNING, resources=[adentro])


# ------------------------------- Regla 3: escribir dentro de un recurso --
def test_no_se_escribe_dentro_de_un_lugar_ocupado(ops, tmp_path):
    biblioteca = tmp_path / "biblioteca"
    biblioteca.mkdir()
    ops.start(OperationKind.SCANNING, resources=[biblioteca])
    with pytest.raises(OperationBusy):
        ops.start(OperationKind.CONVERTING, write=[biblioteca / "nuevo.wbfs"])


def test_escribir_fuera_del_lugar_ocupado_si_se_puede(ops, tmp_path):
    biblioteca = tmp_path / "biblioteca"
    biblioteca.mkdir()
    afuera = tmp_path / "otro-lado"
    afuera.mkdir()
    ops.start(OperationKind.SCANNING, resources=[biblioteca])
    ops.start(OperationKind.CONVERTING, write=[afuera / "nuevo.wbfs"])
    assert len(ops.active_operations()) == 2


# ------------------------------------------ Regla 4: el mismo archivo --
def test_dos_lecturas_del_mismo_archivo_conviven(ops, tmp_path):
    archivo = tmp_path / "juego.iso"
    ops.start(OperationKind.VERIFYING, read=[archivo])
    ops.start(OperationKind.TRANSFERRING, read=[archivo])
    assert len(ops.active_operations()) == 2


def test_borrar_un_archivo_que_se_esta_convirtiendo_no(ops, tmp_path):
    """El caso que motivó todo el módulo."""
    archivo = tmp_path / "juego.iso"
    ops.start(OperationKind.CONVERTING, read=[archivo], write=[tmp_path / "juego.wbfs"])
    with pytest.raises(OperationBusy):
        ops.start(OperationKind.DELETING, write=[archivo])


def test_las_rutas_se_normalizan_antes_de_comparar(ops, tmp_path):
    """'juego.iso' y './sub/../juego.iso' son el mismo archivo."""
    archivo = tmp_path / "juego.iso"
    archivo.write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    equivalente = tmp_path / "sub" / ".." / "juego.iso"
    ops.start(OperationKind.CONVERTING, write=[archivo])
    with pytest.raises(OperationBusy):
        ops.start(OperationKind.DELETING, write=[equivalente])


# ------------------------------------------------------ Consultas --
def test_is_resource_busy_protege_la_unidad_de_destino(ops, tmp_path):
    """Lo que hace que "Expulsar unidad" se niegue mientras se copia."""
    assert ops.is_resource_busy(tmp_path) is None
    ops.start(OperationKind.TRANSFERRING, resources=[tmp_path])
    ocupada = ops.is_resource_busy(tmp_path)
    assert ocupada is not None
    assert ocupada.kind is OperationKind.TRANSFERRING


def test_is_resource_busy_tambien_si_solo_se_escribe_adentro(ops, tmp_path):
    ops.start(OperationKind.CONVERTING, write=[tmp_path / "sub" / "x.wbfs"])
    assert ops.is_resource_busy(tmp_path) is not None


def test_is_path_busy(ops, tmp_path):
    archivo = tmp_path / "juego.iso"
    assert not ops.is_path_busy(archivo)
    ops.start(OperationKind.VERIFYING, read=[archivo])
    assert ops.is_path_busy(archivo)
    assert not ops.is_path_busy(tmp_path / "otro.iso")


def test_check_no_registra_nada(ops, tmp_path):
    """`check` revalida antes de tocar el disco; no puede dejar una
    operación fantasma anotada."""
    ops.check(OperationKind.DELETING, write=[tmp_path / "a.iso"])
    assert not ops.is_busy()


def test_conflicts_for_resuelve_varios_de_una(ops, tmp_path):
    archivo = tmp_path / "juego.iso"
    ops.start(OperationKind.CONVERTING, write=[archivo])
    resultado = ops.conflicts_for(
        [("borrar", OperationKind.DELETING, "write"),
         ("verificar", OperationKind.VERIFYING, "read")],
        read=[archivo], write=[archivo])
    assert resultado["borrar"] is not None
    assert resultado["verificar"] is not None


# ----------------------------------------------------- Historial --
def test_el_resultado_va_al_historial_sin_traducir(tmp_path):
    """Al log va OperationKind.value (siempre español); la traducción se
    aplica al mostrarlo. Ver oplog.LogEntry.operation."""
    from wiibackup_manager.oplog import OperationLog, STATUS_OK
    from wiibackup_manager.operations import OperationOutcome

    log = OperationLog(tmp_path / "history.json")
    ops = OperationManager(log=log)
    op = ops.start(OperationKind.DELETING, write=[tmp_path / "a.iso"])
    ops.finish(op, OperationOutcome(status=STATUS_OK, target="Mario Kart",
                                    detail="a.iso → papelera"))
    entradas = log.entries()
    assert len(entradas) == 1
    assert entradas[0].operation == "Eliminando"
    assert entradas[0].target == "Mario Kart"


def test_el_escaneo_no_ensucia_el_historial(tmp_path):
    from wiibackup_manager.oplog import OperationLog, STATUS_OK
    from wiibackup_manager.operations import OperationOutcome

    log = OperationLog(tmp_path / "history.json")
    ops = OperationManager(log=log)
    op = ops.start(OperationKind.SCANNING, resources=[tmp_path])
    ops.finish(op, OperationOutcome(status=STATUS_OK, target="biblioteca"))
    assert log.is_empty()


def test_los_listeners_se_avisan(ops, tmp_path):
    avisos = []
    ops.add_listener(lambda: avisos.append(1))
    op = ops.start(OperationKind.DELETING, write=[tmp_path / "a.iso"])
    ops.finish(op)
    assert len(avisos) == 2      # uno al arrancar, uno al terminar
