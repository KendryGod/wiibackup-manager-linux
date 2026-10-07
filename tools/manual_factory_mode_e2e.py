#!/usr/bin/env python3
"""Prueba manual de extremo a extremo del Modo Fábrica (`drives.py`),
contra un disco virtual (archivo + loop device) en vez de un USB real.

Por qué existe
--------------
Los cuatro blindajes de Modo Fábrica son la parte más peligrosa de esta
app -un error ahí no arruina un juego, arruina el disco que sea que esté
en /dev/sdX en ese momento- y por eso no alcanza con probarlos con mocks
(eso ya lo hace `tests/test_drives_factory.py`, de forma automática y sin
privilegios). Este script los ejercita contra un dispositivo de bloque de
mentira PERO REAL: un archivo de unos cientos de MB expuesto con
`losetup` como si fuera un USB. Confirma tres cosas:

1) BLINDAJE 1 rechaza el loop device: el kernel nunca lo marca
   removable=1 (es un archivo, no algo hot-pluggable), exactamente el
   mismo motivo por el que rechazaría un disco interno. Ni aparece en
   `list_candidate_drives()`.
2) BLINDAJE 4 aborta si alguna partición del dispositivo aparece montada
   en un punto que se trata como crítico. Se simula apuntando
   `CRITICAL_MOUNTPOINTS` a la carpeta de prueba en vez de a rutas reales
   del sistema -no hay forma segura de probar esto contra /home de
   verdad, y no hace falta: lo que se prueba es que la función SABE
   encontrar la partición correcta y frenar, no que conozca de memoria
   la lista de rutas del sistema operativo.
3) BLINDAJE 3 (identidad física) aborta si la serie que reporta udev
   cambia entre el chequeo de antes de desmontar y el de justo antes de
   `mkfs.vfat`. Un loop device no tiene serie de verdad -no es un bus con
   identidad física, es un archivo-, así que acá se simula igual que la
   Fase 1 simula que ES removable: reemplazando `drives.device_identity`
   por una versión que, para este loop device puntual, devuelve un valor
   la primera vez que se la llama y OTRO distinto la segunda -exactamente
   lo que pasaría si el kernel reciclara `/dev/sdb` para un USB distinto
   entre esos dos momentos. `format_as_wii_usb` en sí no se toca: nada
   más se le inyecta un `run` que registra los comandos para confirmar
   que el paso privilegiado (wipefs/parted/mkfs.vfat) nunca llegó a
   correr.
4) El camino feliz corre `format_as_wii_usb` DE VERDAD (mismo código que
   usaría la interfaz) sobre el loop device, que primero se deja como
   viene un USB real de fábrica: tabla MBR con una partición FAT32
   montada. Es el estado en el que el Kingston DataTraveler hacía fallar
   el formateo viejo ("Partitions or virtual mappings on device"). Blindaje
   1 se fuerza a pasar -es la única forma de llegar hasta acá con un loop
   device, que nunca es removible de verdad- y de ahí en más todo es
   real: blindajes 3 y 4, wipefs, parted, mkfs.vfat sobre la PARTICIÓN,
   montaje y creación de apps/games/wbfs.
5) El OTRO camino que llega al mismo `mkfs.vfat`: `format_fat32`, el
   formateo de propósito general que ofrece "Verificar Memoria" cuando una
   memoria pasa la prueba. Es la misma función que usa `format_as_wii_usb`
   por debajo, así que lo que se confirma acá es lo que las diferencia: que
   formatea igual de bien SIN dejar las carpetas de Wii, y que la etiqueta
   que se le pasa termina de verdad en el volumen.

Requiere root: crear/soltar un loop device y formatear con `mkfs.vfat`
son operaciones de root en cualquier distro. Se corre con

    sudo python3 tools/manual_factory_mode_e2e.py [--size-mb 256]

El loop device se crea con `losetup -P` para que el kernel exponga sus
particiones (`/dev/loopNp1`), igual que con un USB.

Blindaje 1 "forzado": el script privilegiado de `format_fat32` relee
`removable` directo de sysfs, así que parchear `is_removable_block_device`
no alcanza. Para las Fases 3 a 5, `drives._SYS_BLOCK` apunta a una copia
de /sys/block/loopN con removable=1 y el tamaño real.

Corriendo con sudo, el montaje final queda a nombre de root
(/run/media/root/...): `format_fat32` monta como el usuario que corre el
proceso, y acá ese usuario ES root. En la app, que corre como usuario
normal, queda en /run/media/<usuario>/. Para comprobar justamente eso, la
Fase 4 vuelve a montar la partición como el usuario que invocó sudo
(`SUDO_USER`) y confirma que puede escribir.

No toca ningún disco de la máquina real: crea su propio archivo de imagen
en un directorio temporal y lo limpia (`losetup -d` + borrar el archivo)
al final, pase lo que pase.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiibackup_manager import drives  # noqa: E402

PASA, FALLA = "[OK]  ", "[FALLO]"
resultados: list[tuple[str, bool, str]] = []


def marcar(nombre: str, ok: bool, detalle: str = "") -> None:
    resultados.append((nombre, ok, detalle))
    print(f"{PASA if ok else FALLA} {nombre}" + (f" — {detalle}" if detalle else ""))


def crear_loop_device(tamano_mb: int, workdir: Path) -> tuple[Path, Path]:
    """Crea un archivo de `tamano_mb` MB y lo expone como loop device.
    Devuelve (ruta_del_archivo, ruta_del_dispositivo /dev/loopN)."""
    imagen = workdir / "disco-virtual.img"
    subprocess.run(
        ["dd", "if=/dev/zero", f"of={imagen}", "bs=1M", f"count={tamano_mb}",
         "status=none"],
        check=True,
    )
    resultado = subprocess.run(
        ["losetup", "--find", "--show", "-P", str(imagen)],
        capture_output=True, text=True, check=True,
    )
    loop_dev = Path(resultado.stdout.strip())
    return imagen, loop_dev


@contextlib.contextmanager
def loop_como_removible(loop_dev: Path, workdir: Path):
    """`drives._SYS_BLOCK` apuntando a una copia de /sys/block/loopN con
    removable=1 y el tamaño real (ver docstring del módulo)."""
    falso = workdir / "sys_block"
    entrada = falso / loop_dev.name
    entrada.mkdir(parents=True, exist_ok=True)
    (entrada / "removable").write_text("1\n")
    (entrada / "size").write_text(
        (Path("/sys/block") / loop_dev.name / "size").read_text())
    original = drives._SYS_BLOCK
    drives._SYS_BLOCK = falso
    try:
        yield
    finally:
        drives._SYS_BLOCK = original


def dejar_como_usb_de_fabrica(loop_dev: Path, workdir: Path) -> Path:
    """MBR + una partición FAT32 montada: como llega un USB nuevo y como
    lo automonta el escritorio al conectarlo."""
    subprocess.run(["parted", "-s", str(loop_dev), "mklabel", "msdos"], check=True)
    subprocess.run(["parted", "-s", str(loop_dev), "mkpart", "primary", "fat32",
                    "1MiB", "100%"], check=True)
    subprocess.run(["udevadm", "settle"], capture_output=True)
    (particion,) = drives.partitions_of(loop_dev)
    subprocess.run(["mkfs.vfat", "-F", "32", "-n", "FABRICA", str(particion)],
                   capture_output=True, check=True)
    punto = workdir / "montaje-de-fabrica"
    punto.mkdir(exist_ok=True)
    subprocess.run(["mount", str(particion), str(punto)], check=True)
    return particion


# --------------------------------------------------------------- Fase 1 --
def fase_1_blindaje_1(loop_dev: Path) -> None:
    """El loop device tiene que quedar afuera de la lista blanca, igual
    que un disco interno: es exactamente lo que hace que sea seguro
    usarlo para las Fases 3 y 4 sin arriesgar nada -si el Blindaje 1 fallara acá
    (falso positivo), format_as_wii_usb() ni siquiera necesitaría el
    monkeypatch de más abajo, que es la señal de que algo anda mal."""
    print("\n=== Fase 1: Blindaje 1 (lista blanca de removibles) ===")
    es_removible = drives.is_removable_block_device(loop_dev)
    marcar("Fase 1: el kernel NO marca el loop device como removable "
           "(esperado: igual que un disco interno)",
           es_removible is False, f"is_removable_block_device={es_removible}")

    candidatos = [c for c in drives.list_candidate_drives() if c.path == loop_dev]
    marcar("Fase 1: list_candidate_drives() NO incluye el loop device",
           len(candidatos) == 0, str(candidatos))


# --------------------------------------------------------------- Fase 2 --
def fase_2_blindaje_4(loop_dev: Path, workdir: Path) -> None:
    print("\n=== Fase 2: Blindaje 4 (montajes críticos) ===")
    subprocess.run(["mkfs.vfat", "-F", "32", str(loop_dev)],
                    capture_output=True, text=True, check=True)

    punto = workdir / "montaje-critico-simulado"
    punto.mkdir()
    subprocess.run(["mount", str(loop_dev), str(punto)], check=True)
    try:
        original = drives.CRITICAL_MOUNTPOINTS
        drives.CRITICAL_MOUNTPOINTS = frozenset({str(punto)})
        try:
            device = drives.BlockDevice(
                path=loop_dev, model="Loop de prueba",
                size_bytes=drives.device_size_bytes(loop_dev) or 0)
            try:
                drives.check_no_critical_mounts(device)
                aborto = False
            except drives.CriticalMountError:
                aborto = True
            marcar("Fase 2: check_no_critical_mounts aborta con una partición "
                   "montada en un punto tratado como crítico", aborto)
        finally:
            drives.CRITICAL_MOUNTPOINTS = original
    finally:
        subprocess.run(["umount", str(punto)], capture_output=True)


# --------------------------------------------------------------- Fase 3 --
def fase_3_identidad(loop_dev: Path, workdir: Path) -> None:
    print("\n=== Fase 3: Blindaje 3 (identidad física entre desmontar y formatear) ===")
    original_identity = drives.device_identity

    llamadas = {"n": 0}

    def _identidad_simulada(device_path, **kwargs):
        # Solo se simula PARA ESTE loop device: si algún otro código de
        # camino llegara a consultar la identidad de otro dispositivo
        # (no debería pasar acá), se lo deja pasar a la función real.
        if Path(device_path) != loop_dev:
            return original_identity(device_path, **kwargs)
        llamadas["n"] += 1
        return "LOOP-SERIE-INICIAL" if llamadas["n"] == 1 else "LOOP-SERIE-DISTINTA"

    comandos_ejecutados: list[list[str]] = []

    def _run_real_pero_registrado(cmd, **kwargs):
        comandos_ejecutados.append(cmd)
        return subprocess.run(cmd, **kwargs)

    drives.device_identity = _identidad_simulada
    try:
        with loop_como_removible(loop_dev, workdir):
            _fase_3_formatear(loop_dev, _run_real_pero_registrado)
    finally:
        drives.device_identity = original_identity

    titulo = ("Fase 3: se consultó la identidad exactamente dos veces "
              "(antes de desmontar y otra vez antes de pedir la contraseña)")
    marcar(titulo, llamadas["n"] == 2, f"llamadas={llamadas['n']}")

    hubo_privilegiado = any(
        c[:1] == ["pkexec"] or c[:2] == ["/bin/sh", "-c"]
        for c in comandos_ejecutados)
    marcar("Fase 3: el paso privilegiado (wipefs/parted/mkfs.vfat) NO "
           "llegó a ejecutarse", not hubo_privilegiado,
           str([c[:2] for c in comandos_ejecutados]))


def _fase_3_formatear(loop_dev: Path, run) -> None:
    titulo = ("Fase 3: format_as_wii_usb aborta con "
              "DeviceIdentityMismatchError cuando la identidad cambia "
              "entre desmontar y formatear")
    tamano = drives.device_size_bytes(loop_dev)
    device = drives.BlockDevice(path=loop_dev, model="Loop de prueba",
                                size_bytes=tamano or 0,
                                identity="LOOP-SERIE-INICIAL")
    try:
        drives.format_as_wii_usb(device, run=run, label="WII_TEST")
    except drives.DeviceIdentityMismatchError:
        marcar(titulo, True)
    except Exception as e:  # noqa: BLE001
        marcar(titulo, False, f"levantó {type(e).__name__} en vez: {e}")
    else:
        marcar(titulo, False, "no levantó ninguna excepción -- llegó a formatear")


# --------------------------------------------------------------- Fase 4 --
def fase_4_formateo_real(loop_dev: Path, workdir: Path) -> None:
    print("\n=== Fase 4: camino feliz — USB con tabla de particiones de fábrica ===")
    particion_vieja = dejar_como_usb_de_fabrica(loop_dev, workdir)
    print(f"Estado inicial: MBR + {particion_vieja} (FAT32) montada")

    # Control: el comando del formateo viejo falla acá igual que en el
    # DataTraveler real. Si esto pasara, la fase no estaría reproduciendo
    # el caso. Va con la partición desmontada -el formateo viejo también
    # desmontaba antes de mkfs-: montada, mkfs.vfat ni siquiera abre el
    # disco ("Device or resource busy") y el control no probaría nada.
    punto_fabrica = workdir / "montaje-de-fabrica"
    subprocess.run(["umount", str(particion_vieja)], check=True)
    viejo = subprocess.run(["mkfs.vfat", "-F", "32", str(loop_dev)],
                           capture_output=True, text=True)
    marcar("Fase 4: (control) mkfs.vfat sobre el disco entero se niega, como "
           "con el USB real", viejo.returncode != 0
           and "Partitions or virtual mappings" in viejo.stderr,
           viejo.stderr.strip())
    # De vuelta montada: format_as_wii_usb tiene que arrancar como con un
    # USB recién conectado y automontado.
    subprocess.run(["mount", str(particion_vieja), str(punto_fabrica)], check=True)

    with loop_como_removible(loop_dev, workdir):
        tamano = drives.device_size_bytes(loop_dev)
        device = drives.BlockDevice(path=loop_dev, model="Loop de prueba",
                                    size_bytes=tamano or 0)
        try:
            punto_montaje = drives.format_as_wii_usb(device, label="WII_TEST")
        except Exception as e:  # noqa: BLE001
            marcar("Fase 4: format_as_wii_usb corrió sin levantar excepción",
                   False, str(e))
            return
    marcar("Fase 4: format_as_wii_usb corrió sin levantar excepción", True,
           f"montado en {punto_montaje}")

    particiones = drives.partitions_of(loop_dev)
    marcar("Fase 4: queda exactamente una partición", len(particiones) == 1,
           str(particiones))
    particion = particiones[0] if particiones else loop_dev
    pttype = subprocess.run(["blkid", "-o", "value", "-s", "PTTYPE", str(loop_dev)],
                            capture_output=True, text=True).stdout.strip()
    marcar("Fase 4: la tabla de particiones es MBR (dos)", pttype == "dos",
           f"PTTYPE={pttype!r}")

    carpetas_ok = all((punto_montaje / c).is_dir() for c in drives.FACTORY_FOLDERS)
    marcar("Fase 4: se crearon apps/games/wbfs en el punto de montaje",
           carpetas_ok, str(sorted(p.name for p in punto_montaje.iterdir())))

    fstype = drives.filesystem_of(punto_montaje)
    marcar("Fase 4: el filesystem resultante es FAT32/vfat",
           fstype in {"vfat", "fat32"}, f"filesystem_of={fstype}")

    origen = subprocess.run(["findmnt", "-no", "SOURCE", str(punto_montaje)],
                            capture_output=True, text=True).stdout.strip()
    marcar("Fase 4: lo montado es la PARTICIÓN, no el disco entero",
           origen == str(particion), f"SOURCE={origen}")

    dueno = os.stat(punto_montaje).st_uid
    marcar("Fase 4: el punto de montaje es del usuario que corre el proceso",
           dueno == os.getuid(), f"st_uid={dueno} uid={os.getuid()}")

    ok_desmonte, detalle = drives.eject_mount_point(punto_montaje)
    marcar("Fase 4: se pudo desmontar el punto de montaje al terminar",
           ok_desmonte, detalle)

    _montar_como_usuario_que_invoco_sudo(particion)


def _montar_como_usuario_que_invoco_sudo(particion: Path) -> None:
    """Lo que hace la app de verdad, que no corre como root: montar con
    udisksctl como el usuario y escribir ahí."""
    usuario = os.environ.get("SUDO_USER")
    if not usuario or usuario == "root":
        print("(sin SUDO_USER: se omite el montaje como usuario normal)")
        return
    como = ["runuser", "-u", usuario, "--"]
    montaje = subprocess.run(como + ["udisksctl", "mount", "-b", str(particion),
                                     "--no-user-interaction"],
                             capture_output=True, text=True)
    if montaje.returncode != 0:
        print(f"(udisksctl no dejó montar como {usuario} desde sudo: "
              f"{montaje.stderr.strip()} -- se omite)")
        return
    punto = Path(subprocess.run(["findmnt", "-no", "TARGET", str(particion)],
                                capture_output=True, text=True).stdout.strip())
    escritura = subprocess.run(como + ["touch", str(punto / "escritura-usuario")],
                               capture_output=True, text=True)
    marcar(f"Fase 4: montada como {usuario} queda en su /run/media y puede escribir",
           escritura.returncode == 0 and f"/{usuario}/" in str(punto),
           f"{punto} {escritura.stderr.strip()}")
    subprocess.run(como + ["udisksctl", "unmount", "-b", str(particion),
                           "--no-user-interaction"], capture_output=True)


# --------------------------------------------------------------- Fase 5 --
def fase_5_formateo_generico(loop_dev: Path, workdir: Path) -> None:
    """El formateo de propósito general de "Verificar Memoria", sobre el
    mismo loop device: mismos blindajes (es la misma función), sin la
    estructura de carpetas de Wii."""
    print("\n=== Fase 5: formateo genérico FAT32 (Verificar Memoria) ===")
    with loop_como_removible(loop_dev, workdir):
        tamano = drives.device_size_bytes(loop_dev)
        device = drives.BlockDevice(path=loop_dev, model="Loop de prueba",
                                    size_bytes=tamano or 0)
        try:
            punto_montaje = drives.format_fat32(device, label="Fotos Mamá")
        except Exception as e:  # noqa: BLE001
            marcar("Fase 5: format_fat32 corrió sin levantar excepción",
                   False, str(e))
            return
    marcar("Fase 5: format_fat32 corrió sin levantar excepción", True,
           f"montado en {punto_montaje}")

    contenido = sorted(p.name for p in punto_montaje.iterdir())
    sin_carpetas_wii = not any(c in contenido for c in drives.FACTORY_FOLDERS)
    marcar("Fase 5: NO se crearon apps/games/wbfs (es un formateo de "
           "propósito general, no Modo Fábrica)",
           sin_carpetas_wii, f"contenido={contenido}")

    fstype = drives.filesystem_of(punto_montaje)
    marcar("Fase 5: el filesystem resultante es FAT32/vfat",
           fstype in {"vfat", "fat32"}, f"filesystem_of={fstype}")

    # La etiqueta que se le pasó tiene acento y minúsculas, o sea que
    # `mkfs.vfat` la habría rechazado tal cual: lo que tiene que haber
    # quedado en el volumen es la versión normalizada.
    esperada = drives.normalize_fat_label("Fotos Mamá")
    if shutil.which("blkid") is None:
        marcar("Fase 5: la etiqueta quedó normalizada en el volumen",
               True, "sin blkid: no se pudo comprobar, se da por bueno")
    else:
        leida = subprocess.run(
            ["blkid", "-s", "LABEL", "-o", "value",
             str(drives.partitions_of(loop_dev)[0])],
            capture_output=True, text=True).stdout.strip()
        marcar("Fase 5: la etiqueta quedó normalizada en el volumen",
               leida == esperada, f"esperada={esperada!r} leída={leida!r}")

    ok_desmonte, detalle = drives.eject_mount_point(punto_montaje)
    marcar("Fase 5: se pudo desmontar el punto de montaje al terminar",
           ok_desmonte, detalle)


def main() -> int:
    if os.geteuid() != 0:
        print("Este script necesita root (losetup, mkfs.vfat, mount).\n"
              "Corré: sudo python3 tools/manual_factory_mode_e2e.py",
              file=sys.stderr)
        return 2

    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size-mb", type=int, default=256,
                     help="Tamaño del disco virtual en MB (default: 256).")
    args = ap.parse_args()

    for herramienta in ("dd", "losetup", "mkfs.vfat", "mount", "umount",
                        "parted", "wipefs", "lsblk", "udevadm", "udisksctl",
                        "blkid", "findmnt", "runuser"):
        if shutil.which(herramienta) is None:
            print(f"Falta '{herramienta}' en el PATH.", file=sys.stderr)
            return 2

    workdir = Path(tempfile.mkdtemp(prefix="wbm-factory-e2e-"))
    loop_dev: Path | None = None
    try:
        _imagen, loop_dev = crear_loop_device(args.size_mb, workdir)
        print(f"Loop device de prueba: {loop_dev} ({args.size_mb} MB, "
              f"respaldado por {_imagen})")

        fase_1_blindaje_1(loop_dev)
        fase_2_blindaje_4(loop_dev, workdir)
        fase_3_identidad(loop_dev, workdir)
        fase_4_formateo_real(loop_dev, workdir)
        fase_5_formateo_generico(loop_dev, workdir)
    finally:
        if loop_dev is not None:
            for punto in subprocess.run(
                    ["lsblk", "-nrpo", "MOUNTPOINT", str(loop_dev)],
                    capture_output=True, text=True).stdout.split():
                subprocess.run(["umount", punto], capture_output=True)
            subprocess.run(["losetup", "-d", str(loop_dev)], capture_output=True)
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n=== Resumen ===")
    ok_total = True
    for nombre, ok, _detalle in resultados:
        ok_total &= ok
        print(f"{PASA if ok else FALLA} {nombre}")
    print("\nTODO OK" if ok_total else "\nHubo fallos — revisar arriba.")
    return 0 if ok_total else 1


if __name__ == "__main__":
    raise SystemExit(main())
