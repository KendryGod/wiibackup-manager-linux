"""Dibuja el Ticket de Entrega en PDF.

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

La marca es del taller, no del código
-------------------------------------
Nombre, eslogan, ubicación, WhatsApp, logo y color salen de `ShopProfile`,
que se arma con lo que cada taller cargó en Ajustes ("Mi taller"). Con
todo vacío el ticket sale igual, con un encabezado neutro: la app la usan
talleres distintos y ninguno tiene que imprimir el nombre de otro.

Dos modos
---------
"Oscuro" es el de la marca, pensado para abrirse en el celular por
WhatsApp. "Claro" es para imprimir: fondo blanco, mismos acentos
(oscurecidos donde son texto, para que se lean sobre blanco), y el nombre
del taller como texto, porque un logo pensado para fondo oscuro se ve mal
o directamente no se ve sobre papel.

Tipografía
----------
No se empaqueta ninguna fuente: Pango acepta una LISTA de familias y usa
la primera que esté instalada, así que se piden en orden de preferencia
(Outfit/Lexend, geométricas y redondeadas, para títulos; Inter para el
texto) con respaldos que vienen en cualquier escritorio GNOME. Nunca queda
un hueco: si no hay ninguna, fontconfig cae a "Sans".

QR opcional
-----------
El código que abre el chat de WhatsApp se arma con `segno` o, si no está,
con `qrcode` (`python3-qrcode` en Fedora). Las dos son Python puro y
ninguna es obligatoria: sin ellas el ticket sale igual, con el número en
texto. Los módulos se dibujan como vectores (nítidos a cualquier zoom)
sobre una tarjeta BLANCA: un QR claro sobre fondo oscuro -invertido- no
lo leen muchas cámaras.
"""
from __future__ import annotations

import io
import re
import urllib.parse
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GdkPixbuf, Pango, PangoCairo  # noqa: E402

from . import atomicfs, config  # noqa: E402
from .formatting import format_size  # noqa: E402
from .i18n import _  # noqa: E402
from .ticket_service import (CONSOLE_GAMECUBE, CONSOLE_WII,  # noqa: E402
                             TicketData)

# A4 en puntos PostScript (72 por pulgada), que es la unidad en la que
# trabaja cairo. Es el mismo formato que tuvo siempre el ticket.
PAGE_WIDTH = 595.276
PAGE_HEIGHT = 841.89
MARGIN = 40.0
CONTENT_WIDTH = PAGE_WIDTH - 2 * MARGIN

# Familias en orden de preferencia (ver "Tipografía" arriba).
FONT_TITLE = "Outfit, Lexend, Montserrat, Inter, Cantarell, Sans"
FONT_BODY = "Inter, Adwaita Sans, Cantarell, Noto Sans, DejaVu Sans, Sans"
FONT_MONO = "Adwaita Mono, DejaVu Sans Mono, Noto Sans Mono, Monospace"

# Lista de juegos: filas de alto fijo, para que la paginación sea una
# cuenta y no una prueba y error. Con muchos juegos van en dos columnas.
ROW_HEIGHT = 20.5
LIST_COLUMNS = 2
LIST_GUTTER = 14.0

# Tamaño adaptable. El ticket se lee en el celular: con pocos juegos la
# hoja tiene lugar de sobra, y llenarla con letra más grande se lee mejor
# que una lista chica arriba y media hoja vacía abajo. Debajo de
# POCOS_JUEGOS se prueban estas combinaciones (escala del contenido,
# columnas de la lista), de la más grande a la más chica, y se usa la
# primera con la que todo entra en una hoja. Una sola columna va primero:
# es la que no corta títulos largos. Con más juegos, o si ninguna entra,
# el tamaño compacto (escala 1, dos columnas).
POCOS_JUEGOS = 20
_ESCALAS_POCOS = ((1.45, 1), (1.3, 1), (1.2, 1), (1.1, 1), (1.3, 2), (1.2, 2),
                  (1.1, 2))
# Último intento antes de pasar a dos hojas, con cualquier cantidad: un
# poco más chico que el compacto (sigue siendo más grande que el ticket
# original). Una hoja entera se lee mejor que una segunda hoja con dos
# filas sueltas; si ni así entra, la lista sigue en otra hoja en tamaño
# compacto.
_ESCALA_DENSA = 0.9
# Lo que sobra después de elegir la escala se reparte entre las secciones,
# hasta este tope por hueco: más que eso deja de verse aireado y pasa a
# verse desarmado.
AIRE_MAX = 36.0

# Caja máxima del logo en el encabezado.
LOGO_MAX_WIDTH = 340.0
LOGO_MAX_HEIGHT = 110.0


def _hex(value: str) -> tuple:
    return config.parse_hex_color(value) or (0.0, 0.0, 0.0)


def _mezcla(a: tuple, b: tuple, t: float) -> tuple:
    """`a` llevado hacia `b` en una fracción `t` (0 = a, 1 = b)."""
    return tuple(x + (y - x) * t for x, y in zip(a, b))


@dataclass(frozen=True)
class _Paleta:
    fondo: tuple
    fondo_arriba: tuple   # degradé sutil del encabezado
    fondo_abajo: tuple    # resplandor del pie
    texto: tuple
    tenue: tuple
    ubicacion: tuple
    etiqueta: tuple       # etiquetas de sección
    eslogan: tuple
    acento: tuple         # líneas, barra, detalles
    acento_texto: tuple   # el acento cuando ES texto
    destacado: tuple      # fondo de los encabezados de grupo
    fila_alterna: tuple
    linea: tuple
    pista: tuple          # fondo de la barra de uso
    # No son colores, pero viajan con ellos a todas las funciones de dibujo
    # del contenido: cuánto se agranda (letra y espaciado) y cuánto espacio
    # extra va entre secciones. Ver POCOS_JUEGOS y `_preparar`.
    escala: float = 1.0
    aire: float = 0.0
    # Lo que sobra aun con el `aire` al tope (una hoja casi sin datos) se
    # reparte arriba y abajo del bloque, para que quede centrado y no
    # pegado al encabezado con media hoja vacía debajo.
    arriba: float = 0.0


