"""Ejecutables y WADs de Wii mínimos pero bien formados, para las pruebas.

Desde que la Store valida la estructura de `boot.dol`, `boot.elf` y los
`.wad` antes de instalar (`wii_formats`), un ZIP de prueba no puede traer
un `boot.dol` que diga "dol": se rechazaría, igual que un "404: Not
Found". Estas funciones arman uno válido con `marca` adentro, para que una
prueba pueda seguir distinguiendo la versión vieja de la nueva por el
contenido (`dol(b"nuevo") != dol(b"viejo")`).

También acá: los 13 archivos reales del catálogo de Open Shop Channel con
los que se midieron las reglas (`tests/ejecutables_osc.json`).
"""
from __future__ import annotations

import json
import struct
from pathlib import Path

DIRECCION = 0x80004000


def dol(marca: bytes = b"\x60\x00\x00\x00") -> bytes:
    """Un DOL con una sola sección de código (`marca`) cargada en
    0x80004000, con el punto de entrada al principio."""
    cuerpo = marca or b"\x60\x00\x00\x00"
    cabecera = bytearray(0x100)
    struct.pack_into(">I", cabecera, 0x00, 0x100)        # offset de text0
    struct.pack_into(">I", cabecera, 0x48, DIRECCION)    # dirección de text0
    struct.pack_into(">I", cabecera, 0x90, len(cuerpo))  # tamaño de text0
    struct.pack_into(">I", cabecera, 0xE0, DIRECCION)    # punto de entrada
    return bytes(cabecera) + cuerpo


def elf(marca: bytes = b"\x60\x00\x00\x00") -> bytes:
    """Un ELF32 big-endian de PowerPC con un solo segmento (`marca`)."""
    cuerpo = marca or b"\x60\x00\x00\x00"
    ident = b"\x7fELF\x01\x02\x01" + bytes(9)
    cabecera = ident + struct.pack(">HHIIIIIHHHHHH", 2, 20, 1, DIRECCION, 52, 0, 0,
                                   52, 32, 1, 40, 0, 0)
    segmento = struct.pack(">8I", 1, 52 + 32, DIRECCION, DIRECCION, len(cuerpo),
                           len(cuerpo), 5, 4)
    return cabecera + segmento + cuerpo


def _al(x: int, n: int = 0x40) -> int:
    return (x + n - 1) // n * n


def wad(contenidos=(b"contenido",), tipo: bytes = b"Is") -> bytes:
    """Un WAD con la cabecera de 0x20, certificados y ticket en cero, un
    TMD coherente con `contenidos` y los datos alineados como en uno real."""
    cert, tik = 0x100, 0x2A4
    tmd = bytearray(0x1E4 + 36 * len(contenidos))
    struct.pack_into(">H", tmd, 0x1DE, len(contenidos))
    for k, c in enumerate(contenidos):
        struct.pack_into(">IHHQ", tmd, 0x1E4 + 36 * k, k, k, 1, len(c))
    datos = b"".join(c.ljust(_al(len(c)), b"\0") for c in contenidos)
    cabecera = struct.pack(">IHHIIIIII", 0x20, int.from_bytes(tipo, "big"), 0, cert, 0,
                           tik, len(tmd), len(datos), 0)
    partes = [cabecera, bytes(cert), bytes(tik), bytes(tmd), datos]
    return b"".join(p.ljust(_al(len(p)), b"\0") for p in partes)


def reales() -> list[dict]:
    """Los archivos de `tests/ejecutables_osc.json`, con `cabeza` en bytes."""
    datos = json.loads((Path(__file__).parent / "ejecutables_osc.json").read_text(
        encoding="utf-8"))
    return [{**f, "cabeza": bytes.fromhex(f["cabeza"])} for f in datos["archivos"]]


def archivo_real(real: dict) -> bytes:
    """El archivo real armado de nuevo: su cabeza y ceros hasta su tamaño.
    Alcanza para los validadores, que no miran más allá."""
    return real["cabeza"].ljust(real["tamano"], b"\0")
