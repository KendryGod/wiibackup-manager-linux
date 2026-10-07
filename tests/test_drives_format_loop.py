"""`drives.format_fat32` contra un loop device REAL con tabla de
particiones previa: wipefs, parted y mkfs.vfat de verdad.

Necesita root (losetup, parted, mount), así que en la suite normal y en
CI se saltea. Para correrlo:

    sudo python3 -B -m pytest -p no:cacheprovider tests/test_drives_format_loop.py

(`-B` y `-p no:cacheprovider` para no dejar __pycache__ ni .pytest_cache
de root adentro del repo.)

El caso con MBR es el que reproduce exactamente el error del Kingston
DataTraveler: con el código anterior (`mkfs.vfat` sobre el disco entero)
falla con "Partitions or virtual mappings on device".

Un loop device nunca es removable=1 para el kernel, y el script
privilegiado relee `removable` directo de sysfs. Por eso, en vez de
parchear una función de Python, `_SYS_BLOCK` apunta a una copia de
/sys/block/loopN con removable=1 y el tamaño real: el resto de los
blindajes (tamaño, montajes) se ejercitan contra el dispositivo real.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from wiibackup_manager import drives

_HERRAMIENTAS = ("losetup", "parted", "wipefs", "mkfs.vfat", "lsblk",
                 "udevadm", "udisksctl", "blkid", "mount", "umount")

pytestmark = [
    pytest.mark.skipif(os.geteuid() != 0, reason="necesita root (loop devices)"),
    pytest.mark.skipif(any(shutil.which(h) is None for h in _HERRAMIENTAS),
                       reason="faltan herramientas de disco"),
]


def _sh(*cmd, check=True):
    return subprocess.run([str(c) for c in cmd], capture_output=True,
                          text=True, check=check)


def _montajes_de(dev: str) -> list[str]:
    salida = _sh("lsblk", "-nrpo", "MOUNTPOINT", dev, check=False).stdout
    return [linea for linea in salida.splitlines() if linea.strip()]


@pytest.fixture
def loop(tmp_path, monkeypatch):
    imagen = tmp_path / "usb.img"
    _sh("truncate", "-s", "256M", imagen)
    dev = _sh("losetup", "--find", "--show", "-P", imagen).stdout.strip()
    nombre = Path(dev).name

    sys_block = tmp_path / "sys_block"
    (sys_block / nombre).mkdir(parents=True)
    (sys_block / nombre / "removable").write_text("1\n")
    sectores = Path(f"/sys/block/{nombre}/size").read_text().strip()
    (sys_block / nombre / "size").write_text(sectores + "\n")
    monkeypatch.setattr(drives, "_SYS_BLOCK", sys_block)

    device = drives.BlockDevice(path=Path(dev), model="Loop de prueba",
                                size_bytes=int(sectores) * 512,
                                identity=drives.device_identity(dev))
    try:
        yield dev, device, tmp_path
    finally:
        for punto in _montajes_de(dev):
            _sh("umount", punto, check=False)
        _sh("losetup", "-d", dev, check=False)


def _particionar(dev: str, tabla: str, *, n: int = 1, fstype: str = "vfat") -> list[str]:
    _sh("parted", "-s", dev, "mklabel", tabla)
    if n == 1:
        _sh("parted", "-s", dev, "mkpart", "primary", "fat32", "1MiB", "100%")
    else:
        _sh("parted", "-s", dev, "mkpart", "primary", "1MiB", "50%")
        _sh("parted", "-s", dev, "mkpart", "primary", "50%", "100%")
    _sh("udevadm", "settle", check=False)
    particiones = drives.partitions_of(dev)
    assert len(particiones) == n
    for p in particiones:
        if fstype == "vfat":
            _sh("mkfs.vfat", "-F", "32", p)
        else:
            _sh("mkfs." + fstype, "-q", p)
    return [str(p) for p in particiones]


def _verificar_resultado(dev: str, punto: Path) -> None:
    particiones = drives.partitions_of(dev)
    assert len(particiones) == 1
    particion = str(particiones[0])
    assert _sh("blkid", "-o", "value", "-s", "PTTYPE", dev).stdout.strip() == "dos"
    assert _sh("blkid", "-o", "value", "-s", "TYPE", particion).stdout.strip() == "vfat"
    # El filesystem está en la partición, no en el disco entero.
    assert _sh("blkid", "-o", "value", "-s", "TYPE", dev, check=False).stdout.strip() != "vfat"
    assert punto.is_dir()
    assert os.stat(punto).st_uid == os.getuid()
    (punto / "escritura.txt").write_text("ok")
    _sh("udisksctl", "unmount", "-b", particion, check=False)


def test_disco_sin_tabla(loop):
    dev, device, _tmp = loop
    _verificar_resultado(dev, drives.format_fat32(device, label="SIN_TABLA"))


def test_disco_con_mbr_reproduce_el_usb_real(loop):
    """El estado de fábrica del DataTraveler: MBR + una partición FAT32."""
    dev, device, _tmp = loop
    _particionar(dev, "msdos")
    # Control: este es exactamente el comando viejo, y falla como en el USB.
    viejo = _sh("mkfs.vfat", "-F", "32", dev, check=False)
    assert viejo.returncode != 0
    assert "Partitions or virtual mappings" in viejo.stderr

    _verificar_resultado(dev, drives.format_fat32(device, label="WII_USB"))


def test_disco_con_gpt_y_dos_particiones(loop):
    dev, device, _tmp = loop
    _particionar(dev, "gpt", n=2)
    _verificar_resultado(dev, drives.format_fat32(device))


def test_disco_con_particion_montada(loop):
    dev, device, tmp = loop
    (particion,) = _particionar(dev, "msdos")
    punto_viejo = tmp / "montaje-viejo"
    punto_viejo.mkdir()
    _sh("mount", particion, punto_viejo)
    assert _montajes_de(dev)

    _verificar_resultado(dev, drives.format_fat32(device))


def test_format_as_wii_usb_crea_las_carpetas_en_la_particion(loop):
    dev, device, _tmp = loop
    _particionar(dev, "msdos")
    punto = drives.format_as_wii_usb(device)
    for carpeta in drives.FACTORY_FOLDERS:
        assert (punto / carpeta).is_dir()
    _verificar_resultado(dev, punto)