def _paleta(theme: str, accent: str) -> _Paleta:
    """Colores medidos sobre el flyer de referencia (fondo #00000C con
    un degradé hacia #0B0720, morado #A97FF8, eslogan #F0B0F0, etiquetas
    #94DCD6). El acento lo elige cada taller; lo que se deriva de él
    -el fondo de los encabezados de grupo- se calcula, así cambiar el
    color en Ajustes cambia todo el ticket de forma coherente."""
    acento = _hex(accent)
    if theme == config.TICKET_THEME_LIGHT:
        blanco = (1.0, 1.0, 1.0)
        return _Paleta(
            fondo=blanco, fondo_arriba=blanco, fondo_abajo=blanco,
            texto=_hex("#15131F"), tenue=_hex("#5E5A70"),
            ubicacion=_hex("#4A4658"),
            # Las versiones claras del teal y del eslogan no llegan a 3:1
            # sobre blanco; estas son el mismo tono, oscurecido.
            etiqueta=_hex("#1B7A73"), eslogan=_hex("#8C4A9E"),
            acento=acento, acento_texto=_mezcla(acento, (0, 0, 0), 0.35),
            destacado=_mezcla(acento, blanco, 0.82),
            fila_alterna=_hex("#F3F1F8"), linea=_hex("#DCD8E6"),
            pista=_hex("#E8E5F0"),
        )
    fondo = _hex("#02030F")
    return _Paleta(
        fondo=fondo, fondo_arriba=_hex("#0B0720"), fondo_abajo=_hex("#0D0826"),
        texto=_hex("#FFFFFF"), tenue=_hex("#9C98B4"),
        ubicacion=_hex("#EDEAE0"),
        etiqueta=_hex("#94DCD6"), eslogan=_hex("#E9A8EE"),
        acento=acento, acento_texto=acento,
        destacado=_mezcla(fondo, acento, 0.30),
        fila_alterna=_hex("#0B0B22"), linea=_hex("#2A2840"),
        pista=_hex("#1C1A30"),
    )


@dataclass(frozen=True)
class ShopProfile:
    """Los datos del taller con los que se firma el ticket. Ver
    `config.Settings` ("Mi taller")."""

    name: str = ""
    slogan: str = ""
    location: str = ""
    whatsapp: str = ""
    logo_path: str = ""
    accent: str = config.DEFAULT_ACCENT_COLOR
    theme: str = config.TICKET_THEME_DARK

    def is_empty(self) -> bool:
        """True si no se cargó NADA del taller: el ticket sale con el
        encabezado neutro y sin WhatsApp, y vale la pena avisarlo."""
        return not any((self.name, self.slogan, self.location,
                        self.whatsapp, self.logo_path))

    @classmethod
    def from_settings(cls, settings: config.Settings) -> "ShopProfile":
        accent = settings.shop_accent_color
        if config.parse_hex_color(accent) is None:
            accent = config.DEFAULT_ACCENT_COLOR
        theme = settings.ticket_theme
        if theme not in config.TICKET_THEMES:
            theme = config.TICKET_THEME_DARK
        return cls(
            name=settings.shop_name.strip(),
            slogan=settings.shop_slogan.strip(),
            location=settings.shop_location.strip(),
            whatsapp=config.clean_whatsapp(settings.shop_whatsapp),
            logo_path=settings.shop_logo_path.strip(),
            accent=accent,
            theme=theme,
        )


# Partículas de los nombres y apellidos que van en minúscula salvo al
# principio: "Juan de la Cruz", "Ana Pérez y Gómez", "Ludwig van Beethoven".
# Al principio sí llevan mayúscula ("De la Cruz" a secas).
_PARTICULAS = frozenset({
    "de", "del", "la", "las", "los", "y", "e", "da", "das", "do", "dos",
    "di", "du", "van", "von", "der", "den", "le",
})


def _capitalizar_palabra(palabra: str) -> str:
    """"garcía-lópez" -> "García-López", "o'brien" -> "O'Brien",
    "mcdonald" -> "McDonald": mayúscula al principio de cada tramo
    separado por guion o apóstrofo, y después del "Mc" de los apellidos
    escoceses e irlandeses."""
    tramos = re.split(r"([-'’])", palabra.lower())
    salida = []
    for tramo in tramos:
        if tramo in ("-", "'", "’") or not tramo:
            salida.append(tramo)
        elif tramo.startswith("mc") and len(tramo) > 2:
            salida.append("Mc" + tramo[2].upper() + tramo[3:])
        else:
            salida.append(tramo[0].upper() + tramo[1:])
    return "".join(salida)


def format_client_name(nombre: str) -> str:
    """El nombre del cliente como se imprime en el ticket: "kendry flores"
    -> "Kendry Flores", "juan de la cruz" -> "Juan de la Cruz".

    Solo para el PDF: lo que se escribió en el diálogo no se toca. Una
    palabra que ya viene con mayúsculas y minúsculas mezcladas ("DeLuca",
    "McDonald") se deja como está: quien la escribió así sabía lo que
    quería, y ninguna regla automática lo va a saber mejor. Lo que viene
    todo en minúscula o todo en mayúscula es lo que se formatea."""
    palabras = nombre.split()
    salida = []
    for i, palabra in enumerate(palabras):
        if palabra != palabra.lower() and palabra != palabra.upper():
            salida.append(palabra)
        elif i > 0 and palabra.lower() in _PARTICULAS:
            salida.append(palabra.lower())
        else:
            salida.append(_capitalizar_palabra(palabra))
    return " ".join(salida)


def format_service(texto: str) -> str:
    """El "servicio realizado" con mayúscula inicial: "hackeo e
    instalacion gaming" -> "Hackeo e instalacion gaming". Solo la primera
    letra: el resto queda como se escribió, para no romper siglas ("USB",
    "HDMI") ni nombres propios."""
    texto = texto.strip()
    return texto[:1].upper() + texto[1:] if texto else texto


def format_whatsapp(digits: str) -> str:
    """"50400001111" -> "+504 0000 1111": bloques de cuatro desde la
    derecha, que es como se agrupa casi cualquier número local, y el
    resto adelante como código de país. No pretende saber el formato de
    cada país; solo que el número se pueda leer y dictar."""
    digits = config.clean_whatsapp(digits)
    if not digits:
        return ""
    bloques = []
    while len(digits) > 4:
        bloques.insert(0, digits[-4:])
        digits = digits[:-4]
    bloques.insert(0, digits)
    return "+" + " ".join(bloques)


