"""Dibuja el Ticket de Entrega en un PDF de una página.

Por qué acá y no en `ticket_service`
------------------------------------
`ticket_service` responde "qué lleva la unidad"; este módulo responde
"cómo se ve eso en una hoja". Son dos cosas que cambian por motivos
distintos -sumar un dato al ticket no es lo mismo que mover un título o
cambiar un color- y separarlas deja que el conteo se pruebe sin generar
un solo PDF, y que el PDF se pruebe con datos armados a mano sin
necesitar una unidad.

Por qué cairo y no una librería de PDF
--------------------------------------
Porque no suma una dependencia. La app ya depende de PyGObject, que
arrastra pycairo y Pango: `cairo.PDFSurface` genera PDF de verdad y
`PangoCairo` dibuja el texto con las fuentes del sistema, incluidos los
acentos y la "ñ" que un generador de PDF hecho a mano tendría que
resolver a mano. Sumar reportlab o fpdf para esto sería pedirle al
usuario que instale algo más para imprimir una hoja.

El diseño apunta a WhatsApp
---------------------------
Una sola página, tipografía grande y bloques separados: el cliente lo va
a abrir en el celular, no a imprimirlo. Por eso los números que importan
-cuántos juegos, cuánto espacio- van grandes y no en una tabla apretada.
"""
from __future__ import annotations

from pathlib import Path

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Pango, PangoCairo  # noqa: E402

from . import atomicfs  # noqa: E402
from .i18n import _  # noqa: E402
from .library import format_size  # noqa: E402
from .ticket_service import TicketData  # noqa: E402

# A4 en puntos PostScript (72 por pulgada), que es la unidad en la que
# trabaja cairo. A4 y no Carta porque es el tamaño de papel de Argentina,
# donde se entregan estos equipos.
PAGE_WIDTH = 595.276
PAGE_HEIGHT = 841.89
MARGIN = 56.0  # ~2 cm

# Gris de la marca y grises de apoyo. Se definen como constantes y no
# sueltos en el código para que cambiar el look sea tocar un solo bloque.
_TINTA = (0.11, 0.12, 0.14)
_TENUE = (0.45, 0.47, 0.50)
_LINEA = (0.85, 0.86, 0.88)
_ACENTO = (0.13, 0.39, 0.71)


