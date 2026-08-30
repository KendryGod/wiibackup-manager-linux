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
el momento en que se la llama. `format_fat32` las vuelve a llamar a
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
  de que el dispositivo sigue siendo removible, pesa lo mismo y tiene la
  misma identidad física (serie/WWN vía udev) que cuando se armó el
  diálogo. `format_fat32` lo corre DOS veces -antes de desmontar y
  otra vez justo antes de `mkfs.vfat`- porque ahí en el medio hay una
  ventana real: el desmontaje puede tardar, y es el momento en que menos
  se está mirando el dispositivo.
- BLINDAJE 4 (`check_no_critical_mounts` / `mounted_critical_paths`):
  última línea de defensa. Se corre siempre, pase lo que pase con los
  blindajes anteriores: si alguna partición del disco está montada en
  una ruta del sistema operativo, se aborta.

Formatear en FAT32 es UNA sola función -`format_fat32`- y los dos flujos
que formatean pasan por ahí: Modo Fábrica (`format_as_wii_usb`, que no es
más que `format_fat32` y encima la estructura apps/games/wbfs) y el
formateo de propósito general que se ofrece al terminar de verificar una
memoria. Los blindajes viven ADENTRO de `format_fat32`, no en cada
llamador: así un flujo nuevo que quiera formatear no puede olvidarse de
correr alguno, ni puede terminar con una copia propia -y desactualizada-
de la lista blanca de removibles. Lo único que cambia entre un flujo y
otro es lo que pasa DESPUÉS de montar (la estructura de carpetas de Wii,
o nada) y qué tamaño de clúster se pide.

Un blindaje más que no vive en las funciones de arriba sino en cómo se
usa `OperationManager` (`operations.py`): Modo Fábrica registra el disco
entero (`BlockDevice.path`, ej. `/dev/sdb`) como `resources` de una
operación `FORMATTING` antes de tocarlo, y Transferencias/Homebrew hacen
lo mismo con el disco físico detrás de su punto de montaje
(`physical_disk_for_path`, más abajo) además del punto de montaje de
siempre. Así el `OperationManager` bloquea formatear un disco mientras se
le está escribiendo algo, y viceversa -sin esto, nada impedía lanzar
`mkfs.vfat` sobre una unidad con una copia a mitad de camino.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
import unicodedata
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
# /sys/class/block trae disco enteros Y particiones en un mismo nivel
# (a diferencia de /sys/block, que solo lista discos enteros como
# subcarpetas): es lo que permite mapear una partición montada de vuelta
# al disco físico que la contiene. Ver `_whole_disk_path`.
_SYS_CLASS_BLOCK = Path("/sys/class/block")

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

# Valores propios de Modo Fábrica, que NO valen para un formateo de
# propósito general: la etiqueta con la que se reconoce una unidad
# preparada por la app, y los 64 sectores por clúster (64 * 512 B = 32 KB)
# que recomiendan USB Loader GX y Nintendont para discos grandes.
#
# En un formateo genérico el clúster se deja elegir a `mkfs.vfat`, que lo
# ajusta al tamaño real del dispositivo. Fijar 32 KB en una memoria chica
# no falla -`mkfs.vfat` 4.2 formatea igual, avisando "Number of clusters
# for 32 bit FAT is less then suggested minimum"- pero deja un
# filesystem por debajo del mínimo recomendado para FAT32 (que algún
# lector puede no querer) y desperdicia 32 KB por cada archivo chico. Para
# una memoria que va a llevar fotos o documentos eso es todo costo sin
# ningún beneficio: el clúster grande es una optimización para los
# archivos de varios GB de un USB de Wii.
WII_USB_LABEL = "WII_USB"
WII_USB_SECTORS_PER_CLUSTER = 64

# Lo que una etiqueta de volumen FAT puede tener: 11 caracteres como
# máximo, en mayúsculas, sin los caracteres que el propio `mkfs.vfat`
# rechaza. Se normaliza acá en vez de dejar que falle el comando, porque
# el usuario escribe la etiqueta a mano en la interfaz y no tiene por qué
# conocer las reglas de un formato de 1996.
FAT_LABEL_MAX_LEN = 11
_FAT_LABEL_FORBIDDEN = set('*?.,;:/\\|+=<>[]"')