def whatsapp_url(digits: str, message: str = "") -> str:
    url = f"https://wa.me/{config.clean_whatsapp(digits)}"
    if message:
        url += "?text=" + urllib.parse.quote(message, safe="")
    return url


def qr_matrix(text: str) -> Optional[list]:
    """Módulos del QR de `text` como filas de booleanos (sin zona de
    silencio), o None si no hay ninguna librería de QR instalada. Ver
    "QR opcional" arriba."""
    try:
        import segno
    except ImportError:
        segno = None
    if segno is not None:
        qr = segno.make_qr(text, error="m")
        return [[bool(m) for m in fila] for fila in qr.matrix]
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_M
    except ImportError:
        return None
    qr = qrcode.QRCode(border=0, error_correction=ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    return [[bool(m) for m in fila] for fila in qr.get_matrix()]


# ------------------------------------------------------------- Texto --
def _layout(ctx, texto: str, font: str, *, ancho: float = None,
            espaciado: float = 0.0, max_lineas: int = 0,
            alinear=Pango.Alignment.LEFT):
    """Un layout de Pango listo para medir o dibujar.

    El texto entra con `set_text` y nunca como markup: el nombre del
    cliente o el título de un juego pueden traer "&" o "<", que en markup
    rompen el dibujo. `max_lineas` corta con "…" en vez de dejar que el
    texto invada lo que sigue; con 1, un título largo no se sale de su
    columna."""
    layout = PangoCairo.create_layout(ctx)
    # 72 ppp: así "11" en la descripción de fuente son 11 puntos de la
    # hoja, y no 11 * 96/72 como da el valor por defecto de Pango.
    PangoCairo.context_set_resolution(layout.get_context(), 72)
    layout.set_font_description(Pango.FontDescription.from_string(font))
    if ancho is not None:
        layout.set_width(int(ancho * Pango.SCALE))
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        if max_lineas:
            layout.set_height(-max_lineas)
            layout.set_ellipsize(Pango.EllipsizeMode.END)
    layout.set_alignment(alinear)
    if espaciado:
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_letter_spacing_new(int(espaciado * Pango.SCALE)))
        layout.set_attributes(attrs)
    layout.set_text(texto, -1)
    return layout


def _medida(layout) -> tuple:
    _tinta, logica = layout.get_extents()
    return logica.width / Pango.SCALE, logica.height / Pango.SCALE


def _mostrar(ctx, layout, x: float, y: float, color) -> float:
    ctx.set_source_rgb(*color)
    ctx.move_to(x, y)
    PangoCairo.show_layout(ctx, layout)
    return _medida(layout)[1]


def _texto(ctx, x, y, texto, font, color, **kw) -> float:
    """Dibuja `texto` con la esquina superior izquierda en (x, y) y
    devuelve la altura que ocupó: cada bloque arranca donde terminó el
    anterior, así un bloque opcional que falta no deja hueco."""
    return _mostrar(ctx, _layout(ctx, texto, font, **kw), x, y, color)


def _centrado(ctx, y, texto, font, color, espaciado: float = 0.0) -> float:
    layout = _layout(ctx, texto, font, espaciado=espaciado)
    ancho, _alto = _medida(layout)
    # El espaciado de letras también se agrega después de la última, y
    # sin descontarlo el texto queda corrido a la izquierda.
    x = (PAGE_WIDTH - (ancho - espaciado)) / 2
    return _mostrar(ctx, layout, x, y, color)


def _pt(tamano: float, p: _Paleta) -> str:
    """Tamaño de letra del contenido, ya multiplicado por la escala."""
    return f"{tamano * p.escala:.2f}"


_ETIQUETA = 9.2
_ESPACIADO_ETIQUETA = 1.8


def _fuente_etiqueta(p: _Paleta) -> str:
    return f"{FONT_BODY} Semi-Bold {_pt(_ETIQUETA, p)}"


def _etiqueta(ctx, x, y, texto, p: _Paleta, ancho: float = None,
              color=None) -> float:
    """Etiqueta: chica, en mayúsculas y espaciada. En el teal de la marca
    si es de sección; las de los campos de adentro van en el tono tenue,
    para que se lea qué es título y qué es dato."""
    return _texto(ctx, x, y, texto.upper(), _fuente_etiqueta(p),
                  color or p.etiqueta, espaciado=_ESPACIADO_ETIQUETA,
                  ancho=ancho, max_lineas=1)


def _rect_redondeado(ctx, x, y, w, h, r) -> None:
    r = min(r, w / 2, h / 2)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -1.5708, 0)
    ctx.arc(x + w - r, y + h - r, r, 0, 1.5708)
    ctx.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    ctx.arc(x + r, y + r, r, 3.1416, 4.7124)
    ctx.close_path()


# ------------------------------------------------------------- Fondo --
def _fondo(ctx, p: _Paleta) -> None:
    ctx.set_source_rgb(*p.fondo)
    ctx.paint()
    if p.fondo_arriba != p.fondo:
        grad = cairo.LinearGradient(0, 0, 0, PAGE_HEIGHT * 0.35)
        grad.add_color_stop_rgb(0, *p.fondo_arriba)
        grad.add_color_stop_rgb(1, *p.fondo)
        ctx.set_source(grad)
        ctx.rectangle(0, 0, PAGE_WIDTH, PAGE_HEIGHT * 0.35)
        ctx.fill()
    if p.fondo_abajo != p.fondo:
        grad = cairo.LinearGradient(0, PAGE_HEIGHT * 0.82, 0, PAGE_HEIGHT)
        grad.add_color_stop_rgb(0, *p.fondo)
        grad.add_color_stop_rgb(1, *p.fondo_abajo)
        ctx.set_source(grad)
        ctx.rectangle(0, PAGE_HEIGHT * 0.82, PAGE_WIDTH, PAGE_HEIGHT * 0.18)
        ctx.fill()


