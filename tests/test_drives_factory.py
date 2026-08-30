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


# ------------------------------------- Blindaje 3: identidad física (udev) --
def _fake_udevadm(propiedades: dict[str, str]):
    """`run` falso que simula `udevadm info --query=property --name=...`,
    devolviendo `propiedades` como líneas CLAVE=valor (el formato real de
    udevadm)."""
    salida = "\n".join(f"{k}={v}" for k, v in propiedades.items()) + "\n"

    def _run(cmd, **_k):
        assert cmd[:2] == ["udevadm", "info"]
        return subprocess.CompletedProcess(cmd, 0, salida, "")
    return _run


def test_device_identity_prefiere_id_serial(sys_block):
    run = _fake_udevadm({"ID_SERIAL": "SanDisk_Ultra_AA11",
                         "ID_SERIAL_SHORT": "AA11", "ID_WWN": "0xdead"})
    assert drives.device_identity("/dev/sdb", run=run) == "SanDisk_Ultra_AA11"


def test_device_identity_cae_a_serial_short_si_falta_id_serial(sys_block):
    run = _fake_udevadm({"ID_SERIAL_SHORT": "AA11", "ID_WWN": "0xdead"})
    assert drives.device_identity("/dev/sdb", run=run) == "AA11"


def test_device_identity_cae_a_wwn_como_ultimo_recurso(sys_block):
    run = _fake_udevadm({"ID_WWN": "0xdead"})
    assert drives.device_identity("/dev/sdb", run=run) == "0xdead"


def test_device_identity_none_si_no_hay_ninguna_propiedad(sys_block):
    """Algunos lectores de tarjetas SD integrados no exponen serie ni WWN:
    None, no una excepción -quien llama decide qué hacer con la falta de
    dato (`verify_still_safe` no bloquea Modo Fábrica por esto solo)."""
    run = _fake_udevadm({"ID_MODEL": "SD_Reader"})
    assert drives.device_identity("/dev/sdb", run=run) is None


def test_device_identity_none_si_udevadm_falla(sys_block):
    def _run(cmd, **_k):
        return subprocess.CompletedProcess(cmd, 2, "", "no such device")
    assert drives.device_identity("/dev/sdb", run=_run) is None


def test_device_identity_none_si_udevadm_no_esta_instalado(sys_block):
    def _run(cmd, **_k):
        raise FileNotFoundError("udevadm")
    assert drives.device_identity("/dev/sdb", run=_run) is None


def test_verify_still_safe_pasa_si_la_identidad_coincide(sys_block):
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    device = drives.BlockDevice(path=_dev("sdb"), model="X",
                                size_bytes=1000 * 512, identity="SERIE-A")
    run = _fake_udevadm({"ID_SERIAL": "SERIE-A"})
    drives.verify_still_safe(device, run=run)  # no levanta nada


def test_verify_still_safe_rechaza_si_la_identidad_cambio(sys_block):
    """El caso central del blindaje: mismo /dev/sdb, mismo tamaño (el
    kernel reasignó el nombre a otro USB de igual capacidad), pero la
    serie que reporta udev ya no es la que se confirmó."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    device = drives.BlockDevice(path=_dev("sdb"), model="X",
                                size_bytes=1000 * 512, identity="SERIE-A")
    run = _fake_udevadm({"ID_SERIAL": "SERIE-B"})
    with pytest.raises(drives.DeviceIdentityMismatchError):
        drives.verify_still_safe(device, run=run)


def test_verify_still_safe_no_chequea_identidad_si_no_se_pudo_capturar(sys_block):
    """Si al listar el dispositivo no se pudo determinar su identidad
    (`BlockDevice.identity is None`), `verify_still_safe` no la vuelve a
    pedir: no hay nada contra qué comparar, y tratar "no sé" como
    "cambió" bloquearía Modo Fábrica entero para esos dispositivos."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    device = drives.BlockDevice(path=_dev("sdb"), model="X",
                                size_bytes=1000 * 512, identity=None)

    def _no_deberia_llamarse(cmd, **_k):
        raise AssertionError("no debería consultar udevadm si no hay "
                             "identidad previa contra qué comparar")

    drives.verify_still_safe(device, run=_no_deberia_llamarse)  # no levanta nada


