"""Pruebas de los blindajes del Modo Fábrica en `drives.py`.

Todo acá corre sin hardware ni privilegios: `_SYS_BLOCK` y `_PROC_MOUNTS`
son constantes de módulo justamente para poder apuntarlas, con
monkeypatch, a un `/sys/block` y un `/proc/mounts` de mentira armados en
`tmp_path`. Lo único que se prueba con un dispositivo de bloque REAL (un
loop device) es el formateo de punta a punta, y eso vive aparte en
`tools/manual_factory_mode_e2e.py` porque necesita root -no corresponde
en la suite automática.

Los comandos externos (`umount`, `mkfs.vfat`, `udisksctl`) se prueban acá
con un `run` falso inyectado (mismo patrón que `dispatch` en
`queue_manager`): confirma que `format_as_wii_usb` arma bien los
argumentos y respeta el orden blindajes -> desmontar -> formatear ->
montar, sin ejecutar un solo comando real.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wiibackup_manager import drives


def _dev(name: str) -> Path:
    return Path("/dev") / name


# --------------------------------------------------------------- Fixtures --
@pytest.fixture
def sys_block(tmp_path, monkeypatch):
    """Un /sys/block de juguete. Devuelve la carpeta para que cada test
    arme los dispositivos que necesite."""
    base = tmp_path / "sys_block"
    base.mkdir()
    monkeypatch.setattr(drives, "_SYS_BLOCK", base)
    return base


@pytest.fixture
def proc_mounts(tmp_path, monkeypatch):
    """Un /proc/mounts de juguete. Devuelve una función para fijar su
    contenido (lista de líneas "origen punto ...")."""
    path = tmp_path / "proc_mounts"
    monkeypatch.setattr(drives, "_PROC_MOUNTS", path)

    def _set(lineas: list[str]) -> None:
        path.write_text("\n".join(lineas) + "\n" if lineas else "")
    return _set


def _make_block_device(sys_block, name, *, removable, size_sectors,
                       model="USB Drive"):
    """Arma /sys_block/<name>/{removable,size,device/model} como los
    expone de verdad el kernel bajo /sys/block."""
    d = sys_block / name
    d.mkdir()
    (d / "removable").write_text("1\n" if removable else "0\n")
    (d / "size").write_text(f"{size_sectors}\n")
    (d / "device").mkdir()
    (d / "device" / "model").write_text(model + "\n")


# ----------------------------------------------------- Blindaje 1: lista --
def test_is_removable_block_device_true_si_el_flag_es_1(sys_block):
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    assert drives.is_removable_block_device("/dev/sdb") is True


def test_is_removable_block_device_false_para_un_disco_interno(sys_block):
    """El caso que importa de verdad: un NVMe/SATA interno (removable=0)
    tiene que dar False, sin excepciones."""
    _make_block_device(sys_block, "nvme0n1", removable=False, size_sectors=10**9)
    assert drives.is_removable_block_device("/dev/nvme0n1") is False


def test_is_removable_block_device_falla_cerrado_si_no_existe(sys_block):
    """Sin el archivo `removable` (dispositivo que no existe, o al que no
    se puede acceder) se falla CERRADO: no es candidato, nunca abierto."""
    assert drives.is_removable_block_device("/dev/no-existe") is False


def test_list_candidate_drives_excluye_discos_fijos(sys_block):
    _make_block_device(sys_block, "sda", removable=False, size_sectors=10**9,
                       model="Samsung SSD 970")
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=10**7,
                       model="SanDisk Ultra")
    candidatos = drives.list_candidate_drives()
    assert [c.path.name for c in candidatos] == ["sdb"]
    assert candidatos[0].model == "SanDisk Ultra"


def test_list_candidate_drives_ignora_loop_ram_dm(sys_block):
    _make_block_device(sys_block, "loop0", removable=False, size_sectors=1000)
    _make_block_device(sys_block, "ram0", removable=False, size_sectors=1000)
    _make_block_device(sys_block, "dm-0", removable=False, size_sectors=1000)
    _make_block_device(sys_block, "sr0", removable=False, size_sectors=1000)
    assert drives.list_candidate_drives() == []


def test_list_candidate_drives_da_vacio_sin_sys_block(tmp_path, monkeypatch):
    monkeypatch.setattr(drives, "_SYS_BLOCK", tmp_path / "no-existe")
    assert drives.list_candidate_drives() == []


# ---------------------------------------------- Blindaje 3: re-verificar --
def test_verify_still_safe_pasa_si_nada_cambio(sys_block):
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=2_000_000)
    device = drives.BlockDevice(path=_dev("sdb"), model="X",
                                size_bytes=2_000_000 * 512)
    drives.verify_still_safe(device)  # no levanta nada


def test_verify_still_safe_rechaza_si_dejo_de_ser_removible(sys_block):
    """Simula que entre el diálogo y la confirmación, /dev/sdb pasó a ser
    otra cosa (o el kernel lo reasignó a un disco fijo)."""
    _make_block_device(sys_block, "sdb", removable=False, size_sectors=2_000_000)
    device = drives.BlockDevice(path=_dev("sdb"), model="X",
                                size_bytes=2_000_000 * 512)
    with pytest.raises(drives.UnsafeDeviceError):
        drives.verify_still_safe(device)


def test_verify_still_safe_rechaza_si_cambio_el_tamano(sys_block):
    """El tamaño mostrado en el diálogo de confirmación ya no coincide:
    puede ser un USB distinto conectado en el mismo puerto/letra."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1_000_000)
    device = drives.BlockDevice(path=_dev("sdb"), model="X",
                                size_bytes=2_000_000 * 512)
    with pytest.raises(drives.DeviceChangedError):
        drives.verify_still_safe(device)