def _divisor(ctx, y: float, p: _Paleta, x0=MARGIN, x1=PAGE_WIDTH - MARGIN,
             centro: bool = True) -> None:
    """Línea fina de lado a lado con un tramo corto de acento en el
    medio, como el divisor del flyer."""
    ctx.set_source_rgb(*p.linea)
    ctx.set_line_width(0.6)
    ctx.move_to(x0, y)
    ctx.line_to(x1, y)
    ctx.stroke()
    if centro:
        medio = (x0 + x1) / 2
        ctx.set_source_rgb(*p.acento)
        ctx.set_line_width(1.6)
        ctx.move_to(medio - 40, y)
        ctx.line_to(medio + 40, y)
        ctx.stroke()


# -------------------------------------------------------------- Logo --
def _cargar_logo(path: str) -> Optional[cairo.ImageSurface]:
    """El logo como superficie de cairo, o None si no se puede usar.

    Se carga con GdkPixbuf -acepta PNG, JPEG, WebP...- y se pasa a cairo
    como PNG en memoria, que es el único formato que cairo lee solo. Un
    archivo que se movió o no es una imagen no rompe el ticket: el
    encabezado cae al nombre en texto."""
    if not path:
        return None
    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(Path(path).expanduser()))
        ok, data = pixbuf.save_to_bufferv("png", [], [])
        if not ok:
            return None
        return cairo.ImageSurface.create_from_png(io.BytesIO(data))
    except Exception:  # noqa: BLE001 - cualquier fallo = sin logo
        return None


def _dibujar_logo(ctx, logo, y: float, max_w: float, max_h: float,
                  centrado: bool = True, x: float = MARGIN) -> float:
    escala = min(max_w / logo.get_width(), max_h / logo.get_height(), 1.0)
    w, h = logo.get_width() * escala, logo.get_height() * escala
    if centrado:
        x = (PAGE_WIDTH - w) / 2
    ctx.save()
    ctx.translate(x, y)
    ctx.scale(escala, escala)
    ctx.set_source_surface(logo, 0, 0)
    ctx.get_source().set_filter(cairo.FILTER_BEST)
    ctx.paint()
    ctx.restore()
    return h


def _nombre_en_texto(ctx, nombre: str, y: float, p: _Paleta,
                     tamano: float = 32, centrado: bool = True,
                     x: float = MARGIN) -> float:
    """El nombre del taller como texto, con la ÚLTIMA palabra en el color
    de acento ("Taller *Pérez*"). Son dos layouts uno al lado del otro y
    no un markup con colores, para no tener que escapar un nombre que
    podría traer "&"."""
    partes = nombre.rsplit(" ", 1)
    primero = partes[0] + " " if len(partes) == 2 else ""
    ultimo = partes[-1]
    font = f"{FONT_TITLE} Bold {tamano}"
    l1 = _layout(ctx, primero, font)
    l2 = _layout(ctx, ultimo, font)
    w1, h1 = _medida(l1) if primero else (0.0, 0.0)
    w2, h2 = _medida(l2)
    if centrado:
        x = (PAGE_WIDTH - (w1 + w2)) / 2
    if primero:
        _mostrar(ctx, l1, x, y, p.texto)
    _mostrar(ctx, l2, x + w1, y, p.acento_texto)
    return max(h1, h2)


# ------------------------------------------------------------ Bloques --
def _encabezado(ctx, data: TicketData, shop: ShopProfile, p: _Paleta,
                logo) -> float:
    """Logo (o nombre), eslogan, ubicación y divisor. Devuelve la `y`
    donde termina."""
    y = MARGIN - 6
    if logo is None and not (shop.name or shop.slogan or shop.location):
        # Sin datos del taller: el título encabeza la hoja, y el divisor
        # va debajo de él y no flotando arriba de nada.
        y += 10
        y += _centrado(ctx, y, _("Ticket de Entrega").upper(),
                       f"{FONT_TITLE} Bold 15", p.texto, espaciado=3.0)
        y += 14
        _divisor(ctx, y, p)
        return y + 22
    if logo is not None:
        y += _dibujar_logo(ctx, logo, y, LOGO_MAX_WIDTH, LOGO_MAX_HEIGHT)
        y += 6
    elif shop.name:
        y += _nombre_en_texto(ctx, shop.name, y, p)
        y += 4
    # Con logo, el eslogan ya está adentro de la imagen (es lo que deja
    # `tools/prepare_logo.py`): repetirlo en texto lo imprimiría dos veces.
    if shop.slogan and logo is None:
        y += _centrado(ctx, y, shop.slogan.upper(), f"{FONT_TITLE} Medium 13.4",
                       p.eslogan, espaciado=4.0)
        y += 6
    if shop.location:
        y += _centrado(ctx, y, shop.location.upper(), f"{FONT_BODY} 9.2",
                       p.ubicacion, espaciado=2.6)
        y += 4
    y += 8
    _divisor(ctx, y, p)
    y += 14
    y += _centrado(ctx, y, _("Ticket de Entrega").upper(),
                   f"{FONT_TITLE} Bold 15", p.texto, espaciado=3.0)
    return y + 14


def _par(ctx, x, y, etiqueta, valor, ancho, p: _Paleta,
         tamano: float = 15, max_lineas: int = 2,
         color_etiqueta=None) -> float:
    """Etiqueta arriba, valor grande abajo. Devuelve la altura."""
    alto = _etiqueta(ctx, x, y, etiqueta, p, ancho=ancho,
                     color=color_etiqueta)
    alto += 3 * p.escala
    alto += _texto(ctx, x, y + alto, valor,
                   f"{FONT_BODY} Semi-Bold {_pt(tamano, p)}",
                   p.texto, ancho=ancho, max_lineas=max_lineas)
    return alto


def _entra(ctx, texto: str, font: str, ancho: float,
           espaciado: float = 0.0) -> bool:
    """True si `texto` entra en una línea de `ancho` sin que Pango lo
    corte. Se le pregunta a Pango y no se compara contra una medida: el
    espaciado de letras hace que corte un par de puntos antes de lo que
    dice el ancho medido."""
    return not _layout(ctx, texto, font, ancho=ancho, espaciado=espaciado,
                       max_lineas=1).is_ellipsized()


