"""Pruebas del Ticket de Entrega con la marca del taller: la lista de
juegos que se lee de la unidad, los datos del taller en la configuración
y el PDF en sus dos modos.

Todos los datos son ficticios -taller, cliente, número de WhatsApp-. El
texto del PDF se lee con `pdftotext` (poppler), que es lo más parecido a
"lo que ve el cliente" que se puede comprobar sin mirar la hoja; si no
está instalado, esas pruebas se saltean en vez de sumar una dependencia.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path

import cairo
import pytest

from wiibackup_manager import config, gametdb, pdf_export, ticket_service
from wiibackup_manager.ticket_service import ConsoleInfo, GameEntry

NUMERO_FICTICIO = "50400001111"

requiere_poppler = pytest.mark.skipif(
    shutil.which("pdftotext") is None or shutil.which("pdfinfo") is None,
    reason="poppler-utils no está instalado")


# ------------------------------------------------------------- Helpers --
def _archivo(path: Path, contenido: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contenido)
    return path


def _texto(pdf: Path, pagina: int = None) -> str:
    args = ["pdftotext"]
    if pagina is not None:
        args += ["-f", str(pagina), "-l", str(pagina)]
    r = subprocess.run(args + [str(pdf), "-"], capture_output=True,
                       text=True, timeout=30, check=True)
    return r.stdout


def _compacto(texto: str) -> str:
    """Sin espacios: las etiquetas van con letras espaciadas y
    `pdftotext` las devuelve como "M O D E L O"."""
    return "".join(texto.split())


def _paginas(pdf: Path) -> int:
    r = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True,
                       timeout=30, check=True)
    return int(re.search(r"^Pages:\s+(\d+)", r.stdout, re.MULTILINE).group(1))


def _juegos(wii: int, gamecube: int = 0) -> tuple:
    juegos = [GameEntry("wii", f"W{i:05d}", f"Juego de Wii {i:03d}")
              for i in range(wii)]
    juegos += [GameEntry("gc", f"G{i:05d}", f"Juego de GameCube {i:03d}")
               for i in range(gamecube)]
    return tuple(juegos)


def _datos(**kw):
    base = dict(
        client_name="Cliente Ficticio", notes="Incluye 2 controles",
        generated_at=datetime(2026, 1, 2, 10, 30),
        drive_label="USB_PRUEBA", drive_path=Path("/run/media/x/USB"),
        total_bytes=64 * 1024 ** 3, used_bytes=40 * 1024 ** 3,
        free_bytes=24 * 1024 ** 3, filesystem="FAT32",
        contents=ticket_service.DriveContents(0, 0, 2),
        console=ConsoleInfo("Modelo Ficticio X1", "SERIE-0001", "9.9Z",
                            "Servicio de prueba"),
        games=(),
    )
    base.update(kw)
    juegos = base["games"]
    if "contents" not in kw:
        base["contents"] = ticket_service.DriveContents(
            sum(1 for g in juegos if g.console == "wii"),
            sum(1 for g in juegos if g.console == "gc"), 2)
    return ticket_service.TicketData(**base)


def _taller(**kw):
    base = dict(name="Taller Ficticio", slogan="Eslogan de prueba",
                location="Ciudad Ficticia", whatsapp=NUMERO_FICTICIO)
    base.update(kw)
    return pdf_export.ShopProfile(**base)


@pytest.fixture
def sin_qr(monkeypatch):
    """Como si no hubiera ninguna librería de QR instalada: un `None` en
    `sys.modules` hace que el `import` levante ImportError."""
    monkeypatch.setitem(sys.modules, "segno", None)
    monkeypatch.setitem(sys.modules, "qrcode", None)


@pytest.fixture
def qr_falso(monkeypatch):
    """Un `segno` de mentira, para probar el dibujo del QR sin depender de
    que la librería esté instalada en la máquina que corre la suite."""
    llamadas = []

    def make_qr(texto, error="m"):
        llamadas.append(texto)
        matriz = [bytearray((x + y) % 2 for x in range(21)) for y in range(21)]
        return types.SimpleNamespace(matrix=tuple(matriz))

    monkeypatch.setitem(sys.modules, "segno",
                        types.SimpleNamespace(make_qr=make_qr))
    return llamadas


# ================================================ Lista de juegos Wii --
def test_lista_los_juegos_wii_con_el_id_del_nombre_del_archivo(tmp_path):
    raiz = tmp_path / "usb"
    _archivo(raiz / "wbfs" / "RMCP01" / "RMCP01.wbfs")
    _archivo(raiz / "wbfs" / "Wii Sports [RSPE01]" / "RSPE01.wbfs")
    juegos = ticket_service.list_wii_games(raiz)
    assert {(g.game_id, g.title) for g in juegos} == {
        ("RMCP01", "RMCP01"), ("RSPE01", "Wii Sports")}
    assert all(g.console == "wii" for g in juegos)


def test_el_titulo_sale_primero_de_gametdb(tmp_path):
    raiz = tmp_path / "usb"
    _archivo(raiz / "wbfs" / "RMCP01" / "RMCP01.wbfs")
    _archivo(raiz / "wbfs" / "Carpeta [RSPE01]" / "RSPE01.wbfs")
    titulos = {"RMCP01": "Mario Kart Wii"}
    juegos = ticket_service.list_wii_games(raiz, titulos.get)
    assert [g.title for g in juegos] == ["Carpeta", "Mario Kart Wii"]


def test_una_busqueda_de_titulo_que_falla_cae_a_la_carpeta(tmp_path):
    raiz = tmp_path / "usb"
    _archivo(raiz / "wbfs" / "Juego [RSPE01]" / "RSPE01.wbfs")

    def rota(_gid):
        raise RuntimeError("caché ilegible")

    assert ticket_service.list_wii_games(raiz, rota)[0].title == "Juego"


def test_los_juegos_wii_van_en_orden_alfabetico_sin_mirar_acentos(tmp_path):
    raiz = tmp_path / "usb"
    for gid in ("AAAA01", "BBBB01", "CCCC01"):
        _archivo(raiz / "wbfs" / gid / f"{gid}.wbfs")
    titulos = {"AAAA01": "zelda", "BBBB01": "Épica", "CCCC01": "Avión"}
    juegos = ticket_service.list_wii_games(raiz, titulos.get)
    assert [g.title for g in juegos] == ["Avión", "Épica", "zelda"]


def test_un_wbfs_a_medio_copiar_no_se_lista(tmp_path):
    """Un temporal de escritura de `atomicfs` es `.{nombre}.parcial-xxx`:
    oculto, y no es un juego entregado."""
    raiz = tmp_path / "usb"
    _archivo(raiz / "wbfs" / "RMCP01" / "RMCP01.wbfs")
    _archivo(raiz / "wbfs" / "RSPE01" / ".RSPE01.wbfs.parcial-ab12cd")
    assert [g.game_id for g in ticket_service.list_wii_games(raiz)] == ["RMCP01"]
    assert ticket_service.count_wii_games(raiz) == 1


# =========================================== Lista de juegos GameCube --
def test_lista_las_carpetas_de_gamecube_con_su_id(tmp_path):
    raiz = tmp_path / "usb"
    _archivo(raiz / "games" / "Metroid Prime [GM8E01]" / "game.iso")
    _archivo(raiz / "games" / "F-Zero GX [GFZE01]" / "game.ciso")
    juegos = ticket_service.list_gamecube_games(raiz)
    assert [(g.game_id, g.title) for g in juegos] == [
        ("GFZE01", "F-Zero GX"), ("GM8E01", "Metroid Prime")]
    assert all(g.console == "gc" for g in juegos)


def test_gamecube_ignora_parciales_respaldos_y_restos(tmp_path):
    """Solo cuenta una carpeta con `game.iso`/`game.ciso` TERMINADO. Una
    copia a medias (`.game.iso.parcial-*`), un respaldo huérfano, una
    carpeta con solo el segundo disco o una vacía no son juegos."""
    raiz = tmp_path / "usb"
    juegos = raiz / "games"
    _archivo(juegos / "Bueno [GOOD01]" / "game.iso")
    _archivo(juegos / "Bueno [GOOD01]" / "disc2.iso")
    _archivo(juegos / "Copiando [PART01]" / ".game.iso.parcial-x1y2z3")
    _archivo(juegos / "Respaldo [BACK01]" / ".game.iso.respaldo-1234")
    _archivo(juegos / "Solo disco 2 [DSC201]" / "disc2.iso")
    (juegos / "Vacia [EMPT01]").mkdir(parents=True)
    _archivo(juegos / ".Oculta [HIDE01]" / "game.iso")
    lista = ticket_service.list_gamecube_games(raiz)
    assert [g.game_id for g in lista] == ["GOOD01"]
    assert ticket_service.count_gamecube_games(raiz) == 1


def test_el_conteo_y_la_lista_del_ticket_coinciden(tmp_path):
    raiz = tmp_path / "usb"
    _archivo(raiz / "wbfs" / "RMCP01" / "RMCP01.wbfs")
    _archivo(raiz / "wbfs" / "RSPE01.wbfs")
    _archivo(raiz / "games" / "Metroid Prime [GM8E01]" / "game.iso")
    _archivo(raiz / "games" / "Copiando [PART01]" / ".game.iso.parcial-1")
    datos = ticket_service.collect_ticket_data(
        raiz, usage=lambda _p: shutil._ntuple_diskusage(100, 50, 50),
        filesystem=lambda _p: "vfat", title_lookup=lambda _g: None)
    assert datos.contents.wii_games == 2
    assert datos.contents.gamecube_games == 1
    assert [g.console for g in datos.games] == ["wii", "wii", "gc"]


def test_los_datos_de_consola_se_limpian(tmp_path):
    datos = ticket_service.collect_ticket_data(
        tmp_path, console=ConsoleInfo("  Wii  ", "\tS1 ", "", " "),
        usage=lambda _p: shutil._ntuple_diskusage(100, 50, 50),
        filesystem=lambda _p: "vfat", title_lookup=lambda _g: None)
    assert datos.console == ConsoleInfo("Wii", "S1", "", "")


def test_sin_datos_de_consola_queda_vacio(tmp_path):
    datos = ticket_service.collect_ticket_data(
        tmp_path, usage=lambda _p: shutil._ntuple_diskusage(100, 50, 50),
        filesystem=lambda _p: "vfat", title_lookup=lambda _g: None)
    assert datos.console.is_empty()
    assert datos.games == ()


# ====================================================== GameTDB caché --
_WIITDB_MINIMO = """<?xml version="1.0" encoding="UTF-8"?>
<datafile>
  <game name="x"><id>RMCP01</id>
    <locale lang="EN"><title>Mario Kart Wii</title></locale>
    <locale lang="ES"><title>Mario Kart Wii (ES)</title></locale>
  </game>
  <game name="y"><id>GM8E01</id>
    <locale lang="EN"><title>Metroid Prime</title></locale>
  </game>