def test_verify_still_safe_rechaza_si_el_dispositivo_desaparecio(sys_block):
    device = drives.BlockDevice(path=_dev("sdz"), model="X", size_bytes=123)
    with pytest.raises(drives.UnsafeDeviceError):
        drives.verify_still_safe(device)


# ------------------------------------------- Blindaje 4: montajes críticos --
@pytest.mark.parametrize("punto_critico", ["/", "/home", "/boot", "/boot/efi"])
def test_check_no_critical_mounts_aborta_si_hay_una_particion_del_sistema(
        proc_mounts, punto_critico):
    proc_mounts([f"/dev/sdb1 {punto_critico} ext4 rw 0 0"])
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=123)
    with pytest.raises(drives.CriticalMountError):
        drives.check_no_critical_mounts(device)


def test_check_no_critical_mounts_pasa_si_esta_montado_en_run_media(proc_mounts):
    """El propio caso de uso normal: el USB que se quiere formatear está
    montado donde lo automontó gvfs, en /run/media/... -no es crítico."""
    proc_mounts(["/dev/sdb1 /run/media/usuario/MIUSB vfat rw 0 0"])
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=123)
    drives.check_no_critical_mounts(device)  # no levanta nada


def test_check_no_critical_mounts_ignora_montajes_de_otro_disco(proc_mounts):
    """/dev/sdc1 montado en / no tiene nada que ver con /dev/sdb, que es
    el que se está por formatear."""
    proc_mounts(["/dev/sdc1 / ext4 rw 0 0"])
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=123)
    drives.check_no_critical_mounts(device)  # no levanta nada


def test_check_no_critical_mounts_detecta_el_disco_entero_sin_particionar(proc_mounts):
    """Un USB formateado 'a lo superfloppy' (sin tabla de particiones) se
    monta directo como /dev/sdb, no /dev/sdb1."""
    proc_mounts(["/dev/sdb / vfat rw 0 0"])
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=123)
    with pytest.raises(drives.CriticalMountError):
        drives.check_no_critical_mounts(device)


def test_mounted_critical_paths_falla_cerrado_sin_proc_mounts(tmp_path, monkeypatch):
    """Si ni siquiera se puede leer /proc/mounts, no poder confirmar que
    es seguro NO es lo mismo que confirmar que lo es: se trata como si
    todo estuviera montado en rutas críticas."""
    monkeypatch.setattr(drives, "_PROC_MOUNTS", tmp_path / "no-existe")
    assert drives.mounted_critical_paths("/dev/sdb") == sorted(drives.CRITICAL_MOUNTPOINTS)


