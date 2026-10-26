"""GameCube: `game.iso` y `game.ciso` son el MISMO juego en la carpeta.

Nintendont arranca un juego por su carpeta (`games/<Título [ID6]>/`), y
`gc_dest_path` conserva la extensión del archivo entrante. Si en la
carpeta ya estaba `game.ciso` y se manda el mismo juego en `.iso`, el
chequeo "¿ya existe?" miraba solo `game.iso`: no preguntaba, no lo
salteaba, y la carpeta terminaba con los dos archivos. Lo mismo con el
disco 2 (`disc2.iso` frente a `disc2.ciso`).
"""
from __future__ import annotations

import errno
import time
import types
from pathlib import Path

import pytest

from wiibackup_manager import (library_ops, recovery_service, transfer_plan,
                               wit_wrapper)
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


# ---------------------------------------------- Envío (send_to_wbfs_drive) --
def test_sin_reemplazar_la_otra_extension_cuenta_como_que_ya_esta(usb, tmp_path):
    raiz, carpeta = usb
    (carpeta / "game.ciso").write_bytes(b"viejo ciso")

    with pytest.raises(library_ops.DestinationExistsError):
        library_ops.send_to_wbfs_drive(_juego(tmp_path), raiz)

    assert _contenido(carpeta) == {"game.ciso": b"viejo ciso"}


def test_reemplazando_queda_un_solo_archivo(usb, tmp_path):
    raiz, carpeta = usb
    (carpeta / "game.ciso").write_bytes(b"viejo ciso")

    library_ops.send_to_wbfs_drive(_juego(tmp_path), raiz, overwrite=True)

    assert _contenido(carpeta) == {"game.iso": b"juego nuevo"}


def test_disco_2_reemplaza_su_variante_y_no_toca_el_disco_1(usb, tmp_path):
    raiz, carpeta = usb
    (carpeta / "game.iso").write_bytes(b"disco 1")
    (carpeta / "disc2.ciso").write_bytes(b"disco 2 viejo")

    library_ops.send_to_wbfs_drive(_juego(tmp_path, disco=1), raiz, overwrite=True)

    assert _contenido(carpeta) == {"disc2.iso": b"juego nuevo", "game.iso": b"disco 1"}


def test_si_la_copia_falla_la_variante_vieja_vuelve(usb, tmp_path, monkeypatch):
    raiz, carpeta = usb
    (carpeta / "game.ciso").write_bytes(b"viejo ciso")

    def copia_que_falla(src, dest, cb, cancel=None, digest=None):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(library_ops, "_copy_with_progress", copia_que_falla)
    with pytest.raises(OSError):
        library_ops.send_to_wbfs_drive(_juego(tmp_path), raiz, overwrite=True)

    assert _contenido(carpeta) == {"game.ciso": b"viejo ciso"}


def test_si_se_cancela_la_variante_vieja_vuelve(usb, tmp_path):
    raiz, carpeta = usb
    (carpeta / "game.ciso").write_bytes(b"viejo ciso")
    token = wit_wrapper.CancellationToken()
    token.cancel()

    with pytest.raises(wit_wrapper.OperationCancelled):
        library_ops.send_to_wbfs_drive(_juego(tmp_path), raiz, overwrite=True,
                                       cancel=token)

    assert _contenido(carpeta) == {"game.ciso": b"viejo ciso"}


def test_la_variante_vieja_se_borra_recien_con_lo_nuevo_en_disco(
        usb, tmp_path, monkeypatch):
    """Mientras se copia, la variante vieja está apartada (no borrada): si
    el proceso muere ahí, el Recovery Manager la encuentra y la devuelve."""
    raiz, carpeta = usb
    (carpeta / "game.ciso").write_bytes(b"viejo ciso")
    real = library_ops._copy_with_progress
    vistos = {}

    def copia_espiada(src, dest, cb, cancel=None, digest=None):
        vistos["durante"] = sorted(p.name for p in carpeta.iterdir())
        real(src, dest, cb, cancel, digest=digest)
        vistos["al_terminar"] = sorted(p.name for p in carpeta.iterdir())

    monkeypatch.setattr(library_ops, "_copy_with_progress", copia_espiada)
    library_ops.send_to_wbfs_drive(_juego(tmp_path), raiz, overwrite=True)

    assert any(n.startswith(".game.ciso.respaldo-") for n in vistos["durante"])
    assert "game.ciso" not in vistos["durante"]
    assert "game.iso" in vistos["al_terminar"]