# Discos que /sys/block puede listar pero que nunca son "un USB para
# preparar": loopback, ramdisk, device-mapper, zram, lectores ópticos.
_IGNORED_BLOCK_PREFIXES = ("loop", "ram", "dm-", "zram", "sr")


class FormatGuardError(RuntimeError):
    """Base de los errores con los que un blindaje aborta un formateo:
    los mismos para Modo Fábrica y para el formateo de propósito general,
    porque los dos entran por `format_fat32`. Nunca se levanta directo:
    siempre una de las subclases de abajo, para que quien llame sepa CUÁL
    blindaje frenó la operación y se lo pueda decir al usuario con
    precisión."""


class UnsafeDeviceError(FormatGuardError):
    """BLINDAJE 1 / 3: el dispositivo no es removible (o dejó de serlo, o
    directamente ya no existe)."""


class DeviceChangedError(FormatGuardError):
    """BLINDAJE 3: el dispositivo cambió de tamaño desde que se armó la
    confirmación. Es la señal de que puede ser un disco distinto -mismo
    nombre de dispositivo, otro USB- y no el que el usuario aprobó."""


class DeviceIdentityMismatchError(FormatGuardError):
    """BLINDAJE 3: la identidad física del dispositivo (serie/WWN vía
    udev) cambió desde que se armó la confirmación, aunque `/dev/sdX`,
    la removibilidad y hasta el tamaño coincidan.

    Es el caso que `DeviceChangedError` no cubre: dos USB del mismo
    modelo tienen el mismo tamaño exacto, y el kernel recicla el mismo
    nombre de dispositivo para el próximo que se conecta en ese puerto.
    Sin esto, desconectar el USB confirmado y conectar OTRO idéntico en
    tamaño antes de que termine el formateo pasaría los blindajes 1 y 3
    (tamaño) igual."""


class CriticalMountError(FormatGuardError):
    """BLINDAJE 4: alguna partición del dispositivo está montada en una
    ruta crítica del sistema operativo."""


class StillMountedError(FormatGuardError):
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