def _grilla(ctx, y, pares, columnas, p: _Paleta, tamano=15,
            separacion=12.0, de_seccion: bool = False) -> float:
    """`pares` (etiqueta, valor) repartidos en hasta `columnas` columnas,
    fila por fila. Cada fila mide lo que mide su celda más alta.

    Si con letra grande alguna etiqueta o algún valor no entra en el
    ancho de su columna, se usan la mitad de columnas (y así): una
    etiqueta cortada con "…" no se puede leer, y una fila más sí."""
    valor_font = f"{FONT_BODY} Semi-Bold {_pt(tamano, p)}"
    while columnas > 1:
        ancho_col = (CONTENT_WIDTH - (columnas - 1) * separacion) / columnas
        if all(_entra(ctx, e.upper(), _fuente_etiqueta(p), ancho_col,
                      _ESPACIADO_ETIQUETA)
               and _entra(ctx, v, valor_font, ancho_col)
               for e, v in pares if len(v) < 40):
            break
        columnas //= 2
    ancho_col = (CONTENT_WIDTH - (columnas - 1) * separacion) / columnas
    for i in range(0, len(pares), columnas):
        alto_fila = 0.0
        for j, (etiqueta, valor) in enumerate(pares[i:i + columnas]):
            x = MARGIN + j * (ancho_col + separacion)
            alto_fila = max(alto_fila, _par(
                ctx, x, y, etiqueta, valor, ancho_col, p, tamano,
                color_etiqueta=None if de_seccion else p.tenue))
        y += alto_fila + 9 * p.escala
    return y


def _cabecera_de_seccion(ctx, y, titulo, p: _Paleta, detalle: str = "") -> float:
    """Título de sección con una línea fina debajo y, opcionalmente, un
    dato chico a la derecha. Arriba lleva el `aire` que le toque."""
    y += 2 * p.escala + p.aire
    alto = _etiqueta(ctx, MARGIN, y, titulo, p)
    if detalle:
        layout = _layout(ctx, detalle, f"{FONT_BODY} {_pt(9.8, p)}",
                         ancho=CONTENT_WIDTH * 0.6, max_lineas=1,
                         alinear=Pango.Alignment.RIGHT)
        _mostrar(ctx, layout, PAGE_WIDTH - MARGIN - CONTENT_WIDTH * 0.6,
                 y - 0.5, p.tenue)
    y += alto + 4 * p.escala
    _divisor(ctx, y, p, centro=False)
    return y + 7 * p.escala


def _seccion_cliente(ctx, y, data: TicketData, p: _Paleta) -> float:
    pares = []
    if data.client_name:
        pares.append((_("Cliente"), format_client_name(data.client_name)))
    pares.append((_("Fecha de entrega"),
                  data.generated_at.strftime("%d/%m/%Y · %H:%M")))
    return _grilla(ctx, y + p.aire + p.arriba, pares, 2, p, tamano=18.3,
                   de_seccion=True)


def _seccion_consola(ctx, y, data: TicketData, p: _Paleta) -> float:
    c = data.console
    pares = [(etiqueta, valor) for etiqueta, valor in (
        (_("Modelo"), c.model),
        (_("Número de serie"), c.serial),
        (_("Versión del sistema"), c.system_version),
        # El número de serie y la versión van tal cual: "LU12ab" o "4.3u"
        # no son texto para embellecer, y cambiarles una letra es mentir.
        (_("Servicio realizado"), format_service(c.service)),
    ) if valor]
    if not pares:
        return y
    y = _cabecera_de_seccion(ctx, y, _("Consola"), p)
    return _grilla(ctx, y, pares, 4, p, tamano=14.6)


def _seccion_unidad(ctx, y, data: TicketData, p: _Paleta) -> float:
    ratio = data.used_ratio
    # El porcentaje va en la línea del título, al lado del nombre de la
    # unidad, y no en una línea propia debajo de la barra: es una fila
    # menos, que en una hoja llena es una fila más de juegos.
    detalle = data.drive_label
    if ratio is not None:
        detalle += " · " + _("{percent:.0f}% de la unidad ocupado").format(
            percent=ratio * 100)
    y = _cabecera_de_seccion(ctx, y, _("Unidad"), p, detalle=detalle)
    y = _grilla(ctx, y, [
        (_("Capacidad total"), format_size(data.total_bytes)),
        (_("Espacio usado"), format_size(data.used_bytes)),
        (_("Espacio libre"), format_size(data.free_bytes)),
        (_("Formato"), data.filesystem),
    ], 4, p, tamano=14.6)
    if ratio is not None:
        alto = 7.0 * p.escala
        _rect_redondeado(ctx, MARGIN, y, CONTENT_WIDTH, alto, alto / 2)
        ctx.set_source_rgb(*p.pista)
        ctx.fill()
        lleno = CONTENT_WIDTH * max(0.0, min(1.0, ratio))
        if lleno > 0:
            _rect_redondeado(ctx, MARGIN, y, max(lleno, alto), alto, alto / 2)
            ctx.set_source_rgb(*p.acento)
            ctx.fill()
        y += alto + 12 * p.escala
    return y


def _seccion_notas(ctx, y, data: TicketData, p: _Paleta) -> float:
    if not data.notes:
        return y
    y = _cabecera_de_seccion(ctx, y, _("Notas"), p)
    # Tope de líneas: las notas son texto libre, y unas notas larguísimas
    # no pueden empujar todo lo demás a otra hoja.
    y += _texto(ctx, MARGIN, y, data.notes, f"{FONT_BODY} {_pt(12.2, p)}",
                p.texto, ancho=CONTENT_WIDTH, max_lineas=6)
    return y + 9 * p.escala


# ------------------------------------------------- Lista de juegos --
@dataclass(frozen=True)
class _Fila:
    """Una fila de la lista: el encabezado de un grupo o un juego."""

    grupo: str                 # CONSOLE_WII / CONSOLE_GAMECUBE
    titulo: str = ""
    game_id: str = ""
    es_grupo: bool = False
    continuacion: bool = False


