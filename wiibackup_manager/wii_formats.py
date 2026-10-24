"""Estructura de los ejecutables (DOL, ELF) y los WADs de Wii.

Para no instalar basura en la unidad del cliente. `testzip` comprueba que
el ZIP de una app esté sano -el CRC de cada archivo-, no que lo de adentro
sirva: un ZIP íntegro con un `boot.dol` que es un "Not Found" de 9 bytes,
un "404: Not Found" o una página HTML se instalaba como "Instalada ✓".

Se mira la ESTRUCTURA y nunca un tamaño mínimo: en el catálogo de Open
Shop Channel hay archivos legítimos de 9 bytes (los `.cli` de gcmm), y un
DOL bien formado puede medir 260. Lo que no tiene la forma del formato se
rechaza, mida lo que mida.

Medido contra el catálogo entero de OSC el 2026-10-24 (299 apps, 395
archivos: 372 DOL, 22 ELF y un WAD, el `plforwarder` de postloader): con
estas reglas pasan todos. Una cosa que salió de esa medición: el `boot.dol`
de "grim" (ResidualVM) es un ELF. El Homebrew Channel decide si un
ejecutable es DOL o ELF por el contenido, no por el nombre, y acá también
(`problema_ejecutable`).

Las funciones reciben el principio del archivo (`cabeza`, hasta `CABEZA`
bytes) y su tamaño real, y devuelven el motivo del rechazo, ya traducido,
o None si está bien formado. No leen el archivo ellas mismas: lo mismo
sirve para un miembro de un ZIP que para un archivo en disco.
"""
from __future__ import annotations

import struct
from pathlib import PurePosixPath
from typing import Optional

from .i18n import _

# Cuánto del principio de un archivo hace falta leer para validarlo. Todo lo
# que se mira está al principio: la tabla del DOL (0x100 bytes), la cabecera
# y los segmentos de un ELF (en los del catálogo, hasta 0x94) y la cabecera
# y el TMD de un WAD (hasta ~25 KB con 512 contenidos). Un archivo que
# pone su cabecera más lejos que esto no es uno que arme una herramienta
# de la Wii.
CABEZA = 1024 * 1024

# Las direcciones donde puede cargarse una sección: MEM1 (24 MB) y MEM2 (64
# MB), con y sin caché. Todos los DOL del catálogo cargan en MEM1 con caché
# (de 0x80001800 a 0x81065de0).
_MEMORIA = ((0x80000000, 0x81800000), (0x90000000, 0x94000000),
            (0xC0000000, 0xC1800000), (0xD0000000, 0xD4000000))

_DOL_TABLA = 0x100
_DOL_SECCIONES = 18          # 7 de código y 11 de datos
_DOL_CODIGO = 7

_ELF_FIRMA = b"\x7fELF"
_ELF_CABECERA = 52
_ELF_POWERPC = 20
_ELF_SEGMENTO = 32           # tamaño de una entrada de la tabla en ELF32
_PT_LOAD = 1

_WAD_CABECERA = 0x20
_WAD_TIPOS = (b"Is", b"ib")  # instalable y boot2; "Bk" es un respaldo
_TMD_CABECERA = 0x1E4
_TMD_CONTENIDO = 36


def _en_memoria(direccion: int, tamano: int) -> bool:
    return any(lo <= direccion and direccion + tamano <= hi for lo, hi in _MEMORIA)


def problema_dol(cabeza: bytes, tamano: int) -> Optional[str]:
    """Que el DOL tenga su tabla de secciones, que cada sección caiga
    dentro del archivo y en la memoria de la Wii, que haya código y que el
    punto de entrada esté en él."""
    if tamano < _DOL_TABLA or len(cabeza) < _DOL_TABLA:
        return _("no llega a tener la tabla de secciones de un DOL ({size} bytes)").format(
            size=tamano)
    offsets = struct.unpack(">18I", cabeza[0x00:0x48])
    direcciones = struct.unpack(">18I", cabeza[0x48:0x90])
    tamanos = struct.unpack(">18I", cabeza[0x90:0xD8])
    (entrada,) = struct.unpack(">I", cabeza[0xE0:0xE4])
    codigo = []
    for i in range(_DOL_SECCIONES):
        offset, direccion, largo = offsets[i], direcciones[i], tamanos[i]
        if largo == 0:
            continue
        if offset < _DOL_TABLA or offset + largo > tamano:
            return _("la sección {n} del DOL se sale del archivo").format(n=i)
        if not _en_memoria(direccion, largo):
            return _("la sección {n} del DOL cae fuera de la memoria de la Wii").format(n=i)
        if i < _DOL_CODIGO:
            codigo.append((direccion, direccion + largo))
    if not codigo:
        return _("el DOL no tiene código")
    if not any(inicio <= entrada < fin for inicio, fin in codigo):
        return _("el punto de entrada del DOL no cae en su código")
    return None