def test_recovery_devuelve_la_variante_vieja_y_saca_la_nueva(usb, tmp_path):
    """El proceso murió con la variante vieja apartada y la nueva ya con su
    nombre final: restaurar deja UN solo archivo, el viejo."""
    _raiz, carpeta = usb
    (carpeta / ".game.ciso.respaldo-999999999").write_bytes(b"viejo ciso")
    (carpeta / "game.iso").write_bytes(b"nuevo a medias")
    [resto] = recovery_service.scan([carpeta.parent.parent], ops=OperationManager())
    assert resto.kind is recovery_service.LeftoverKind.BACKUP
    assert recovery_service.restore_overwrites(resto) is True

    recovery_service.restore(resto)

    assert _contenido(carpeta) == {"game.ciso": b"viejo ciso"}


# ---------------------------------------------------------------- Cola --
def test_la_cola_en_lote_lo_saltea_como_ya_presente(usb, tmp_path):
    raiz, carpeta = usb
    (carpeta / "game.ciso").write_bytes(b"viejo ciso")
    cola = TransferQueue(OperationManager(), dispatch=lambda f, *a: f(*a))
    job = cola.add_jobs([_juego(tmp_path)], raiz)[0]
    limite = time.monotonic() + 10
    while not job.is_final and time.monotonic() < limite:
        time.sleep(0.01)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.SKIPPED, job.error_msg
    assert _contenido(carpeta) == {"game.ciso": b"viejo ciso"}


# --------------------------------------- Confirmación de un solo juego --
def test_transferir_un_juego_pregunta_si_esta_la_otra_extension(
        usb, tmp_path, monkeypatch):
    from wiibackup_manager.widgets import transfer_view
    raiz, carpeta = usb
    (carpeta / "game.ciso").write_bytes(b"viejo ciso")
    juego = _juego(tmp_path)
    preguntas, encolados = [], []
    monkeypatch.setattr(transfer_view.gtk_helpers, "confirm_overwrite",
                        lambda parent, body, accion: preguntas.append(body))
    vista = types.SimpleNamespace(
        _dest_path=raiz, _selected_games=lambda: [juego],
        _show_toast=lambda _m: None, get_root=lambda: None,
        _enqueue=lambda sel, root, overwrite=False: encolados.append(overwrite))

    transfer_view.TransferView._on_transfer_clicked(vista)

    assert encolados == [], "encoló sin preguntar"
    assert len(preguntas) == 1 and "game.ciso" in preguntas[0]


def test_enviar_desde_la_biblioteca_pregunta_si_esta_la_otra_extension(
        usb, tmp_path, monkeypatch):
    from wiibackup_manager import window
    raiz, carpeta = usb
    (carpeta / "game.ciso").write_bytes(b"viejo ciso")
    juego = _juego(tmp_path)
    preguntas, enviados = [], []
    monkeypatch.setattr(window.gtk_helpers, "confirm_overwrite",
                        lambda parent, body, accion: preguntas.append(body))
    ventana = types.SimpleNamespace(
        settings=types.SimpleNamespace(wit_binary="wit"),
        _show_toast=lambda _m: None,
        _start_send=lambda games, root, overwrite=False: enviados.append(overwrite))
    dialogo = types.SimpleNamespace(
        select_folder_finish=lambda _r: types.SimpleNamespace(get_path=lambda: str(raiz)))

    window.WiiBackupWindow._on_batch_send_folder_chosen(ventana, dialogo, None, [juego])

    assert enviados == [], "envió sin preguntar"
    assert len(preguntas) == 1 and "game.ciso" in preguntas[0]


# ------------------------------- Misma ID6, carpeta con otro título --
# El caso real en hardware: el CISO tomó el título de `wit` y el ISO el
# nombre interno del disco, así que `gc_dest_path` armaba otra carpeta y
# el mismo juego quedaba dos veces. La carpeta de un juego de GameCube se
# reconoce por el `[ID6]` del final, no por el título.
CARPETA_CISO = "The Legend of Zelda Ocarina of Time  Master Quest [D43E01]"


def _juego_con_otro_titulo(tmp_path, ext=".iso", disco=0):
    juego = _juego(tmp_path, ext=ext, disco=disco)
    return Game(path=juego.path, game_id=juego.game_id,
                title="ZELDA OCARINA MULTI PACK", fmt=juego.fmt,
                size_bytes=juego.size_bytes, identified_by="iso",
                console="gc", disc_number=disco)