def _filas_de_juegos(data: TicketData) -> list:
    filas = []
    for consola in (CONSOLE_WII, CONSOLE_GAMECUBE):
        juegos = [g for g in data.games if g.console == consola]
        if not juegos:
            continue
        filas.append(_Fila(consola, es_grupo=True))
        filas.extend(_Fila(consola, g.title, g.game_id) for g in juegos)
    return filas


def _llenar(pendientes: list, capacidad: int, columnas: int) -> tuple:
    """Llena hasta `columnas` columnas de `capacidad` filas con las
    primeras de `pendientes`. Devuelve (columnas, lo que sobró)."""
    pendientes = list(pendientes)
    hoja = []
    for _k in range(columnas):
        if not pendientes:
            break
        columna = []
        if not pendientes[0].es_grupo:
            # Columna que arranca a mitad de un grupo (en la de al lado o
            # en la hoja siguiente): se repite el encabezado como
            # "(continuación)", sin el total, que ya está en el original.
            columna.append(_Fila(pendientes[0].grupo, es_grupo=True,
                                 continuacion=True))
        while pendientes and len(columna) < capacidad:
            if pendientes[0].es_grupo and len(columna) == capacidad - 1:
                break
            columna.append(pendientes.pop(0))
        hoja.append(columna)
    return hoja, pendientes


def paginar(filas: list, capacidades: list,
            columnas: int = LIST_COLUMNS) -> list:
    """Reparte `filas` en hojas de `columnas` columnas. `capacidades[i]`
    es cuántas filas entran por columna en la hoja i (la última se repite
    para las hojas que hagan falta). Devuelve una lista de hojas, cada una
    una lista de columnas.

    Reglas, para que la lista se lea bien partida:
    - una fila nunca se parte: cada una ocupa exactamente un lugar;
    - un encabezado de grupo nunca queda solo al pie de una columna: pasa
      a la siguiente junto con su primer juego;
    - una columna que arranca a mitad de un grupo repite el encabezado, así
      ninguna fila queda sin saber de qué consola es;
    - en la hoja donde termina la lista, las columnas se EQUILIBRAN (se usa
      la menor altura con la que todo entra): 15 juegos son dos columnas
      parejas y no una llena y otra con tres filas."""
    hojas = []
    pendientes = list(filas)
    while pendientes:
        n = len(hojas)
        capacidad = capacidades[min(n, len(capacidades) - 1)]
        if capacidad < 2:
            # Sin lugar para un encabezado y un juego, esta hoja no lleva
            # lista; si eso pasa con la capacidad que se repite, la lista
            # no terminaría nunca.
            if n >= len(capacidades) - 1:
                raise ValueError("capacidad de columna insuficiente")
            hojas.append([])
            continue
        hoja, resto = _llenar(pendientes, capacidad, columnas)
        if not resto:
            # Entra todo: buscar la menor capacidad con la que sigue
            # entrando, para que las columnas queden parejas.
            for menor in range(2, capacidad):
                prueba, sobra = _llenar(pendientes, menor, columnas)
                if not sobra:
                    hoja = prueba
                    break
        hojas.append(hoja)
        pendientes = resto
    return hojas


def _nombre_de_grupo(consola: str) -> str:
    return "Wii" if consola == CONSOLE_WII else "GameCube"


def _alto_fila(p: _Paleta) -> float:
    return ROW_HEIGHT * p.escala


def _dibujar_columna(ctx, columna: list, x: float, y: float, ancho: float,
                     data: TicketData, p: _Paleta) -> None:
    cantidad = {c: sum(1 for g in data.games if g.console == c)
                for c in (CONSOLE_WII, CONSOLE_GAMECUBE)}
    alto = _alto_fila(p)
    sangria = 10 * p.escala
    alterna = False
    for fila in columna:
        if fila.es_grupo:
            _rect_redondeado(ctx, x, y + 1, ancho, alto - 2, 3)
            ctx.set_source_rgb(*p.destacado)
            ctx.fill()
            ctx.set_source_rgb(*p.acento)
            ctx.rectangle(x, y + 1, 2.5, alto - 2)
            ctx.fill()
            nombre = _nombre_de_grupo(fila.grupo)
            ancho_cuenta = 0.0
            if fila.continuacion:
                nombre = _("{console} (continuación)").format(console=nombre)
            else:
                ancho_cuenta = 56 * p.escala
                cuenta = _layout(ctx, str(cantidad[fila.grupo]),
                                 f"{FONT_BODY} Semi-Bold {_pt(10.4, p)}",
                                 ancho=ancho_cuenta, max_lineas=1,
                                 alinear=Pango.Alignment.RIGHT)
                _mostrar(ctx, cuenta, x + ancho - ancho_cuenta - sangria,
                         y + (alto - _medida(cuenta)[1]) / 2, p.etiqueta)
            layout = _layout(ctx, nombre, f"{FONT_TITLE} Bold {_pt(11, p)}",
                             ancho=ancho - ancho_cuenta - 2 * sangria,
                             max_lineas=1)
            _mostrar(ctx, layout, x + sangria,
                     y + (alto - _medida(layout)[1]) / 2, p.texto)
            alterna = False
        else:
            if alterna:
                ctx.set_source_rgb(*p.fila_alterna)
                ctx.rectangle(x, y, ancho, alto)
                ctx.fill()
            alterna = not alterna
            ancho_id = 56.0 * p.escala
            titulo = _layout(ctx, fila.titulo, f"{FONT_BODY} {_pt(10.4, p)}",
                             ancho=ancho - ancho_id - 3 * sangria, max_lineas=1)
            _mostrar(ctx, titulo, x + sangria,
                     y + (alto - _medida(titulo)[1]) / 2, p.texto)
            if fila.game_id:
                gid = _layout(ctx, fila.game_id, f"{FONT_MONO} {_pt(9.2, p)}",
                              ancho=ancho_id, max_lineas=1,
                              alinear=Pango.Alignment.RIGHT)
                _mostrar(ctx, gid, x + ancho - ancho_id - sangria,
                         y + (alto - _medida(gid)[1]) / 2, p.tenue)
        y += alto


def _dibujar_lista(ctx, columnas: list, y: float, data: TicketData,
                   p: _Paleta, cantidad: int = LIST_COLUMNS) -> None:
    ancho = (CONTENT_WIDTH - (cantidad - 1) * LIST_GUTTER) / cantidad
    for j, columna in enumerate(columnas):
        x = MARGIN + j * (ancho + LIST_GUTTER)
        _dibujar_columna(ctx, columna, x, y, ancho, data, p)


