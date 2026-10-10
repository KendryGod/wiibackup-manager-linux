"""Reemplazar un juego que ya está en la unidad y que la copia se corte.

El caso de la prueba de hardware: Super Smash Bros. Brawl (WBFS partido en
`RSBE01.wbfs` + `RSBE01.wbf1`) a un pendrive que ya lo tenía, y el
pendrive se desenchufa a mitad de `wit`. Lo que se fija acá:

- Reemplazar NUNCA pasa por la papelera: el original se aparta con un
  rename en la misma carpeta (`DestinationGuard`), lo nuevo se escribe al
  lado, y el respaldo se borra recién cuando lo nuevo está completo y
  bajado a la unidad. Si algo falla con la unidad presente, vuelve a su
  lugar.
- Si la unidad desaparece, no hay dónde devolverlo: el respaldo queda en
  la unidad y la cola tiene que decir eso -"tu juego quedó guardado"-, no
  un error con rutas internas.

La unidad que "desaparece" se simula con las mismas señales que mira la
app de verdad: los renames/borrados sobre ella fallan con EIO y su punto
de montaje deja de responder (`drives._responde`).
"""
from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path

import pytest

from wiibackup_manager import (atomicfs, drives, fileops, library_ops,
                               transfer_plan, trash)
from wiibackup_manager.game_model import Game
from wiibackup_manager.operations import OperationManager
from wiibackup_manager.queue_manager import JobStatus, TransferQueue

ORIGINAL = {"RSBE01.wbfs": b"ORIGINAL parte 0", "RSBE01.wbf1": b"ORIGINAL parte 1"}


def sync_dispatch(func, *args):
    func(*args)


@pytest.fixture
def usb(tmp_path):
    raiz = tmp_path / "WII_USB"
    carpeta = raiz / "wbfs" / "RSBE01"
    carpeta.mkdir(parents=True)
    for nombre, datos in ORIGINAL.items():
        (carpeta / nombre).write_bytes(datos)
    return raiz


@pytest.fixture
def brawl(tmp_path):
    src = tmp_path / "brawl.wbfs"
    src.write_bytes(b"x" * 64)
    return Game(path=src, game_id="RSBE01", title="Super Smash Bros. Brawl",
                fmt="WBFS", size_bytes=8_000_000_000, identified_by="wbfs",
                console="wii")


@pytest.fixture
def sin_papelera(monkeypatch):
    """Si algo del camino de reemplazo intentara usar la papelera, el test
    lo ve: la papelera de una unidad FAT ni siquiera libera espacio."""
    def prohibido(*_a, **_k):
        raise AssertionError("reemplazar un juego no puede usar la papelera")
    monkeypatch.setattr(trash, "send_to_trash", prohibido)
    monkeypatch.setattr(trash, "delete_permanently", prohibido)


class UnidadQueSeVa:
    """Hace que la unidad "se desenchufe" cuando se llama `desenchufar()`."""

    def __init__(self, monkeypatch, raiz: Path):
        self.raiz = str(raiz)
        self.ida = False
        real_replace, real_unlink = os.replace, os.unlink
        real_responde = drives._responde

        def replace(a, b, *k, **kw):
            if self.ida and self.raiz in str(a):
                raise OSError(errno.EIO, "Input/output error")
            return real_replace(a, b, *k, **kw)

        def unlink(p, *k, **kw):
            if self.ida and self.raiz in str(p):
                raise OSError(errno.EIO, "Input/output error")
            return real_unlink(p, *k, **kw)

        def responde(path):
            if self.ida and self.raiz in str(path):
                return False
            return real_responde(path)

        monkeypatch.setattr(atomicfs.os, "replace", replace)
        monkeypatch.setattr(os, "unlink", unlink)
        monkeypatch.setattr(drives, "_responde", responde)

    def desenchufar(self):
        self.ida = True

    def reconectar(self):
        self.ida = False


def _wit_falso(monkeypatch, al_escribir=None, codigo=0):
    """`wit COPY` de mentira: escribe su temporal oculto como el de verdad
    (`.RSBE01.wbfs.XXXX.tmp`), y si sale bien lo renombra a las partes
    finales."""
    monkeypatch.setattr(library_ops.wit_wrapper, "is_available", lambda _b: True)
    monkeypatch.setattr(library_ops.drives, "needs_wbfs_split", lambda _p: True)
    monkeypatch.setattr(library_ops.drives, "is_fat_filesystem", lambda _p: True)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)

    def convert(src, dest, fmt, binary, **_kw):
        dest = Path(dest)
        tmp = dest.parent / f".{dest.name}.LtnLTy.tmp"
        tmp.write_bytes(b"juego nuevo a medias")
        if al_escribir is not None:
            al_escribir()
        if codigo != 0:
            return subprocess.CompletedProcess([], codigo, "",
                                               "wit: write error: Input/output error")
        os.replace(tmp, dest)
        dest.with_suffix(".wbf1").write_bytes(b"juego nuevo parte 1")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(library_ops.wit_wrapper, "convert", convert)


def _correr(juego, raiz, **kw):
    cola = TransferQueue(OperationManager(), dispatch=sync_dispatch)
    job = cola.add_jobs([juego], raiz, overwrite=True, **kw)[0]
    import time
    t0 = time.monotonic()
    while not job.is_final and time.monotonic() - t0 < 10:
        time.sleep(0.01)
    cola.shutdown(wait=5)
    return job


def _contenido(carpeta: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(carpeta.iterdir())}