# ------------------------------------- Blindaje 4: montajes críticos --
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


# ------------------------------- Recurso físico (para el OperationManager) --
@pytest.fixture
def sys_class_block(tmp_path, monkeypatch):
    """Un /sys/class/block de juguete: symlinks planos apuntando a un
    árbol de directorios reales, igual que el de verdad (donde una
    partición vive anidada adentro de la carpeta de su disco, y el
    archivo `partition` solo existe en las particiones)."""
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


def _fake_findmnt(origen_por_destino: dict[str, str]):
    """`run` falso que simula `findmnt -n -o SOURCE --target <path>`."""
    def _run(cmd, **_k):
        assert cmd[0] == "findmnt"
        destino = cmd[-1]
        origen = origen_por_destino.get(destino)
        if origen is None:
            return subprocess.CompletedProcess(cmd, 1, "", "")
        return subprocess.CompletedProcess(cmd, 0, origen + "\n", "")
    return _run


def test_whole_disk_path_de_una_particion_da_el_disco_entero(sys_class_block):
    sys_class_block("sdb", particiones=("sdb1", "sdb2"))
    assert drives._whole_disk_path("/dev/sdb1") == Path("/dev/sdb")


def test_whole_disk_path_de_un_disco_entero_se_devuelve_igual(sys_class_block):
    sys_class_block("sdb")
    assert drives._whole_disk_path("/dev/sdb") == Path("/dev/sdb")


def test_whole_disk_path_none_si_no_esta_en_sys_class_block(sys_class_block):
    sys_class_block("sdb")
    assert drives._whole_disk_path("/dev/no-existe") is None


def test_physical_disk_for_path_resuelve_particion_montada(sys_class_block, monkeypatch):
    sys_class_block("sdb", particiones=("sdb1",))
    monkeypatch.setattr(drives.subprocess, "run",
                        _fake_findmnt({"/run/media/usuario/MIUSB": "/dev/sdb1"}))
    resultado = drives.physical_disk_for_path("/run/media/usuario/MIUSB")
    assert resultado == Path("/dev/sdb")


def test_physical_disk_for_path_none_si_findmnt_no_reconoce_el_punto(
        sys_class_block, monkeypatch):
    sys_class_block("sdb", particiones=("sdb1",))
    monkeypatch.setattr(drives.subprocess, "run", _fake_findmnt({}))
    assert drives.physical_disk_for_path("/no/es/un/montaje") is None


def test_resources_for_mount_point_incluye_el_disco_fisico(sys_class_block, monkeypatch):
    sys_class_block("sdb", particiones=("sdb1",))
    monkeypatch.setattr(drives.subprocess, "run",
                        _fake_findmnt({"/run/media/usuario/MIUSB": "/dev/sdb1"}))
    recursos = drives.resources_for_mount_point("/run/media/usuario/MIUSB")
    assert recursos == [Path("/run/media/usuario/MIUSB"), Path("/dev/sdb")]


def test_resources_for_mount_point_solo_el_punto_si_no_hay_fisico(
        sys_class_block, monkeypatch):
    """Sin poder determinar el disco físico (ej. findmnt no reconoce el
    punto), se degrada al comportamiento de siempre -solo el punto de
    montaje- en vez de romper la operación entera."""
    sys_class_block("sdb", particiones=("sdb1",))
    monkeypatch.setattr(drives.subprocess, "run", _fake_findmnt({}))
    recursos = drives.resources_for_mount_point("/algo/raro")
    assert recursos == [Path("/algo/raro")]


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