# --------------------------------------------------------------- Pie --
QR_SIZE = 78.0
QR_PADDING = 7.0


def _alto_del_pie(shop: ShopProfile, completo: bool = True) -> float:
    """El pie con el WhatsApp va en la primera hoja, que es la que el
    cliente ve al abrir el PDF; las demás llevan un pie de una línea y le
    dejan ese lugar a la lista."""
    return 112.0 if shop.whatsapp and completo else 30.0


def _dibujar_qr(ctx, matriz: list, x: float, y: float, lado: float) -> None:
    """Tarjeta blanca con esquinas redondeadas y el QR encima, en
    vectores. Las corridas horizontales de módulos se juntan en un solo
    rectángulo y cada uno se estira un pelo hacia abajo: sin eso, algunos
    visores dejan líneas finas entre filas al antialiasear, y un lector
    puede confundirlas con módulos claros."""
    _rect_redondeado(ctx, x, y, lado, lado, 8)
    ctx.set_source_rgb(1, 1, 1)
    ctx.fill()
    n = len(matriz)
    modulo = (lado - 2 * QR_PADDING) / n
    ox, oy = x + QR_PADDING, y + QR_PADDING
    ctx.set_source_rgb(0, 0, 0)
    for r, fila in enumerate(matriz):
        c = 0
        while c < n:
            if not fila[c]:
                c += 1
                continue
            inicio = c
            while c < n and fila[c]:
                c += 1
            ctx.rectangle(ox + inicio * modulo, oy + r * modulo,
                          (c - inicio) * modulo, modulo * 1.04)
    ctx.fill()


def _pie(ctx, data: TicketData, shop: ShopProfile, p: _Paleta, pagina: int,
         total: int, qr) -> None:
    base = PAGE_HEIGHT - MARGIN + 8
    if shop.whatsapp and pagina == 1:
        arriba = PAGE_HEIGHT - MARGIN - _alto_del_pie(shop) + 18
        _divisor(ctx, arriba - 10, p)
        ancho_texto = CONTENT_WIDTH - (QR_SIZE + 16 if qr else 0)
        y = arriba + 6
        y += _etiqueta(ctx, MARGIN, y, _("Escríbenos por WhatsApp"), p,
                       ancho=ancho_texto)
        y += 2
        y += _texto(ctx, MARGIN, y, format_whatsapp(shop.whatsapp),
                    f"{FONT_TITLE} Bold 26", p.texto, ancho=ancho_texto,
                    max_lineas=1)
        if qr:
            _texto(ctx, MARGIN, y + 2,
                   _("Escaneá el código para abrir el chat."),
                   f"{FONT_BODY} 9.8", p.tenue, ancho=ancho_texto, max_lineas=1)
            _dibujar_qr(ctx, qr, PAGE_WIDTH - MARGIN - QR_SIZE, arriba,
                        QR_SIZE)
    else:
        _divisor(ctx, base - 10, p, centro=False)
    _texto(ctx, MARGIN, base, _("Generado por WiiBackup Manager"),
           f"{FONT_BODY} 8.6", p.tenue)
    if total > 1:
        layout = _layout(ctx, _("Página {page} de {total}")
                         .format(page=pagina, total=total),
                         f"{FONT_BODY} 8.6", ancho=150,
                         alinear=Pango.Alignment.RIGHT)
        _mostrar(ctx, layout, PAGE_WIDTH - MARGIN - 150, base, p.tenue)


def _encabezado_continuacion(ctx, data: TicketData, shop: ShopProfile,
                             p: _Paleta, logo) -> float:
    """El encabezado de las hojas 2 en adelante: la marca en chico, de
    quién es el ticket y de qué fecha, para que una hoja suelta se pueda
    reconocer."""
    y = MARGIN - 6
    if logo is not None:
        alto = _dibujar_logo(ctx, logo, y, 200, 46, centrado=False)
    elif shop.name:
        alto = _nombre_en_texto(ctx, shop.name, y, p, tamano=18,
                                centrado=False)
    else:
        alto = _texto(ctx, MARGIN, y, _("Ticket de Entrega"),
                      f"{FONT_TITLE} Bold 16", p.texto)
    partes = [format_client_name(data.client_name)] if data.client_name else []
    partes.append(data.generated_at.strftime("%d/%m/%Y"))
    detalle = _layout(ctx, " · ".join(partes), f"{FONT_BODY} 11",
                      ancho=220, max_lineas=1, alinear=Pango.Alignment.RIGHT)
    _mostrar(ctx, detalle, PAGE_WIDTH - MARGIN - 220,
             y + (alto - _medida(detalle)[1]) / 2, p.tenue)
    y += alto + 10
    _divisor(ctx, y, p)
    return y + 16


def _seccion_juegos_cabecera(ctx, y, data: TicketData, p: _Paleta,
                             continuacion: bool = False) -> float:
    titulo = _("Juegos") if not continuacion else _("Juegos (continuación)")
    c = data.contents
    detalle = _("{wii} Wii · {gc} GameCube · {hb} Homebrew").format(
        wii=c.wii_games, gc=c.gamecube_games, hb=c.homebrew_apps)
    return _cabecera_de_seccion(ctx, y, titulo, p, detalle=detalle)


# ------------------------------------------------------------- Hoja --
def render_ticket(data: TicketData, dest: Path,
                  shop: Optional[ShopProfile] = None) -> Path:
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
    shop = shop or ShopProfile()
    with atomicfs.atomic_write_target(dest, mkparents=True) as tmp:
        surface = cairo.PDFSurface(str(tmp), PAGE_WIDTH, PAGE_HEIGHT)
        try:
            surface.set_metadata(cairo.PDFMetadata.TITLE, _("Ticket de Entrega"))
            surface.set_metadata(cairo.PDFMetadata.CREATOR, "WiiBackup Manager")
            _dibujar(cairo.Context(surface), data, shop)
        finally:
            # `finish()` es lo que vuelca el PDF al archivo. Va en un
            # `finally` para que un error a mitad del dibujo no deje el
            # surface abierto: el `atomic_write_target` de afuera se
            # encarga de que ese archivo incompleto no llegue a `dest`.
            surface.finish()
    return dest


