#!/usr/bin/env python3
"""Recorta un logo de una imagen y le quita el fondo oscuro.

Para qué
--------
El Ticket de Entrega puede llevar el logo del taller arriba (Ajustes →
Mi taller → Logo). Pocas veces el taller tiene ese logo como PNG
transparente: lo habitual es un afiche o un flyer, con el logo en el medio
y otras cosas alrededor. Esta herramienta saca SOLO la parte pedida y
vuelve transparente el fondo oscuro, para que el logo se apoye limpio
sobre el fondo del ticket.

Uso
---
    python3 tools/prepare_logo.py ENTRADA SALIDA.png --caja X0,Y0,X1,Y1 \\
        [--umbral 0.3] [--radio 3] [--margen 12]

`--caja` es el rectángulo a recortar, en píxeles de la imagen original.
Conviene dejarlo apenas más grande que el logo y lejos de cualquier otro
dibujo: lo que quede adentro y sea claro va a terminar en el logo.

Cómo se quita el fondo
----------------------
Clave por brillo con el fondo estimado de la propia imagen:

1. El color de fondo es la mediana de la mitad más oscura del recorte
   (en un logo sobre fondo oscuro, eso es el fondo).
2. El brillo de cada píxel se toma como el MÁXIMO entre la luminancia y
   el canal más alto (ponderado): con luminancia sola, un morado medio
   tono -mucho azul, poco verde- queda casi tan oscuro como el fondo y se
   borraría junto con él.
3. Para cada píxel se busca el más brillante a `--radio` píxeles: es el
   "núcleo" del trazo al que pertenece. Si ese núcleo no le saca al fondo
   por lo menos `--umbral`, no hay trazo cerca y el píxel es fondo.
4. La opacidad es dónde cae el píxel entre el fondo y su núcleo (0 = como
   el fondo, 1 = como el núcleo), y el color es el DEL NÚCLEO. Así un
   borde antialiaseado queda como el mismo color del trazo, más
   transparente, en vez de un gris: si se lo dejara con su color, el
   oscuro del fondo original -y la sombra que suelen traer estos afiches
   detrás de las letras- quedaría como un halo sucio alrededor del logo
   sobre cualquier otro fondo.
5. Se recorta a lo que quedó visible, con un margen.

No toca la imagen original. Necesita PyGObject con GdkPixbuf, que la app
ya usa: no suma dependencias.
"""
from __future__ import annotations

import argparse
import sys

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib  # noqa: E402