def test_format_as_wii_usb_aborta_si_la_identidad_cambia_entre_desmontar_y_mkfs(
        sys_block, proc_mounts):
    """El caso que motiva el segundo chequeo: el Blindaje 3 pasa antes de
    desmontar (misma serie que se confirmó), pero para cuando está por
    correr `mkfs.vfat` -después de desmontar, que puede tardar- la serie
    que reporta udev ya es otra. Nada después del desmontaje debería
    llegar a ejecutarse: ni `mkfs.vfat` ni el montaje posterior."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    proc_mounts([])  # nada montado: blindaje 4 pasa limpio

    llamadas_udevadm = []

    def _fake_run(cmd, **_k):
        if cmd[0] == "umount":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["udevadm", "info"]:
            llamadas_udevadm.append(cmd)
            # Primera consulta (antes de desmontar): la serie confirmada.
            # Segunda consulta (justo antes de mkfs.vfat): otro disco.
            serie = "SERIE-A" if len(llamadas_udevadm) == 1 else "SERIE-B"
            return subprocess.CompletedProcess(cmd, 0, f"ID_SERIAL={serie}\n", "")
        raise AssertionError(f"no debería llegar a ejecutar {cmd} "
                             "-el segundo chequeo de identidad tiene que "
                             "frenar antes de mkfs.vfat")

    device = drives.BlockDevice(path=_dev("sdb"), model="X",
                                size_bytes=1000 * 512, identity="SERIE-A")
    with pytest.raises(drives.DeviceIdentityMismatchError):
        drives.format_as_wii_usb(device, run=_fake_run)

    assert len(llamadas_udevadm) == 2, (
        "tienen que ser exactamente dos consultas a udevadm: una antes de "
        "desmontar y otra justo antes de mkfs.vfat")


# ------------------------------ normalize_fat_label: etiqueta de volumen --
@pytest.mark.parametrize("entrada, esperado", [
    (None, ""),
    ("", ""),
    ("   ", ""),
    ("respaldos", "RESPALDOS"),
    ("  mi disco  ", "MI DISCO"),
    # Los acentos y la ñ se transliteran: FAT no guarda UTF-8, y
    # `mkfs.vfat` mide el límite de 11 en bytes.
    ("Fotos Mamá", "FOTOS MAMA"),
    ("ñandú", "NANDU"),
    # Caracteres que `mkfs.vfat` rechaza: se van, no hacen fallar el
    # comando.
    ("Fotos:2026", "FOTOS2026"),
    ("a/b\\c*d?e", "ABCDE"),
    # Más de 11: se corta en vez de que falle el formateo al final.
    ("ETIQUETA-DEMASIADO-LARGA", "ETIQUETA-DE"),
    # Si no queda nada usable, queda vacío = "sin etiqueta" (NO NAME).
    ("...", ""),
    ("日本", ""),
])
def test_normalize_fat_label(entrada, esperado):
    assert drives.normalize_fat_label(entrada) == esperado


def test_normalize_fat_label_nunca_pasa_del_limite_en_bytes():
    """El límite de FAT es de 11 BYTES, no de 11 caracteres: por eso la
    normalización transliteran a ASCII antes de cortar. Un texto de puros
    acentos, que en UTF-8 ocuparía el doble, tiene que quedar igual dentro
    del límite."""
    etiqueta = drives.normalize_fat_label("áéíóúáéíóúáéíóú")
    assert len(etiqueta.encode()) <= drives.FAT_LABEL_MAX_LEN


# ----------------------- format_fat32: el mecanismo blindado compartido --
def _fake_run_formateo(punto_montaje, comandos):
    """`run` falso para el camino feliz: registra los comandos en
    `comandos` y simula umount/mkfs.vfat/udisksctl sin ejecutar nada."""
    def _run(cmd, **_k):
        comandos.append(cmd)
        if cmd[0] == "umount":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "mkfs.vfat" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[0] == "udisksctl" and cmd[1] == "mount":
            return subprocess.CompletedProcess(
                cmd, 0, f"Mounted /dev/sdb at {punto_montaje}.\n", "")
        raise AssertionError(f"comando inesperado: {cmd}")
    return _run


def test_format_fat32_no_crea_la_estructura_de_wii(sys_block, proc_mounts, tmp_path):
    """La razón de ser de que `format_fat32` exista aparte: es un formateo
    de propósito general. Formatea y monta, y ahí termina -apps/games/wbfs
    son cosa de Modo Fábrica, y una memoria formateada para llevar fotos
    no tiene por qué quedar con tres carpetas de un loader de Wii."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    proc_mounts([])
    punto_montaje = tmp_path / "run_media" / "SIN_ETIQUETA"
    punto_montaje.mkdir(parents=True)

    comandos = []
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=1000 * 512)
    resultado = drives.format_fat32(
        device, run=_fake_run_formateo(punto_montaje, comandos))

    assert resultado == punto_montaje
    assert sorted(p.name for p in punto_montaje.iterdir()) == []
    for carpeta in drives.FACTORY_FOLDERS:
        assert not (punto_montaje / carpeta).exists()