def _secciones(ctx, data: TicketData, shop: ShopProfile, base: _Paleta,
               p: _Paleta, logo) -> float:
    """Encabezado y secciones de la primera hoja; devuelve dónde arranca
    la lista. El encabezado va siempre en `base` (tamaño fijo: logo y
    título no se agrandan); el resto, en `p`."""
    y = _encabezado(ctx, data, shop, base, logo)
    y = _seccion_cliente(ctx, y, data, p)
    y = _seccion_consola(ctx, y, data, p)
    y = _seccion_unidad(ctx, y, data, p)
    y = _seccion_notas(ctx, y, data, p)
    return _seccion_juegos_cabecera(ctx, y, data, p)


def _huecos(data: TicketData) -> int:
    """Cuántos lugares reciben `aire`: el bloque del cliente y cada
    cabecera de sección que se dibuja."""
    return (3 + (0 if data.console.is_empty() else 1)
            + (1 if data.notes else 0))


def _mensaje_sin_juegos(ctx, y, p: _Paleta) -> float:
    return _texto(ctx, MARGIN, y + 2,
                  _("No se encontraron juegos de Wii ni de GameCube en la unidad."),
                  f"{FONT_BODY} {_pt(12.2, p)}", p.tenue, ancho=CONTENT_WIDTH) + 2


def _alto_lista(ctx, filas: list, disponible: float, p: _Paleta,
                columnas: int) -> Optional[float]:
    """Lo que ocupa la lista si entra ENTERA en `disponible`, o None."""
    if not filas:
        alto = _mensaje_sin_juegos(ctx, 0, p)
        return alto if alto <= disponible else None
    capacidad = int(disponible // _alto_fila(p))
    if capacidad < 2:
        return None
    hojas = paginar(filas, [capacidad], columnas)
    if len(hojas) != 1:
        return None
    return max(len(c) for c in hojas[0]) * _alto_fila(p)


def _preparar(data: TicketData, shop: ShopProfile, base: _Paleta, logo,
              filas: list, fin_de_lista: float) -> tuple:
    """Elige escala, columnas y aire (ver POCOS_JUEGOS). Se mide
    dibujando en una superficie descartable, que es la única forma de
    saber el alto real con la fuente que de verdad está instalada.
    Devuelve (estilo, columnas)."""
    borrador = cairo.Context(cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA,
                                                    None))
    candidatos = (_ESCALAS_POCOS if len(data.games) < POCOS_JUEGOS else ())
    candidatos += ((1.0, LIST_COLUMNS), (_ESCALA_DENSA, LIST_COLUMNS))
    for escala, columnas in candidatos:
        p = replace(base, escala=escala)
        y = _secciones(borrador, data, shop, base, p, logo)
        alto = _alto_lista(borrador, filas, fin_de_lista - y, p, columnas)
        if alto is None:
            continue
        # Entra en una hoja: lo que sobra se reparte entre las secciones.
        # Se deja un punto de margen para que el redondeo no haga que la
        # última fila deje de entrar.
        sobra = max(fin_de_lista - y - alto - 1, 0.0)
        aire = min(sobra / _huecos(data), AIRE_MAX)
        arriba = (sobra - aire * _huecos(data)) / 2
        return replace(p, aire=aire, arriba=arriba), columnas
    # No entra en una hoja: tamaño compacto y la lista sigue en otras.
    return base, LIST_COLUMNS


def _dibujar(ctx, data: TicketData, shop: ShopProfile) -> None:
    """Todas las hojas, de arriba hacia abajo.

    `y` va bajando a medida que se dibuja y cada bloque devuelve lo que
    ocupó, porque casi todo es opcional (cliente, consola, notas) y no
    puede quedar un hueco donde falta algo. La lista de juegos va al
    final: es lo único que puede no entrar, y así lo que no cabe sigue en
    la hoja siguiente sin mover nada de lo de arriba."""
    base = _paleta(shop.theme, shop.accent)
    # El logo es para fondo oscuro; en el modo claro el nombre va en texto.
    logo = (_cargar_logo(shop.logo_path)
            if shop.theme != config.TICKET_THEME_LIGHT else None)
    qr = None
    if shop.whatsapp:
        mensaje = _("Hola, tengo una consulta sobre mi entrega del {date}") \
            .format(date=data.generated_at.strftime("%d/%m/%Y"))
        qr = qr_matrix(whatsapp_url(shop.whatsapp, mensaje))

    fin_de_lista = PAGE_HEIGHT - MARGIN - _alto_del_pie(shop) - 4
    filas = _filas_de_juegos(data)
    p, columnas = _preparar(data, shop, base, logo, filas, fin_de_lista)

    _fondo(ctx, base)
    y = _secciones(ctx, data, shop, base, p, logo)
    if not filas:
        _mensaje_sin_juegos(ctx, y, p)
        _pie(ctx, data, shop, base, 1, 1, qr)
        return

    # Cuánto entra en cada hoja: la primera, lo que quedó libre debajo de
    # las secciones; las siguientes, todo menos el encabezado chico.
    borrador = cairo.Context(cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA,
                                                    None))
    y_cont = _encabezado_continuacion(borrador, data, shop, base, logo)
    y_cont = _seccion_juegos_cabecera(borrador, y_cont, data, p, True)
    fin_siguientes = PAGE_HEIGHT - MARGIN - _alto_del_pie(shop, False) - 4
    primera = int((fin_de_lista - y) // _alto_fila(p))
    siguientes = int((fin_siguientes - y_cont) // _alto_fila(p))
    hojas = paginar(filas, [primera, siguientes], columnas)

    for n, hoja in enumerate(hojas, start=1):
        if n > 1:
            ctx.show_page()
            _fondo(ctx, base)
            y = _encabezado_continuacion(ctx, data, shop, base, logo)
            y = _seccion_juegos_cabecera(ctx, y, data, p, True)
        _dibujar_lista(ctx, hoja, y, data, p, columnas)
        _pie(ctx, data, shop, base, n, len(hojas), qr)
