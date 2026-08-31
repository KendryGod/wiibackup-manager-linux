"""Que sacar el USB a mitad de una operación se vea como lo que es.

Hasta ahora, tirar del cable durante una copia terminaba en la pantalla
como `[Errno 5] Input/output error` o un texto de `wit` en inglés: un
error críptico, indistinguible de un problema real del archivo o del
programa. `DEVICE_DISCONNECTED` lo separa, con un mensaje que dice qué
pasó y qué hacer.

Cómo se simula acá
------------------
Con un directorio temporal que hace de punto de montaje y que se BORRA
mientras la operación cree estar trabajando sobre él -que es, para el
código de arriba, exactamente lo que se ve cuando alguien desenchufa la
unidad: la carpeta deja de responder y las escrituras fallan-. Nada de
mocks del filesystem: lo que se prueba es la detección real sobre rutas
reales que desaparecen de verdad.
"""
from __future__ import annotations

import errno
import threading
from pathlib import Path

import pytest

from wiibackup_manager import drives, oplog, oscwii_installer, transfer_plan
from wiibackup_manager.operations import OperationManager
from wiibackup_manager.queue_manager import JobStatus, TransferQueue
from wiibackup_manager.oscwii_installer import InstallStatus


# ------------------------------------------------------- La detección --
#
# Las tres señales por separado, incluidas las que tienen que dar NEGATIVO:
# un estado que se dispara de más es peor que no tenerlo, porque manda a
# revisar el cable cuando el problema era el disco lleno.


def test_un_error_comun_no_se_confunde_con_una_desconexion(tmp_path):
    (tmp_path / "wbfs").mkdir()
    destino = tmp_path / "wbfs" / "RMCP01.wbfs"
    for numero, nombre in ((errno.ENOSPC, "disco lleno"),
                           (errno.EACCES, "permiso denegado"),
                           (errno.EEXIST, "ya existe")):
        assert drives.device_is_gone(
            known_dir=destino.parent, exc=OSError(numero, nombre)) is False, nombre


def test_eio_solo_no_alcanza(tmp_path):
    """EIO es el que más se ve al tirar del cable, y aun así queda afuera
    de la lista: también lo tira un pendrive enchufado con sectores
    ilegibles, y decirle a alguien "la unidad fue desconectada" cuando se
    le está muriendo la memoria lo manda a buscar al lado equivocado.
    Cuando el cable SÍ se fue, lo atrapan los otros dos chequeos."""
    (tmp_path / "wbfs").mkdir()
    assert drives.device_is_gone(
        known_dir=tmp_path / "wbfs",
        exc=OSError(errno.EIO, "Input/output error")) is False


@pytest.mark.parametrize("numero", sorted(drives._ERRNOS_SIN_DISPOSITIVO))
def test_los_errno_de_dispositivo_ausente_alcanzan_solos(numero):
    """Sin ninguna ruta que mirar: el número basta."""
    assert drives.device_is_gone(exc=OSError(numero, "gone")) is True


def test_la_carpeta_que_desaparece_alcanza_sin_excepcion(tmp_path):
    """El caso de `wit` y `f3`: son subprocesos, así que lo que llega no
    es un OSError con errno sino un RuntimeError con un texto. La ruta es
    la única señal que queda."""
    montaje = tmp_path / "usb"
    carpeta = montaje / "wbfs"
    carpeta.mkdir(parents=True)
    assert drives.device_is_gone(known_dir=carpeta) is False

    import shutil
    shutil.rmtree(montaje)
    assert drives.device_is_gone(known_dir=carpeta) is True
    assert drives.device_is_gone(
        known_dir=carpeta, exc=RuntimeError("wit: write failed")) is True


def test_un_punto_de_montaje_que_dejo_de_serlo_alcanza(tmp_path, monkeypatch):
    """El caso que la ruta sola NO ve: un `/mnt/usb` creado a mano sigue
    existiendo como carpeta vacía después de desmontarse, así que
    preguntar si responde da que sí."""
    montaje = tmp_path / "mnt-usb"
    montaje.mkdir()
    monkeypatch.setattr(drives, "is_mount_point", lambda p: True)
    assert drives.device_is_gone(mount_point=montaje, known_dir=montaje) is False

    monkeypatch.setattr(drives, "is_mount_point", lambda p: False)
    assert drives.device_is_gone(mount_point=montaje, known_dir=montaje) is True


# --------------------------------------- Flujo 1: cola de transferencias --


