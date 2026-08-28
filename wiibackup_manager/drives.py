"""Detección de discos/USB/SD removibles montados en el sistema.

En Fedora/GNOME (udisks2 + gvfs) las unidades removibles se auto-montan en
/run/media/$USER/<etiqueta> (algunas otras distros usan /media/$USER/). No
hace falta hablar con udisks2 por D-Bus para esto: alcanza con listar esos
directorios y medir el espacio libre de cada punto de montaje.

Modo Fábrica (preparar un USB desde cero) es la parte de este módulo con
más para perder: un error acá no arruina un juego, arruina el disco que
sea que esté en `/dev/sdX` en ese momento. Por eso las funciones de más
abajo (`is_removable_block_device`, `mounted_critical_paths`,
`verify_still_safe`, `check_no_critical_mounts`) no comparten NADA de
estado entre sí ni cachean nada: cada una lee el estado real del kernel en
el momento en que se la llama. `format_as_wii_usb` las vuelve a llamar a
todas justo antes de tocar el disco, sin confiar en un chequeo que se
haya hecho antes (por ejemplo, al abrir el diálogo de confirmación en la
interfaz) porque entre que se abre ese diálogo y que el usuario confirma
puede pasar cualquier cosa: que desconecte el USB y conecte otra cosa en
el mismo puerto, que el propio kernel reasigne la letra del dispositivo.

Los "blindajes" a los que se refieren los docstrings de acá son:

- BLINDAJE 1 (`is_removable_block_device` / `list_candidate_drives`):
  lista blanca. Un disco fijo (NVMe, SATA interno) nunca debería llegar
  a mostrarse como candidato, y punto.
- BLINDAJE 2: vive en la interfaz (Objetivo 1/2 de la ventana), no acá:
  el diálogo de confirmación que obliga a escribir "FORMATEAR".
- BLINDAJE 3 (`verify_still_safe`): re-chequeo, ya en el hilo de fondo,
  de que el dispositivo sigue siendo removible y pesa lo mismo que
  cuando se armó el diálogo.
- BLINDAJE 4 (`check_no_critical_mounts` / `mounted_critical_paths`):
  última línea de defensa. Se corre siempre, pase lo que pase con los
  blindajes anteriores: si alguna partición del disco está montada en
  una ruta del sistema operativo, se aborta.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

MOUNT_ROOTS = ("/run/media", "/media")


@dataclass
class DriveInfo:
    name: str
    mount_point: Path
    free_bytes: int
    total_bytes: int

    @property
    def free_gb(self) -> float:
        return self.free_bytes / (1024 ** 3)

    @property
    def total_gb(self) -> float:
        return self.total_bytes / (1024 ** 3)


def list_removable_drives() -> list[DriveInfo]:
    """Escanea /run/media/$USER y /media/$USER en busca de unidades montadas."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
    drives: list[DriveInfo] = []
    seen: set[str] = set()

    for root in MOUNT_ROOTS:
        base = Path(root) / user
        if not base.is_dir():
            continue
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            # os.path.ismount filtra subcarpetas que no son, ellas mismas,
            # un punto de montaje (por ejemplo si alguien creó una carpeta
            # vacía ahí a mano).
            if not entry.is_dir() or not os.path.ismount(entry):
                continue
            key = str(entry.resolve())
            if key in seen:
                continue
            seen.add(key)
            try:
                usage = shutil.disk_usage(entry)
            except OSError:
                continue
            drives.append(DriveInfo(
                name=entry.name,
                mount_point=entry,
                free_bytes=usage.free,
                total_bytes=usage.total,
            ))

    return drives


def is_mount_point(path: Path) -> bool:
    """True si `path` es en sí mismo un punto de montaje (y no, por ejemplo,
    una carpeta cualquiera del disco interno), condición para que tenga
    sentido ofrecer expulsarlo."""
    try:
        return os.path.ismount(path)
    except OSError:
        return False


