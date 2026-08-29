"""Envoltorio de `wit`: parseo de su salida y cancelación.

No se ejecuta `wit` de verdad (puede no estar instalado, y una conversión
real tarda minutos): se prueba lo que la app hace ALREDEDOR del proceso,
que es donde estaban los bugs -parsear una salida con formato variable,
matar el proceso al cancelar, limpiar los temporales-.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from wiibackup_manager import wit_wrapper


# ------------------------------------------------------- Salida de wit --
def test_strip_ansi():
    """`wit` colorea su salida aunque esté redirigida a una pipe, así que
    no se puede confiar en que venga limpia solo por capturarla."""
    assert wit_wrapper._strip_ansi("\x1b[32mOK\x1b[0m") == "OK"
    assert wit_wrapper._strip_ansi("sin color") == "sin color"


def test_find_id6_line_saltea_encabezados():
    """La fila de datos no está en un índice fijo: `wit LIST` antepone
    encabezados y separadores que cambian de una build a otra."""
    salida = (
        "ID6     MiB  Reg.  Título\n"
        "------------------------------\n"
        "RMCP01  4482  PAL   Mario Kart Wii\n"
    )
    assert wit_wrapper._find_id6_line(salida) == ("RMCP01", "Mario Kart Wii")


def test_find_id6_line_con_color():
    salida = "\x1b[1mRMCP01\x1b[0m  4482  PAL   Mario Kart Wii\n"
    assert wit_wrapper._find_id6_line(salida) == ("RMCP01", "Mario Kart Wii")


def test_find_id6_line_normaliza_a_mayusculas():
    assert wit_wrapper._find_id6_line("rmcp01 4482 PAL Mario Kart Wii") == \
        ("RMCP01", "Mario Kart Wii")


def test_find_id6_line_sin_fila_de_datos():
    assert wit_wrapper._find_id6_line("ID6  MiB  Reg.\n-----\n") is None
    assert wit_wrapper._find_id6_line("") is None


def test_find_id6_line_ignora_lo_que_no_es_un_id6():
    """Una línea que arranca con algo de 6 caracteres pero no es un ID6
    (con '/' o '..') no puede pasar por ID: termina en una ruta."""
    assert wit_wrapper._find_id6_line("../../  4482  PAL  falso") is None


# ------------------------------------------------------------ Consola --
@pytest.mark.parametrize("game_id,esperado", [
    ("GZ2E01", "gc"),      # GameCube: primer carácter 'G'
    ("gz2e01", "gc"),      # minúsculas: se normaliza igual que un game_id
    ("RMCE01", "wii"),
    ("SBQE01", "wii"),
])
def test_console_for_id(game_id, esperado):
    assert wit_wrapper.console_for_id(game_id) == esperado


# ------------------------------------------------------- Cancelación --
def test_token_arranca_sin_cancelar():
    assert not wit_wrapper.CancellationToken().cancelled


def test_token_cancel():
    t = wit_wrapper.CancellationToken()
    t.cancel()
    assert t.cancelled


def test_cancel_mata_el_proceso_adjunto():
    t = wit_wrapper.CancellationToken()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    try:
        assert t.attach(proc) is True
        t.cancel()
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_attach_despues_de_cancelar_devuelve_false_y_mata():
    """La ventana de carrera: cancelar justo entre que se decide lanzar
    `wit` y que arranca dejaba un proceso huérfano corriendo."""
    t = wit_wrapper.CancellationToken()
    t.cancel()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    try:
        assert t.attach(proc) is False
        assert proc.wait(timeout=10) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_detach():
    t = wit_wrapper.CancellationToken()
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    t.attach(proc)
    t.detach(proc)
    t.cancel()          # ya no hay proceso al que pedirle nada
    assert t.cancelled


# --------------------------------------------------- Archivos de salida --
def test_output_files_incluye_el_destino_y_sus_partes(tmp_path):
    dest = tmp_path / "RMCP01.wbfs"
    dest.write_bytes(b"x")
    (tmp_path / "RMCP01.wbf1").write_bytes(b"x")
    (tmp_path / "RMCP01.wbf2").write_bytes(b"x")
    encontrados = {p.name for p in wit_wrapper.output_files(dest)}
    assert encontrados == {"RMCP01.wbfs", "RMCP01.wbf1", "RMCP01.wbf2"}


def test_output_files_incluye_los_temporales_ocultos_de_wit(tmp_path):
    """Mientras copia, `wit` escribe en `.{nombre}.{random}.tmp` y recién
    al terminar renombra: sin contarlos, un fallo dejaba temporales de
    varios GB huérfanos."""
    dest = tmp_path / "RMCP01.wbfs"
    (tmp_path / ".RMCP01.wbfs.a1b2.tmp").write_bytes(b"x")
    encontrados = {p.name for p in wit_wrapper.output_files(dest)}
    assert ".RMCP01.wbfs.a1b2.tmp" in encontrados


def test_output_files_con_nada_escrito(tmp_path):
    assert wit_wrapper.output_files(tmp_path / "RMCP01.wbfs") == set()


def test_cleanup_new_output_files_borra_solo_lo_nuevo(tmp_path):
    """Lo que ya estaba antes de arrancar es del usuario y no se toca."""
    dest = tmp_path / "RMCP01.wbfs"
    viejo = tmp_path / "RMCP01.wbf1"
    viejo.write_bytes(b"del usuario")
    antes = wit_wrapper.output_files(dest)

    dest.write_bytes(b"basura a medio escribir")
    wit_wrapper.cleanup_new_output_files(dest, antes)

    assert not dest.exists()
    assert viejo.exists()


def test_is_available_con_un_binario_inexistente():
    assert not wit_wrapper.is_available("wit-que-no-existe-en-ningun-lado")


def test_find_wit_con_un_binario_inexistente():
    assert wit_wrapper.find_wit("wit-que-no-existe-en-ningun-lado") is None


# ------------------------------------------------- Timeout de ISOSIZE --
# `wit ISOSIZE` no recorre el archivo, solo lee la estructura del disco
# (0.02 s medido con juegos reales). Con el timeout general de 30 minutos,
# un archivo corrupto retenía la cola de transferencias todo ese rato: la
# cola pregunta el tamaño ANTES de cada copia, así que el resto de la
# tanda esperaba también.
def test_isosize_usa_su_propio_timeout(monkeypatch, tmp_path):
    visto = {}

    def _run_espia(binary, *args, timeout=None):
        visto["args"] = args
        visto["timeout"] = timeout
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(wit_wrapper, "find_wit", lambda b: "/usr/bin/wit")
    monkeypatch.setattr(wit_wrapper, "_run", _run_espia)

    wit_wrapper.iso_size_bytes(tmp_path / "juego.iso")

    assert visto["args"][0] == "ISOSIZE"
    assert visto["timeout"] == wit_wrapper.ISOSIZE_TIMEOUT
    assert visto["timeout"] != wit_wrapper.DEFAULT_WIT_TIMEOUT


def test_el_timeout_de_isosize_esta_en_el_rango_previsto():
    """Suficiente para un USB lento o con sectores que reintentan, y muy
    lejos de los 30 minutos que se heredaban del timeout general."""
    assert 60.0 <= wit_wrapper.ISOSIZE_TIMEOUT <= 120.0


def test_el_timeout_general_no_cambio():
    """VERIFY sí puede tardar legítimamente muchísimo (lee el disco
    entero), así que su límite queda como estaba: lo que se acortó es
    ISOSIZE, no todas las operaciones sin progreso."""
    assert wit_wrapper.DEFAULT_WIT_TIMEOUT == 1800.0


def test_isosize_que_se_queda_sin_tiempo_devuelve_none(monkeypatch, tmp_path):
    """Que se agote el timeout no rompe nada: `iso_size_bytes` devuelve
    None y quien llama estima el tamaño por otro lado (la copia no se
    cancela por esto)."""
    def _run_timeout(binary, *args, timeout=None):
        return wit_wrapper._timeout_result(
            list(args), subprocess.TimeoutExpired(args, timeout), timeout)

    monkeypatch.setattr(wit_wrapper, "find_wit", lambda b: "/usr/bin/wit")
    monkeypatch.setattr(wit_wrapper, "_run", _run_timeout)

    assert wit_wrapper.iso_size_bytes(tmp_path / "juego.iso") is None