def test_format_fat32_sin_etiqueta_no_le_pasa_n_a_mkfs(
        sys_block, proc_mounts, tmp_path):
    """Etiqueta vacía = "sin etiqueta". Se omite `-n` en vez de pasarle un
    string vacío, así el volumen queda como NO NAME."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    proc_mounts([])
    punto_montaje = tmp_path / "run_media" / "disco"
    punto_montaje.mkdir(parents=True)

    comandos = []
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=1000 * 512)
    drives.format_fat32(device, run=_fake_run_formateo(punto_montaje, comandos),
                        label="   ")

    mkfs_cmd = next(c for c in comandos if "mkfs.vfat" in c)
    assert "-n" not in mkfs_cmd


def test_format_fat32_normaliza_la_etiqueta_antes_de_pasarla(
        sys_block, proc_mounts, tmp_path):
    """Lo que escriba el usuario en el campo de etiqueta llega a
    `mkfs.vfat` ya normalizado, nunca crudo: si no, un acento o una coma
    de más harían fallar el formateo recién al final."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    proc_mounts([])
    punto_montaje = tmp_path / "run_media" / "disco"
    punto_montaje.mkdir(parents=True)

    comandos = []
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=1000 * 512)
    drives.format_fat32(device, run=_fake_run_formateo(punto_montaje, comandos),
                        label="Fotos Mamá")

    mkfs_cmd = next(c for c in comandos if "mkfs.vfat" in c)
    assert mkfs_cmd[mkfs_cmd.index("-n") + 1] == "FOTOS MAMA"


def test_format_fat32_deja_el_cluster_a_mkfs_por_defecto(
        sys_block, proc_mounts, tmp_path):
    """Sin `-s`, `mkfs.vfat` elige el tamaño de clúster según el tamaño
    real del dispositivo. Fijarlo en los 32 KB de Modo Fábrica en una
    memoria chica deja un FAT32 por debajo del mínimo recomendado de
    clústeres (`mkfs.vfat` avisa y lo hace igual) y desperdicia 32 KB por
    archivo: una optimización para los archivos gigantes de un USB de Wii
    que no tiene sentido en un formateo de propósito general."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    proc_mounts([])
    punto_montaje = tmp_path / "run_media" / "disco"
    punto_montaje.mkdir(parents=True)

    comandos = []
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=1000 * 512)
    drives.format_fat32(device, run=_fake_run_formateo(punto_montaje, comandos))

    mkfs_cmd = next(c for c in comandos if "mkfs.vfat" in c)
    assert "-s" not in mkfs_cmd
    assert "-F" in mkfs_cmd and "32" in mkfs_cmd
    assert str(device.path) in mkfs_cmd


def test_format_as_wii_usb_sigue_pidiendo_clusters_de_32kb(
        sys_block, proc_mounts, tmp_path):
    """El contrapunto del test de arriba: que `format_fat32` deje el
    clúster automático NO cambia lo que hace Modo Fábrica, que sí lo fija
    porque los USB Loaders esperan 32 KB (64 sectores de 512 B)."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    proc_mounts([])
    punto_montaje = tmp_path / "run_media" / "WII_USB"
    punto_montaje.mkdir(parents=True)

    comandos = []
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=1000 * 512)
    drives.format_as_wii_usb(device, run=_fake_run_formateo(punto_montaje, comandos))

    mkfs_cmd = next(c for c in comandos if "mkfs.vfat" in c)
    assert mkfs_cmd[mkfs_cmd.index("-s") + 1] == str(drives.WII_USB_SECTORS_PER_CLUSTER)
    assert mkfs_cmd[mkfs_cmd.index("-n") + 1] == drives.WII_USB_LABEL


