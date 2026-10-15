"""Una salida de `wit` que no es UTF-8 no rompe nada.

`_run_cancellable` (y con él `_run`: `identify`, `disc_console`,
`iso_size_bytes`, `verify`, `wit CMP`) lanzaba `wit` en modo texto sin
errores tolerantes. Medido con `wit` v3.05a: `wit LIST --sections` imprime
la línea `name=` con los bytes crudos de la cabecera del disco, y con un
título japonés son Shift-JIS, no UTF-8. `communicate()` levantaba
`UnicodeDecodeError` y, como el escaneo solo atrapaba `WitNotFoundError`,
fallaba el escaneo entero de la biblioteca. `_run_with_progress` ya
decodificaba con "replace"; ahora `_run_cancellable` también.

Y el corte por tiempo: en modo texto, lo que trae `TimeoutExpired` viene en
BYTES, y `_timeout_result` lo sumaba a un `str`: un `wit` que alcanzó a
escribir algo y se colgó terminaba en `TypeError`.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from wiibackup_manager import wit_wrapper

SALIDA = b"[disc-0]\nid=RMCJ01\nname=\x83\x7d\x83\x8a\xff\n"


def _python(codigo: str) -> tuple:
    return sys.executable, "-c", codigo


def test_una_salida_no_utf8_se_lee_igual():
    r = wit_wrapper._run_cancellable(
        *_python(f"import sys; sys.stdout.buffer.write({SALIDA!r})"))

    assert r.returncode == 0
    assert "id=RMCJ01" in r.stdout and "�" in r.stdout


def test_un_stderr_no_utf8_tambien():
    r = wit_wrapper._run_cancellable(
        *_python("import sys; sys.stderr.buffer.write(b'error \\xff\\n'); sys.exit(4)"))

    assert r.returncode == 4 and "error" in r.stderr


def test_un_corte_por_tiempo_con_salida_a_medias_devuelve_texto():
    r = wit_wrapper._run_cancellable(
        *_python("import sys, time; sys.stdout.buffer.write(b'id=RMCJ01 \\xff\\n');"
                 "sys.stderr.buffer.write(b'a medias \\xfe\\n');"
                 "sys.stdout.flush(); sys.stderr.flush(); time.sleep(30)"),
        timeout=1)

    assert r.returncode == wit_wrapper.TIMEOUT_RETURNCODE
    assert isinstance(r.stdout, str) and isinstance(r.stderr, str)
    assert "id=RMCJ01" in r.stdout and "a medias" in r.stderr


def test_identify_con_un_titulo_shift_jis(tmp_path):
    """`identify` de punta a punta, con un `wit` de mentira que imprime lo
    mismo que el real con un disco japonés."""
    wit = tmp_path / "wit"
    wit.write_text(f"#!{sys.executable}\nimport sys\n"
                   "sys.stdout.buffer.write(b'[disc-0]\\nid=RMCJ01\\n"
                   "disctype=2 Wii\\nname=\\x83\\x7d\\x83\\x8a\\n')\n")
    wit.chmod(0o755)

    info = wit_wrapper.identify(tmp_path / "juego.wbfs", str(wit))

    assert info is not None and info.game_id == "RMCJ01"


@pytest.mark.skipif(shutil.which("wit") is None, reason="wit no está instalado")
def test_con_wit_real_un_disco_con_titulo_shift_jis(tmp_path):
    from test_convertir_gamecube_ciso_iso import _imagen_gc

    iso = tmp_path / "juego.iso"
    _imagen_gc(iso, game_id=b"GTSJ01", nombre="マリオカート".encode("shift_jis"))
    crudo = subprocess.run(["wit", "LIST", "--sections", str(iso)],
                           capture_output=True).stdout
    assert b"name=\x83" in crudo, "wit dejó de imprimir el nombre crudo: revisar"

    info = wit_wrapper.identify(iso)

    assert info is not None and info.game_id == "GTSJ01" and info.console == "gc"
