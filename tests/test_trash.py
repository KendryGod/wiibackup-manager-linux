"""Papelera del sistema.

Usa Gio (no GTK): no hace falta un display, pero sí que el entorno tenga
una papelera utilizable, que en CI no siempre pasa. Los tests que borran
de verdad se saltan solos si `can_trash` dice que no.
"""
from __future__ import annotations

import pytest

from wiibackup_manager import trash


def test_can_trash_de_un_archivo_que_no_existe(tmp_path):
    """Ante la duda, False: un False de más solo hace que se avise "esto
    se borra definitivamente"; un True de más prometería una papelera que
    después no está."""
    assert trash.can_trash(tmp_path / "no-existe.iso") is False


def test_delete_permanently(tmp_path):
    archivo = tmp_path / "juego.iso"
    archivo.write_bytes(b"x")
    trash.delete_permanently(archivo)
    assert not archivo.exists()


def test_delete_permanently_de_algo_que_no_esta_levanta(tmp_path):
    with pytest.raises(OSError):
        trash.delete_permanently(tmp_path / "no-existe.iso")


def test_trash_unsupported_es_un_oserror():
    """Hereda de OSError a propósito: los `except OSError` que ya rodean
    las operaciones de archivo lo atrapan sin cambios."""
    assert issubclass(trash.TrashUnsupported, OSError)


def test_send_to_trash_mueve_el_archivo(tmp_path):
    archivo = tmp_path / "juego.iso"
    archivo.write_bytes(b"contenido")
    if not trash.can_trash(archivo):
        pytest.skip("este filesystem no tiene papelera (habitual en CI)")
    trash.send_to_trash(archivo)
    assert not archivo.exists()


def test_send_to_trash_donde_no_hay_papelera_levanta_y_no_borra(tmp_path):
    """Lo importante: cuando no hay papelera NO se cae en borrar el
    archivo igual. Esa decisión vuelve a la interfaz, que pregunta de
    nuevo diciendo que es definitivo."""
    archivo = tmp_path / "juego.iso"
    archivo.write_bytes(b"contenido")
    if trash.can_trash(archivo):
        pytest.skip("este filesystem sí tiene papelera")
    with pytest.raises(OSError):
        trash.send_to_trash(archivo)
    assert archivo.exists(), "el archivo no se puede borrar como consuelo"