def device_identity(device_path, *, run=subprocess.run) -> str | None:
    """Identidad física estable de `device_path`, vía la base de datos de
    udev: el número de serie (o el WWN si el disco no expone serie), que
    no cambia aunque el kernel reasigne `/dev/sdX` a otro dispositivo.

    A diferencia de `removable`/`size` (que se leen directo de /sys/block
    porque el kernel los expone ahí sin intermediarios), la identidad
    física no vive en un archivo fijo: depende del bus (USB, ATA, NVMe)
    y de qué reglas de udev corrieron para ESE dispositivo en particular.
    `udevadm info` es la interfaz estable para pedirla sin tener que saber
    de antemano con qué bus se está tratando -es la misma fuente que llena
    los symlinks de `/dev/disk/by-id/`.

    Se prueban, en orden, `ID_SERIAL` (serie completa, la más específica),
    `ID_SERIAL_SHORT` (a veces `ID_SERIAL` no está pero esta sí) y
    `ID_WWN` (World Wide Name, típico de discos que no exponen serie de
    otra forma). None si ninguna está disponible -algunos lectores de
    tarjetas SD integrados no exponen nada de esto- o si `udevadm` no
    está instalado: quien llama (`verify_still_safe`) trata None como
    "no se puede confirmar la identidad" y no como "coincide"."""
    try:
        resultado = run(
            ["udevadm", "info", "--query=property", f"--name={device_path}"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if resultado.returncode != 0:
        return None
    propiedades: dict[str, str] = {}
    for linea in resultado.stdout.splitlines():
        clave, separador, valor = linea.partition("=")
        if separador:
            propiedades[clave] = valor
    for clave in ("ID_SERIAL", "ID_SERIAL_SHORT", "ID_WWN"):
        valor = propiedades.get(clave)
        if valor:
            return valor
    return None


def _whole_disk_path(device_path) -> Path | None:
    """Disco entero detrás de `device_path`: si es una partición
    (`/dev/sdb1`, `/dev/nvme0n1p1`) devuelve el disco que la contiene
    (`/dev/sdb`, `/dev/nvme0n1`); si `device_path` ya es un disco entero,
    lo devuelve tal cual. None si no aparece en /sys/class/block.

    Partición vs. disco entero se distingue con el archivo `partition`
    que el kernel expone -SOLO en subcarpetas de partición- bajo
    `/sys/class/block/<nombre>/partition`: no se puede adivinar por el
    nombre (`sdb1` vs. `nvme0n1p1` siguen convenciones de nombrado
    distintas por bus), pero ese archivo es la misma señal para
    cualquiera. Cuando es una partición, el directorio real (resuelto el
    symlink de /sys/class/block) queda anidado un nivel adentro del disco
    que la contiene, así que el nombre del padre es el disco entero."""
    nombre = Path(device_path).name
    entrada = _SYS_CLASS_BLOCK / nombre
    try:
        real = entrada.resolve(strict=True)
    except OSError:
        return None
    if (entrada / "partition").exists():
        return Path("/dev") / real.parent.name
    return Path("/dev") / nombre


def physical_disk_for_path(path) -> Path | None:
    """El disco físico entero (ej. `/dev/sdb`) detrás del punto de
    montaje `path`, o None si no se pudo determinar.

    Existe para que Transferencias y la instalación de Homebrew declaren,
    ante el `OperationManager`, el MISMO recurso físico que Modo Fábrica
    -así una operación de formateo se bloquea mutuamente con cualquiera
    que esté escribiendo en el mismo disco, sin importar en qué punto de
    montaje ocurra esa escritura (Modo Fábrica desmonta el disco antes de
    formatear: comparar contra el punto de montaje no serviría de nada
    ahí, porque para ese momento ya no existe).

    None cuando `path` no corresponde a ningún punto de montaje que
    `findmnt` reconozca, o cuando el dispositivo que reporta no aparece
    en /sys/class/block (por ejemplo, un filesystem de red). Quien llama
    debería tratar ese None como "no se pudo identificar el disco físico"
    -no bloquea nada nuevo, pero tampoco rompe lo que ya funcionaba antes
    de que existiera esta función."""
    dispositivo = _block_device_for(Path(path))
    if dispositivo is None:
        return None
    return _whole_disk_path(dispositivo)


def candidate_for_mount_point(mount_point) -> "BlockDevice | None":
    """El `BlockDevice` de la LISTA BLANCA que corresponde al disco físico
    detrás de `mount_point`, o None si no hay ninguno.

    Es el puente entre los dos mundos del módulo: los flujos que trabajan
    sobre un punto de montaje (verificar una memoria con f3, transferir,
    instalar homebrew) y los que necesitan el disco entero (formatear).
    Sin esto, ofrecer "formatear esta unidad" después de verificarla
    obligaría a fabricar un `BlockDevice` a mano a partir del punto de
    montaje, que es exactamente la forma de saltearse el BLINDAJE 1 sin
    darse cuenta.

    Acá no hay atajo posible: el disco tiene que aparecer en
    `list_candidate_drives()` -o sea, el kernel tiene que marcarlo
    removable=1- o esta función devuelve None y no hay nada que formatear.
    Un disco interno no puede llegar por este camino, igual que no puede
    llegar por el desplegable de Modo Fábrica.

    Se relista en el momento (no se cachea nada) para que el `size_bytes`
    y la `identity` que quedan congelados en el `BlockDevice` sean los de
    AHORA: son justo los valores contra los que `verify_still_safe` va a
    comparar más tarde, cuando el usuario confirme."""
    fisico = physical_disk_for_path(mount_point)
    if fisico is None:
        return None
    for candidato in list_candidate_drives():
        if candidato.path == fisico:
            return candidato
    return None


def resources_for_mount_point(path) -> list[Path]:
    """Los `resources` que Transferencias y la instalación de Homebrew
    tienen que declarar ante el `OperationManager` al escribir en el
    punto de montaje `path`: el punto de montaje en sí (de siempre, es lo
    que ya usaba el botón "Expulsar unidad") MÁS el disco físico que hay
    detrás (`physical_disk_for_path`), si se lo pudo determinar.

    El físico es lo que hace que choquen con Modo Fábrica formateando el
    mismo disco -que declara el disco entero, no un punto de montaje,
    porque lo desmonta como parte de formatear- sin dejar de chocar entre
    sí por el punto de montaje de siempre. Si no se pudo determinar el
    disco físico, se devuelve solo el punto de montaje: ni mejora ni
    empeora el comportamiento que había antes de que existiera esta
    función."""
    punto = Path(path)
    fisico = physical_disk_for_path(punto)
    return [punto] if fisico is None else [punto, fisico]


@dataclass(frozen=True)
class BlockDevice:
    """Un disco candidato a Modo Fábrica. `size_bytes` queda congelado acá
    en el momento en que se listó -es justamente lo que `verify_still_safe`
    vuelve a medir más tarde para detectar que cambió.

    `identity` es lo mismo pero para la identidad física del dispositivo
    (ver `device_identity`): puede ser `None` si no se pudo determinar
    (dispositivo sin serie expuesta por udev), caso en el que
    `verify_still_safe` no la vuelve a chequear -no tener el dato no es
    lo mismo que poder confirmar que cambió, y bloquear Modo Fábrica
    entero para esos dispositivos sería peor que la protección que se
    gana en el resto."""
    path: Path
    model: str
    size_bytes: int
    identity: str | None = None

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
                                      size_bytes=size,
                                      identity=device_identity(device_path)))
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


