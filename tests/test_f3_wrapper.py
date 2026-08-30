"""Pruebas de `f3_wrapper`: el parseo de la salida de f3 y el veredicto.

Nada de esto corre `f3write`/`f3read` de verdad -llenar un pendrive de 64
GB y volver a leerlo tarda horas y necesita un pendrive-. Lo que se prueba
es lo que puede estar mal sin que nadie se dé cuenta:

- que la salida real de f3 se parsee bien (los textos de acá están copiados
  del formato que imprime f3 8.0, incluidas las líneas de progreso que se
  reescriben con `\\r`);
- que el veredicto sea estricto: una memoria que pierde UN sector no pasa;
- que los archivos de prueba se borren siempre, también cuando f3 falla o
  cuando se cancela, y que NO se toquen los que ya estaban.

La única parte que sí ejecuta un proceso real es
`test_run_streaming_entrega_lineas_cortadas_por_retorno_de_carro`, con un
`python3 -c` que imita cómo escribe f3: es el camino (leer de a poco y
cortar en `\\r`) que hace que la barra de progreso se mueva en vez de
quedarse quieta horas.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from wiibackup_manager import f3_wrapper


# Salida real de f3write sobre una memoria de ~3 GB libres (recortada a
# tres archivos, con las líneas de progreso que se reescriben con \r).
F3WRITE_OK = """F3 write 8.0
Copyright (C) 2010 Digirati Internacional
Licensed under GPLv3

Free space: 2.93 GB
Creating file 1.h2w ... 34.21% -- 12.30 MB/s -- 3:21
Creating file 1.h2w ... OK!
Creating file 2.h2w ... 71.02% -- 11.90 MB/s -- 1:40
Creating file 2.h2w ... OK!
Creating file 3.h2w ... OK!
Free space: 0.00 Byte
Average writing speed: 12.10 MB/s
"""

F3READ_OK = """F3 read 8.0
Copyright (C) 2010 Digirati Internacional
Licensed under GPLv3

                  SECTORS      ok/corrupted/changed/overwritten
Validating file 1.h2w ... 2097152/        0/      0/      0
Validating file 2.h2w ... 2097152/        0/      0/      0
Validating file 3.h2w ... 1949696/        0/      0/      0

  Data OK: 2.93 GB (6144000 sectors)
Data LOST: 0.00 Byte (0 sectors)
\t       Corrupted: 0.00 Byte (0 sectors)
\tSlightly changed: 0.00 Byte (0 sectors)
\t     Overwritten: 0.00 Byte (0 sectors)
Average reading speed: 24.60 MB/s
"""

# La misma memoria, pero trucha: dice 64 GB y tiene 8. Casi todo lo que se
# escribió después de los primeros archivos volvió sobrescrito.
F3READ_TRUCHA = """F3 read 8.0

                  SECTORS      ok/corrupted/changed/overwritten
Validating file 1.h2w ... 2097152/        0/      0/      0
Validating file 2.h2w ...       0/        0/      0/ 2097152

  Data OK: 1.00 GB (2097152 sectors)
