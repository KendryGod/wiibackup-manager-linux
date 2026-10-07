"""`drives.format_fat32` de punta a punta: tabla de particiones, paso
privilegiado único, montaje como el usuario.

Origen: con un Kingston DataTraveler real (que viene con tabla MBR de
fábrica) el formateo fallaba con "mkfs.vfat: Partitions or virtual
mappings on device '/dev/sda', not making filesystem", porque se le
pasaba a `mkfs.vfat` el disco entero.

Acá se corre el script privilegiado REAL (`drives._FORMAT_SCRIPT`) con
/bin/sh, pero contra herramientas de disco falsas (`fake_disk_tools`): sin
root, sin hardware. El `mkfs.vfat` falso reproduce el chequeo de dosfstools,
así que el código anterior falla acá con el mismo mensaje literal que dio
el USB real.

Las pruebas contra un loop device de verdad (que necesitan root) viven en
`test_drives_format_loop.py`.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from fake_disk_tools import MENSAJE_MKFS_CON_PARTICIONES, crear_disco_falso
from wiibackup_manager import drives

SECTORES = 2_000_000


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    """Arma /sys/block, /proc/mounts y las herramientas falsas para UN
    disco. Devuelve una función que crea el disco con el estado inicial
    pedido y devuelve (herramientas, BlockDevice, punto_de_montaje)."""
    sys_block = tmp_path / "sys_block"
    sys_block.mkdir()
    monkeypatch.setattr(drives, "_SYS_BLOCK", sys_block)
    proc_mounts = tmp_path / "proc_mounts"
    proc_mounts.write_text("")
    monkeypatch.setattr(drives, "_PROC_MOUNTS", proc_mounts)

    def _crear(nombre="sdb", *, particiones=(), tabla=None, montajes=(),
               identidad="SERIE-A"):
        d = sys_block / nombre
        d.mkdir()
        (d / "removable").write_text("1\n")
        (d / "size").write_text(f"{SECTORES}\n")
        disco = Path("/dev") / nombre
        punto = tmp_path / "run_media" / "kendry" / "USB"
        herramientas = crear_disco_falso(
            tmp_path / "herramientas", disco, proc_mounts=proc_mounts,
            punto_montaje=punto, particiones=list(particiones), tabla=tabla)
        proc_mounts.write_text(
            "".join(f"{origen} {destino} vfat rw 0 0\n" for origen, destino in montajes))
        herramientas.fijar_identidad(identidad)
        device = drives.BlockDevice(path=disco, model="DataTraveler 3.0",
                                    size_bytes=SECTORES * 512, identity=identidad)
        return herramientas, device, punto
    return _crear


def _formatear(herramientas, device, **kwargs):
    kwargs.setdefault("mount_timeout", 2.0)
    return drives.format_fat32(device, run=herramientas.run, **kwargs)


def _mkfs(herramientas) -> list[str]:
    return next(c for c in herramientas.llamadas() if c[0] == "mkfs.vfat")


# ------------------------------------------------ El bug del USB real --
def test_usb_con_tabla_mbr_de_fabrica_se_formatea(entorno):
    """El caso exacto del DataTraveler: MBR con una partición vfat. Con el
    código viejo (`mkfs.vfat` sobre /dev/sdb) esto fallaba con
    "Partitions or virtual mappings on device"."""
    herramientas, device, punto = entorno(particiones=["/dev/sdb1"], tabla="msdos")

    resultado = _formatear(herramientas, device, label="WII_USB")

    assert resultado == punto
    assert herramientas.formateado() == "/dev/sdb1"
    assert herramientas.particiones() == ["/dev/sdb1"]
    assert herramientas.tabla() == "msdos"
    assert _mkfs(herramientas)[-1] == "/dev/sdb1"


def test_el_mkfs_falso_reproduce_el_mensaje_del_usb_real(entorno):
    """Control del arnés: el `mkfs.vfat` falso se niega igual que el real
    si le pasan el disco entero con particiones. Es lo que hace que el
    test de arriba falle con el código viejo."""
    herramientas, _device, _punto = entorno(particiones=["/dev/sdb1"])
    r = herramientas.run(["mkfs.vfat", "-F", "32", "/dev/sdb"],
                         capture_output=True, text=True)
    assert r.returncode != 0
    assert r.stderr.strip() == MENSAJE_MKFS_CON_PARTICIONES.format(dev="/dev/sdb")


def test_disco_sin_tabla_queda_con_mbr_y_una_particion(entorno):
    herramientas, device, punto = entorno()

    assert _formatear(herramientas, device) == punto
    assert herramientas.tabla() == "msdos"
    assert herramientas.formateado() == "/dev/sdb1"


def test_disco_gpt_se_reemplaza_por_mbr(entorno):
    """Un disco que venía de otro sistema con GPT (y varias particiones)
    termina con MBR y UNA partición: lo que esperan los USB Loaders."""
    herramientas, device, _punto = entorno(
        particiones=["/dev/sdb1", "/dev/sdb2"], tabla="gpt")

    _formatear(herramientas, device)

    assert herramientas.tabla() == "msdos"
    assert herramientas.particiones() == ["/dev/sdb1"]
    assert herramientas.formateado() == "/dev/sdb1"
    # Las firmas de las particiones viejas se borran antes que la del disco.
    wipefs = [c[-1] for c in herramientas.llamadas() if c[0] == "wipefs"]
    assert wipefs[:3] == ["/dev/sdb1", "/dev/sdb2", "/dev/sdb"]


def test_disco_con_particion_montada_se_desmonta_y_se_formatea(entorno):
    herramientas, device, punto = entorno(
        particiones=["/dev/sdb1"], tabla="msdos",
        montajes=[("/dev/sdb1", "/run/media/kendry/XBOX360")])

    assert _formatear(herramientas, device) == punto
    nombres = herramientas.nombres()
    assert nombres.index("umount") < nombres.index("wipefs")
    assert herramientas.formateado() == "/dev/sdb1"


def test_en_sd_la_particion_es_p1_y_no_se_arma_pegando_un_1(entorno):
    """mmcblk0 + "1" daría mmcblk01, que no existe: la partición se
    descubre con lsblk, no concatenando."""
    herramientas, device, _punto = entorno("mmcblk0", particiones=["/dev/mmcblk0p1"])

    _formatear(herramientas, device)

    assert herramientas.formateado() == "/dev/mmcblk0p1"
    assert ["udisksctl", "mount", "-b", "/dev/mmcblk0p1"] in herramientas.llamadas()


# ------------------------------------------ Opciones de mkfs.vfat --
def test_mkfs_conserva_fat32_cluster_y_etiqueta_normalizada(entorno):
    herramientas, device, _punto = entorno()

    _formatear(herramientas, device, label="Fotos Mamá", sectors_per_cluster=64)

    assert _mkfs(herramientas)[1:] == ["-F", "32", "-s", "64", "-n", "FOTOS MAMA",
                                      "/dev/sdb1"]


def test_mkfs_sin_etiqueta_ni_cluster(entorno):
    herramientas, device, _punto = entorno()

    _formatear(herramientas, device, label="   ")

    assert _mkfs(herramientas)[1:] == ["-F", "32", "/dev/sdb1"]


def test_format_as_wii_usb_pasa_por_el_mismo_camino_y_crea_carpetas(entorno):
    herramientas, device, punto = entorno(particiones=["/dev/sdb1"], tabla="msdos")

    resultado = drives.format_as_wii_usb(device, run=herramientas.run,
                                         mount_timeout=2.0)

    assert resultado == punto
    for carpeta in drives.FACTORY_FOLDERS:
        assert (punto / carpeta).is_dir()
    assert _mkfs(herramientas)[1:] == [
        "-F", "32", "-s", str(drives.WII_USB_SECTORS_PER_CLUSTER),
        "-n", drives.WII_USB_LABEL, "/dev/sdb1"]


def test_el_error_de_mkfs_llega_tal_cual(entorno):
    herramientas, device, _punto = entorno()
    herramientas.hacer_fallar_mkfs("mkfs.vfat: Device is too big for FAT32")

    with pytest.raises(RuntimeError, match="too big"):
        _formatear(herramientas, device)
    assert "udisksctl" not in herramientas.nombres()


# --------------------- Identidad: último chequeo justo antes de wipefs --
def test_la_identidad_se_reverifica_antes_del_primer_wipefs(entorno):
    """Orden en el log: hay una consulta de identidad DESPUÉS del último
    paso no destructivo (el pedido de contraseña) y ANTES del primer
    `wipefs`, y otra entre `parted` y `mkfs.vfat`."""
    herramientas, device, _punto = entorno(particiones=["/dev/sdb1"])
    herramientas.al_pedir_contrasena = lambda: herramientas.estado.joinpath(
        "llamadas.log").open("a").write("PKEXEC\n")

    _formatear(herramientas, device)

    nombres = herramientas.nombres()
    contrasena = nombres.index("PKEXEC")
    primer_wipefs = nombres.index("wipefs")
    assert "udevadm" in nombres[contrasena:primer_wipefs]
    llamadas = herramientas.llamadas()
    ultimo_parted = max(i for i, c in enumerate(llamadas) if c[0] == "parted")
    mkfs = nombres.index("mkfs.vfat")
    assert ["udevadm", "info", "--query=property", "--name=/dev/sdb"] in \
        llamadas[ultimo_parted:mkfs]


def test_usb_cambiado_mientras_se_pedia_la_contrasena_no_se_toca(entorno):
    """Los chequeos en Python pasan (misma serie), pero mientras el
    diálogo de `pkexec` está abierto conectan otro USB del mismo tamaño
    en el mismo puerto. El script lo detecta antes de `wipefs`."""
    herramientas, device, _punto = entorno(particiones=["/dev/sdb1"])
    herramientas.al_pedir_contrasena = lambda: herramientas.fijar_identidad("SERIE-B")

    with pytest.raises(drives.DeviceIdentityMismatchError):
        _formatear(herramientas, device)

    nombres = herramientas.nombres()
    for destructivo in ("wipefs", "parted", "mkfs.vfat"):
        assert destructivo not in nombres
    assert herramientas.particiones() == ["/dev/sdb1"]


def test_identidad_que_cambia_entre_parted_y_mkfs_frena_el_mkfs(entorno):
    herramientas, device, _punto = entorno()
    herramientas.cambiar_identidad_despues_de_parted("SERIE-B")

    with pytest.raises(drives.DeviceIdentityMismatchError):
        _formatear(herramientas, device)
    assert "mkfs.vfat" not in herramientas.nombres()


def test_tamano_que_cambia_mientras_se_pedia_la_contrasena(entorno, tmp_path):
    herramientas, device, _punto = entorno()
    herramientas.al_pedir_contrasena = lambda: (
        drives._SYS_BLOCK / "sdb" / "size").write_text("123\n")

    with pytest.raises(drives.DeviceChangedError):
        _formatear(herramientas, device)
    assert "wipefs" not in herramientas.nombres()


def test_deja_de_ser_removible_mientras_se_pedia_la_contrasena(entorno):
    herramientas, device, _punto = entorno()
    herramientas.al_pedir_contrasena = lambda: (
        drives._SYS_BLOCK / "sdb" / "removable").write_text("0\n")

    with pytest.raises(drives.UnsafeDeviceError):
        _formatear(herramientas, device)
    assert "wipefs" not in herramientas.nombres()


def test_automontado_mientras_se_pedia_la_contrasena(entorno):
    herramientas, device, _punto = entorno(particiones=["/dev/sdb1"])

    def _automontar():
        drives._PROC_MOUNTS.write_text("/dev/sdb1 /run/media/kendry/X vfat rw 0 0\n")
    herramientas.al_pedir_contrasena = _automontar

    with pytest.raises(drives.StillMountedError):
        _formatear(herramientas, device)
    assert "wipefs" not in herramientas.nombres()


def test_sin_identidad_capturada_el_script_no_la_exige(entorno):
    """Mismo criterio que `verify_still_safe`: si al listar no se pudo
    capturar la identidad (lector de SD sin serie), no hay contra qué
    comparar y no se bloquea."""
    herramientas, device, _punto = entorno(identidad=None)

    _formatear(herramientas, device)
    assert herramientas.formateado() == "/dev/sdb1"


@pytest.mark.parametrize("propiedades", [
    {"ID_SERIAL": "Kingston_DT_ABC", "ID_SERIAL_SHORT": "ABC", "ID_WWN": "0x5"},
    {"ID_SERIAL_SHORT": "ABC", "ID_WWN": "0x5"},
    {"ID_WWN": "0x5000"},
])
def test_el_script_lee_la_identidad_igual_que_device_identity(entorno, propiedades):
    """La identidad que compara el script tiene que ser la misma que
    capturó `device_identity` (misma precedencia de propiedades), o un
    USB legítimo sin ID_SERIAL se rechazaría siempre."""
    herramientas, device, _punto = entorno()
    herramientas.fijar_propiedades_udev(propiedades)
    identidad = drives.device_identity(device.path, run=herramientas.run)
    device = drives.BlockDevice(path=device.path, model=device.model,
                                size_bytes=device.size_bytes, identity=identidad)

    _formatear(herramientas, device)
    assert herramientas.formateado() == "/dev/sdb1"


# ------------------------------------------------- pkexec --
def test_contrasena_cancelada_no_toca_nada(entorno):
    herramientas, device, _punto = entorno()
    comandos = []

    def _run(cmd, **kwargs):
        comandos.append(cmd)
        if cmd[0] == "pkexec":
            return subprocess.CompletedProcess(cmd, 126, "", "")
        return herramientas.run(cmd, **kwargs)

    if os.geteuid() == 0:
        pytest.skip("como root no se usa pkexec")
    with pytest.raises(RuntimeError, match="autorización"):
        drives.format_fat32(device, run=_run, mount_timeout=2.0)
    assert sum(1 for c in comandos if c[0] == "pkexec") == 1
    assert "wipefs" not in herramientas.nombres()


def test_una_sola_llamada_privilegiada(entorno):
    herramientas, device, _punto = entorno(particiones=["/dev/sdb1"])
    comandos = []

    def _run(cmd, **kwargs):
        comandos.append(list(cmd))
        return herramientas.run(cmd, **kwargs)

    drives.format_fat32(device, run=_run, mount_timeout=2.0)

    privilegiados = [c for c in comandos if c[0] == "pkexec"
                     or c[:2] == ["/bin/sh", "-c"]]
    assert len(privilegiados) == 1
    assert ["udisksctl", "mount", "-b", "/dev/sdb1"] in comandos


# -------------------------------- Montaje como usuario + postcondición --
def test_el_montaje_no_corre_dentro_del_paso_privilegiado(entorno):
    """udisksctl se llama desde Python, como el usuario, y DESPUÉS del
    script: si montara root, el FAT quedaría con uid=0."""
    herramientas, device, _punto = entorno()
    _formatear(herramientas, device)
    nombres = herramientas.nombres()
    assert nombres.index("udisksctl") > nombres.index("mkfs.vfat")
    assert "udisksctl" not in drives._FORMAT_SCRIPT
    assert "mount " not in drives._FORMAT_SCRIPT


def test_montaje_de_otro_usuario_no_se_reporta_como_exito(entorno, monkeypatch):
    """Lo que pasaba con /run/media/root/...: el punto de montaje existe,
    pero no es del usuario que corre la app."""
    herramientas, device, _punto = entorno()
    uid_real = os.getuid()
    monkeypatch.setattr(drives.os, "getuid", lambda: uid_real + 1)

    with pytest.raises(drives.MountNotWritableError, match="pertenece"):
        _formatear(herramientas, device)


def test_montaje_sin_permiso_de_escritura_no_se_reporta_como_exito(entorno):
    if os.geteuid() == 0:
        pytest.skip("root escribe aunque el modo diga que no")
    herramientas, device, punto = entorno()
    punto.mkdir(parents=True)
    punto.chmod(0o500)
    try:
        with pytest.raises(drives.MountNotWritableError):
            _formatear(herramientas, device)
    finally:
        punto.chmod(0o700)


def test_partitions_of_filtra_solo_las_particiones():
    def _run(cmd, **_k):
        assert cmd == ["lsblk", "-nrpo", "NAME,TYPE", "/dev/nvme1n1"]
        return subprocess.CompletedProcess(
            cmd, 0, "/dev/nvme1n1 disk\n/dev/nvme1n1p1 part\n/dev/nvme1n1p2 part\n", "")
    assert drives.partitions_of("/dev/nvme1n1", run=_run) == [
        Path("/dev/nvme1n1p1"), Path("/dev/nvme1n1p2")]