</datafile>
"""


@pytest.fixture
def wiitdb_en_cache(monkeypatch):
    """Un wiitdb.xml mínimo en la caché aislada de las pruebas, con el
    índice en memoria vacío antes y después."""
    monkeypatch.setattr(gametdb, "_wiitdb_index", None)

    def sin_red(*_a, **_k):
        raise AssertionError("cached_title no puede descargar nada")

    monkeypatch.setattr(gametdb.urllib.request, "urlopen", sin_red)
    path = gametdb.wiitdb_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_WIITDB_MINIMO, encoding="utf-8")
    yield path
    path.unlink(missing_ok=True)


def test_el_titulo_sale_de_la_cache_de_gametdb(wiitdb_en_cache):
    assert gametdb.cached_title("RMCP01") == "Mario Kart Wii"
    assert gametdb.cached_title("rmcp01", "ES") == "Mario Kart Wii (ES)"
    # Sin título en ese idioma, cae al inglés.
    assert gametdb.cached_title("GM8E01", "ES") == "Metroid Prime"
    assert gametdb.cached_title("ZZZZ99") is None
    assert gametdb.cached_title("no-es-un-id") is None


def test_sin_cache_de_gametdb_no_hay_titulo_ni_descarga(monkeypatch):
    monkeypatch.setattr(gametdb, "_wiitdb_index", None)

    def sin_red(*_a, **_k):
        raise AssertionError("cached_title no puede descargar nada")

    monkeypatch.setattr(gametdb.urllib.request, "urlopen", sin_red)
    gametdb.wiitdb_cache_path().unlink(missing_ok=True)
    assert gametdb.cached_title("RMCP01") is None


def test_el_ticket_usa_la_cache_de_gametdb_por_defecto(tmp_path, wiitdb_en_cache):
    raiz = tmp_path / "usb"
    _archivo(raiz / "wbfs" / "RMCP01" / "RMCP01.wbfs")
    datos = ticket_service.collect_ticket_data(
        raiz, usage=lambda _p: shutil._ntuple_diskusage(100, 50, 50),
        filesystem=lambda _p: "vfat")
    assert datos.games[0].title == "Mario Kart Wii"


# ================================================ Datos del taller --
def test_los_datos_del_taller_arrancan_vacios_y_en_modo_oscuro():
    s = config.Settings()
    assert (s.shop_name, s.shop_slogan, s.shop_location, s.shop_whatsapp,
            s.shop_logo_path) == ("", "", "", "", "")
    assert s.shop_accent_color == config.DEFAULT_ACCENT_COLOR
    assert s.ticket_theme == config.TICKET_THEME_DARK


def test_los_datos_del_taller_se_guardan_en_la_configuracion_del_usuario():
    s = config.Settings()
    s.shop_name = "Taller Ficticio"
    s.shop_whatsapp = NUMERO_FICTICIO
    s.ticket_theme = config.TICKET_THEME_LIGHT
    s.save()
    try:
        leida = config.Settings.load()
        assert leida.shop_name == "Taller Ficticio"
        assert leida.shop_whatsapp == NUMERO_FICTICIO
        assert leida.ticket_theme == config.TICKET_THEME_LIGHT
    finally:
        config.CONFIG_FILE.unlink(missing_ok=True)


def test_valores_invalidos_del_taller_caen_al_defecto():
    import json
    config.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_FILE.write_text(json.dumps({
        "shop_accent_color": "violeta", "ticket_theme": "neon",
        "shop_whatsapp": "+504 0000-1111", "shop_name": "Taller Ficticio",
    }))
    try:
        s = config.Settings.load()
        assert s.shop_accent_color == config.DEFAULT_ACCENT_COLOR
        assert s.ticket_theme == config.TICKET_THEME_DARK
        assert s.shop_whatsapp == NUMERO_FICTICIO
        assert s.shop_name == "Taller Ficticio"
    finally:
        config.CONFIG_FILE.unlink(missing_ok=True)


def test_el_perfil_del_taller_sale_de_los_ajustes():
    s = config.Settings(shop_name="  Taller Ficticio ",
                        shop_whatsapp="+504 0000 1111",
                        shop_accent_color="#123456", ticket_theme="light")
    taller = pdf_export.ShopProfile.from_settings(s)
    assert taller.name == "Taller Ficticio"
    assert taller.whatsapp == NUMERO_FICTICIO
    assert taller.accent == "#123456"
    assert taller.theme == "light"


def test_el_codigo_del_ticket_no_trae_ninguna_marca_fija():
    """La app la usan talleres distintos: la marca sale de Ajustes."""
    raiz = Path(__file__).resolve().parent.parent / "wiibackup_manager"
    for nombre in ("pdf_export.py", "ticket_service.py",
                   "widgets/ticket_dialog.py"):
        assert "gamefix" not in (raiz / nombre).read_text().lower(), nombre


@pytest.mark.parametrize("digitos,esperado", [
    (NUMERO_FICTICIO, "+504 0000 1111"),
    ("12345", "+1 2345"),
    ("1234", "+1234"),
    ("", ""),
])
def test_el_numero_se_muestra_agrupado(digitos, esperado):
    assert pdf_export.format_whatsapp(digitos) == esperado


def test_el_enlace_de_whatsapp_lleva_el_mensaje_codificado():
    url = pdf_export.whatsapp_url("+504 0000-1111", "Hola, 02/01/2026 & más")
    assert url.startswith(f"https://wa.me/{NUMERO_FICTICIO}?text=")
    assert " " not in url and "&" not in url.split("?", 1)[1]


# ======================================================= Paginación --
def _filas(wii, gc=0):
    return pdf_export._filas_de_juegos(_datos(games=_juegos(wii, gc)))


def _juegos_en(hojas):
    return [f for hoja in hojas for col in hoja for f in col if not f.es_grupo]


@pytest.mark.parametrize("wii,gc", [(1, 0), (15, 0), (11, 4), (60, 20), (0, 7)])
def test_la_paginacion_no_pierde_ni_repite_juegos(wii, gc):
    filas = _filas(wii, gc)
    hojas = pdf_export.paginar(filas, [12, 30])
    juegos = _juegos_en(hojas)
    assert [f.game_id for f in juegos] == [f.game_id for f in filas
                                           if not f.es_grupo]
    for hoja in hojas:
        assert len(hoja) <= pdf_export.LIST_COLUMNS
        for i, col in enumerate(hoja):
            assert len(col) <= (12 if hoja is hojas[0] else 30)
            # Nunca un encabezado solo al pie de una columna.
            assert not col[-1].es_grupo
            # Toda columna arranca diciendo de qué consola es.
            assert col[0].es_grupo


def test_las_columnas_de_la_ultima_hoja_quedan_parejas():
    hojas = pdf_export.paginar(_filas(15), [40, 40])
    assert len(hojas) == 1
    izquierda, derecha = hojas[0]
    assert abs(len(izquierda) - len(derecha)) <= 1


def test_todo_encabezado_repetido_es_continuacion():
    """Tanto en la columna de al lado como en la hoja siguiente: el que
    abre el grupo es el original; los que se repiten dicen
    "(continuación)"."""
    hojas = pdf_export.paginar(_filas(60), [10, 20])
    columnas = [col for hoja in hojas for col in hoja]
    assert not columnas[0][0].continuacion
    for col in columnas[1:]:
        if col[0].grupo == "wii":
            assert col[0].continuacion


def test_una_primera_hoja_sin_lugar_pasa_la_lista_a_la_siguiente():
    hojas = pdf_export.paginar(_filas(3), [1, 20])
    assert hojas[0] == []
    assert len(_juegos_en(hojas)) == 3


# ============================================================= PDF --
@requiere_poppler
@pytest.mark.parametrize("modo", ["dark", "light"])
def test_el_pdf_trae_todos_los_datos_en_los_dos_modos(tmp_path, modo, qr_falso):
    datos = _datos(games=_juegos(3, 2))
    pdf = pdf_export.render_ticket(datos, tmp_path / f"{modo}.pdf",
                                   _taller(theme=modo))
    texto = _texto(pdf)
    compacto = _compacto(texto).upper()
    assert _paginas(pdf) == 1
    assert "Cliente Ficticio" in texto
    for valor in ("Modelo Ficticio X1", "SERIE-0001", "9.9Z",
                  "Servicio de prueba"):
        assert valor in texto
    for etiqueta in ("MODELO", "NÚMERODESERIE", "VERSIÓNDELSISTEMA",
                     "SERVICIOREALIZADO", "CLIENTE", "CONSOLA", "JUEGOS",
                     "UNIDAD"):
        assert etiqueta in compacto
    for juego in datos.games:
        assert juego.title in texto
        assert juego.game_id in texto
    assert "+504 0000 1111" in texto
    assert "Taller" in texto and "Ficticio" in texto
    assert "Generado por WiiBackup Manager" in texto
    # El QR apunta al chat, con el mensaje prellenado.
    assert qr_falso and qr_falso[0].startswith(f"https://wa.me/{NUMERO_FICTICIO}")


@requiere_poppler
def test_sin_juegos_el_ticket_lo_dice(tmp_path):
    pdf = pdf_export.render_ticket(_datos(games=()), tmp_path / "t.pdf",
                                   _taller())
    assert _paginas(pdf) == 1
    assert "No se encontraron juegos" in _texto(pdf)


@requiere_poppler
def test_quince_juegos_entran_en_una_hoja(tmp_path):
    datos = _datos(games=_juegos(11, 4))
    pdf = pdf_export.render_ticket(datos, tmp_path / "t.pdf", _taller())
    assert _paginas(pdf) == 1
    texto = _texto(pdf)
    assert all(g.title in texto for g in datos.games)


@requiere_poppler
def test_sesenta_y_pico_juegos_siguen_en_otra_hoja(tmp_path):
    datos = _datos(games=_juegos(48, 17))
    pdf = pdf_export.render_ticket(datos, tmp_path / "t.pdf", _taller())
    paginas = _paginas(pdf)
    assert paginas >= 2
    textos = [_texto(pdf, n) for n in range(1, paginas + 1)]
    for juego in datos.games:
        # Cada juego aparece en exactamente una hoja, entero.
        assert sum(t.count(juego.title) for t in textos) == 1, juego.title
    for n, texto in enumerate(textos[1:], start=2):
        # Encabezado repetido: de quién es, de qué fecha, y que sigue.
        assert "Cliente Ficticio" in texto
        assert "02/01/2026" in texto
        assert "CONTINUACIÓN" in _compacto(texto).upper()
        assert f"Página {n} de {paginas}" in texto
        # El pie con el WhatsApp va en la primera hoja; las demás llevan
        # uno de una línea y le dejan ese lugar a la lista.
        assert "+504 0000 1111" not in texto
    assert "+504 0000 1111" in textos[0]


@requiere_poppler
def test_nombres_con_caracteres_especiales_y_largos(tmp_path):
    largo = "Un título larguísimo " * 8
    juegos = (GameEntry("wii", "AMPE01", "Tom & Jerry <Edición> «Ñandú»"),
              GameEntry("wii", "LARG01", largo.strip()))
    datos = _datos(client_name="José & <Asociados> Ñúñez", games=juegos,
                   notes="Notas con <etiquetas> & acentos: áéíóú")
    pdf = pdf_export.render_ticket(datos, tmp_path / "t.pdf",
                                   _taller(name="Taller & <Hijos>"))
    texto = _texto(pdf)
    assert "Tom & Jerry <Edición> «Ñandú»" in texto
    assert "José & <Asociados> Ñúñez" in texto
    assert "<etiquetas> & acentos: áéíóú" in texto
    assert "Taller &" in texto and "<Hijos>" in texto
    # El título largo se corta con "…" para no salirse de su columna. Eso
    # se mide por posición y no por texto: cairo asocia el "…" al texto
    # que reemplaza, así que `pdftotext` devuelve el título entero (y
    # copiarlo del PDF también da el nombre completo, que está bien).
    cajas = subprocess.run(["pdftotext", "-bbox", str(pdf), "-"],
                           capture_output=True, text=True, timeout=30,
                           check=True).stdout
    palabras = re.findall(r'xMin="([\d.]+)" yMin="[\d.]+" xMax="([\d.]+)"'
                          r'[^>]*>([^<]*)</word>', cajas)
    inicio_id = next(float(x0) for x0, _x1, w in palabras if w == "LARG01")
    del_titulo = [float(x1) for _x0, x1, w in palabras if "larguísimo" in w]
    assert del_titulo
    assert max(del_titulo) < inicio_id
    assert inicio_id < pdf_export.PAGE_WIDTH - pdf_export.MARGIN


@requiere_poppler
def test_campos_de_consola_vacios_no_imprimen_su_etiqueta(tmp_path):
    datos = _datos(console=ConsoleInfo(model="Solo Modelo"))
    compacto = _compacto(_texto(pdf_export.render_ticket(
        datos, tmp_path / "t.pdf", _taller()))).upper()
    assert "MODELO" in compacto
    assert "NÚMERODESERIE" not in compacto
    assert "VERSIÓNDELSISTEMA" not in compacto
    assert "SERVICIOREALIZADO" not in compacto


@requiere_poppler
def test_sin_ningun_dato_de_consola_no_hay_seccion(tmp_path):
    datos = _datos(console=ConsoleInfo())
    compacto = _compacto(_texto(pdf_export.render_ticket(
        datos, tmp_path / "t.pdf", _taller()))).upper()
    assert "CONSOLA" not in compacto
    assert "MODELO" not in compacto


@requiere_poppler
def test_sin_libreria_de_qr_el_ticket_sale_con_el_numero(tmp_path, sin_qr):
    assert pdf_export.qr_matrix("https://wa.me/1") is None
    pdf = pdf_export.render_ticket(_datos(), tmp_path / "t.pdf", _taller())
    texto = _texto(pdf)
    assert "+504 0000 1111" in texto
    # La invitación a escanear solo tiene sentido si hay código.
    assert "Escaneá" not in texto


def test_el_qr_de_respaldo_usa_qrcode(monkeypatch):
    """Sin `segno`, se usa `qrcode` (el que trae Fedora como
    python3-qrcode)."""
    monkeypatch.setitem(sys.modules, "segno", None)

    class QRCode:
        def __init__(self, border, error_correction):
            assert border == 0

        def add_data(self, texto):
            self.texto = texto

        def make(self, fit):
            pass

        def get_matrix(self):
            return [[True, False], [False, True]]

    constantes = types.SimpleNamespace(ERROR_CORRECT_M=0)
    monkeypatch.setitem(sys.modules, "qrcode",
                        types.SimpleNamespace(QRCode=QRCode,
                                              constants=constantes))
    monkeypatch.setitem(sys.modules, "qrcode.constants", constantes)
    assert pdf_export.qr_matrix("x") == [[True, False], [False, True]]


@requiere_poppler
def test_sin_whatsapp_no_hay_pie_de_contacto(tmp_path, qr_falso):
    pdf = pdf_export.render_ticket(_datos(), tmp_path / "t.pdf",
                                   _taller(whatsapp=""))
    texto = _compacto(_texto(pdf)).upper()
    assert "WHATSAPP" not in texto
    assert not qr_falso


@requiere_poppler
def test_sin_datos_del_taller_el_encabezado_es_neutro(tmp_path):
    pdf = pdf_export.render_ticket(_datos(), tmp_path / "t.pdf",
                                   pdf_export.ShopProfile())
    texto = _texto(pdf)
    assert "TICKETDEENTREGA" in _compacto(texto).upper()
    assert "Cliente Ficticio" in texto


def _logo_de_prueba(path: Path) -> Path:
    superficie = cairo.ImageSurface(cairo.FORMAT_ARGB32, 300, 80)
    ctx = cairo.Context(superficie)
    ctx.set_source_rgba(0.6, 0.5, 0.95, 1)
    ctx.rectangle(10, 10, 280, 60)
    ctx.fill()
    superficie.write_to_png(str(path))
    return path


@requiere_poppler
def test_con_logo_el_nombre_no_va_en_texto(tmp_path):
    logo = _logo_de_prueba(tmp_path / "logo.png")
    pdf = pdf_export.render_ticket(_datos(), tmp_path / "t.pdf",
                                   _taller(logo_path=str(logo)))
    texto = _texto(pdf)
    assert "Taller Ficticio" not in texto
    assert "Cliente Ficticio" in texto


@requiere_poppler
def test_en_modo_claro_el_nombre_va_en_texto_aunque_haya_logo(tmp_path):
    logo = _logo_de_prueba(tmp_path / "logo.png")
    pdf = pdf_export.render_ticket(
        _datos(), tmp_path / "t.pdf",
        _taller(logo_path=str(logo), theme="light"))
    texto = _texto(pdf)
    assert "Taller" in texto and "Ficticio" in texto


@requiere_poppler
def test_un_logo_que_no_existe_cae_al_nombre(tmp_path):
    pdf = pdf_export.render_ticket(
        _datos(), tmp_path / "t.pdf",
        _taller(logo_path=str(tmp_path / "no-existe.png")))
    texto = _texto(pdf)
    assert "Taller" in texto and "Ficticio" in texto


def test_un_logo_que_no_es_una_imagen_no_rompe(tmp_path):
    falso = _archivo(tmp_path / "logo.png", b"esto no es un png")
    assert pdf_export._cargar_logo(str(falso)) is None


@requiere_poppler
def test_unas_notas_larguisimas_no_empujan_el_ticket_a_otra_hoja(tmp_path):
    notas = "Se revisó el lector y se cambió la lente. " * 60
    pdf = pdf_export.render_ticket(_datos(notes=notas, games=_juegos(5)),
                                   tmp_path / "t.pdf", _taller())
    assert _paginas(pdf) == 1


def test_el_pdf_se_escribe_con_atomicfs(tmp_path, monkeypatch):
    """Sigue usando la escritura atómica: si falla, no queda archivo."""
    def boom(*_args):
        raise RuntimeError("falla simulada")

    monkeypatch.setattr(pdf_export, "_dibujar", boom)
    with pytest.raises(RuntimeError):
        pdf_export.render_ticket(_datos(), tmp_path / "t.pdf", _taller())
    assert list(tmp_path.iterdir()) == []


# ======================================================== Interfaz --
def test_el_dialogo_del_ticket_entrega_los_datos_de_consola(monkeypatch):
    """El handler real, contra un `self` de mentira (sin display)."""
    from wiibackup_manager.widgets import ticket_dialog

    class _Fila:
        def __init__(self, texto):
            self.texto = texto

        def get_text(self):
            return self.texto

    recibido = []
    vista = types.SimpleNamespace(
        name_row=_Fila("Cliente Ficticio"),
        model_row=_Fila("Modelo"), serial_row=_Fila("S-1"),
        system_row=_Fila(""), service_row=_Fila("Limpieza"),
        _notes_text=lambda: "notas", close=lambda: None,
        on_generate=lambda *a: recibido.append(a))
    ticket_dialog.TicketDialog._on_generate_clicked(vista)
    assert recibido == [("Cliente Ficticio", "notas",
                         ConsoleInfo("Modelo", "S-1", "", "Limpieza"))]


# ================================================ tools/prepare_logo --
def _prepare_logo():
    import importlib.util
    ruta = Path(__file__).resolve().parent.parent / "tools" / "prepare_logo.py"
    spec = importlib.util.spec_from_file_location("prepare_logo", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


def test_prepare_logo_recorta_y_vuelve_transparente_el_fondo(tmp_path):
    """Un "afiche" ficticio: fondo oscuro azulado, un texto blanco y uno
    morado medio tono en el medio, y un "ícono" de otra marca en la
    esquina que queda FUERA de la caja y no puede aparecer."""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf

    superficie = cairo.ImageSurface(cairo.FORMAT_RGB24, 400, 300)
    ctx = cairo.Context(superficie)
    ctx.set_source_rgb(0.05, 0.03, 0.15)
    ctx.paint()
    ctx.set_source_rgb(1, 1, 1)
    ctx.rectangle(100, 120, 120, 40)          # "palabra" blanca
    ctx.fill()
    ctx.set_source_rgb(0.66, 0.5, 0.97)
    ctx.rectangle(230, 120, 60, 40)           # parte morada
    ctx.fill()
    ctx.set_source_rgb(0.6, 0.4, 0.8)
    ctx.rectangle(5, 5, 30, 30)               # ícono ajeno, fuera de la caja
    ctx.fill()
    entrada = tmp_path / "afiche.png"
    superficie.write_to_png(str(entrada))

    salida = tmp_path / "logo.png"
    modulo = _prepare_logo()
    assert modulo.main([str(entrada), str(salida), "--caja", "60,90,340,200",
                        "--margen", "4"]) == 0

    logo = GdkPixbuf.Pixbuf.new_from_file(str(salida))
    assert logo.get_has_alpha()
    # Recortado a lo visible (190x40) más el margen, sin el ícono ajeno.
    assert (logo.get_width(), logo.get_height()) == (198, 48)
    datos = logo.read_pixel_bytes().get_data()
    paso = logo.get_rowstride()

    def pixel(x, y):
        i = y * paso + x * 4
        return tuple(datos[i:i + 4])

    assert pixel(0, 0)[3] == 0                    # fondo: transparente
    assert pixel(30, 24) == (255, 255, 255, 255)  # blanco: opaco
    morado = pixel(160, 24)
    assert morado[3] == 255 and morado[2] > morado[1]  # el morado sobrevive


def test_prepare_logo_rechaza_una_salida_que_no_es_png(tmp_path):
    with pytest.raises(SystemExit):
        _prepare_logo().main([str(tmp_path / "x.jpg"), str(tmp_path / "y.jpg"),
                              "--caja", "0,0,10,10"])


# ================================================== Tamaño adaptable --
def _plan(datos, taller=None):
    taller = taller or _taller()
    base = pdf_export._paleta(taller.theme, taller.accent)
    fin = (pdf_export.PAGE_HEIGHT - pdf_export.MARGIN
           - pdf_export._alto_del_pie(taller) - 4)
    return pdf_export._preparar(datos, taller, base, None,
                                pdf_export._filas_de_juegos(datos), fin)


def test_con_pocos_juegos_la_letra_es_mas_grande_y_va_en_una_columna():
    estilo, columnas = _plan(_datos(games=_juegos(2)))
    assert estilo.escala > 1.0
    assert columnas == 1


def test_con_pocos_juegos_lo_que_sobra_se_reparte():
    estilo, _cols = _plan(_datos(games=_juegos(2)))
    assert estilo.aire > 0


def test_con_muchos_juegos_queda_el_tamano_compacto():
    estilo, columnas = _plan(_datos(games=_juegos(48, 17)))
    assert estilo.escala == 1.0
    assert columnas == pdf_export.LIST_COLUMNS
    assert estilo.aire == 0


@pytest.mark.parametrize("wii,gc", [(20, 0), (25, 5)])
def test_desde_veinte_juegos_nunca_se_agranda(wii, gc):
    estilo, _cols = _plan(_datos(games=_juegos(wii, gc)))
    assert estilo.escala <= 1.0


def _layouts_dibujados(monkeypatch):
    """Registra (texto, ¿cortado con "…"?) de cada texto que se dibuja."""
    dibujados = []
    original = pdf_export._mostrar

    def espia(ctx, layout, x, y, color):
        dibujados.append((layout.get_text(), layout.is_ellipsized()))
        return original(ctx, layout, x, y, color)

    monkeypatch.setattr(pdf_export, "_mostrar", espia)
    return dibujados


@pytest.mark.parametrize("wii,gc,con_taller", [
    (0, 0, False), (2, 0, True), (2, 1, True), (11, 4, True), (48, 17, True)])
@pytest.mark.parametrize("modo", ["dark", "light"])
def test_ningun_texto_se_corta(tmp_path, monkeypatch, wii, gc, con_taller, modo):
    """Con letra grande, una etiqueta como "CAPACIDAD TOTAL" no entra en
    una cuarta parte de la hoja: la grilla tiene que pasar a dos columnas
    en vez de cortarla con "…". Se le pregunta a cada layout de Pango si
    quedó cortado, porque `pdftotext` devuelve el texto entero aunque en
    la hoja se vea el "…"."""
    dibujados = _layouts_dibujados(monkeypatch)
    taller = _taller(theme=modo) if con_taller else pdf_export.ShopProfile(theme=modo)
    pdf_export.render_ticket(_datos(games=_juegos(wii, gc)),
                             tmp_path / "t.pdf", taller)
    cortados = [t for t, cortado in dibujados if cortado]
    assert cortados == []


def _palabras(pdf: Path) -> list:
    """(página, texto, x0, y0, x1, y1) de cada palabra, según poppler."""
    cajas = subprocess.run(["pdftotext", "-bbox", str(pdf), "-"],
                           capture_output=True, text=True, timeout=30,
                           check=True).stdout
    palabras = []
    for n, pagina in enumerate(cajas.split("<page ")[1:], start=1):
        for x0, y0, x1, y1, w in re.findall(
                r'xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" '
                r'yMax="([\d.]+)">([^<]*)</word>', pagina):
            palabras.append((n, w, float(x0), float(y0), float(x1), float(y1)))
    return palabras


@requiere_poppler
@pytest.mark.parametrize("wii,gc", [(0, 0), (2, 0), (11, 4), (48, 17)])
def test_nada_se_sale_de_la_hoja_ni_pisa_el_pie(tmp_path, wii, gc):
    datos = _datos(games=_juegos(wii, gc))
    pdf = pdf_export.render_ticket(datos, tmp_path / "t.pdf", _taller())
    palabras = _palabras(pdf)
    margen = pdf_export.MARGIN - 1
    for _n, w, x0, y0, x1, y1 in palabras:
        assert x0 >= margen and x1 <= pdf_export.PAGE_WIDTH - margen, w
        assert y0 >= 20 and y1 <= pdf_export.PAGE_HEIGHT - 20, w
    # La lista termina antes de la línea que abre el pie con el WhatsApp.
    pie = (pdf_export.PAGE_HEIGHT - pdf_export.MARGIN
           - pdf_export._alto_del_pie(_taller()) + 8)
    titulos = {g.title.split()[-1] for g in datos.games}
    for n, w, _x0, _y0, _x1, y1 in palabras:
        if n == 1 and w in titulos:
            assert y1 < pie, w


@requiere_poppler
def test_con_dos_juegos_la_hoja_no_queda_vacia_abajo(tmp_path):
    """El caso que motivó el tamaño adaptable: con dos juegos el contenido
    ocupaba el tercio de arriba. Ahora la lista termina cerca del pie."""
    datos = _datos(games=_juegos(2))
    pdf = pdf_export.render_ticket(datos, tmp_path / "t.pdf", _taller())
    ultimo = max(y1 for n, w, _x0, _y0, _x1, y1 in _palabras(pdf)
                 if w == "001")
    pie = pdf_export.PAGE_HEIGHT - pdf_export.MARGIN - pdf_export._alto_del_pie(
        _taller())
    assert pie - ultimo < 80


@requiere_poppler
def test_la_columna_de_al_lado_no_repite_el_total(tmp_path):
    """Con 15 juegos la lista de Wii sigue en la columna de la derecha:
    ese encabezado dice "Wii (continuación)" y no vuelve a poner 11."""
    datos = _datos(games=_juegos(11, 4))
    pdf = pdf_export.render_ticket(datos, tmp_path / "t.pdf", _taller())
    texto = _texto(pdf)
    assert "Wii (continuación)" in texto
    # El 11 aparece en el resumen ("11 Wii") y en el encabezado original;
    # una tercera vez sería el total repetido.
    assert len(re.findall(r"(?<![\w/])11(?![\w/])", texto)) == 2


# ================================================== Aviso sin taller --
def test_un_taller_sin_datos_esta_vacio():
    assert pdf_export.ShopProfile().is_empty()
    assert pdf_export.ShopProfile.from_settings(config.Settings()).is_empty()
    assert not _taller().is_empty()
    assert not pdf_export.ShopProfile(whatsapp=NUMERO_FICTICIO).is_empty()


def _toast_al_terminar(sin_taller: bool, visor_ok: bool = True) -> list:
    """El handler real de la vista, contra un `self` de mentira."""
    from wiibackup_manager.widgets import transfer_view

    toasts = []
    vista = types.SimpleNamespace(_show_toast=toasts.append)

    class Lanzador:
        def launch_finish(self, _r):
            if not visor_ok:
                raise RuntimeError("sin visor")

    transfer_view.TransferView._on_ticket_opened(
        vista, Lanzador(), None, Path("/x/Ticket.pdf"), sin_taller)
    return toasts


def test_el_aviso_final_dice_si_salio_sin_datos_del_taller():
    (aviso,) = _toast_al_terminar(sin_taller=True)
    assert "Ticket guardado en /x/Ticket.pdf" in aviso
    assert "Ajustes → General → Mi taller" in aviso
    (aviso,) = _toast_al_terminar(sin_taller=True, visor_ok=False)
    assert "no se pudo abrir el visor" in aviso and "Mi taller" in aviso


def test_con_datos_del_taller_no_hay_aviso():
    (aviso,) = _toast_al_terminar(sin_taller=False)
    assert "Mi taller" not in aviso
