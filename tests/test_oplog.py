"""Historial persistente: orden, tolerancia a archivos rotos y exportación."""
from __future__ import annotations

import json

import pytest

from wiibackup_manager import oplog


@pytest.fixture
def log(tmp_path):
    return oplog.OperationLog(tmp_path / "history.json")


def test_registrar_y_leer(log):
    log.record("Eliminando", "Mario Kart", oplog.STATUS_OK, "a.iso → papelera")
    entradas = log.entries()
    assert len(entradas) == 1
    assert entradas[0].operation == "Eliminando"
    assert entradas[0].status_label == "Completada"


def test_un_estado_inventado_no_se_guarda(log):
    """El historial se dibuja traduciendo el estado a texto y a un ícono:
    uno desconocido no se podría mostrar bien."""
    assert log.record("Eliminando", "x", "inventado") is None
    assert log.is_empty()


def test_persiste_entre_instancias(tmp_path):
    ruta = tmp_path / "history.json"
    oplog.OperationLog(ruta).record("Convirtiendo", "Zelda", oplog.STATUS_OK)
    assert len(oplog.OperationLog(ruta).entries()) == 1


def test_la_mas_reciente_va_primero(log):
    log.record("Convirtiendo", "primero", oplog.STATUS_OK)
    log.record("Eliminando", "segundo", oplog.STATUS_OK)
    assert [e.target for e in log.entries()] == ["segundo", "primero"]


def test_entradas_del_mismo_segundo_conservan_su_orden(log):
    """Tres borrados seguidos terminan dentro del mismo segundo y
    comparten timestamp: sin desempate quedaban justo al revés."""
    for i in range(5):
        log.record("Eliminando", f"juego-{i}", oplog.STATUS_OK)
    assert [e.target for e in log.entries()] == [f"juego-{i}" for i in (4, 3, 2, 1, 0)]


def test_una_entrada_corrupta_no_se_lleva_puesto_el_resto(tmp_path):
    ruta = tmp_path / "history.json"
    ruta.write_text(json.dumps([
        {"timestamp": "2026-01-01T10:00:00-03:00", "operation": "Convirtiendo",
         "target": "buena", "status": "ok", "detail": ""},
        {"esto": "no es una entrada"},
        {"timestamp": "2026-01-01T11:00:00-03:00", "operation": "Eliminando",
         "target": "otra buena", "status": "ok", "detail": ""},
        "ni esto",
    ]), encoding="utf-8")
    entradas = oplog.OperationLog(ruta).entries()
    assert {e.target for e in entradas} == {"buena", "otra buena"}


def test_un_json_ilegible_arranca_un_historial_nuevo(tmp_path):
    ruta = tmp_path / "history.json"
    ruta.write_text("{ esto no es json", encoding="utf-8")
    assert oplog.OperationLog(ruta).is_empty()


def test_una_fecha_sin_zona_no_rompe_el_orden(tmp_path):
    """Mezclar timestamps con y sin zona horaria hacía que el sorted de
    entries() levantara TypeError y se llevara puesto todo el historial."""
    ruta = tmp_path / "history.json"
    ruta.write_text(json.dumps([
        {"timestamp": "2026-01-01T10:00:00", "operation": "Convirtiendo",
         "target": "sin zona", "status": "ok", "detail": ""},
        {"timestamp": "2026-01-02T10:00:00-03:00", "operation": "Eliminando",
         "target": "con zona", "status": "ok", "detail": ""},
    ]), encoding="utf-8")
    entradas = oplog.OperationLog(ruta).entries()
    assert [e.target for e in entradas] == ["con zona", "sin zona"]


def test_una_fecha_ilegible_va_al_final_sin_romper(tmp_path):
    ruta = tmp_path / "history.json"
    ruta.write_text(json.dumps([
        {"timestamp": "no es una fecha", "operation": "Convirtiendo",
         "target": "rota", "status": "ok", "detail": ""},
        {"timestamp": "2026-01-02T10:00:00-03:00", "operation": "Eliminando",
         "target": "buena", "status": "ok", "detail": ""},
    ]), encoding="utf-8")
    entradas = oplog.OperationLog(ruta).entries()
    assert [e.target for e in entradas] == ["buena", "rota"]


def test_se_respeta_el_tope_de_entradas(tmp_path, monkeypatch):
    monkeypatch.setattr(oplog, "MAX_ENTRIES", 5)
    log = oplog.OperationLog(tmp_path / "history.json")
    for i in range(12):
        log.record("Eliminando", f"j{i}", oplog.STATUS_OK)
    entradas = log.entries()
    assert len(entradas) == 5
    # Las que se caen son las más viejas.
    assert entradas[0].target == "j11"


def test_clear(log):
    log.record("Eliminando", "x", oplog.STATUS_OK)
    log.clear()
    assert log.is_empty()
    assert oplog.OperationLog(log.path).is_empty()   # también en disco


def test_export_text(log):
    log.record("Eliminando", "Mario Kart", oplog.STATUS_OK, "a.iso → papelera")
    texto = log.export_text()
    assert "Historial de operaciones" in texto
    assert "Eliminando: Mario Kart" in texto
    assert "a.iso → papelera" in texto


def test_export_text_vacio(log):
    assert "(sin operaciones registradas)" in log.export_text()


def test_los_listeners_se_avisan(log):
    avisos = []
    log.add_listener(lambda: avisos.append(1))
    log.record("Eliminando", "x", oplog.STATUS_OK)
    log.clear()
    assert len(avisos) == 2


def test_un_listener_que_falla_no_rompe_al_que_registro(log):
    """Una vista ya destruida no puede tirar abajo el worker que acaba de
    terminar de copiar un juego."""
    def revienta():
        raise RuntimeError("vista destruida")
    otros = []
    log.add_listener(revienta)
    log.add_listener(lambda: otros.append(1))
    log.record("Eliminando", "x", oplog.STATUS_OK)
    assert otros == [1]