# ------- format_fat32: los blindajes valen igual que para Modo Fábrica --
def test_format_fat32_respeta_la_lista_blanca_de_removibles(sys_block):
    """El blindaje que no se relaja por ser un formateo "simple": un disco
    que el kernel no marca removable=1 se rechaza igual, sin ejecutar un
    solo comando. Soportar discos externos grandes no significa aceptar
    discos fijos."""
    _make_block_device(sys_block, "nvme0n1", removable=False, size_sectors=10**9)
    device = drives.BlockDevice(path=_dev("nvme0n1"), model="Interno",
                                size_bytes=10**9 * 512)

    def _no_deberia_correr(*_a, **_k):
        raise AssertionError("no debería ejecutarse ningún comando")

    with pytest.raises(drives.UnsafeDeviceError):
        drives.format_fat32(device, run=_no_deberia_correr)


def test_format_fat32_aborta_si_hay_una_particion_en_una_ruta_critica(
        sys_block, proc_mounts):
    """Blindaje 4 en el camino genérico: aunque el dispositivo sea
    removible y del tamaño confirmado, una partición montada en /home
    frena el formateo antes de `mkfs.vfat`."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    proc_mounts(["/dev/sdb1 /home ext4 rw 0 0"])
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=1000 * 512)

    llamadas = []

    def _fake_run(cmd, **_k):
        llamadas.append(cmd)
        raise AssertionError("no debería llegar a ejecutar nada")

    with pytest.raises(drives.CriticalMountError):
        drives.format_fat32(device, run=_fake_run)
    assert llamadas == []


def test_format_fat32_aborta_si_algo_sigue_montado_despues_de_umount(
        sys_block, proc_mounts):
    """Blindaje 5: `umount` puede fallar en silencio (permisos,
    dispositivo ocupado). Si /proc/mounts sigue mostrando la partición
    montada después de intentar desmontarla, `mkfs.vfat` no se corre."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    # Sigue montado antes y después del umount simulado: nada crítico,
    # pero montado al fin.
    proc_mounts(["/dev/sdb1 /run/media/kendry/USB vfat rw 0 0"])
    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=1000 * 512)

    comandos = []

    def _fake_run(cmd, **_k):
        comandos.append(cmd)
        if cmd[0] == "umount":
            return subprocess.CompletedProcess(cmd, 1, "", "umount: target is busy")
        raise AssertionError(f"no debería llegar a ejecutar {cmd}")

    with pytest.raises(drives.StillMountedError):
        drives.format_fat32(device, run=_fake_run)
    assert not any("mkfs.vfat" in c for c in comandos)