@pytest.fixture
def usb_otro_titulo(tmp_path, monkeypatch):
    raiz = tmp_path / "USB2"
    carpeta = raiz / "games" / CARPETA_CISO
    carpeta.mkdir(parents=True)
    (carpeta / "game.ciso").write_bytes(b"viejo ciso")
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: 10 ** 12)
    return raiz, carpeta


def _carpetas(raiz):
    return sorted(p.name for p in (raiz / "games").iterdir())


def test_con_otro_titulo_el_juego_ya_esta(usb_otro_titulo, tmp_path):
    raiz, carpeta = usb_otro_titulo
    juego = _juego_con_otro_titulo(tmp_path)
    assert transfer_plan.existing_destination(juego, raiz) == carpeta / "game.ciso"
    with pytest.raises(library_ops.DestinationExistsError):
        library_ops.send_to_wbfs_drive(juego, raiz)
    assert _carpetas(raiz) == [CARPETA_CISO]


def test_con_otro_titulo_reemplazar_deja_una_sola_carpeta_y_un_archivo(
        usb_otro_titulo, tmp_path):
    raiz, carpeta = usb_otro_titulo
    library_ops.send_to_wbfs_drive(_juego_con_otro_titulo(tmp_path), raiz,
                                   overwrite=True)
    assert _carpetas(raiz) == [CARPETA_CISO]
    assert _contenido(carpeta) == {"game.iso": b"juego nuevo"}


def test_con_otro_titulo_el_original_se_aparta_y_vuelve_si_falla(
        usb_otro_titulo, tmp_path, monkeypatch):
    raiz, carpeta = usb_otro_titulo
    vistos = {}

    def copia_que_falla(src, dest, cb, cancel=None, digest=None):
        vistos["durante"] = sorted(p.name for p in carpeta.iterdir())
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(library_ops, "_copy_with_progress", copia_que_falla)
    with pytest.raises(OSError):
        library_ops.send_to_wbfs_drive(_juego_con_otro_titulo(tmp_path), raiz,
                                       overwrite=True)
    assert any(n.startswith(".game.ciso.respaldo-") for n in vistos["durante"])
    assert _carpetas(raiz) == [CARPETA_CISO]
    assert _contenido(carpeta) == {"game.ciso": b"viejo ciso"}


def test_disco_2_con_otro_titulo_va_a_la_carpeta_del_disco_1(usb_otro_titulo, tmp_path):
    raiz, carpeta = usb_otro_titulo
    library_ops.send_to_wbfs_drive(_juego_con_otro_titulo(tmp_path, disco=1), raiz)
    assert _carpetas(raiz) == [CARPETA_CISO]
    assert _contenido(carpeta) == {"disc2.iso": b"juego nuevo", "game.ciso": b"viejo ciso"}


def test_transferir_con_otro_titulo_pregunta(usb_otro_titulo, tmp_path, monkeypatch):
    from wiibackup_manager.widgets import transfer_view
    raiz, _carpeta = usb_otro_titulo
    juego = _juego_con_otro_titulo(tmp_path)
    preguntas, encolados = [], []
    monkeypatch.setattr(transfer_view.gtk_helpers, "confirm_overwrite",
                        lambda parent, body, accion: preguntas.append(body))
    vista = types.SimpleNamespace(
        _dest_path=raiz, _selected_games=lambda: [juego],
        _show_toast=lambda _m: None, get_root=lambda: None,
        _enqueue=lambda sel, root, overwrite=False: encolados.append(overwrite))

    transfer_view.TransferView._on_transfer_clicked(vista)

    assert encolados == [] and len(preguntas) == 1
    assert CARPETA_CISO in preguntas[0]


def test_la_cola_con_otro_titulo_lo_saltea(usb_otro_titulo, tmp_path):
    raiz, carpeta = usb_otro_titulo
    cola = TransferQueue(OperationManager(), dispatch=lambda f, *a: f(*a))
    job = cola.add_jobs([_juego_con_otro_titulo(tmp_path)], raiz)[0]
    limite = time.monotonic() + 10
    while not job.is_final and time.monotonic() < limite:
        time.sleep(0.01)
    cola.shutdown(wait=5)
    assert job.status is JobStatus.SKIPPED, job.error_msg
    assert _carpetas(raiz) == [CARPETA_CISO]