def _block_device_for(path: Path) -> str | None:
    """Dispositivo de bloque (ej. /dev/sdb1) montado en `path`, vía findmnt."""
    try:
        result = subprocess.run(
            ["findmnt", "-n", "-o", "SOURCE", "--target", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


# FAT clásico (FAT12/16/32, que Linux suele reportar como "vfat" o a veces
# "msdos"): límite duro de ~4GiB por archivo. exFAT explícitamente NO entra
# acá: se diseñó justamente para levantar ese límite.
_FAT32_FSTYPES = {"vfat", "fat", "fat12", "fat16", "fat32", "msdos"}


def filesystem_of(path: Path) -> str | None:
    """Tipo de filesystem (ej. 'vfat', 'ext4') montado en `path` o en el
    punto de montaje que lo contiene, vía findmnt. None si no se pudo
    determinar con confianza (findmnt ausente, `path` no corresponde a
    ningún punto de montaje reconocible, etc.)."""
    try:
        result = subprocess.run(
            ["findmnt", "-no", "FSTYPE", "--target", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.strip().splitlines()
    return lines[0].strip().lower() if lines and lines[0].strip() else None


def needs_wbfs_split(path: Path) -> bool:
    """True si hay que dividir el WBFS resultante en partes de ~4GiB para
    que quepa en el filesystem del destino (`path`, que debe existir).

    FAT32 tiene un límite duro de ~4GiB por archivo, y hay discos Wii
    dual-layer que lo superan. Si no podemos determinar el filesystem con
    confianza (findmnt ausente, montaje no reconocible, etc.) preferimos
    dividir de todos modos: la opción `--split` de `wit` solo separa el
    archivo cuando el resultado realmente supera ese límite (ver
    `wit_wrapper.convert`), así que pedir la división "por las dudas" en un
    filesystem que sí soporta archivos grandes no rompe nada, solo es una
    precaución de sobra."""
    fstype = filesystem_of(path)
    if fstype is None:
        return True
    return fstype in _FAT32_FSTYPES


def eject_mount_point(path: Path) -> tuple[bool, str]:
    """Desmonta de forma segura `path` para que la unidad se pueda
    desconectar físicamente. Usa udisksctl (unmount + power-off, lo que
    además apaga eléctricamente la unidad removible cuando el hardware lo
    soporta) y si no está disponible cae a `gio mount -u`."""
    if not is_mount_point(path):
        return False, f"'{path}' no es un punto de montaje: no hace falta expulsarlo."

    device = _block_device_for(path)
    if device:
        try:
            result = subprocess.run(
                ["udisksctl", "unmount", "-b", device],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            # power-off es mejor esfuerzo: no todos los lectores de tarjetas
            # SD lo soportan, pero cuando funciona deja la unidad lista para
            # desconectar sin más riesgo que el de un USB ya desmontado.
            subprocess.run(["udisksctl", "power-off", "-b", device],
                            capture_output=True, text=True, timeout=30)
            return True, "Unidad expulsada de forma segura."
        if result is not None:
            stderr = result.stderr.strip()
            if "not authorized" not in stderr.lower():
                return False, stderr or "No se pudo desmontar la unidad."

    try:
        result = subprocess.run(
            ["gio", "mount", "-u", str(path)],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"No se pudo expulsar la unidad: {e}"

    if result.returncode == 0:
        return True, "Unidad expulsada de forma segura."
    return False, result.stderr.strip() or "No se pudo desmontar la unidad."


# ================================================================== #
# Modo Fábrica: preparar un USB desde cero (formatear + estructura). #
# ================================================================== #
#
# Rutas del sistema como constantes de módulo (y no hardcodeadas dentro de
# cada función) a propósito: así los tests pueden apuntarlas a un
# /sys/block y un /proc/mounts de mentira con monkeypatch, sin tocar el
# disco real de la máquina que corre la suite.
_SYS_BLOCK = Path("/sys/block")
_PROC_MOUNTS = Path("/proc/mounts")

# Puntos de montaje del sistema operativo que jamás deberían aparecer
# colgando de un disco que se está por formatear. No es "la raíz nada
# más": /boot/efi separado de /boot es un layout comunísimo (Fedora con
# UEFI), y /home puede vivir en su propio disco/partición.
CRITICAL_MOUNTPOINTS = frozenset({
    "/", "/home", "/root", "/boot", "/boot/efi", "/usr", "/var", "/etc",
    "/opt", "/srv", "/tmp",
})

# Carpetas que espera encontrar USB Loader GX / Nintendont en un USB
# recién preparado.
FACTORY_FOLDERS = ("apps", "games", "wbfs")

# Discos que /sys/block puede listar pero que nunca son "un USB para
# preparar": loopback, ramdisk, device-mapper, zram, lectores ópticos.
_IGNORED_BLOCK_PREFIXES = ("loop", "ram", "dm-", "zram", "sr")


class FactoryModeError(RuntimeError):
    """Base de los errores que aborta el Modo Fábrica. Nunca se levanta
    directo: siempre una de las subclases de abajo, para que quien
    llame sepa CUÁL blindaje frenó la operación y se lo pueda decir al
    usuario con precisión."""


class UnsafeDeviceError(FactoryModeError):
    """BLINDAJE 1 / 3: el dispositivo no es removible (o dejó de serlo, o
    directamente ya no existe)."""


class DeviceChangedError(FactoryModeError):
    """BLINDAJE 3: el dispositivo cambió de tamaño desde que se armó la
    confirmación. Es la señal de que puede ser un disco distinto -mismo
    nombre de dispositivo, otro USB- y no el que el usuario aprobó."""


class CriticalMountError(FactoryModeError):
    """BLINDAJE 4: alguna partición del dispositivo está montada en una
    ruta crítica del sistema operativo."""


class StillMountedError(FactoryModeError):
    """BLINDAJE 5: después de intentar desmontar todo, `device_path` (o
    alguna de sus particiones) sigue apareciendo en /proc/mounts. Pasa,
    por ejemplo, si `umount` falla en silencio por permisos insuficientes:
    sin este chequeo, `mkfs.vfat` correría igual sobre un dispositivo con
    una partición todavía montada, con riesgo de corromper los datos que
    tenga esa partición."""


def _device_name(device_path) -> str:
    """'/dev/sdb' -> 'sdb'. Nombre tal como aparece bajo /sys/block."""
    return Path(device_path).name


def is_removable_block_device(device_path) -> bool:
    """BLINDAJE 1: ¿el kernel dice que este dispositivo es removible?

    Fuente de verdad: /sys/block/<disco>/removable, que lo expone el
    propio kernel (0 = fijo, 1 = removible) y no depende de heurísticas de
    nombre ("sdb debe ser un USB", falso en máquinas con varios discos
    SATA) ni de lo que reporte udisks2/gvfs, que en discos SATA
    hot-plug puede no coincidir.

    Si no se puede leer el archivo (dispositivo inexistente, sin
    permisos) se falla CERRADO: no removible, no es candidato. No poder
    confirmar que algo es seguro no es lo mismo que confirmar que lo es."""
    flag_path = _SYS_BLOCK / _device_name(device_path) / "removable"
    try:
        return flag_path.read_text().strip() == "1"
    except OSError:
        return False


def device_size_bytes(device_path) -> int | None:
    """Tamaño real de `device_path` en bytes, leído del kernel
    (/sys/block/<disco>/size está en sectores de 512 bytes, siempre,
    independientemente del tamaño de bloque físico del disco). None si no
    se pudo leer."""
    size_path = _SYS_BLOCK / _device_name(device_path) / "size"
    try:
        return int(size_path.read_text().strip()) * 512
    except (OSError, ValueError):
        return None


def _device_model(device_path) -> str:
    """Modelo del disco (ej. 'Ultra_USB_3.0'), solo para mostrar en la
    interfaz. Puramente cosmético: nunca se usa para decidir nada de
    seguridad, así que si no está disponible (falta en varios tipos de
    dispositivo, ej. tarjetas SD por lector integrado) no es un problema,
    da string vacío."""
    model_path = _SYS_BLOCK / _device_name(device_path) / "device" / "model"
    try:
        return model_path.read_text().strip()
    except OSError:
        return ""


@dataclass(frozen=True)
class BlockDevice:
    """Un disco candidato a Modo Fábrica. `size_bytes` queda congelado acá
    en el momento en que se listó -es justamente lo que `verify_still_safe`
    vuelve a medir más tarde para detectar que cambió."""
    path: Path
    model: str
    size_bytes: int

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 ** 3)

    @property
    def display_name(self) -> str:
        """Texto para el desplegable y el diálogo de confirmación: modelo
        y tamaño primero (lo que el usuario reconoce a simple vista), la
        ruta del dispositivo al final como referencia técnica."""
        modelo = self.model or "Disco"
        return f"{modelo} ({self.size_gb:.1f} GB) — {self.path}"


def list_candidate_drives() -> list[BlockDevice]:
    """BLINDAJE 1 aplicado al listado completo: recorre /sys/block y
    devuelve SOLO lo que el kernel marca removable=1. Un disco interno
    (NVMe, SATA) no llega a esta lista bajo ninguna circunstancia, así que
    ni siquiera puede llegar a mostrarse en el desplegable de la interfaz
    para que alguien lo elija por error."""
    candidatos: list[BlockDevice] = []
    try:
        entradas = sorted(_SYS_BLOCK.iterdir())
    except OSError:
        return []
    for entrada in entradas:
        if entrada.name.startswith(_IGNORED_BLOCK_PREFIXES):
            continue
        device_path = Path("/dev") / entrada.name
        if not is_removable_block_device(device_path):
            continue
        size = device_size_bytes(device_path)
        if not size:
            continue
        candidatos.append(BlockDevice(path=device_path,
                                      model=_device_model(device_path),
                                      size_bytes=size))
    return candidatos


def _unescape_mount_field(value: str) -> str:
    """/proc/mounts escapa espacio, tab, salto de línea y backslash como
    \\040/\\011/\\012/\\134 dentro de cada campo (formato mtab clásico)."""
    return (value.replace("\\040", " ").replace("\\011", "\t")
                 .replace("\\012", "\n").replace("\\134", "\\"))


def _iter_proc_mounts() -> list[tuple[str, str]]:
    """[(dispositivo_origen, punto_de_montaje), ...] tal como los reporta
    el kernel en este mismo instante. Se lee /proc/mounts (y no
    /etc/mtab, que puede estar desactualizado o ser un symlink roto) para
    tener el estado real, no lo que alguien escribió en algún momento."""
    texto = _PROC_MOUNTS.read_text()
    filas = []
    for linea in texto.splitlines():
        partes = linea.split()
        if len(partes) < 2:
            continue
        filas.append((_unescape_mount_field(partes[0]),
                      _unescape_mount_field(partes[1])))
    return filas


def _partition_pattern(device_path) -> re.Pattern:
    """Coincide con `device_path` (el disco entero) y con cualquiera de
    sus particiones: /dev/sdb -> sdb, sdb1, sdb2...; /dev/nvme0n1 ->
    nvme0n1, nvme0n1p1...; /dev/loop0 (con -P de losetup) -> loop0,
    loop0p1..."""
    return re.compile(rf"^{re.escape(str(device_path))}(p?\d+)?$")


def mounted_critical_paths(device_path) -> list[str]:
    """BLINDAJE 4, la última línea de defensa: ¿alguna partición de este
    disco (o el disco entero, montado sin particionar) está montada en
    una ruta crítica del sistema operativo AHORA MISMO?

    No comparte estado con ningún blindaje anterior: lee /proc/mounts de
    nuevo cada vez que se la llama, porque su único trabajo es no confiar
    en nada que se haya sabido en otro momento.

    Devuelve la lista de rutas críticas encontradas (vacía = seguro). Si
    /proc/mounts no se puede leer, se falla CERRADO: se devuelve como si
    TODAS las rutas críticas estuvieran ahí, porque no poder confirmar
    que es seguro formatear no es lo mismo que confirmar que lo es."""
    patron = _partition_pattern(device_path)
    try:
        filas = _iter_proc_mounts()
    except OSError:
        return sorted(CRITICAL_MOUNTPOINTS)
    return [punto for origen, punto in filas
            if patron.match(origen) and punto in CRITICAL_MOUNTPOINTS]


def _mount_points_of(device_path) -> list[tuple[str, str]]:
    """[(origen, punto_de_montaje), ...] de TODO lo que esté montado de
    `device_path` o sus particiones (no solo lo crítico): es lo que hay
    que desmontar antes de formatear."""
    patron = _partition_pattern(device_path)
    try:
        filas = _iter_proc_mounts()
    except OSError:
        return []
    return [(origen, punto) for origen, punto in filas if patron.match(origen)]


def verify_still_safe(device: BlockDevice) -> None:
    """BLINDAJE 3: re-chequeo, ya en el hilo de fondo y justo antes de
    escribir, de que `device.path` sigue siendo removible y pesa lo mismo
    que cuando se armó el diálogo de confirmación.

    Entre que el usuario ve el diálogo (con el modelo y el tamaño
    impresos) y aprieta el botón puede pasar cualquier cosa: que saque el
    USB y conecte otro dispositivo en el mismo puerto (el kernel suele
    reciclar `/dev/sdb` para el próximo que aparezca), o simplemente que
    haya elegido mal en una máquina con varios discos conectados. Esta
    función no confía en nada de lo que se sabía al armar el diálogo:
    vuelve a preguntarle al kernel.

    No devuelve nada: levanta `UnsafeDeviceError` o `DeviceChangedError`
    si algo no cierra."""
    if not is_removable_block_device(device.path):
        raise UnsafeDeviceError(
            f"{device.path} ya no es un dispositivo removible (o "
            "desapareció). Se aborta el formateo por seguridad.")
    tamano_actual = device_size_bytes(device.path)
    if tamano_actual is None or tamano_actual != device.size_bytes:
        raise DeviceChangedError(
            f"El tamaño de {device.path} cambió desde que se confirmó "
            f"({device.size_bytes} → {tamano_actual} bytes). Puede ser "
            "otro dispositivo: se aborta el formateo por seguridad.")


def check_no_critical_mounts(device: BlockDevice) -> None:
    """BLINDAJE 4: la última línea de defensa. Se llama SIEMPRE desde
    `format_as_wii_usb`, sin importar que los blindajes 1 y 3 ya hayan
    dado OK -de hecho es la única razón de ser de esta función: no
    confiar en que nada anterior alcance."""
    criticos = mounted_critical_paths(device.path)
    if criticos:
        raise CriticalMountError(
            f"{device.path} tiene una partición montada en "
            f"{', '.join(sorted(criticos))}, que es un punto de montaje "
            "del sistema operativo. Se aborta el formateo.")


def _unmount_all(device_path, *, run=subprocess.run) -> None:
    """Desmonta todo lo que esté montado de `device_path` o sus
    particiones. Best-effort: si algo ya estaba desmontado no es un
    error, y si `umount` falla (dispositivo ocupado, permisos) esta
    función no lo nota -quien llama es responsable de volver a consultar
    /proc/mounts después (BLINDAJE 5, `StillMountedError` más abajo) en
    vez de asumir que acá adentro quedó todo desmontado."""
    for origen, _punto in _mount_points_of(device_path):
        run(["umount", origen], capture_output=True, text=True, timeout=30)


def _mount_after_format(device_path, *, run=subprocess.run,
                        timeout: float = 15.0) -> Path:
    """Monta `device_path` recién formateado y devuelve dónde quedó. Se
    pide el montaje explícito con `udisksctl` en vez de esperar a que
    gvfs lo autodetecte (más rápido y determinista); si igual se
    adelantó y ya está montado, o si `udisksctl` no está disponible, se
    cae a sondear /proc/mounts un rato."""
    try:
        resultado = run(["udisksctl", "mount", "-b", str(device_path)],
                        capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        resultado = None
    if resultado is not None and resultado.returncode == 0:
        match = re.search(r" at (/\S.*)\.\s*$", resultado.stdout.strip())
        if match:
            return Path(match.group(1))

    inicio = time.monotonic()
    while time.monotonic() - inicio < timeout:
        puntos = _mount_points_of(device_path)
        if puntos:
            return Path(puntos[0][1])
        time.sleep(0.2)
    raise RuntimeError(
        f"{device_path} se formateó pero no se pudo montar después.")


def format_as_wii_usb(device: BlockDevice, *, run=subprocess.run,
                       label: str = "WII_USB", cluster_size_kb: int = 64,
                       mount_timeout: float = 15.0) -> Path:
    """Ejecuta el Modo Fábrica de verdad sobre `device`: re-corre los
    blindajes 3 y 4 (nunca se confía en un chequeo hecho en otro momento,
    ni siquiera uno hecho un segundo antes por quien llama), desmonta lo
    que haya montado, confirma con el Blindaje 5 que ese desmontaje surtió
    efecto de verdad, formatea FAT32 vía `mkfs.vfat` y arma la estructura
    de carpetas que esperan USB Loader GX / Nintendont.

    Pensada para correr en un hilo de fondo: no toca GTK ni nada de la
    interfaz -quien la llama es responsable de reportar progreso/resultado
    con `GLib.idle_add`, igual que hace `queue_manager`.

    `mkfs.vfat` se lanza vía `pkexec`, salvo que el proceso YA sea root
    (`os.geteuid() == 0`): eso es lo que pasa en el script de pruebas
    manual, corrido con `sudo`, y pedirle a `pkexec` que lance un agente
    gráfico de autenticación ahí no tendría sentido (y probablemente ni
    funcione sin sesión gráfica). Con un usuario normal desde la app, en
    cambio, `pkexec` es el que muestra el diálogo de contraseña del
    sistema.

    Devuelve el punto de montaje final. Levanta la subclase de
    `FactoryModeError` que corresponda si algún blindaje no pasa, o
    `RuntimeError` si `mkfs.vfat` (o el montaje posterior) fallan."""
    verify_still_safe(device)
    check_no_critical_mounts(device)

    _unmount_all(device.path, run=run)

    puntos_restantes = _mount_points_of(device.path)
    if puntos_restantes:
        raise StillMountedError(
            f"{device.path} sigue teniendo algo montado en "
            f"{', '.join(sorted(punto for _origen, punto in puntos_restantes))} "
            "después de intentar desmontarlo (umount pudo haber fallado "
            "por permisos u otro motivo). Se aborta el formateo por "
            "seguridad.")

    prefijo = [] if os.geteuid() == 0 else ["pkexec"]
    resultado = run(
        prefijo + ["mkfs.vfat", "-F", "32", "-s", str(cluster_size_kb),
                   "-n", label, str(device.path)],
        capture_output=True, text=True, timeout=300,
    )
    if resultado.returncode != 0:
        raise RuntimeError(
            resultado.stderr.strip() or "mkfs.vfat terminó con error desconocido.")

    punto_montaje = _mount_after_format(device.path, run=run, timeout=mount_timeout)
    for carpeta in FACTORY_FOLDERS:
        (punto_montaje / carpeta).mkdir(parents=True, exist_ok=True)

    return punto_montaje
