"""Modo Fábrica y el `OperationManager`: que formatear y escribir en el
mismo disco físico se bloqueen mutuamente.

Antes de este archivo, `window._start_factory_format` lanzaba
`drives.format_as_wii_usb` directo, sin pasar por `self.ops` -la Cola de
Transferencias y la instalación de Homebrew sí se registraban (con el
punto de montaje como recurso), pero Modo Fábrica no se enteraba de ellas
ni ellas de Modo Fábrica. Resultado: nada impedía correr `mkfs.vfat`
sobre un USB mientras se le copiaba un juego encima, con datos a mitad de
escritura.

La corrección tiene dos partes que se prueban acá juntas, porque una sin
la otra no alcanza:

1. Modo Fábrica declara el disco físico entero (`BlockDevice.path`, ej.
   `/dev/sdb`) como recurso -no un punto de montaje, porque formatear lo
   desmonta primero.
2. Transferencias y la instalación de Homebrew declaran ESE MISMO disco
   físico (`drives.resources_for_mount_point`, que resuelve el punto de
   montaje al disco de verdad vía /sys/class/block) ADEMÁS del punto de
   montaje de siempre.

Todo corre contra un `/sys/class/block` y un `findmnt` de mentira (mismo
patrón que `tests/test_drives_factory.py`): nada de esto toca hardware ni
necesita privilegios, y sin embargo ejercita la resolución real de
partición -> disco físico, no rutas escritas a mano que coincidan por
casualidad."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wiibackup_manager import drives
from wiibackup_manager.operations import OperationBusy, OperationKind, OperationManager


# --------------------------------------------------------------- Fixtures --
@pytest.fixture
def sys_class_block(tmp_path, monkeypatch):
    """Un /sys/class/block de juguete: mismo fixture que
    `test_drives_factory.py` (ver ahí el porqué de la estructura)."""
    devices = tmp_path / "devices_block"
    devices.mkdir()
    class_block = tmp_path / "class_block"
    class_block.mkdir()
    monkeypatch.setattr(drives, "_SYS_CLASS_BLOCK", class_block)

    def _agregar_disco(nombre: str, particiones: tuple[str, ...] = ()) -> None:
        disco_dir = devices / nombre
        disco_dir.mkdir()
        (class_block / nombre).symlink_to(disco_dir)
        for particion in particiones:
            part_dir = disco_dir / particion
            part_dir.mkdir()
            (part_dir / "partition").write_text("1\n")
            (class_block / particion).symlink_to(part_dir)

    return _agregar_disco


@pytest.fixture
def fake_findmnt(monkeypatch):
    """Fija qué `/dev/sdX1` reporta `findmnt` para cada punto de montaje."""
    origen_por_destino: dict[str, str] = {}

    def _run(cmd, **_k):
        assert cmd[0] == "findmnt"
        origen = origen_por_destino.get(cmd[-1])
        if origen is None:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return subprocess.CompletedProcess(cmd, 0, origen + "\n", "")

    monkeypatch.setattr(drives.subprocess, "run", _run)
    return origen_por_destino


def _montar(fake_findmnt, punto: str, particion: str) -> Path:
    """Registra en el `findmnt` falso que `punto` es donde está montada
    `particion`, y devuelve `punto` como Path -para no repetir la
    conversión en cada test."""
    fake_findmnt[punto] = particion
    return Path(punto)


# ------------------------------------------------------ Formatear vs. copiar --
def test_formatear_se_bloquea_si_hay_una_transferencia_al_mismo_disco(
        sys_class_block, fake_findmnt, tmp_path):
    sys_class_block("sdb", particiones=("sdb1",))
    punto_montaje = _montar(fake_findmnt, "/run/media/usuario/USB", "/dev/sdb1")

    ops = OperationManager()
    # Igual que `queue_manager._acquire_operation`.
    ops.start(OperationKind.TRANSFERRING,
             read=[tmp_path / "juego.iso"],
             write=[punto_montaje / "wbfs" / "juego.wbfs"],
             resources=drives.resources_for_mount_point(punto_montaje))

    # Igual que `window._start_factory_format`: el disco físico ENTERO,
    # no el punto de montaje (que Modo Fábrica va a desmontar).
    with pytest.raises(OperationBusy) as exc:
        ops.start(OperationKind.FORMATTING, resources=[Path("/dev/sdb")])
    assert exc.value.blocker.kind is OperationKind.TRANSFERRING


def test_formatear_se_bloquea_si_hay_una_instalacion_de_homebrew_al_mismo_disco(
        sys_class_block, fake_findmnt):
    sys_class_block("sdc", particiones=("sdc1",))
    punto_montaje = _montar(fake_findmnt, "/run/media/usuario/HOMEBREW", "/dev/sdc1")

    ops = OperationManager()
    # Igual que `homebrew_store_view._start_install`.
    ops.start(OperationKind.INSTALLING_HOMEBREW,
             write=[punto_montaje / "apps" / "wiidonut"],
             resources=drives.resources_for_mount_point(punto_montaje))

    with pytest.raises(OperationBusy) as exc:
        ops.start(OperationKind.FORMATTING, resources=[Path("/dev/sdc")])
    assert exc.value.blocker.kind is OperationKind.INSTALLING_HOMEBREW


def test_transferencia_se_bloquea_si_fabrica_esta_formateando_el_mismo_disco(
        sys_class_block, fake_findmnt, tmp_path):
    """La misma protección, en el sentido inverso: si Modo Fábrica ya
    empezó a formatear, una transferencia que arranca después hacia el
    mismo disco (por el punto de montaje viejo, que puede seguir
    apareciendo un instante mientras se desmonta) tiene que esperar, no
    pisar el formateo."""
    sys_class_block("sdb", particiones=("sdb1",))
    punto_montaje = _montar(fake_findmnt, "/run/media/usuario/USB", "/dev/sdb1")

    ops = OperationManager()
    ops.start(OperationKind.FORMATTING, resources=[Path("/dev/sdb")])

    with pytest.raises(OperationBusy) as exc:
        ops.start(OperationKind.TRANSFERRING,
                 read=[tmp_path / "juego.iso"],
                 write=[punto_montaje / "wbfs" / "juego.wbfs"],
                 resources=drives.resources_for_mount_point(punto_montaje))
    assert exc.value.blocker.kind is OperationKind.FORMATTING


def test_formatear_no_se_bloquea_por_una_transferencia_a_otro_disco(
        sys_class_block, fake_findmnt, tmp_path):
    """Que el bloqueo sea por disco y no un candado global: una
    transferencia a /dev/sdc no debería frenar el formateo de /dev/sdb."""
    sys_class_block("sdb")
    sys_class_block("sdc", particiones=("sdc1",))
    punto_montaje = _montar(fake_findmnt, "/run/media/usuario/OTRO", "/dev/sdc1")

    ops = OperationManager()
    ops.start(OperationKind.TRANSFERRING,
             read=[tmp_path / "juego.iso"],
             write=[punto_montaje / "wbfs" / "juego.wbfs"],
             resources=drives.resources_for_mount_point(punto_montaje))

    ops.start(OperationKind.FORMATTING, resources=[Path("/dev/sdb")])  # no levanta nada


def test_formatear_se_habilita_apenas_termina_la_transferencia(
        sys_class_block, fake_findmnt, tmp_path):
    sys_class_block("sdb", particiones=("sdb1",))
    punto_montaje = _montar(fake_findmnt, "/run/media/usuario/USB", "/dev/sdb1")

    ops = OperationManager()
    transferencia = ops.start(
        OperationKind.TRANSFERRING,
        read=[tmp_path / "juego.iso"],
        write=[punto_montaje / "wbfs" / "juego.wbfs"],
        resources=drives.resources_for_mount_point(punto_montaje))

    with pytest.raises(OperationBusy):
        ops.start(OperationKind.FORMATTING, resources=[Path("/dev/sdb")])

    ops.finish(transferencia)

    formateo = ops.start(OperationKind.FORMATTING, resources=[Path("/dev/sdb")])
    assert formateo.kind is OperationKind.FORMATTING