# ---------------------------------------- format_as_wii_usb: orquestación --
def test_format_as_wii_usb_aborta_por_blindaje_3_sin_ejecutar_nada(sys_block):
    """Si el blindaje 3 frena, ni `umount` ni `mkfs.vfat` deberían
    llegar a correr."""
    _make_block_device(sys_block, "sdb", removable=False, size_sectors=1000)
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=1000 * 512)

    def _no_deberia_correr(*_a, **_k):
        raise AssertionError("no debería ejecutarse ningún comando")

    with pytest.raises(drives.UnsafeDeviceError):
        drives.format_as_wii_usb(device, run=_no_deberia_correr)


def test_format_as_wii_usb_aborta_por_blindaje_4_sin_ejecutar_mkfs(
        sys_block, proc_mounts):
    """Blindaje 3 pasa (sigue siendo removible, mismo tamaño) pero
    Blindaje 4 frena porque hay una partición montada en /home: tampoco
    debería llegar a correr `mkfs.vfat`. Este es el caso que más importa
    de los cuatro: es la última línea de defensa, la que tiene que frenar
    aunque todo lo anterior haya dicho que sí."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    proc_mounts(["/dev/sdb1 /home ext4 rw 0 0"])
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=1000 * 512)

    llamadas = []

    def _fake_run(cmd, **_k):
        llamadas.append(cmd)
        raise AssertionError("no debería llegar a ejecutar nada")

    with pytest.raises(drives.CriticalMountError):
        drives.format_as_wii_usb(device, run=_fake_run)
    assert llamadas == []


def test_format_as_wii_usb_feliz_llama_mkfs_y_crea_carpetas(
        sys_block, proc_mounts, tmp_path):
    """Camino feliz con `run` falso (sin tocar ningún disco real): los
    dos blindajes pasan, se llama a mkfs.vfat con los flags esperados
    (con o sin `pkexec` adelante, según con qué usuario corra la suite) y
    se crean apps/games/wbfs en el punto que reporta el `udisksctl mount`
    simulado."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    proc_mounts([])  # nada montado: blindaje 4 pasa limpio

    punto_montaje = tmp_path / "run_media" / "WII_USB"
    punto_montaje.mkdir(parents=True)

    comandos = []

    def _fake_run(cmd, **_k):
        comandos.append(cmd)
        if cmd[0] == "umount":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "mkfs.vfat" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "udisksctl" and cmd[1] == "mount":
            return subprocess.CompletedProcess(
                cmd, 0, f"Mounted /dev/sdb at {punto_montaje}.\n", "")
        raise AssertionError(f"comando inesperado: {cmd}")

    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=1000 * 512)
    resultado = drives.format_as_wii_usb(device, run=_fake_run, label="WII_USB")

    assert resultado == punto_montaje
    for carpeta in drives.FACTORY_FOLDERS:
        assert (punto_montaje / carpeta).is_dir()

    mkfs_cmd = next(c for c in comandos if "mkfs.vfat" in c)
    assert "-F" in mkfs_cmd and "32" in mkfs_cmd
    assert "-n" in mkfs_cmd and "WII_USB" in mkfs_cmd
    assert str(device.path) in mkfs_cmd


def test_format_as_wii_usb_propaga_error_de_mkfs(sys_block, proc_mounts):
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    proc_mounts([])

    def _fake_run(cmd, **_k):
        if cmd[0] == "umount":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "mkfs.vfat" in cmd:
            return subprocess.CompletedProcess(cmd, 1, "", "mkfs.vfat: dispositivo ocupado")
        raise AssertionError(f"no debería llegar a llamar a {cmd}")

    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=1000 * 512)
    with pytest.raises(RuntimeError, match="ocupado"):
        drives.format_as_wii_usb(device, run=_fake_run)
