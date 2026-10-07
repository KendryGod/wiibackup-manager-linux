"""Herramientas de disco falsas para probar `drives.format_fat32` de
punta a punta SIN root ni hardware.

`format_fat32` hace casi todo su trabajo en un script de shell privilegiado
(`drives._FORMAT_SCRIPT`) que llama a wipefs, parted, lsblk, udevadm y
mkfs.vfat. Mockear `run` y mirar los argumentos no prueba ese script: acá
se lo ejecuta DE VERDAD (con /bin/sh), pero con un PATH que tiene adelante
versiones falsas de esas herramientas. Cada una:

- deja una línea en `llamadas.log` con su nombre y argumentos, en orden,
  para poder afirmar qué corrió y en qué orden (ej. que la identidad se
  consultó antes del primer `wipefs`);
- lee/escribe un estado mínimo en archivos (`particiones`, `montado`,
  `identidad`) que se comporta como el kernel lo suficiente para este
  flujo: `parted mkpart` crea la partición con la convención de nombres
  del bus (`sdb1`, pero `mmcblk0p1`), `wipefs` sobre el disco tira las
  particiones, `umount` saca la línea de /proc/mounts.

`mkfs.vfat` falso reproduce el chequeo real de dosfstools que originó el
bug: sobre un disco ENTERO que todavía tiene particiones se niega con el
mismo mensaje literal ("Partitions or virtual mappings on device ...").
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

MENSAJE_MKFS_CON_PARTICIONES = (
    "mkfs.vfat: Partitions or virtual mappings on device '{dev}', "
    "not making filesystem (use -I to override)")

_COMUN = r'''#!/bin/sh
E="$ESTADO_DISCO"
printf '%s' "$(basename "$0")" >> "$E/llamadas.log"
for a in "$@"; do printf '\037%s' "$a" >> "$E/llamadas.log"; done
printf '\n' >> "$E/llamadas.log"
ultimo=""; for a in "$@"; do ultimo=$a; done
nombre_part() { case "$1" in *[0-9]) printf '%sp1' "$1" ;; *) printf '%s1' "$1" ;; esac; }
'''

_STUBS = {
    "lsblk": r'''
case "$*" in
  *MOUNTPOINT*) grep -E "^$ultimo(p?[0-9]+)? " "$(cat "$E/proc_mounts")" | awk '{print $2}'
                echo ;;
  *) printf '%s disk\n' "$ultimo"
     [ -s "$E/particiones" ] && sed 's/$/ part/' "$E/particiones" ;;
esac
exit 0
''',
    "wipefs": r'''
if [ "$ultimo" = "$(cat "$E/disco")" ]; then : > "$E/particiones"; fi
exit 0
''',
    "parted": r'''
case "$*" in
  *mklabel*) : > "$E/particiones"; printf '%s\n' "$4" > "$E/tabla" ;;
  *mkpart*) nombre_part "$2" > "$E/particiones"; echo >> "$E/particiones"
            if [ -f "$E/identidad_tras_parted" ]; then mv "$E/identidad_tras_parted" "$E/identidad"; fi ;;
esac
exit 0
''',
    "udevadm": r'''
case "$1" in
  settle) exit 0 ;;
  info) if [ -f "$E/udev_props" ]; then cat "$E/udev_props"
        elif [ -f "$E/identidad" ]; then printf 'ID_SERIAL=%s\n' "$(cat "$E/identidad")"
        fi
        exit 0 ;;
esac
exit 0
''',
    "mkfs.vfat": r'''
if [ -f "$E/mkfs_falla" ]; then cat "$E/mkfs_falla" >&2; exit 1; fi
if [ "$ultimo" = "$(cat "$E/disco")" ] && [ -s "$E/particiones" ]; then
  echo "mkfs.vfat: Partitions or virtual mappings on device '$ultimo', not making filesystem (use -I to override)" >&2
  exit 1
fi
printf '%s\n' "$ultimo" > "$E/formateado"
exit 0
''',
    "udisksctl": r'''
mkdir -p "$(cat "$E/punto_montaje")"
printf '%s %s vfat rw 0 0\n' "$ultimo" "$(cat "$E/punto_montaje")" >> "$(cat "$E/proc_mounts")"
echo "Mounted $ultimo at $(cat "$E/punto_montaje")"
exit 0
''',
    "umount": r'''
pm=$(cat "$E/proc_mounts")
grep -v "^$ultimo " "$pm" > "$pm.tmp" || true
mv "$pm.tmp" "$pm"
exit 0
''',
}


@dataclass
class DiscoFalso:
    """Estado del disco simulado y acceso a lo que pasó."""
    estado: Path
    bin_dir: Path
    disco: Path
    # Se llama justo antes de ejecutar el comando que llevaba `pkexec`:
    # simula lo que puede pasar mientras el diálogo de contraseña está
    # abierto (ej. que cambien el USB por otro).
    al_pedir_contrasena: Optional[Callable[[], None]] = field(default=None)

    def llamadas(self) -> list[list[str]]:
        log = self.estado / "llamadas.log"
        if not log.exists():
            return []
        # Argumentos separados por \x1f (no por espacios): una etiqueta
        # como "FOTOS MAMA" es UN argumento.
        return [linea.split("\x1f") for linea in log.read_text().splitlines() if linea]

    def nombres(self) -> list[str]:
        return [c[0] for c in self.llamadas()]

    def particiones(self) -> list[str]:
        return (self.estado / "particiones").read_text().split()

    def formateado(self) -> str | None:
        f = self.estado / "formateado"
        return f.read_text().strip() if f.exists() else None

    def tabla(self) -> str | None:
        f = self.estado / "tabla"
        return f.read_text().strip() if f.exists() else None

    def fijar_identidad(self, identidad: str | None) -> None:
        f = self.estado / "identidad"
        if identidad is None:
            f.unlink(missing_ok=True)
        else:
            f.write_text(identidad)

    def cambiar_identidad_despues_de_parted(self, identidad: str) -> None:
        (self.estado / "identidad_tras_parted").write_text(identidad)

    def fijar_propiedades_udev(self, propiedades: dict[str, str]) -> None:
        (self.estado / "udev_props").write_text(
            "".join(f"{k}={v}\n" for k, v in propiedades.items()))

    def hacer_fallar_mkfs(self, mensaje: str) -> None:
        (self.estado / "mkfs_falla").write_text(mensaje + "\n")

    def run(self, cmd, **kwargs):
        """El `run` que se le inyecta a `format_fat32`: ejecuta el comando
        de verdad, pero con las herramientas falsas adelante en el PATH.
        `pkexec` se saca (no hay a quién pedirle contraseña en una suite);
        lo demás -incluido `/bin/sh -c <script>`- corre tal cual."""
        cmd = list(cmd)
        if cmd and cmd[0] == "pkexec":
            cmd = cmd[1:]
        if cmd[:2] == ["/bin/sh", "-c"] and self.al_pedir_contrasena is not None:
            self.al_pedir_contrasena()
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '/usr/bin:/bin')}"
        env["ESTADO_DISCO"] = str(self.estado)
        kwargs.pop("env", None)
        return subprocess.run(cmd, env=env, **kwargs)


def crear_disco_falso(base: Path, disco: Path, *, proc_mounts: Path,
                      punto_montaje: Path, particiones: list[str] = (),
                      tabla: str | None = None) -> DiscoFalso:
    estado = base / "estado"
    bin_dir = base / "bin"
    estado.mkdir(parents=True)
    bin_dir.mkdir()
    for nombre, cuerpo in _STUBS.items():
        stub = bin_dir / nombre
        stub.write_text(_COMUN + cuerpo)
        stub.chmod(0o755)
    (estado / "disco").write_text(str(disco))
    (estado / "particiones").write_text("".join(f"{p}\n" for p in particiones))
    (estado / "proc_mounts").write_text(str(proc_mounts))
    (estado / "punto_montaje").write_text(str(punto_montaje))
    if tabla:
        (estado / "tabla").write_text(tabla)
    if not proc_mounts.exists():
        proc_mounts.write_text("")
    return DiscoFalso(estado=estado, bin_dir=bin_dir, disco=disco)