def _texto(ctx, x: float, y: float, texto: str, *, font: str = "Sans 11",
           color=_TINTA, ancho: float = None) -> float:
    """Dibuja `texto` con la esquina superior izquierda en (x, y) y
    devuelve la altura que ocupó, para que quien llama sepa dónde sigue.

    Devolver la altura -en vez de que cada bloque calcule su propio salto-
    es lo que permite que las secciones opcionales (cliente, notas) entren
    o no sin dejar un hueco: el que sigue arranca donde terminó el
    anterior, midiendo de verdad y no con un número fijo. Las notas, que
    son texto libre del usuario y pueden ocupar varias líneas, dependen de
    eso."""
    layout = PangoCairo.create_layout(ctx)
    layout.set_font_description(Pango.FontDescription(font))
    if ancho is not None:
        layout.set_width(int(ancho * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    layout.set_text(texto, -1)
    ctx.set_source_rgb(*color)
    ctx.move_to(x, y)
    PangoCairo.show_layout(ctx, layout)
    return layout.get_pixel_size().height


def _linea(ctx, y: float) -> None:
    ctx.set_source_rgb(*_LINEA)
    ctx.set_line_width(0.75)
    ctx.move_to(MARGIN, y)
    ctx.line_to(PAGE_WIDTH - MARGIN, y)
    ctx.stroke()


def _barra_de_uso(ctx, y: float, ratio: float) -> float:
    """La barra de "cuán llena está la unidad". Es el único gráfico del
    ticket y está para que se entienda de un vistazo, sin leer los
    números: es la misma lectura que da la barra de la pantalla de
    Transferir, en papel."""
    ancho = PAGE_WIDTH - 2 * MARGIN
    alto = 9.0
    ctx.set_source_rgb(*_LINEA)
    ctx.rectangle(MARGIN, y, ancho, alto)
    ctx.fill()
    ctx.set_source_rgb(*_ACENTO)
    ctx.rectangle(MARGIN, y, ancho * max(0.0, min(1.0, ratio)), alto)
    ctx.fill()
    return alto


def _dato(ctx, x: float, y: float, etiqueta: str, valor: str,
          ancho: float) -> float:
    """Un par etiqueta/valor: la etiqueta chica y tenue arriba, el valor
    grande abajo. Devuelve la altura total."""
    alto = _texto(ctx, x, y, etiqueta, font="Sans 9", color=_TENUE, ancho=ancho)
    alto += 2
    alto += _texto(ctx, x, y + alto, valor, font="Sans Bold 15", ancho=ancho)
    return alto


def render_ticket(data: TicketData, dest: Path) -> Path:
    """Escribe el ticket de `data` como PDF en `dest` y devuelve `dest`.

    Se escribe a través de `atomicfs.atomic_write_target` -la primitiva
    que ya usa el resto de la app- para que un fallo a mitad del dibujo no
    deje un PDF cortado con el nombre del definitivo: si algo sale mal, no
    hay archivo, y el usuario no le manda al cliente una hoja a medias.
    Nótese que esto escribe en el disco local (donde el usuario guarda el
    ticket), nunca en la unidad del cliente: para la unidad, el ticket es
    de solo lectura.

    `mkparents=True` porque el destino habitual es una carpeta de
    documentos que puede no existir todavía."""
    dest = Path(dest)
    with atomicfs.atomic_write_target(dest, mkparents=True) as tmp:
        surface = cairo.PDFSurface(str(tmp), PAGE_WIDTH, PAGE_HEIGHT)
        try:
            _dibujar(cairo.Context(surface), data)
        finally:
            # `finish()` es lo que vuelca el PDF al archivo. Va en un
            # `finally` para que un error a mitad del dibujo no deje el
            # surface abierto: el `atomic_write_target` de afuera se
            # encarga de que ese archivo incompleto no llegue a `dest`.
            surface.finish()
    return dest


def _dibujar(ctx, data: TicketData) -> None:
    """Todo el contenido de la hoja, de arriba hacia abajo.

    `y` va bajando a medida que se dibuja y cada bloque devuelve lo que
    ocupó. Escrito así -y no con posiciones fijas- porque hay dos bloques
    que pueden no estar (cliente y notas) y uno que crece según lo que
    escriba el usuario (notas): con coordenadas fijas, un ticket sin
    cliente dejaría un hueco y uno con notas largas escribiría encima del
    pie."""
    ancho_util = PAGE_WIDTH - 2 * MARGIN
    y = MARGIN

    # ------------------------------------------------------- Encabezado --
    y += _texto(ctx, MARGIN, y, _("Ticket de Entrega"), font="Sans Bold 24")
    y += 4
    y += _texto(ctx, MARGIN, y, _("GameFix SPS"), font="Sans 12", color=_ACENTO)
    y += 18
    _linea(ctx, y)
    y += 18

    # ------------------------------------------------ Cliente y fecha --
    # El nombre del cliente es opcional: si no se cargó, el ticket sale
    # igual -sirve como comprobante del contenido de la unidad aunque no
    # esté a nombre de nadie- y la fecha ocupa su lugar sin dejar hueco.
    if data.client_name:
        y += _dato(ctx, MARGIN, y, _("Cliente"), data.client_name, ancho_util)
        y += 14
    y += _dato(ctx, MARGIN, y, _("Fecha de entrega"),
               data.generated_at.strftime("%d/%m/%Y %H:%M"), ancho_util)
    y += 22

    # ------------------------------------------------------- Contenido --
    y += _texto(ctx, MARGIN, y, _("Contenido de la unidad"),
                font="Sans Bold 13")
    y += 12

    columnas = [
        (_("Juegos de Wii"), str(data.contents.wii_games)),
        (_("Juegos de GameCube"), str(data.contents.gamecube_games)),
        (_("Apps de Homebrew"), str(data.contents.homebrew_apps)),
    ]
    ancho_col = ancho_util / len(columnas)
    alto_fila = 0.0
    for i, (etiqueta, valor) in enumerate(columnas):
        alto_fila = max(alto_fila, _dato(ctx, MARGIN + i * ancho_col, y,
                                         etiqueta, valor, ancho_col - 8))
    y += alto_fila + 22

    # ---------------------------------------------------------- Unidad --
    y += _texto(ctx, MARGIN, y, _("Unidad"), font="Sans Bold 13")
    y += 12

    unidad = [
        (_("Capacidad total"), format_size(data.total_bytes)),
        (_("Espacio usado"), format_size(data.used_bytes)),
        (_("Espacio libre"), format_size(data.free_bytes)),
        (_("Formato"), data.filesystem),
    ]
    ancho_col = ancho_util / len(unidad)
    alto_fila = 0.0
    for i, (etiqueta, valor) in enumerate(unidad):
        alto_fila = max(alto_fila, _dato(ctx, MARGIN + i * ancho_col, y,
                                         etiqueta, valor, ancho_col - 8))
    y += alto_fila + 14

    ratio = data.used_ratio
    if ratio is not None:
        y += _barra_de_uso(ctx, y, ratio) + 6
        y += _texto(ctx, MARGIN, y,
                    _("{percent:.0f}% de la unidad ocupado")
                    .format(percent=ratio * 100),
                    font="Sans 9", color=_TENUE)
    y += 22

    # ----------------------------------------------------------- Notas --
    if data.notes:
        y += _texto(ctx, MARGIN, y, _("Notas"), font="Sans Bold 13")
        y += 8
        y += _texto(ctx, MARGIN, y, data.notes, font="Sans 11",
                    ancho=ancho_util)

    # ------------------------------------------------------------- Pie --
    # Anclado abajo y no después del último bloque: es el pie de la hoja,
    # y con notas cortas quedaría flotando en el medio.
    pie = PAGE_HEIGHT - MARGIN - 12
    _linea(ctx, pie - 10)
    _texto(ctx, MARGIN, pie,
           _("Generado por WiiBackup Manager · GameFix SPS"),
           font="Sans 9", color=_TENUE)