def test_format_fat32_revisa_la_identidad_dos_veces(sys_block, proc_mounts):
    """El blindaje más sutil, también acá: la identidad física se
    consulta antes de desmontar y OTRA VEZ justo antes de `mkfs.vfat`. Si
    en el medio el kernel recicló `/dev/sdb` para otro dispositivo del
    mismo tamaño, no se formatea."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    proc_mounts([])

    llamadas_udevadm = []

    def _fake_run(cmd, **_k):
        if cmd[0] == "umount":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:2] == ["udevadm", "info"]:
            llamadas_udevadm.append(cmd)
            serie = "SERIE-A" if len(llamadas_udevadm) == 1 else "SERIE-B"
            return subprocess.CompletedProcess(cmd, 0, f"ID_SERIAL={serie}\n", "")
        raise AssertionError(f"no debería llegar a ejecutar {cmd}")

    device = drives.BlockDevice(path=_dev("sdb"), model="X",
                                size_bytes=1000 * 512, identity="SERIE-A")
    with pytest.raises(drives.DeviceIdentityMismatchError):
        drives.format_fat32(device, run=_fake_run)

    assert len(llamadas_udevadm) == 2


def test_format_fat32_propaga_el_error_de_mkfs(sys_block, proc_mounts):
    """Un disco demasiado grande para FAT32, o cualquier otro rechazo de
    `mkfs.vfat`, llega tal cual a quien llama: es el mensaje que la
    interfaz le muestra al usuario."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    proc_mounts([])

    def _fake_run(cmd, **_k):
        if cmd[0] == "umount":
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if "mkfs.vfat" in cmd:
            return subprocess.CompletedProcess(
                cmd, 1, "", "mkfs.vfat: Device is too big for FAT32")
        raise AssertionError(f"no debería llegar a llamar a {cmd}")

    device = drives.BlockDevice(path=_dev("sdb"), model="X", size_bytes=1000 * 512)
    with pytest.raises(RuntimeError, match="too big"):
        drives.format_fat32(device, run=_fake_run)


# ------------------- candidate_for_mount_point: el puente a la lista blanca --
@pytest.fixture
def identidad_fija(monkeypatch):
    """`device_identity` sin tocar udev de verdad.

    Se parchea la función y no `subprocess.run` porque
    `list_candidate_drives` la llama sin pasarle `run`, así que usa el
    `subprocess.run` que quedó ligado como valor por defecto al importar
    el módulo -parchear el módulo `subprocess` no lo alcanza."""
    monkeypatch.setattr(drives, "device_identity",
                        lambda _path, **_kwargs: "SERIE-USB")


def test_candidate_for_mount_point_da_el_dispositivo_de_la_lista_blanca(
        sys_block, sys_class_block, identidad_fija, monkeypatch):
    """El puente que usa "Verificar Memoria" para pasar del punto de
    montaje (donde trabaja f3) al disco entero (que es lo que se formatea).
    El `BlockDevice` que devuelve tiene que ser el MISMO que devolvería el
    desplegable de Modo Fábrica, con tamaño e identidad incluidos: son los
    valores contra los que `verify_still_safe` compara después."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    sys_class_block("sdb", particiones=("sdb1",))
    monkeypatch.setattr(drives.subprocess, "run",
                        _fake_findmnt({"/run/media/usuario/MIUSB": "/dev/sdb1"}))

    device = drives.candidate_for_mount_point("/run/media/usuario/MIUSB")

    assert device is not None
    assert device.path == _dev("sdb")
    assert device.size_bytes == 1000 * 512
    assert device.identity == "SERIE-USB"


def test_candidate_for_mount_point_none_para_un_disco_no_removible(
        sys_block, sys_class_block, monkeypatch):
    """El caso que importa: un disco interno montado en algún lado NO
    puede llegar a ofrecerse para formatear por este camino. La lista
    blanca es la misma que la de Modo Fábrica y no se afloja porque el
    formateo sea "simple"."""
    _make_block_device(sys_block, "nvme0n1", removable=False, size_sectors=10**9)
    sys_class_block("nvme0n1", particiones=("nvme0n1p1",))
    monkeypatch.setattr(drives.subprocess, "run",
                        _fake_findmnt({"/datos": "/dev/nvme0n1p1"}))

    assert drives.candidate_for_mount_point("/datos") is None


def test_candidate_for_mount_point_none_si_no_se_resuelve_el_disco(
        sys_block, sys_class_block, monkeypatch):
    """Sin poder determinar qué disco hay detrás del punto de montaje (un
    filesystem de red, un findmnt que no lo reconoce) no hay nada que
    formatear: se falla cerrado, no se adivina."""
    _make_block_device(sys_block, "sdb", removable=True, size_sectors=1000)
    sys_class_block("sdb", particiones=("sdb1",))
    monkeypatch.setattr(drives.subprocess, "run", _fake_findmnt({}))

    assert drives.candidate_for_mount_point("/no/es/un/montaje") is None