def is_critical_system_path(path) -> bool:
    """¿`path`, resuelto a ruta absoluta real, es uno de los puntos de
    montaje críticos del sistema operativo (`CRITICAL_MOUNTPOINTS`)?

    Mismo criterio que usa el BLINDAJE 4 para decidir si una partición
    montada es peligrosa, pero aplicado directo a una ruta de destino en
    vez de a lo que reporte /proc/mounts: así, cualquier función que
    reciba una carpeta de destino de quien la llame (no solo Modo
    Fábrica, ver `oscwii_installer.install_app`) puede protegerse a sí
    misma reusando la misma lista de rutas críticas, en vez de mantener
    una copia propia que se puede desactualizar.

    Si `path` no se puede resolver (no existe, symlink roto), se compara
    la ruta tal cual la pasaron: no poder confirmar dónde apunta de
    verdad no es motivo para asumir que es segura."""
    try:
        resolved = Path(path).resolve()
    except OSError:
        resolved = Path(path)
    return str(resolved) in CRITICAL_MOUNTPOINTS


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


def verify_still_safe(device: BlockDevice, *, run=subprocess.run) -> None:
    """BLINDAJE 3: re-chequeo, ya en el hilo de fondo, de que
    `device.path` sigue siendo removible, pesa lo mismo y tiene la misma
    identidad física que cuando se armó el diálogo de confirmación.
    `format_fat32` la llama DOS veces: acá y otra vez justo antes de
    `mkfs.vfat`, así que esta función no asume en qué momento del flujo
    está -siempre vuelve a preguntarle al kernel/udev, nunca confía en
    una llamada anterior, ni siquiera una hecha un segundo antes.

    Entre que el usuario ve el diálogo (con el modelo y el tamaño
    impresos) y aprieta el botón -o entre que se desmonta el disco y se
    lo formatea, la otra ventana que cubre esta función- puede pasar
    cualquier cosa: que saque el USB y conecte otro dispositivo en el
    mismo puerto (el kernel suele reciclar `/dev/sdb` para el próximo que
    aparezca), o que conecte uno del mismo tamaño exacto -el tamaño solo
    no alcanza para distinguirlos, por eso la identidad física.

    No devuelve nada: levanta `UnsafeDeviceError`, `DeviceChangedError` o
    `DeviceIdentityMismatchError` si algo no cierra."""
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
    # Si no se pudo determinar la identidad al listar el dispositivo (ver
    # `BlockDevice.identity`), no hay nada contra qué comparar: no tener
    # el dato no es lo mismo que poder confirmar que cambió.
    if device.identity is not None:
        identidad_actual = device_identity(device.path, run=run)
        if identidad_actual != device.identity:
            raise DeviceIdentityMismatchError(
                f"La identidad física de {device.path} cambió desde que "
                f"se confirmó ({device.identity} → {identidad_actual}). "
                "Puede ser otro dispositivo del mismo tamaño reconectado "
                "en el mismo puerto: se aborta el formateo por seguridad.")