# ================================================ La unidad desaparece --
def test_desenchufar_a_mitad_de_un_reemplazo_lo_dice_y_guarda_el_original(
        usb, brawl, monkeypatch, sin_papelera):
    unidad = UnidadQueSeVa(monkeypatch, usb)
    _wit_falso(monkeypatch, al_escribir=unidad.desenchufar, codigo=1)

    job = _correr(brawl, usb)

    assert job.status is JobStatus.DEVICE_DISCONNECTED, job.error_msg
    assert "desconectada" in job.error_msg
    assert "original quedó guardado" in job.error_msg
    assert "restaurarlo" in job.error_msg
    # Nada de rutas internas ni de "no se pudo restaurar".
    assert "respaldo-" not in job.error_msg
    assert "no se pudo restaurar" not in job.error_msg.lower()

    # Al reconectar: el original, ENTERO, sigue en la unidad (apartado con
    # nombre oculto, en la misma carpeta) junto al temporal cortado. Nada
    # fue a parar a una papelera.
    unidad.reconectar()
    carpeta = usb / "wbfs" / "RSBE01"
    respaldos = {n: d for n, d in _contenido(carpeta).items() if ".respaldo-" in n}
    assert sorted(d for d in respaldos.values()) == sorted(ORIGINAL.values())
    assert any(n.endswith(".tmp") for n in _contenido(carpeta))
    assert not list(usb.glob(".Trash-*"))


# ============================== Las garantías del reemplazo, sin cortes --
def test_un_reemplazo_que_falla_con_la_unidad_presente_devuelve_el_original(
        usb, brawl, monkeypatch, sin_papelera):
    _wit_falso(monkeypatch, codigo=1)
    job = _correr(brawl, usb)
    assert job.status is JobStatus.ERROR
    assert _contenido(usb / "wbfs" / "RSBE01") == ORIGINAL


def test_el_respaldo_se_borra_recien_con_lo_nuevo_en_la_unidad(
        usb, brawl, monkeypatch, sin_papelera):
    """Orden: apartar el original (rename en la misma carpeta) → escribir
    lo nuevo → bajarlo a la unidad → recién ahí borrar el respaldo."""
    carpeta = usb / "wbfs" / "RSBE01"
    eventos = []

    def al_escribir():
        nombres = [p.name for p in carpeta.iterdir()]
        # Mientras `wit` escribe, el original está apartado AL LADO, con
        # nombre oculto, y completo.
        assert sorted(n for n in nombres if ".respaldo-" in n) == [
            ".RSBE01.wbf1.respaldo-%d" % os.getpid(),
            ".RSBE01.wbfs.respaldo-%d" % os.getpid()]
        eventos.append("wit escribe")

    _wit_falso(monkeypatch, al_escribir=al_escribir)
    real_flush = fileops.flush_and_drop_cache

    def flush(paths, **kw):
        assert any(".respaldo-" in p.name for p in carpeta.iterdir()), \
            "el respaldo se borró antes de bajar lo nuevo"
        eventos.append("bajar a la unidad")
        return real_flush(paths, **kw)

    monkeypatch.setattr(fileops, "flush_and_drop_cache", flush)
    job = _correr(brawl, usb)

    assert job.status is JobStatus.DONE, job.error_msg
    assert eventos == ["wit escribe", "bajar a la unidad"]
    assert _contenido(carpeta) == {"RSBE01.wbf1": b"juego nuevo parte 1",
                                   "RSBE01.wbfs": b"juego nuevo a medias"}


def test_el_mensaje_de_la_biblioteca_tambien_dice_que_el_original_quedo(
        usb, brawl, monkeypatch, sin_papelera):
    """El envío desde la Biblioteca comparte el criterio con la cola."""
    unidad = UnidadQueSeVa(monkeypatch, usb)
    _wit_falso(monkeypatch, al_escribir=unidad.desenchufar, codigo=1)
    with pytest.raises(library_ops.RollbackFailedError) as info:
        library_ops.send_to_wbfs_drive(brawl, usb, overwrite=True)
    assert library_ops.replace_cut_by_disconnect(info.value, usb)
    assert "original quedó guardado" in library_ops.original_kept_message()


# ==================== El punto de montaje queda como carpeta que responde --
def test_desconexion_con_el_punto_de_montaje_vacio_igual_se_reconoce(
        usb, brawl, monkeypatch, sin_papelera):
    """Un punto de montaje creado a mano (`/mnt/usb`) sigue existiendo como
    carpeta vacía cuando la unidad se va: la carpeta responde y el error de
    `wit` no trae errno. La única señal es que dejó de ser punto de
    montaje, y la rama del reemplazo cortado tiene que mirarla igual que
    la rama de los demás errores."""
    ida = {"si": False}
    real_replace, real_es_montaje = os.replace, drives.is_mount_point

    def replace(a, b, *k, **kw):
        if ida["si"] and str(usb) in str(a):
            raise OSError(errno.ENOENT, "No such file or directory")
        return real_replace(a, b, *k, **kw)

    def es_montaje(path):
        if Path(path) == usb:
            return not ida["si"]
        return real_es_montaje(path)

    monkeypatch.setattr(atomicfs.os, "replace", replace)
    monkeypatch.setattr(drives, "is_mount_point", es_montaje)
    _wit_falso(monkeypatch, al_escribir=lambda: ida.__setitem__("si", True), codigo=1)

    job = _correr(brawl, usb)

    assert job.status is JobStatus.DEVICE_DISCONNECTED, job.error_msg
    assert job.error_msg == library_ops.original_kept_message()