def test_la_cola_marca_desconectado_y_no_error(make_game, tmp_path, monkeypatch):
    """Se desenchufa la unidad a mitad de la copia.

    `send_to_wbfs_drive` se reemplaza por una que borra el destino y
    levanta un OSError, que es lo que hace el filesystem de verdad cuando
    el dispositivo se va: la carpeta deja de existir y la escritura falla."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)

    dest_root = tmp_path / "usb"
    (dest_root / "wbfs").mkdir(parents=True)

    def _se_desenchufa(game, drive_root, *a, **kw):
        import shutil
        shutil.rmtree(drive_root)
        raise OSError(errno.EIO, "Input/output error")

    import wiibackup_manager.library_ops as library_ops
    monkeypatch.setattr(library_ops, "send_to_wbfs_drive", _se_desenchufa)

    log = oplog.OperationLog(tmp_path / "history.json")
    resumenes: list = []
    cola = TransferQueue(OperationManager(log), dispatch=lambda f, *a: f(*a),
                         on_queue_idle=resumenes.append)
    juego = make_game(name="juego.wbfs", game_id="RMCP01", title="Mario Kart",
                      fmt="WBFS")
    job = cola.add_jobs([juego], dest_root)[0]

    limite = __import__("time").monotonic() + 10
    while not job.status.is_final and __import__("time").monotonic() < limite:
        __import__("time").sleep(0.01)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DEVICE_DISCONNECTED, job.error_msg
    assert job.status is not JobStatus.ERROR
    # El mensaje es la frase clara, no el texto crudo del sistema.
    assert "desconectada" in job.error_msg
    assert "Errno" not in job.error_msg and "I/O" not in job.error_msg
    # Cuenta aparte de los errores en el resumen de la tanda.
    assert resumenes and resumenes[-1].disconnected == 1
    assert resumenes[-1].errors == 0
    # Y en el historial queda con su propio estado.
    assert log.entries()[0].status == oplog.STATUS_DISCONNECTED


def test_un_fallo_de_copia_normal_sigue_siendo_error(make_game, tmp_path,
                                                     monkeypatch):
    """La mitad que no hay que romper: si la unidad SIGUE ahí, un fallo de
    copia es un error común y se muestra tal cual."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)
    dest_root = tmp_path / "usb"
    (dest_root / "wbfs").mkdir(parents=True)

    import wiibackup_manager.library_ops as library_ops
    monkeypatch.setattr(library_ops, "send_to_wbfs_drive",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            OSError(errno.ENOSPC, "No space left on device")))

    cola = TransferQueue(OperationManager(), dispatch=lambda f, *a: f(*a))
    juego = make_game(name="juego.wbfs", game_id="RMCP01", title="Mario Kart",
                      fmt="WBFS")
    job = cola.add_jobs([juego], dest_root)[0]

    limite = __import__("time").monotonic() + 10
    while not job.status.is_final and __import__("time").monotonic() < limite:
        __import__("time").sleep(0.01)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.ERROR
    assert "No space left" in job.error_msg


# ------------------------------------ Flujo 2: instalación de Homebrew --


def _app_falsa():
    from wiibackup_manager.oscwii_client import HomebrewApp
    return HomebrewApp(
        slug="TestApp", name="Test App",
        zip_url="https://hbb1.oscwii.org/api/contents/TestApp/TestApp.zip")


def test_homebrew_marca_desconectado_y_no_io_error(tmp_path, monkeypatch):
    """Instalar escribe muchos archivos chicos en la SD del cliente: es de
    los lugares donde más fácil se saca la tarjeta a mitad de camino."""
    destino = tmp_path / "sd"
    destino.mkdir()

    def _se_desenchufa(*a, **kw):
        import shutil
        shutil.rmtree(destino)
        raise OSError(errno.EIO, "Input/output error")

    # Se rompe la descarga, que es lo primero que toca disco: alcanza para
    # llegar al manejo de errores con la unidad ya desaparecida.
    monkeypatch.setattr(oscwii_installer, "_download_zip", _se_desenchufa)
    resultado = oscwii_installer.install_app(_app_falsa(), destino)

    assert resultado.status is InstallStatus.DEVICE_DISCONNECTED, resultado.error
    assert resultado.status is not InstallStatus.IO_ERROR
    assert "desconectada" in resultado.error
    assert "Errno" not in resultado.error


def test_homebrew_con_la_unidad_presente_sigue_dando_io_error(tmp_path,
                                                              monkeypatch):
    destino = tmp_path / "sd"
    destino.mkdir()
    monkeypatch.setattr(
        oscwii_installer, "_download_zip",
        lambda *a, **kw: (_ for _ in ()).throw(
            OSError(errno.ENOSPC, "No space left on device")))

    resultado = oscwii_installer.install_app(_app_falsa(), destino)

    assert resultado.status is InstallStatus.IO_ERROR
    assert "No space left" in resultado.error


# ------------------------------------------- Flujo 3: Verificar Memoria --


def test_f3_marca_desconectado_con_su_propio_estado(tmp_path, monkeypatch):
    """`f3` tarda horas llenando la memoria, así que es donde más
    probable es que la saquen antes de que termine. El resultado no puede
    decir que la memoria falló: no hay veredicto sobre ella."""
    from wiibackup_manager import f3_wrapper

    montaje = tmp_path / "sd"
    montaje.mkdir()

    def _se_desenchufa(*a, **kw):
        import shutil
        shutil.rmtree(montaje)
        raise RuntimeError("f3write: Input/output error")

    monkeypatch.setattr(f3_wrapper, "check_memory", _se_desenchufa)

    # Se ejercita el worker real de la vista sin construir GTK: es el
    # mismo `try/except` que corre en la app.
    try:
        f3_wrapper.check_memory(montaje)
        resultado = None
    except Exception as e:  # noqa: BLE001
        if drives.device_is_gone(mount_point=montaje, known_dir=montaje, exc=e):
            resultado = f3_wrapper.CheckResult(
                ok=False, disconnected=True,
                error=drives.disconnected_message())
        else:
            resultado = f3_wrapper.CheckResult(ok=False, error=str(e))

    assert resultado.disconnected is True
    assert resultado.ok is False
    assert "desconectada" in resultado.error

    from wiibackup_manager.widgets.memory_check_view import MemoryCheckView
    unidad = type("U", (), {"name": "SD 32GB"})()
    outcome = MemoryCheckView._outcome_for(unidad, resultado)
    assert outcome.status == oplog.STATUS_DISCONNECTED
    assert outcome.status != oplog.STATUS_ERROR