def check_no_critical_mounts(device: BlockDevice) -> None:
    """BLINDAJE 4: la última línea de defensa. Se llama SIEMPRE desde
    `format_fat32`, sin importar que los blindajes 1 y 3 ya hayan
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


def normalize_fat_label(label: str | None) -> str:
    """Deja `label` como una etiqueta de volumen FAT válida, o string
    vacío si no queda nada usable.

    FAT32 guarda la etiqueta en 11 bytes, en mayúsculas y sin varios
    caracteres de puntuación; `mkfs.vfat` directamente falla si le pasan
    algo que no cumple. Como la etiqueta la escribe el usuario a mano en
    la interfaz, se normaliza acá -mayúsculas, afuera lo prohibido y lo
    no imprimible, cortar a 11- en vez de devolverle un error de
    `mkfs.vfat` por una coma de más.

    Los acentos y la ñ se transliteran a ASCII ("Fotos Mamá" ->
    "FOTOS MAMA") en vez de dejarlos pasar: FAT no guarda la etiqueta en
    UTF-8 sino en una página de códigos, y `mkfs.vfat` mide el límite de
    11 en BYTES, así que una etiqueta con acentos puede fallar o quedar
    escrita con basura. Vale más una etiqueta sin tilde que un formateo
    que se cae al final por el nombre.

    String vacío significa "sin etiqueta": quien llama omite `-n` y el
    volumen queda como NO NAME, que es exactamente lo que corresponde
    cuando el campo de etiqueta se deja en blanco."""
    if not label:
        return ""
    # NFKD separa "á" en "a" + tilde combinante; descartar las marcas
    # combinantes (categoría Mn) deja el ASCII de abajo.
    descompuesto = unicodedata.normalize("NFKD", label.strip().upper())
    limpio = "".join(
        c for c in descompuesto
        if unicodedata.category(c) != "Mn"
        and c.isascii() and c.isprintable()
        and c not in _FAT_LABEL_FORBIDDEN
    )
    return limpio[:FAT_LABEL_MAX_LEN].strip()


def format_fat32(device: BlockDevice, *, run=subprocess.run,
                 label: str | None = None,
                 sectors_per_cluster: int | None = None,
                 mount_timeout: float = 15.0) -> Path:
    """Formatea `device` entero como FAT32, con todos los blindajes
    puestos, y devuelve el punto de montaje donde quedó.

    Este es EL mecanismo de formateo de la app: no hay otro. Modo Fábrica
    (`format_as_wii_usb`) y el formateo de propósito general que se ofrece
    al terminar de verificar una memoria llaman los dos acá, así que los
    blindajes se corren una sola vez en un solo lugar y ninguno de los dos
    flujos puede quedarse con una versión propia -la lista blanca de
    removibles vale igual para los dos, sin excepciones: un disco externo
    grande pasa por ser removable=1, no por ser grande.

    Lo que hace, en orden, sin confiar en ningún chequeo que haya hecho
    quien llama (ni siquiera uno hecho un segundo antes):

    1. Blindaje 3 (`verify_still_safe`) y blindaje 4
       (`check_no_critical_mounts`).
    2. Desmonta todo lo que tenga montado y confirma con el blindaje 5
       que el desmontaje surtió efecto de verdad.
    3. Blindaje 3 OTRA VEZ, ya con el disco desmontado: ahí en el medio
       está la ventana real (desmontar puede tardar) entre "confirmamos
       que era el dispositivo correcto" y "empezamos a escribir".
    4. `mkfs.vfat -F 32` sobre el disco entero.
    5. Lo monta de vuelta y devuelve dónde quedó.

    `label` es opcional: se normaliza con `normalize_fat_label` y, si
    queda vacío, no se le pasa `-n` a `mkfs.vfat` (el volumen queda como
    NO NAME). `sectors_per_cluster` en None deja que `mkfs.vfat` elija el
    tamaño de clúster según el tamaño real del dispositivo, que es lo
    correcto para un formateo genérico; Modo Fábrica sí lo fija, porque
    los USB Loaders esperan clústeres de 32 KB.

    Formatea el DISCO ENTERO sin tabla de particiones (`mkfs.vfat` sobre
    `/dev/sdX`, no sobre `/dev/sdX1`), igual que hace Modo Fábrica desde
    siempre: es lo que leen sin quejarse tanto los USB Loaders como
    Windows y Linux en un pendrive o una SD.

    Pensada para correr en un hilo de fondo: no toca GTK ni nada de la
    interfaz -quien la llama es responsable de reportar
    progreso/resultado con `GLib.idle_add`, igual que hace
    `queue_manager`.

    `mkfs.vfat` se lanza vía `pkexec`, salvo que el proceso YA sea root
    (`os.geteuid() == 0`): eso es lo que pasa en el script de pruebas
    manual, corrido con `sudo`, y pedirle a `pkexec` que lance un agente
    gráfico de autenticación ahí no tendría sentido (y probablemente ni
    funcione sin sesión gráfica). Con un usuario normal desde la app, en
    cambio, `pkexec` es el que muestra el diálogo de contraseña del
    sistema.

    Levanta la subclase de `FormatGuardError` que corresponda si algún
    blindaje no pasa, o `RuntimeError` si `mkfs.vfat` (o el montaje
    posterior) fallan -incluido el caso de un disco demasiado grande para
    FAT32, que rechaza el propio `mkfs.vfat` con su mensaje."""
    verify_still_safe(device, run=run)
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

    # BLINDAJE 3 otra vez, ya con el disco desmontado: el desmontaje puede
    # haber tardado (discos lentos, `udisksctl` reintentando), y esta es
    # la ventana real entre "confirmamos que era el dispositivo correcto"
    # y "empezamos a escribir sobre él" -no alcanza con haberlo chequeado
    # antes de desmontar.
    verify_still_safe(device, run=run)

    comando = ["mkfs.vfat", "-F", "32"]
    if sectors_per_cluster is not None:
        comando += ["-s", str(sectors_per_cluster)]
    etiqueta = normalize_fat_label(label)
    if etiqueta:
        comando += ["-n", etiqueta]
    comando.append(str(device.path))

    prefijo = [] if os.geteuid() == 0 else ["pkexec"]
    resultado = run(prefijo + comando, capture_output=True, text=True, timeout=300)
    if resultado.returncode != 0:
        raise RuntimeError(
            resultado.stderr.strip() or "mkfs.vfat terminó con error desconocido.")

    return _mount_after_format(device.path, run=run, timeout=mount_timeout)


def format_as_wii_usb(device: BlockDevice, *, run=subprocess.run,
                      label: str = WII_USB_LABEL,
                      sectors_per_cluster: int = WII_USB_SECTORS_PER_CLUSTER,
                      mount_timeout: float = 15.0) -> Path:
    """Modo Fábrica: `format_fat32` (todos los blindajes + mkfs + montar)
    y encima la estructura de carpetas que esperan USB Loader GX y
    Nintendont.

    Todo lo peligroso pasa en `format_fat32`; lo único propio de Modo
    Fábrica que queda acá es lo específico de Wii: el clúster de 32 KB, la
    etiqueta por defecto y crear apps/games/wbfs después de montar. Esa
    parte NO se comparte con el formateo de propósito general -una memoria
    que se formatea para llevar fotos no tiene por qué quedar con tres
    carpetas de un loader de Wii adentro.

    Devuelve el punto de montaje final. Levanta lo mismo que
    `format_fat32`."""
    punto_montaje = format_fat32(device, run=run, label=label,
                                 sectors_per_cluster=sectors_per_cluster,
                                 mount_timeout=mount_timeout)
    for carpeta in FACTORY_FOLDERS:
        (punto_montaje / carpeta).mkdir(parents=True, exist_ok=True)

    return punto_montaje
