"""`recovery_service.restore_overwrites` mira el disco en el momento.

La lista del Recovery Manager es la del último escaneo (al arrancar o al
montar una unidad): entre la foto y el clic en "Restaurar", lo que había
en el nombre original pudo irse -y entonces no hay nada que confirmar- o
pudo aparecer algo -y entonces restaurar lo pisaría sin preguntar-.
"""
from __future__ import annotations

from wiibackup_manager import library_ops, recovery_service
from wiibackup_manager.operations import OperationManager

PID_MUERTO = 999_999_999


def _respaldo_de_juego(tmp_path, *, con_original: bool):
    carpeta = tmp_path / "wbfs" / "RSBE01"
    carpeta.mkdir(parents=True)
    (carpeta / f".RSBE01.wbfs.{library_ops.MARCA_RESPALDO}-{PID_MUERTO}").write_bytes(
        b"ORIGINAL")
    if con_original:
        (carpeta / "RSBE01.wbfs").write_bytes(b"cortado")
    [resto] = recovery_service.scan([tmp_path], ops=OperationManager())
    return carpeta, resto


def test_si_lo_que_habia_se_fue_despues_del_escaneo_no_pisa_nada(tmp_path):
    carpeta, resto = _respaldo_de_juego(tmp_path, con_original=True)
    assert resto.original_exists is True
    (carpeta / "RSBE01.wbfs").unlink()

    assert recovery_service.restore_overwrites(resto) is False


def test_si_algo_aparecio_despues_del_escaneo_si_pisa(tmp_path):
    carpeta, resto = _respaldo_de_juego(tmp_path, con_original=False)
    assert resto.original_exists is False
    (carpeta / "RSBE01.wbfs").write_bytes(b"juego copiado despues")

    assert recovery_service.restore_overwrites(resto) is True


def test_una_parte_que_aparecio_tambien_cuenta(tmp_path):
    """Un juego dividido: alcanza con que esté ocupado el nombre de UNA de
    las partes que se van a restaurar."""
    carpeta = tmp_path / "wbfs" / "RSBE01"
    carpeta.mkdir(parents=True)
    for nombre in ("RSBE01.wbfs", "RSBE01.wbf1"):
        (carpeta / f".{nombre}.{library_ops.MARCA_RESPALDO}-{PID_MUERTO}").write_bytes(b"x")
    [resto] = recovery_service.scan([tmp_path], ops=OperationManager())
    (carpeta / "RSBE01.wbf1").write_bytes(b"parte nueva")

    assert recovery_service.restore_overwrites(resto) is True
