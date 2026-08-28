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
3) El camino feliz corre `format_as_wii_usb` DE VERDAD (mismo código que
   usaría la interfaz) sobre el loop device: Blindaje 1 se fuerza a pasar
   -es la única forma de llegar hasta acá con un loop device, que nunca
   es removible de verdad- y de ahí en más todo es real: blindaje 3
   (tamaño), blindaje 4 (montajes), `mkfs.vfat`, montaje y creación de
   apps/games/wbfs.

Requiere root: crear/soltar un loop device y formatear con `mkfs.vfat`
son operaciones de root en cualquier distro. Se corre con

    sudo python3 tools/manual_factory_mode_e2e.py [--size-mb 256]

No toca ningún disco de la máquina real: crea su propio archivo de imagen
en un directorio temporal y lo limpia (`losetup -d` + borrar el archivo)
al final, pase lo que pase.
"""
from __future__ import annotations

import argparse
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
        ["losetup", "--find", "--show", str(imagen)],
        capture_output=True, text=True, check=True,
    )
    loop_dev = Path(resultado.stdout.strip())
    return imagen, loop_dev


# --------------------------------------------------------------- Fase 1 --
def fase_1_blindaje_1(loop_dev: Path) -> None:
    """El loop device tiene que quedar afuera de la lista blanca, igual
    que un disco interno: es exactamente lo que hace que sea seguro
    usarlo para la Fase 3 sin arriesgar nada -si el Blindaje 1 fallara acá
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
def fase_3_formateo_real(loop_dev: Path) -> None:
    print("\n=== Fase 3: camino feliz — formateo real sobre el loop device ===")
    # Blindaje 1 (adentro de verify_still_safe) rechazaría el loop device
    # de verdad -es justo lo que confirmó la Fase 1-, así que se lo fuerza
    # a pasar para poder ejercitar el resto del camino de punta a punta.
    # De acá en más TODO es real: nada más se simula.
    original_is_removable = drives.is_removable_block_device

    def _forzar_removible(device_path):
        if Path(device_path) == loop_dev:
            return True
        return original_is_removable(device_path)

    drives.is_removable_block_device = _forzar_removible
    try:
        tamano = drives.device_size_bytes(loop_dev)
        device = drives.BlockDevice(path=loop_dev, model="Loop de prueba",
                                    size_bytes=tamano or 0)
        try:
            punto_montaje = drives.format_as_wii_usb(device, label="WII_TEST")
        except Exception as e:  # noqa: BLE001
            marcar("Fase 3: format_as_wii_usb corrió sin levantar excepción",
                   False, str(e))
            return
        marcar("Fase 3: format_as_wii_usb corrió sin levantar excepción", True,
               f"montado en {punto_montaje}")

        carpetas_ok = all((punto_montaje / c).is_dir() for c in drives.FACTORY_FOLDERS)
        marcar("Fase 3: se crearon apps/games/wbfs en el punto de montaje",
               carpetas_ok, str(sorted(p.name for p in punto_montaje.iterdir())))

        fstype = drives.filesystem_of(punto_montaje)
        marcar("Fase 3: el filesystem resultante es FAT32/vfat",
               fstype in {"vfat", "fat32"}, f"filesystem_of={fstype}")

        ok_desmonte, detalle = drives.eject_mount_point(punto_montaje)
        marcar("Fase 3: se pudo desmontar el punto de montaje al terminar",
               ok_desmonte, detalle)
    finally:
        drives.is_removable_block_device = original_is_removable


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

    for herramienta in ("dd", "losetup", "mkfs.vfat", "mount", "umount"):
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
        fase_3_formateo_real(loop_dev)
    finally:
        if loop_dev is not None:
            subprocess.run(["umount", str(loop_dev)], capture_output=True)
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
