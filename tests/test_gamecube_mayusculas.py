"""GameCube: el mismo disco con la extensión (o el nombre) en mayúsculas.

`gc_group` armaba las variantes solo en minúsculas (`game.iso`,
`game.ciso`) y preguntaba `exists()`. En un sistema de archivos que
distingue mayúsculas (ext4, el `tmp_path` de estas pruebas) una carpeta
con `game.CISO` o `GAME.ISO` -como las deja otra herramienta- no se veía:
no había confirmación, la cola no lo salteaba, `DestinationGuard` no
apartaba el original y quedaban dos archivos. FAT32 no distingue
mayúsculas al buscar, así que ahí no pasaba.
"""
from __future__ import annotations

import errno
import time
from pathlib import Path

import pytest

from wiibackup_manager import (library_ops, recovery_service, transfer_plan)
from wiibackup_manager.game_model import Game
from wiibackup_manager.operations import OperationManager
from wiibackup_manager.queue_manager import JobStatus, TransferQueue

CARPETA = "Zelda OoT MQ [D43E01]"


def _juego(tmp_path, ext=".iso", disco=0, datos=b"juego nuevo"):
    src = tmp_path / "origen" / f"zelda{disco}{ext}"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(datos)
    return Game(path=src, game_id="D43E01", title="Zelda OoT MQ",
                fmt=ext.lstrip(".").upper(), size_bytes=len(datos),
                identified_by="wit", console="gc", disc_number=disco)


@pytest.fixture
def usb(tmp_path, monkeypatch):
    raiz = tmp_path / "USB"
    carpeta = raiz / "games" / CARPETA
    carpeta.mkdir(parents=True)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)
    return raiz, carpeta


def _contenido(c: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(c.iterdir())}


@pytest.mark.parametrize("existente, entrante", [
    ("game.CISO", ".iso"),
    ("GAME.ISO", ".ciso"),
    ("Game.Ciso", ".iso"),
])
def test_el_mismo_disco_en_mayusculas_cuenta_como_ya_presente(
        usb, tmp_path, existente, entrante):
    raiz, carpeta = usb
    (carpeta / existente).write_bytes(b"viejo")
    juego = _juego(tmp_path, ext=entrante)

    assert transfer_plan.existing_destination(juego, raiz) == carpeta / existente
    with pytest.raises(library_ops.DestinationExistsError):
        library_ops.send_to_wbfs_drive(juego, raiz)
    assert _contenido(carpeta) == {existente: b"viejo"}


def test_reemplazar_aparta_la_variante_en_mayusculas_y_deja_un_archivo(usb, tmp_path):
    raiz, carpeta = usb
    (carpeta / "game.CISO").write_bytes(b"viejo")

    library_ops.send_to_wbfs_drive(_juego(tmp_path), raiz, overwrite=True)

    assert _contenido(carpeta) == {"game.iso": b"juego nuevo"}


def test_disco_2_en_mayusculas(usb, tmp_path):
    raiz, carpeta = usb
    (carpeta / "game.iso").write_bytes(b"disco 1")
    (carpeta / "DISC2.CISO").write_bytes(b"disco 2 viejo")

    library_ops.send_to_wbfs_drive(_juego(tmp_path, disco=1), raiz, overwrite=True)

    assert _contenido(carpeta) == {"disc2.iso": b"juego nuevo", "game.iso": b"disco 1"}


def test_si_la_copia_falla_vuelve_la_variante_en_mayusculas(usb, tmp_path, monkeypatch):
    raiz, carpeta = usb
    (carpeta / "GAME.ISO").write_bytes(b"viejo")

    def copia_que_falla(src, dest, cb, cancel=None):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(library_ops, "_copy_with_progress", copia_que_falla)
    with pytest.raises(OSError):
        library_ops.send_to_wbfs_drive(_juego(tmp_path, ext=".ciso"), raiz,
                                       overwrite=True)

    assert _contenido(carpeta) == {"GAME.ISO": b"viejo"}


def test_otros_nombres_parecidos_no_cuentan(usb, tmp_path):
    """Solo el mismo nombre de disco (sin importar mayúsculas) con una
    extensión de GameCube: ni `game2.ciso`, ni `game.iso.bak`, ni
    `game.wbfs`."""
    raiz, carpeta = usb
    for nombre in ("game2.ciso", "game.iso.bak", "game.wbfs", "mygame.ISO"):
        (carpeta / nombre).write_bytes(b"otro")

    assert transfer_plan.existing_destination(_juego(tmp_path), raiz) is None


def test_la_cola_lo_saltea(usb, tmp_path):
    raiz, carpeta = usb
    (carpeta / "game.CISO").write_bytes(b"viejo")
    cola = TransferQueue(OperationManager(), dispatch=lambda f, *a: f(*a))
    job = cola.add_jobs([_juego(tmp_path)], raiz)[0]
    limite = time.monotonic() + 10
    while not job.is_final and time.monotonic() < limite:
        time.sleep(0.01)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.SKIPPED, job.error_msg
    assert _contenido(carpeta) == {"game.CISO": b"viejo"}


def test_recovery_devuelve_el_original_en_mayusculas_y_saca_la_otra_extension(usb):
    """Murió la app con el original (`GAME.ISO`, de otra herramienta)
    apartado y el nuevo (`game.ciso`, que la app escribe en minúsculas) ya
    con su nombre: restaurar deja solo el original."""
    _raiz, carpeta = usb
    (carpeta / ".GAME.ISO.respaldo-999999999").write_bytes(b"original")
    (carpeta / "game.ciso").write_bytes(b"nuevo a medias")
    [resto] = recovery_service.scan([carpeta.parent.parent], ops=OperationManager())
    assert recovery_service.restore_overwrites(resto) is True

    recovery_service.restore(resto)

    assert _contenido(carpeta) == {"GAME.ISO": b"original"}


def test_el_mismo_archivo_con_dos_nombres_cuenta_una_vez(usb, tmp_path):
    """Lo que pasa en FAT32, que no distingue mayúsculas al buscar: `dest`
    existe y el listado lo devuelve con el nombre que tiene en disco. Acá
    se simula con un hardlink: es UN archivo, no dos variantes."""
    _raiz, carpeta = usb
    (carpeta / "game.iso").write_bytes(b"uno solo")
    (carpeta / "GAME.ISO").hardlink_to(carpeta / "game.iso")

    assert transfer_plan.gc_group(carpeta / "game.iso") == [carpeta / "game.iso"]
