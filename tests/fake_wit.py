"""Un `wit` falso, pero ejecutable de verdad, para probar cómo lo llama la
app: con qué argumentos, desde qué carpeta y qué termina escrito.

Reemplazar `wit_wrapper.convert` o `_run_with_progress` con un espía
prueba qué se le PIDE a `wit`, no lo que `wit` hace con eso. Acá se corre
un programa de verdad (con `Popen`, como el real) que imita lo que se le
midió al binario real (wit v3.05a r8638) en lo que importa para estas
pruebas:

- `COPY origen --dest destino`: copia los bytes del origen al destino
  pasando por un temporal oculto (`.{nombre}.{azar}.tmp`) que renombra al
  final, como el real. Sale con 80 si la carpeta del destino no existe.
- `CMP a b`: sale con 0 si los dos archivos son iguales y con 2 si no, o
  si no puede abrir alguno.
- `LIST --sections ruta`: el `[disc-1]` con el ID6 de los primeros 6 bytes
  del archivo; `disctype` Wii si el ID arranca con R, S o W, y GameCube si
  no.

Y lo que motivó todo esto: el `--dest` de `COPY` y el SEGUNDO archivo de
`CMP` pasan por `como_lo_escribe_wit`, que reescribe la ruta como la
reescribe el real (medido con `wit COPY -t`, que dice adónde copiaría sin
escribir nada): junta los espacios seguidos, quita los del principio y el
final del nombre, cambia `* ; ? @ \\ ^ ` | ~` y los caracteres de control
por un espacio, `:` por " - " y expande las secuencias `%` (`%%` es `%`;
`%I`, `%T`, `%N`... son datos del disco). El origen de `COPY`, el primer
archivo de `CMP` y la ruta de `LIST` llegan tal cual (medido también).

Cada llamada queda en `llamadas.log` con la carpeta desde la que se corrió.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

_GUION = r'''
import os, re, sys

RAROS = "*;?@\\^`|~"
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llamadas.log")


def como_lo_escribe_wit(ruta):
    ruta = re.sub(r"%[A-Za-z%]", lambda m: "%" if m.group() == "%%" else "DATO", ruta)
    ruta = ruta.replace(":", " - ")
    ruta = "".join(" " if (c in RAROS or ord(c) < 0x20 or c == "\x7f") else c
                   for c in ruta)
    ruta = re.sub(" {2,}", " ", ruta)
    carpeta, nombre = os.path.split(ruta)
    return os.path.join(carpeta, nombre.strip(" "))


def falla(codigo, texto):
    sys.stderr.write("!! wit: ERROR #%d [%s]\n" % (codigo, texto))
    sys.exit(codigo)


args = sys.argv[1:]
with open(LOG, "a", encoding="utf-8", errors="surrogateescape") as f:
    f.write("\x1f".join([os.getcwd()] + args) + "\n")

if args[0] == "COPY":
    i = args.index("--dest")
    origen, destino = args[i - 1], como_lo_escribe_wit(args[i + 1])
    if not os.path.isfile(origen):
        falla(76, "CAN'T OPEN FILE")
    carpeta = os.path.dirname(destino) or "."
    if not os.path.isdir(carpeta):
        falla(80, "CAN'T CREATE FILE")
    if os.path.exists(destino) and "--overwrite" not in args:
        falla(77, "FILE ALREADY EXISTS")
    temporal = os.path.join(carpeta, "." + os.path.basename(destino) + ".falso.tmp")
    with open(origen, "rb") as src, open(temporal, "wb") as dst:
        dst.write(src.read())
    os.replace(temporal, destino)
    sys.exit(0)

if args[0] == "CMP":
    a, b = args[1], como_lo_escribe_wit(args[2])
    if not (os.path.isfile(a) and os.path.isfile(b)):
        falla(2, "CAN'T OPEN FILE")
    with open(a, "rb") as fa, open(b, "rb") as fb:
        sys.exit(0 if fa.read() == fb.read() else 2)

if args[0] == "LIST" and "--sections" in args:
    ruta = args[-1]
    if not os.path.isfile(ruta):
        falla(76, "CAN'T OPEN FILE")
    with open(ruta, "rb") as f:
        game_id = f.read(6).decode("ascii", "replace")
    consola = "2 Wii" if game_id[:1] in "RSW" else "1 GameCube"
    print("[disc-1]\nid=%s\ntitle=Juego\ndisctype=%s" % (game_id, consola))
    sys.exit(0)

falla(108, "SYNTAX ERROR")
'''


@dataclass
class WitFalso:
    binario: Path

    def llamadas(self) -> list[tuple[str, list[str]]]:
        """(carpeta desde la que se corrió, argumentos) de cada llamada."""
        log = self.binario.parent / "llamadas.log"
        if not log.exists():
            return []
        salida = []
        for linea in log.read_text(encoding="utf-8", errors="surrogateescape").splitlines():
            carpeta, *args = linea.split("\x1f")
            salida.append((carpeta, args))
        return salida


def instalar(directorio: Path) -> WitFalso:
    """Deja el `wit` falso en `directorio/wit`, listo para pasarlo como
    `binary`/`wit_binary` (`find_wit` lo encuentra por ruta absoluta)."""
    directorio.mkdir(parents=True, exist_ok=True)
    binario = directorio / "wit"
    binario.write_text(f"#!{sys.executable} -I\n{_GUION}", encoding="utf-8")
    os.chmod(binario, 0o755)
    return WitFalso(binario)