def problema_elf(cabeza: bytes, tamano: int) -> Optional[str]:
    """Que sea un ELF de 32 bits, big-endian y de PowerPC, con segmentos
    que caigan dentro del archivo y el punto de entrada en uno de ellos."""
    if len(cabeza) < _ELF_CABECERA or cabeza[:4] != _ELF_FIRMA:
        return _("no tiene la firma de un ELF")
    (maquina,) = struct.unpack(">H", cabeza[18:20])
    if cabeza[4] != 1 or cabeza[5] != 2 or maquina != _ELF_POWERPC:
        return _("no es un ELF de PowerPC de 32 bits")
    entrada, tabla = struct.unpack(">II", cabeza[24:32])
    largo_entrada, cantidad = struct.unpack(">HH", cabeza[42:46])
    if largo_entrada < _ELF_SEGMENTO:
        # Una entrada más corta que la de ELF32: la tabla no se puede leer.
        return _("no es un ELF de PowerPC de 32 bits")
    fin_tabla = tabla + cantidad * largo_entrada
    if fin_tabla > tamano or fin_tabla > len(cabeza):
        return _("la tabla de segmentos del ELF se sale del archivo")
    cargados = []
    for k in range(cantidad):
        p = tabla + k * largo_entrada
        tipo, offset, direccion, _fisica, en_archivo, en_memoria = struct.unpack(
            ">6I", cabeza[p:p + 24])
        if tipo != _PT_LOAD:
            continue
        if en_archivo and offset + en_archivo > tamano:
            return _("un segmento del ELF se sale del archivo")
        cargados.append((direccion, direccion + max(en_memoria, en_archivo), en_archivo))
    if not any(en_archivo for _i, _f, en_archivo in cargados):
        return _("el ELF no tiene segmentos para cargar")
    if not any(inicio <= entrada < fin for inicio, fin, _a in cargados):
        return _("el punto de entrada del ELF no cae en ningún segmento")
    return None


def problema_ejecutable(cabeza: bytes, tamano: int) -> Optional[str]:
    """Un `boot.dol` o un `boot.elf`: ELF si empieza con la firma de ELF,
    DOL si no, sin mirar el nombre (como el Homebrew Channel)."""
    if cabeza[:4] == _ELF_FIRMA:
        return problema_elf(cabeza, tamano)
    return problema_dol(cabeza, tamano)


def _al(x: int, n: int) -> int:
    return (x + n - 1) // n * n


def problema_wad(cabeza: bytes, tamano: int) -> Optional[str]:
    """Que el WAD tenga su cabecera de 0x20 con tipo "Is" o "ib", que sus
    secciones (alineadas a 0x40) entren en el archivo y que el TMD sea
    coherente: su tamaño con la cantidad de contenidos, y los contenidos con
    la sección de datos (cada uno cifrado en bloques de 16 bytes y empezando
    en un múltiplo de 0x40). En el WAD de postloader, la suma da exacta."""
    if tamano < _WAD_CABECERA or len(cabeza) < _WAD_CABECERA:
        return _("no tiene la cabecera de un WAD")
    (largo_cabecera,) = struct.unpack(">I", cabeza[0:4])
    if largo_cabecera != _WAD_CABECERA or cabeza[4:6] not in _WAD_TIPOS:
        return _("no tiene la cabecera de un WAD")
    certificados, _reservado, ticket, tmd, datos, _pie = struct.unpack(">6I", cabeza[8:32])
    inicio_tmd = _al(_WAD_CABECERA, 0x40) + _al(certificados, 0x40) + _al(ticket, 0x40)
    inicio_datos = inicio_tmd + _al(tmd, 0x40)
    if (certificados == 0 or ticket == 0 or tmd < _TMD_CABECERA
            or inicio_datos + datos > tamano or inicio_tmd + tmd > len(cabeza)):
        return _("las secciones del WAD no entran en el archivo")
    (cantidad,) = struct.unpack(">H", cabeza[inicio_tmd + 0x1DE:inicio_tmd + 0x1E0])
    if cantidad == 0 or tmd != _TMD_CABECERA + _TMD_CONTENIDO * cantidad:
        return _("el TMD del WAD no es coherente")
    largos = [struct.unpack(">Q", cabeza[p + 8:p + 16])[0]
              for p in (inicio_tmd + _TMD_CABECERA + _TMD_CONTENIDO * k
                        for k in range(cantidad))]
    minimo = sum(_al(_al(x, 16), 0x40) for x in largos[:-1]) + _al(largos[-1], 16)
    maximo = sum(_al(_al(x, 16), 0x40) for x in largos)
    if not minimo <= datos <= maximo:
        return _("los contenidos del TMD no coinciden con los datos del WAD")
    return None


def hay_que_validar(nombre: str) -> bool:
    """True para `boot.dol`, `boot.elf` (sin distinguir mayúsculas: en
    FAT32 son el mismo archivo, y hay ZIPs que los traen así) y cualquier
    `.wad`. Los demás archivos de una app no tienen un formato que mirar."""
    base = PurePosixPath(nombre.replace("\\", "/")).name.lower()
    return base in ("boot.dol", "boot.elf") or base.endswith(".wad")


def problema(nombre: str, cabeza: bytes, tamano: int) -> Optional[str]:
    """El motivo por el que `nombre` no sirve, o None si está bien formado
    o no es de los que se validan (`hay_que_validar`)."""
    if not hay_que_validar(nombre):
        return None
    if nombre.lower().endswith(".wad"):
        return problema_wad(cabeza, tamano)
    return problema_ejecutable(cabeza, tamano)