def _caja(texto: str) -> tuple:
    try:
        x0, y0, x1, y1 = (int(v) for v in texto.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError("la caja va como X0,Y0,X1,Y1")
    if x1 <= x0 or y1 <= y0:
        raise argparse.ArgumentTypeError("la caja tiene que tener X1>X0 e Y1>Y0")
    return x0, y0, x1, y1


def _brillo(r: float, g: float, b: float) -> float:
    luminancia = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return max(luminancia, 0.8 * max(r, g, b))


def _mediana_del_fondo(pixeles: list) -> tuple:
    oscuros = sorted(pixeles, key=lambda c: _brillo(*c))[: max(1, len(pixeles) // 2)]
    return tuple(sorted(c[i] for c in oscuros)[len(oscuros) // 2]
                 for i in range(3))


def _maximo_local(valores: list, ancho: int, alto: int, radio: int) -> list:
    """Para cada posición, el (brillo, color) más brillante en un cuadrado
    de lado 2*radio+1. Se hace en dos pasadas (filas y columnas), que da
    lo mismo que mirar el cuadrado entero y cuesta mucho menos."""
    def pasada(origen, largo, cantidad, indice):
        salida = [None] * len(origen)
        for k in range(cantidad):
            for t in range(largo):
                mejor = None
                for u in range(max(0, t - radio), min(largo, t + radio + 1)):
                    v = origen[indice(k, u)]
                    if mejor is None or v[0] > mejor[0]:
                        mejor = v
                salida[indice(k, t)] = mejor
        return salida

    por_filas = pasada(valores, ancho, alto, lambda y, x: y * ancho + x)
    return pasada(por_filas, alto, ancho, lambda x, y: y * ancho + x)


def quitar_fondo(pixeles: list, ancho: int, alto: int, umbral: float,
                 radio: int) -> list:
    """`pixeles` es una lista de (r, g, b) en 0..1, fila por fila.
    Devuelve (r, g, b, a) en 0..1. Ver los pasos en la cabecera."""
    brillo_fondo = _brillo(*_mediana_del_fondo(pixeles))
    valores = [(_brillo(*c), c) for c in pixeles]
    nucleos = _maximo_local(valores, ancho, alto, radio)

    salida = []
    for (brillo, _color), (brillo_nucleo, color_nucleo) in zip(valores, nucleos):
        contraste = brillo_nucleo - brillo_fondo
        if contraste < umbral:
            salida.append((0.0, 0.0, 0.0, 0.0))
            continue
        a = min(1.0, max(0.0, (brillo - brillo_fondo) / contraste))
        # Lo que apenas se separa del fondo es ruido del JPEG, no borde.
        if a < 0.08:
            a = 0.0
        salida.append((*color_nucleo, a))
    return salida


def recortar_a_lo_visible(rgba: list, ancho: int, alto: int,
                          margen: int, minimo: float = 0.04) -> tuple:
    """(rgba, ancho, alto) recortado al rectángulo de lo que se ve, más
    `margen` píxeles transparentes alrededor."""
    xs, ys = [], []
    for y in range(alto):
        for x in range(ancho):
            if rgba[y * ancho + x][3] > minimo:
                xs.append(x)
                ys.append(y)
    if not xs:
        raise SystemExit("no quedó nada visible: probá con un --umbral más bajo")
    x0, x1 = max(0, min(xs) - margen), min(ancho, max(xs) + 1 + margen)
    y0, y1 = max(0, min(ys) - margen), min(alto, max(ys) + 1 + margen)
    nuevo = [rgba[y * ancho + x] for y in range(y0, y1) for x in range(x0, x1)]
    return nuevo, x1 - x0, y1 - y0


def procesar(entrada: str, salida: str, caja: tuple, umbral: float,
             radio: int, margen: int) -> tuple:
    original = GdkPixbuf.Pixbuf.new_from_file(entrada)
    x0, y0, x1, y1 = caja
    x1 = min(x1, original.get_width())
    y1 = min(y1, original.get_height())
    recorte = original.new_subpixbuf(x0, y0, x1 - x0, y1 - y0)
    ancho, alto = recorte.get_width(), recorte.get_height()
    canales = recorte.get_n_channels()
    paso = recorte.get_rowstride()
    datos = recorte.read_pixel_bytes().get_data()

    pixeles = []
    for y in range(alto):
        fila = y * paso
        for x in range(ancho):
            i = fila + x * canales
            pixeles.append((datos[i] / 255, datos[i + 1] / 255, datos[i + 2] / 255))

    rgba = quitar_fondo(pixeles, ancho, alto, umbral, radio)
    rgba, ancho, alto = recortar_a_lo_visible(rgba, ancho, alto, margen)

    crudo = bytearray()
    for r, g, b, a in rgba:
        crudo += bytes((round(r * 255), round(g * 255), round(b * 255),
                        round(a * 255)))
    pixbuf = GdkPixbuf.Pixbuf.new_from_bytes(
        GLib.Bytes.new(bytes(crudo)), GdkPixbuf.Colorspace.RGB, True, 8,
        ancho, alto, ancho * 4)
    pixbuf.savev(salida, "png", [], [])
    return ancho, alto


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Recorta un logo y vuelve transparente su fondo oscuro.")
    parser.add_argument("entrada")
    parser.add_argument("salida", help="ruta del PNG que se genera")
    parser.add_argument("--caja", type=_caja, required=True,
                        help="rectángulo a recortar: X0,Y0,X1,Y1 en píxeles")
    parser.add_argument("--umbral", type=float, default=0.3,
                        help="cuánto más brillante que el fondo tiene que ser "
                             "un trazo para contar como logo (0..1)")
    parser.add_argument("--radio", type=int, default=3,
                        help="a cuántos píxeles se busca el núcleo del trazo "
                             "(mayor que el ancho del borde antialiaseado)")
    parser.add_argument("--margen", type=int, default=12,
                        help="píxeles transparentes alrededor del resultado")
    args = parser.parse_args(argv)
    if not args.salida.lower().endswith(".png"):
        parser.error("la salida tiene que ser .png (es lo que guarda transparencia)")
    ancho, alto = procesar(args.entrada, args.salida, args.caja, args.umbral,
                           args.radio, args.margen)
    print(f"{args.salida}: {ancho}x{alto}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