Data LOST: 1.00 GB (2097152 sectors)
\t       Corrupted: 0.00 Byte (0 sectors)
\tSlightly changed: 0.00 Byte (0 sectors)
\t     Overwritten: 1.00 GB (2097152 sectors)
Average reading speed: 21.30 MB/s
"""


@pytest.fixture
def f3_instalado(monkeypatch):
    """Hace de cuenta que f3write/f3read están en el PATH, sin necesidad de
    que lo estén en la máquina que corre la suite."""
    monkeypatch.setattr(f3_wrapper, "find_binary", lambda name: f"/usr/bin/{name}")


def _stream_de(*salidas, returncodes=None, motivos=None):
    """`stream` falso: entrega la salida canned que corresponda a cada
    llamada (primero f3write, después f3read) línea por línea, igual que
    haría el lector del proceso real."""
    llamadas = []
    salidas = list(salidas)
    returncodes = list(returncodes or [0] * len(salidas))
    motivos = list(motivos or [""] * len(salidas))

    def _stream(args, on_line, **_kwargs):
        indice = len(llamadas)
        llamadas.append(args)
        for linea in salidas[indice].splitlines():
            on_line(linea)
        return returncodes[indice], motivos[indice]

    _stream.llamadas = llamadas
    return _stream


def _memoria(tmp_path):
    punto = tmp_path / "memoria"
    punto.mkdir()
    return punto


# --------------------------------------------------------- Disponibilidad --
def test_missing_binaries_lista_los_que_faltan(monkeypatch):
    monkeypatch.setattr(f3_wrapper, "find_binary",
                        lambda name: "/usr/bin/f3write" if name == "f3write" else None)
    assert f3_wrapper.missing_binaries() == ["f3read"]
    assert f3_wrapper.is_available() is False


def test_check_memory_avisa_si_falta_f3(tmp_path, monkeypatch):
    """Sin f3 no se puede verificar nada, y el mensaje tiene que decir qué
    instalar: es lo que ve el usuario en el banner de la página."""
    monkeypatch.setattr(f3_wrapper, "find_binary", lambda name: None)
    with pytest.raises(f3_wrapper.F3NotFoundError, match="f3"):
        f3_wrapper.check_memory(_memoria(tmp_path))


# ------------------------------------------------------- Camino feliz --
def test_check_memory_memoria_real(tmp_path, f3_instalado):
    punto = _memoria(tmp_path)
    stream = _stream_de(F3WRITE_OK, F3READ_OK)

    resultado = f3_wrapper.check_memory(punto, stream=stream)

    assert resultado.ok is True
    assert resultado.cancelled is False
    assert resultado.error == ""
    # 6144000 sectores * 512 bytes, sacado del conteo exacto de sectores y
    # no del "2.93 GB" redondeado de al lado.
    assert resultado.ok_bytes == 6144000 * 512
    assert resultado.lost_bytes == 0
    assert resultado.write_speed == "12.10 MB/s"
    assert resultado.read_speed == "24.60 MB/s"
    # Primero f3write, después f3read, los dos sobre el punto de montaje.
    assert [c[0] for c in stream.llamadas] == ["f3write", "f3read"]
    assert all(str(punto) in c for c in stream.llamadas)


def test_check_memory_memoria_trucha_no_pasa(tmp_path, f3_instalado):
    """El caso que justifica la función entera: f3 terminó bien (returncode
    0) pero reporta datos perdidos. Eso NO es una verificación exitosa."""
    resultado = f3_wrapper.check_memory(
        _memoria(tmp_path), stream=_stream_de(F3WRITE_OK, F3READ_TRUCHA))

    assert resultado.ok is False
    assert resultado.error == ""       # no es un error: es el veredicto
    assert resultado.lost_bytes == 2097152 * 512
    assert resultado.overwritten_bytes == 2097152 * 512


def test_check_memory_no_pasa_si_no_se_verifico_nada(tmp_path, f3_instalado):
    """f3read que no reporta un solo sector bueno tampoco es un "pasó":
    sin datos verificados no hay nada que afirmar."""
    vacio = "F3 read 8.0\n  Data OK: 0.00 Byte (0 sectors)\nData LOST: 0.00 Byte (0 sectors)\n"
    resultado = f3_wrapper.check_memory(
        _memoria(tmp_path), stream=_stream_de(F3WRITE_OK, vacio))
    assert resultado.ok is False


# ------------------------------------------------------------- Fallos --
def test_check_memory_error_de_f3write_no_corre_f3read(tmp_path, f3_instalado):
    """Si no se pudo escribir (memoria de solo lectura, sin permisos, medio
    con errores), leer no tiene sentido: se corta ahí y el motivo que
    imprimió f3 llega al resultado."""
    salida = ("F3 write 8.0\nFree space: 2.93 GB\n"
              "Creating file 1.h2w ... Write failure: Input/output error\n")
    stream = _stream_de(salida, returncodes=[1])

    resultado = f3_wrapper.check_memory(_memoria(tmp_path), stream=stream)

    assert resultado.ok is False
    assert "Input/output error" in resultado.error
    assert [c[0] for c in stream.llamadas] == ["f3write"]


def test_check_memory_cuelgue_reporta_el_motivo(tmp_path, f3_instalado):
    """Cuando `_run_streaming` mata el proceso por inactividad devuelve un
    motivo: ese texto es el que hay que mostrar, no un "terminó con error"
    genérico."""
    stream = _stream_de(F3WRITE_OK, returncodes=[1],
                        motivos=["`f3` no informó nada durante 30 minutos"])
    resultado = f3_wrapper.check_memory(_memoria(tmp_path), stream=stream)
    assert resultado.ok is False
    assert "30 minutos" in resultado.error


def test_check_memory_cancelada(tmp_path, f3_instalado):
    """Cancelar no es un error ni un veredicto: es una corrida que no
    terminó, y así queda marcada para que la interfaz no muestre ni el
    visto bueno ni la opción de formatear."""
    class _CancelToken:
        cancelled = True

    resultado = f3_wrapper.check_memory(
        _memoria(tmp_path), stream=_stream_de(F3WRITE_OK),
        cancel=_CancelToken())

    assert resultado.cancelled is True
    assert resultado.ok is False


# ------------------------------------------- Limpieza de los .h2w --
def test_check_memory_borra_sus_archivos_de_prueba(tmp_path, f3_instalado):
    punto = _memoria(tmp_path)

    def _stream_que_crea_archivos(args, on_line, **_kwargs):
        if args[0] == "f3write":
            for i in (1, 2, 3):
                (punto / f"{i}.h2w").write_bytes(b"x")
            for linea in F3WRITE_OK.splitlines():
                on_line(linea)
        else:
            for linea in F3READ_OK.splitlines():
                on_line(linea)
        return 0, ""

    resultado = f3_wrapper.check_memory(punto, stream=_stream_que_crea_archivos)

    assert resultado.ok is True
    assert f3_wrapper.test_files(punto) == []


def test_check_memory_no_borra_archivos_del_usuario(tmp_path, f3_instalado):
    """Solo se borra lo que creó ESTA corrida: un `.h2w` que ya estaba (una
    verificación anterior que quedó a medias) y cualquier archivo del
    usuario quedan donde están."""
    punto = _memoria(tmp_path)
    previo = punto / "9.h2w"
    previo.write_bytes(b"de antes")
    foto = punto / "foto.jpg"
    foto.write_bytes(b"jpg")

    def _stream_que_crea(args, on_line, **_kwargs):
        if args[0] == "f3write":
            (punto / "1.h2w").write_bytes(b"x")
        for linea in (F3WRITE_OK if args[0] == "f3write" else F3READ_OK).splitlines():
            on_line(linea)
        return 0, ""

    f3_wrapper.check_memory(punto, stream=_stream_que_crea)

    assert previo.exists()
    assert foto.exists()
    assert not (punto / "1.h2w").exists()


def test_check_memory_limpia_tambien_cuando_f3_falla(tmp_path, f3_instalado):
    """Lo que no puede pasar es dejarle al cliente la memoria llena de
    archivos de prueba porque la verificación se cortó a la mitad."""
    punto = _memoria(tmp_path)

    def _stream_que_falla(args, on_line, **_kwargs):
        (punto / "1.h2w").write_bytes(b"x")
        (punto / "2.h2w").write_bytes(b"x")
        on_line("Creating file 3.h2w ... Write failure: Input/output error")
        return 1, ""

    f3_wrapper.check_memory(punto, stream=_stream_que_falla)
    assert f3_wrapper.test_files(punto) == []


def test_cleanup_test_files_ignora_lo_que_no_es_h2w(tmp_path):
    punto = _memoria(tmp_path)
    (punto / "1.h2w").write_bytes(b"x")
    (punto / "notas.h2w.txt").write_bytes(b"x")
    (punto / "10.h2w").write_bytes(b"x")

    assert f3_wrapper.cleanup_test_files(punto) == 2
    assert [p.name for p in punto.iterdir()] == ["notas.h2w.txt"]


# ------------------------------------------------------------ Progreso --
def test_progreso_avanza_de_escribir_a_leer(tmp_path, f3_instalado):
    """La barra es una sola para las dos pasadas: escribir ocupa la primera
    mitad y leer la segunda, siempre creciendo."""
    avisos = []
    f3_wrapper.check_memory(_memoria(tmp_path),
                            stream=_stream_de(F3WRITE_OK, F3READ_OK),
                            progress_cb=avisos.append)

    escritura = [p for p in avisos if p.phase == f3_wrapper.PHASE_WRITE]
    lectura = [p for p in avisos if p.phase == f3_wrapper.PHASE_READ]
    assert escritura and lectura
    assert all(p.fraction <= 0.5 for p in escritura)
    assert all(p.fraction >= 0.5 for p in lectura)
    assert [p.fraction for p in avisos] == sorted(p.fraction for p in avisos)
    assert all(0.0 <= p.fraction <= 1.0 for p in avisos)


def test_progreso_saca_velocidad_y_tiempo_restante(tmp_path, f3_instalado):
    avisos = []
    f3_wrapper.check_memory(_memoria(tmp_path),
                            stream=_stream_de(F3WRITE_OK, F3READ_OK),
                            progress_cb=avisos.append)
    primero = avisos[0]
    assert primero.speed == "12.30 MB/s"
    assert primero.eta == "3:21"


# ----------------------------------------- Lectura real de la salida --
def test_run_streaming_entrega_lineas_cortadas_por_retorno_de_carro():
    """El detalle del que depende toda la barra de progreso: f3 reescribe
    la MISMA línea con `\\r` sin saltar de línea, así que hay que cortar
    ahí. Con un proceso de verdad (no un mock) para ejercitar la lectura
    incremental completa."""
    guion = (
        "import sys, time\n"
        "sys.stdout.write('Creating file 1.h2w ... 10.00% -- 1.00 MB/s -- 0:10\\r')\n"
        "sys.stdout.flush()\n"
        "sys.stdout.write('Creating file 1.h2w ... OK!\\n')\n"
        "sys.stdout.write('Free space: 0.00 Byte\\n')\n"
    )
    lineas = []
    rc, motivo = f3_wrapper._run_streaming(
        [sys.executable, "-c", guion], lineas.append)

    assert rc == 0
    assert motivo == ""
    assert lineas == [
        "Creating file 1.h2w ... 10.00% -- 1.00 MB/s -- 0:10",
        "Creating file 1.h2w ... OK!",
        "Free space: 0.00 Byte",
    ]


def test_run_streaming_devuelve_el_returncode_y_captura_stderr():
    """stderr se mezcla con stdout a propósito: el motivo del fallo de f3
    tiene que llegar al mismo parseo que el resto."""
    guion = ("import sys\n"
             "sys.stderr.write('f3write: Permission denied\\n')\n"
             "sys.exit(1)\n")
    lineas = []
    rc, _motivo = f3_wrapper._run_streaming(
        [sys.executable, "-c", guion], lineas.append)

    assert rc == 1
    assert "Permission denied" in "\n".join(lineas)


def test_run_streaming_corta_por_inactividad_y_mata_el_proceso():
    """La red de seguridad: un f3 que no imprime nada durante el timeout se
    da por colgado y se mata, en vez de dejar la operación (y la memoria
    del cliente) tomadas para siempre."""
    guion = "import time\ntime.sleep(60)\n"
    lineas = []
    rc, motivo = f3_wrapper._run_streaming(
        [sys.executable, "-c", guion], lineas.append, inactivity_timeout=0.5)

    assert motivo != ""
    assert rc != 0


def test_run_streaming_no_deja_procesos_vivos_al_cortar():
    """Que el proceso muera de verdad, no que se lo dé por muerto: si
    quedara vivo seguiría escribiendo en la memoria después de que la
    interfaz dijo que la operación terminó."""
    guion = "import time\ntime.sleep(60)\n"
    procesos = []

    popen_real = subprocess.Popen

    def _spy(*args, **kwargs):
        proc = popen_real(*args, **kwargs)
        procesos.append(proc)
        return proc

    original = f3_wrapper.subprocess.Popen
    f3_wrapper.subprocess.Popen = _spy
    try:
        f3_wrapper._run_streaming([sys.executable, "-c", guion],
                                  lambda _l: None, inactivity_timeout=0.5)
    finally:
        f3_wrapper.subprocess.Popen = original

    assert procesos and procesos[0].poll() is not None
