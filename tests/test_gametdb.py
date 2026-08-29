"""Pruebas de `gametdb.py`.

No existía ningún test de este módulo (el 23% de cobertura previo salía
solo de código a nivel de import). Se prioriza el manejo de errores -
timeouts, respuestas mal formadas, región no encontrada, caché corrupta,
XML/ZIP rotos- por sobre el camino feliz, siguiendo el mismo criterio que
ya usa el resto de la red (`oscwii_client`, `wit_wrapper`): la mayoría de
los bugs reales de esta app aparecieron en esas rutas, no en el caso
normal.

Nada de esto toca la red de verdad: se fakea `urllib.request.urlopen`
directo (mismo patrón "objeto falso" que `make_game`/`iso_bytes` en
conftest, sin `unittest.mock`), y `config.CACHE_DIR`/`config.COVERS_DIR`
se aíslan a `tmp_path` en cada test."""
from __future__ import annotations

import io
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib
import struct
from pathlib import Path

import pytest

from wiibackup_manager import config, gametdb


# --------------------------------------------------------------- fixtures --
@pytest.fixture(autouse=True)
def _sandbox_gametdb(tmp_path, monkeypatch):
    """Aísla la caché en disco y el estado en memoria del módulo.

    Sin esto, dos tests que corran en el mismo proceso compartirían
    `config.CACHE_DIR`/`COVERS_DIR` (fijados una sola vez por `conftest.py`
    al importar), los registries de pedidos en vuelo y el índice de wiitdb
    en memoria, que persisten entre tests porque son módulo-nivel."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "COVERS_DIR", tmp_path / "cache" / "covers")
    monkeypatch.setattr(gametdb, "_wiitdb_index", None)
    monkeypatch.setattr(gametdb, "_wiitdb_failed_at", 0.0)
    gametdb._cover_jobs.forget()
    gametdb._extra_jobs.forget()
    yield
    gametdb._cover_jobs.forget()
    gametdb._extra_jobs.forget()


# ------------------------------------------------------------- utilidades --
def _make_png(color=(255, 0, 0)) -> bytes:
    """PNG de 1x1 válido de verdad (se decodifica con GdkPixbuf), armado a
    mano para no depender de ningún archivo de fixtures binario."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00" + bytes(color)))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


class FakeResponse:
    """Imita el objeto que devuelve `urlopen` dentro de un `with`."""

    def __init__(self, status: int = 200, data: bytes = b""):
        self.status = status
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_urlopen_sequence(items):
    """Fake de `urlopen` que devuelve/levanta un ítem por llamada, en orden.

    Un ítem que sea una excepción se levanta; cualquier otra cosa se
    devuelve tal cual (se espera un `FakeResponse`)."""
    it = iter(items)

    def _fake(req, timeout=None):
        item = next(it)
        if isinstance(item, BaseException):
            raise item
        return item

    return _fake


def _esperar(condicion, timeout: float = 5.0) -> bool:
    """Espera a que `condicion()` sea verdadera.

    Los pedidos asincrónicos se resuelven en un hilo del pool, así que el
    resultado no llega en el mismo hilo que los pidió."""
    limite = time.monotonic() + timeout
    while time.monotonic() < limite:
        if condicion():
            return True
        time.sleep(0.01)
    return bool(condicion())


def _zip_with_member(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(name, data)
    return buf.getvalue()


def _wiitdb_xml_bytes(games_xml: str) -> bytes:
    return f"<?xml version='1.0'?><datafile>{games_xml}</datafile>".encode("utf-8")


GAME_XML_COMPLETO = """
<game>
  <id>RMCP01</id>
  <locale lang="EN">
    <title>Mario Kart Wii</title>
    <synopsis>Race your friends online.</synopsis>
  </locale>
  <locale lang="ES">
    <title>Mario Kart Wii (ES)</title>
    <synopsis>Corré con tus amigos.
 \n \nNuevos circuitos.</synopsis>
  </locale>
  <locale lang="KO">
    <synopsis></synopsis>
  </locale>
  <genre>Racing</genre>
  <input players="4">
    <control type="wheel" required="false"/>
    <control type="wiimote"/>
    <control type="WIIMOTE"/>
  </input>
  <date year="2008" month="04" day="24"/>
  <publisher>Nintendo</publisher>
  <developer>Nintendo EAD</developer>
</game>
"""

GAME_XML_VACIO = "<game><id>AAAA01</id></game>"

GAME_XML_SOLO_EN = """
<game>
  <id>BBBB01</id>
  <locale lang="EN">
    <title>Only English Title</title>
    <synopsis>English synopsis.</synopsis>
  </locale>
</game>
"""


# ============================================================================
# Bloque A: carátulas
# ============================================================================

# --------------------------------------------------------- cover_cache_path
def test_cover_cache_path_sin_sufijo_para_wii():
    p = gametdb.cover_cache_path("rmcp01", "en", "wii")
    assert p.name == "RMCP01.EN.png"


def test_cover_cache_path_con_sufijo_para_gc():
    p = gametdb.cover_cache_path("gmse01", "US", "gc")
    assert p.name == "GMSE01.US.gc.png"


def test_cover_cache_path_normaliza_region_con_simbolos():
    p = gametdb.cover_cache_path("RMCP01", "en-us!", "wii")
    assert p.name == "RMCP01.ENUS.png"


def test_cover_cache_path_region_vacia_usa_el_default():
    p = gametdb.cover_cache_path("RMCP01", "", "wii")
    assert p.name == f"RMCP01.{gametdb.DEFAULT_COVER_REGION}.png"


def test_cover_cache_path_id_invalido_levanta_value_error():
    with pytest.raises(ValueError):
        gametdb.cover_cache_path("???", "EN")


# --------------------------------------------------- _decodes_as_image
def test_decodes_as_image_true_para_png_valido(tmp_path):
    p = tmp_path / "ok.png"
    p.write_bytes(_make_png())
    assert gametdb._decodes_as_image(p)


def test_decodes_as_image_false_para_basura(tmp_path):
    p = tmp_path / "mal.png"
    p.write_bytes(b"esto no es una imagen")
    assert not gametdb._decodes_as_image(p)


# --------------------------------------------------- _is_valid_cached_cover
def test_is_valid_cached_cover_true_para_png_completo(tmp_path):
    p = tmp_path / "completo.png"
    p.write_bytes(_make_png())
    assert gametdb._is_valid_cached_cover(p)


def test_is_valid_cached_cover_falso_para_archivo_vacio(tmp_path):
    p = tmp_path / "vacio.png"
    p.write_bytes(b"")
    assert not gametdb._is_valid_cached_cover(p)


def test_is_valid_cached_cover_falso_para_png_truncado(tmp_path):
    """Cabecera PNG válida, pero cortado antes del bloque IEND: la
    descarga interrumpida que este chequeo existe para detectar."""
    p = tmp_path / "cortado.png"
    p.write_bytes(gametdb.PNG_MAGIC + b"datos a medio bajar, sin IEND")
    assert not gametdb._is_valid_cached_cover(p)


def test_is_valid_cached_cover_falso_si_no_existe(tmp_path):
    assert not gametdb._is_valid_cached_cover(tmp_path / "no-existe.png")


def test_is_valid_cached_cover_falso_magic_incorrecto_con_tamano_suficiente(tmp_path):
    """Tamaño de sobra (no es el caso "0 bytes" ni "truncado"), pero los
    primeros bytes no son la cabecera PNG."""
    p = tmp_path / "mal-magic.png"
    p.write_bytes(b"NOPE" + b"x" * 40)
    assert not gametdb._is_valid_cached_cover(p)


# --------------------------------------------------------------- _store_cover
def test_store_cover_falla_decodificacion_no_deja_nada(tmp_path):
    cache_path = tmp_path / "RMCP01.EN.png"
    ok = gametdb._store_cover(cache_path, gametdb.PNG_MAGIC + b"corrupto")
    assert ok is False
    assert not cache_path.exists()
    # tampoco debe quedar el temporal
    assert list(tmp_path.iterdir()) == []


def test_store_cover_falla_escritura_no_deja_nada(tmp_path):
    """Carpeta padre inexistente: falla `write_bytes` con OSError."""
    cache_path = tmp_path / "no-existe" / "RMCP01.EN.png"
    ok = gametdb._store_cover(cache_path, b"cualquier cosa")
    assert ok is False
    assert not cache_path.exists()


def test_store_cover_exitoso_guarda_el_png(tmp_path):
    cache_path = tmp_path / "RMCP01.EN.png"
    png = _make_png()
    ok = gametdb._store_cover(cache_path, png)
    assert ok is True
    assert cache_path.read_bytes() == png


def test_store_cover_falla_escritura_y_tambien_falla_la_limpieza(tmp_path, monkeypatch):
    """Doble falla: ni se puede escribir el temporal ni borrarlo después.
    No debe propagar la segunda excepción, solo devolver False."""
    cache_path = tmp_path / "RMCP01.EN.png"

    def boom_write_bytes(self, *a, **kw):
        raise OSError("disco lleno")

    def boom_unlink(self, *a, **kw):
        raise OSError("también falla el borrado")

    monkeypatch.setattr(Path, "write_bytes", boom_write_bytes)
    monkeypatch.setattr(Path, "unlink", boom_unlink)

    assert gametdb._store_cover(cache_path, b"datos") is False


# --------------------------------------------------------- _log_cover_fetch_failed
def test_log_cover_fetch_failed_escribe_a_stderr(capsys):
    gametdb._log_cover_fetch_failed("RMCP01", "wii", [("EN", "HTTP 500")])
    captured = capsys.readouterr()
    assert "RMCP01" in captured.err
    assert "HTTP 500" in captured.err


# --------------------------------------------------------------- get_cover_path
def test_get_cover_path_id_invalido_no_toca_red(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: (_ for _ in ()).throw(
                             AssertionError("no debería llamar a la red")))
    assert gametdb.get_cover_path("??????") is None


def test_get_cover_path_todas_las_regiones_404_no_loggea(monkeypatch, capsys):
    errores = [urllib.error.HTTPError("http://x", 404, "Not Found", None, None)
               for _ in range(6)]
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_sequence(errores))
    assert gametdb.get_cover_path("RMCP01", region="EN") is None
    assert capsys.readouterr().err == ""


def test_get_cover_path_error_no_404_se_loggea(monkeypatch, capsys):
    secuencia = [urllib.error.HTTPError("http://x", 500, "boom", None, None)]
    secuencia += [urllib.error.HTTPError("http://x", 404, "Not Found", None, None)
                  for _ in range(5)]
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_sequence(secuencia))
    assert gametdb.get_cover_path("RMCP01", region="EN") is None
    err = capsys.readouterr().err
    assert "RMCP01" in err
    assert "HTTP 500" in err


def test_get_cover_path_status_no_200_sin_excepcion(monkeypatch, capsys):
    """El servidor responde (no hay excepción de red) pero con un status
    HTTP que no es 200 ni un error levantado por urllib."""
    secuencia = [FakeResponse(503, b"")]
    secuencia += [urllib.error.HTTPError("http://x", 404, "Not Found", None, None)
                  for _ in range(5)]
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_sequence(secuencia))
    assert gametdb.get_cover_path("RMCP01", region="EN") is None
    assert "status HTTP 503" in capsys.readouterr().err


def test_get_cover_path_timeout_en_una_region_sigue_con_la_siguiente(monkeypatch):
    png = _make_png()
    secuencia = [TimeoutError("se colgó"), FakeResponse(200, png)]
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_sequence(secuencia))
    result = gametdb.get_cover_path("RMCP01", region="EN")
    assert result is not None
    assert result.read_bytes() == png


def test_get_cover_path_sin_red_en_todas_las_regiones(monkeypatch, capsys):
    errores = [urllib.error.URLError("sin red") for _ in range(6)]
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_sequence(errores))
    assert gametdb.get_cover_path("RMCP01", region="EN") is None
    assert "RMCP01" in capsys.readouterr().err


def test_get_cover_path_respuesta_no_es_png(monkeypatch, capsys):
    secuencia = [FakeResponse(200, b"<html>404 not found</html>")]
    secuencia += [urllib.error.HTTPError("http://x", 404, "Not Found", None, None)
                  for _ in range(5)]
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_sequence(secuencia))
    assert gametdb.get_cover_path("RMCP01", region="EN") is None
    assert "no es un PNG" in capsys.readouterr().err


def test_get_cover_path_png_descargado_no_se_puede_decodificar(monkeypatch, capsys):
    secuencia = [FakeResponse(200, gametdb.PNG_MAGIC + b"corrupto")]
    secuencia += [urllib.error.HTTPError("http://x", 404, "Not Found", None, None)
                  for _ in range(5)]
    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen_sequence(secuencia))
    assert gametdb.get_cover_path("RMCP01", region="EN") is None
    assert "decodificar" in capsys.readouterr().err


def test_get_cover_path_cache_corrupta_se_borra_y_reintenta(monkeypatch):
    cache_path = gametdb.cover_cache_path("RMCP01", "EN", "wii")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"")  # 0 bytes: caché corrupta

    png = _make_png()
    monkeypatch.setattr(urllib.request, "urlopen",
                         _fake_urlopen_sequence([FakeResponse(200, png)]))
    result = gametdb.get_cover_path("RMCP01", region="EN")
    assert result == cache_path
    assert cache_path.read_bytes() == png


def test_get_cover_path_force_ignora_cache_valida(monkeypatch):
    cache_path = gametdb.cover_cache_path("RMCP01", "EN", "wii")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    original = _make_png(color=(255, 0, 0))
    cache_path.write_bytes(original)

    nueva = _make_png(color=(0, 255, 0))
    llamadas = []

    def fake_urlopen(req, timeout=None):
        llamadas.append(req)
        return FakeResponse(200, nueva)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    result = gametdb.get_cover_path("RMCP01", region="EN", force=True)
    assert len(llamadas) == 1
    assert result.read_bytes() == nueva


def test_get_cover_path_unlink_de_cache_corrupta_falla_pero_sigue(monkeypatch):
    """Si borrar la caché corrupta falla (permisos), la descarga sigue su
    curso igual: no debe propagar la excepción ni frenar el intento."""
    cache_path = gametdb.cover_cache_path("RMCP01", "EN", "wii")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(b"")  # corrupta

    original_unlink = Path.unlink

    def flaky_unlink(self, *a, **kw):
        if self == cache_path:
            raise OSError("permiso denegado")
        return original_unlink(self, *a, **kw)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    png = _make_png()
    monkeypatch.setattr(urllib.request, "urlopen",
                         _fake_urlopen_sequence([FakeResponse(200, png)]))
    result = gametdb.get_cover_path("RMCP01", region="EN")
    assert result == cache_path
    assert cache_path.read_bytes() == png


def test_get_cover_path_usa_cache_valida_sin_pedir_red(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: (_ for _ in ()).throw(
                             AssertionError("no debería llamar a la red")))
    cache_path = gametdb.cover_cache_path("RMCP01", "EN", "wii")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(_make_png())

    result = gametdb.get_cover_path("RMCP01", region="EN")
    assert result == cache_path


def test_get_cover_path_descarga_exitosa_primera_region(monkeypatch):
    png = _make_png()
    monkeypatch.setattr(urllib.request, "urlopen",
                         _fake_urlopen_sequence([FakeResponse(200, png)]))
    result = gametdb.get_cover_path("RMCP01", region="EN", console="wii")
    assert result is not None
    assert result.read_bytes() == png


# --------------------------------------------------------- fetch_cover_async
def test_fetch_cover_async_id_invalido_no_toca_red(monkeypatch):
    monkeypatch.setattr(gametdb, "get_cover_path",
                         lambda *a, **kw: (_ for _ in ()).throw(
                             AssertionError("no debería llamar a get_cover_path")))
    resultados = []
    gametdb.fetch_cover_async("??????", on_done=resultados.append)
    assert resultados == [None]
    assert gametdb.covers_in_flight() == 0


def test_fetch_cover_async_sin_on_done_no_hace_nada(monkeypatch):
    monkeypatch.setattr(gametdb, "get_cover_path",
                         lambda *a, **kw: (_ for _ in ()).throw(
                             AssertionError("no debería llegar a descargar")))
    gametdb.fetch_cover_async("RMCP01", region="EN", on_done=None)
    assert gametdb.covers_in_flight() == 0


def test_fetch_cover_async_usa_cache_sin_pasar_por_el_executor(monkeypatch):
    monkeypatch.setattr(gametdb, "get_cover_path",
                         lambda *a, **kw: (_ for _ in ()).throw(
                             AssertionError("no debería descargar de nuevo")))
    cache_path = gametdb.cover_cache_path("RMCP01", "EN", "wii")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(_make_png())

    resultados = []
    gametdb.fetch_cover_async("RMCP01", region="EN", on_done=resultados.append,
                               console="wii")
    assert resultados == [cache_path]
    assert gametdb.covers_in_flight() == 0


def test_covers_in_flight_cero_sin_descargas():
    assert gametdb.covers_in_flight() == 0


def test_fetch_cover_async_dedupe_pedidos_concurrentes(monkeypatch, tmp_path):
    """Dos pedidos de la misma carátula mientras la primera descarga sigue
    en curso: el segundo se cuelga del primero, y `get_cover_path` se llama
    una sola vez de verdad."""
    llamadas = []
    iniciado = threading.Event()
    liberar = threading.Event()
    resultado_path = tmp_path / "resultado.png"

    def fake_get_cover_path(game_id, region, force=False, console="wii"):
        llamadas.append((game_id, region, console))
        iniciado.set()
        assert liberar.wait(timeout=5), "el test no liberó a tiempo"
        return resultado_path

    monkeypatch.setattr(gametdb, "get_cover_path", fake_get_cover_path)

    resultados = []
    lock = threading.Lock()

    def hacer_callback(nombre):
        def _cb(path):
            with lock:
                resultados.append((nombre, path))
        return _cb

    gametdb.fetch_cover_async("RMCP01", region="EN", on_done=hacer_callback("uno"),
                               console="wii")
    assert iniciado.wait(timeout=5)
    assert gametdb.covers_in_flight() == 1

    gametdb.fetch_cover_async("RMCP01", region="EN", on_done=hacer_callback("dos"),
                               console="wii")
    liberar.set()

    deadline = time.monotonic() + 5
    while gametdb.covers_in_flight() > 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(llamadas) == 1
    assert len(resultados) == 2
    assert resultados[0][1] == resultado_path
    assert resultados[1][1] == resultado_path


def test_fetch_cover_async_excepcion_inesperada_da_none(monkeypatch):
    """Un error de programación en `get_cover_path` (no una excepción de
    red ya contemplada) no puede dejar el worker colgado ni el callback
    sin llamar: se resuelve como "no hay carátula".

    Va por la API pública y no por las internas del registry: lo que se
    verifica acá es el cableado de gametdb (que no pasa ningún `on_error`,
    o sea que le corresponde el None por defecto). El comportamiento
    genérico del registry se prueba en `test_inflight.py`."""
    def fake_get_cover_path(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(gametdb, "get_cover_path", fake_get_cover_path)
    resultados = []

    gametdb.fetch_cover_async("RMCP01", region="EN", on_done=resultados.append)

    assert _esperar(lambda: len(resultados) == 1)
    assert resultados == [None]
    assert gametdb.covers_in_flight() == 0


# ============================================================================
# Bloque B: metadata extendida (wiitdb.xml)
# ============================================================================

# ------------------------------------------------------------ language_for_region
def test_language_for_region_us_mapea_a_en():
    assert gametdb.language_for_region("us") == "EN"


def test_language_for_region_otros_se_mantienen():
    assert gametdb.language_for_region("es") == "ES"


def test_language_for_region_vacia_usa_default():
    assert gametdb.language_for_region("") == gametdb.DEFAULT_LANGUAGE


# ------------------------------------------------------------------ GameControl
def test_game_control_label_conocido():
    c = gametdb.GameControl(type="wheel", required=True)
    assert c.label == gametdb.CONTROL_LABELS["wheel"]
    assert c.describe() == c.label  # obligatorio: sin aclaración


def test_game_control_label_desconocido_se_muestra_crudo():
    c = gametdb.GameControl(type="futuristic_gizmo", required=False)
    assert c.label == "futuristic_gizmo"
    assert c.describe() == "futuristic_gizmo (opcional)"


# --------------------------------------------------------------- GameExtraInfo
def test_game_extra_info_is_empty_true_sin_datos():
    assert gametdb.GameExtraInfo().is_empty()


def test_game_extra_info_is_empty_false_con_un_dato():
    assert not gametdb.GameExtraInfo(genre="Racing").is_empty()


def test_title_to_show_next_to_prioriza_original():
    info = gametdb.GameExtraInfo(original_title="Mario Kart Wii",
                                  localized_title="Mario Kart Wii (ES)")
    assert info.title_to_show_next_to("Otro título") == (
        gametdb._("Título original"), "Mario Kart Wii")


def test_title_to_show_next_to_cae_a_localizado_si_original_coincide():
    info = gametdb.GameExtraInfo(original_title="Mario Kart Wii",
                                  localized_title="Mario Kart Wii (ES)")
    resultado = info.title_to_show_next_to("MARIO KART WII!!")
    assert resultado == (gametdb._("Título traducido"), "Mario Kart Wii (ES)")


def test_title_to_show_next_to_none_si_ambos_coinciden():
    info = gametdb.GameExtraInfo(original_title="Mario Kart Wii",
                                  localized_title="Mario Kart Wii")
    assert info.title_to_show_next_to("mario, kart. wii") is None


@pytest.mark.parametrize("texto,esperado", [
    ("Super Smash Bros. Brawl!", "supersmashbrosbrawl"),
    (None, ""),
    ("", ""),
])
def test_normalize_title(texto, esperado):
    assert gametdb._normalize_title(texto) == esperado


# ------------------------------------------------------------------------ misc
def test_wiitdb_cache_path():
    assert gametdb.wiitdb_cache_path() == config.CACHE_DIR / "wiitdb.xml"


def test_wiitdb_index_available_refleja_estado(monkeypatch):
    monkeypatch.setattr(gametdb, "_wiitdb_index", None)
    assert gametdb.wiitdb_index_available() is False
    monkeypatch.setattr(gametdb, "_wiitdb_index", {})
    assert gametdb.wiitdb_index_available() is False
    monkeypatch.setattr(gametdb, "_wiitdb_index", {"RMCP01": object()})
    assert gametdb.wiitdb_index_available() is True


def test_extra_info_in_flight_cero_sin_consultas():
    assert gametdb.extra_info_in_flight() == 0


# ------------------------------------------------------------ _xml_is_well_formed
def test_xml_is_well_formed_true_para_xml_valido():
    assert gametdb._xml_is_well_formed(b"<a><b/></a>")


def test_xml_is_well_formed_false_para_xml_cortado():
    assert not gametdb._xml_is_well_formed(b"<a><b>")


# ------------------------------------------------------------------ _download_wiitdb
def test_download_wiitdb_status_no_200(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(503, b""))
    assert gametdb._download_wiitdb() is None


@pytest.mark.parametrize("excepcion", [
    urllib.error.URLError("sin red"),
    urllib.error.HTTPError("http://x", 500, "boom", None, None),
    TimeoutError(),
    OSError("disco desconectado"),
])
def test_download_wiitdb_error_de_red(monkeypatch, excepcion):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: (_ for _ in ()).throw(excepcion))
    assert gametdb._download_wiitdb() is None


def test_download_wiitdb_zip_corrupto(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, b"no soy un zip"))
    assert gametdb._download_wiitdb() is None


def test_download_wiitdb_sin_el_miembro_esperado(monkeypatch):
    data = _zip_with_member("otro.xml", b"<datafile/>")
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, data))
    assert gametdb._download_wiitdb() is None


def test_download_wiitdb_excede_tamano_maximo(monkeypatch):
    data = _zip_with_member(gametdb.WIITDB_MEMBER, b"<datafile/>")
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, data))

    class FakeInfo:
        file_size = gametdb.WIITDB_MAX_UNCOMPRESSED + 1

    monkeypatch.setattr(zipfile.ZipFile, "getinfo", lambda self, name: FakeInfo())
    assert gametdb._download_wiitdb() is None


def test_download_wiitdb_xml_mal_formado(monkeypatch):
    data = _zip_with_member(gametdb.WIITDB_MEMBER, b"<datafile><game>")
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, data))
    assert gametdb._download_wiitdb() is None
    assert not gametdb.wiitdb_cache_path().exists()


def test_download_wiitdb_falla_escritura_temporal(monkeypatch):
    xml = _wiitdb_xml_bytes(GAME_XML_VACIO)
    data = _zip_with_member(gametdb.WIITDB_MEMBER, xml)
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, data))

    def boom_write_bytes(self, *a, **kw):
        raise OSError("disco lleno")

    monkeypatch.setattr(Path, "write_bytes", boom_write_bytes)
    assert gametdb._download_wiitdb() is None


def test_download_wiitdb_falla_escritura_y_tambien_falla_la_limpieza(monkeypatch):
    xml = _wiitdb_xml_bytes(GAME_XML_VACIO)
    data = _zip_with_member(gametdb.WIITDB_MEMBER, xml)
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, data))

    def boom_write_bytes(self, *a, **kw):
        raise OSError("disco lleno")

    def boom_unlink(self, *a, **kw):
        raise OSError("también falla el borrado")

    monkeypatch.setattr(Path, "write_bytes", boom_write_bytes)
    monkeypatch.setattr(Path, "unlink", boom_unlink)
    assert gametdb._download_wiitdb() is None


def test_download_wiitdb_force_con_unlink_que_falla(monkeypatch):
    path = gametdb.wiitdb_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"<datafile/>")

    def boom_unlink(self, *a, **kw):
        raise OSError("permiso denegado")

    monkeypatch.setattr(Path, "unlink", boom_unlink)
    assert gametdb._download_wiitdb(force=True) is None


def test_download_wiitdb_usa_la_cache_sin_forzar(monkeypatch):
    path = gametdb.wiitdb_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"<datafile/>")
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: (_ for _ in ()).throw(
                             AssertionError("no debería llamar a la red")))
    assert gametdb._download_wiitdb() == path


def test_download_wiitdb_exitoso_deja_el_xml_en_cache(monkeypatch):
    xml = _wiitdb_xml_bytes(GAME_XML_VACIO)
    data = _zip_with_member(gametdb.WIITDB_MEMBER, xml)
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, data))
    result = gametdb._download_wiitdb()
    assert result == gametdb.wiitdb_cache_path()
    assert result.read_bytes() == xml


# ----------------------------------------------------------------- _build_index
def test_build_index_xml_malformado(tmp_path):
    p = tmp_path / "roto.xml"
    p.write_bytes(b"<datafile><game>")
    assert gametdb._build_index(p) is None


def test_build_index_archivo_inexistente(tmp_path):
    assert gametdb._build_index(tmp_path / "no-existe.xml") is None


def test_build_index_sin_juegos_da_diccionario_vacio(tmp_path):
    p = tmp_path / "vacio.xml"
    p.write_bytes(_wiitdb_xml_bytes(""))
    assert gametdb._build_index(p) == {}


def test_build_index_ignora_game_sin_id(tmp_path):
    p = tmp_path / "sinid.xml"
    p.write_bytes(_wiitdb_xml_bytes("<game><genre>Racing</genre></game>"))
    assert gametdb._build_index(p) == {}


def test_build_index_ignora_id_vacio(tmp_path):
    p = tmp_path / "idvacio.xml"
    p.write_bytes(_wiitdb_xml_bytes("<game><id></id></game>"))
    assert gametdb._build_index(p) == {}


def test_build_index_arma_el_indice_por_id(tmp_path):
    p = tmp_path / "dos.xml"
    p.write_bytes(_wiitdb_xml_bytes(GAME_XML_VACIO + GAME_XML_SOLO_EN))
    index = gametdb._build_index(p)
    assert set(index.keys()) == {"AAAA01", "BBBB01"}


# ------------------------------------------------------------- _load_wiitdb_index
def test_load_wiitdb_index_reintenta_una_vez_si_la_cache_no_parsea(monkeypatch):
    llamadas_download = []
    resultados_build = iter([None, {"RMCP01": object()}])

    monkeypatch.setattr(gametdb, "_download_wiitdb",
                         lambda force=False: llamadas_download.append(force)
                         or Path("fake-path"))
    monkeypatch.setattr(gametdb, "_build_index",
                         lambda path: next(resultados_build))

    index = gametdb._load_wiitdb_index()
    assert list(index.keys()) == ["RMCP01"]
    assert llamadas_download == [False, True]


def test_load_wiitdb_index_dos_intentos_corruptos_da_vacio(monkeypatch):
    monkeypatch.setattr(gametdb, "_download_wiitdb", lambda force=False: Path("fake"))
    monkeypatch.setattr(gametdb, "_build_index", lambda path: None)
    assert gametdb._load_wiitdb_index() == {}


def test_load_wiitdb_index_descarga_fallida_no_reintenta_el_parseo(monkeypatch):
    llamadas_build = []
    monkeypatch.setattr(gametdb, "_download_wiitdb", lambda force=False: None)
    monkeypatch.setattr(gametdb, "_build_index",
                         lambda path: llamadas_build.append(path) or {})
    assert gametdb._load_wiitdb_index() == {}
    assert llamadas_build == []


# ----------------------------------------------------------- _ensure_wiitdb_index
def test_ensure_wiitdb_index_no_reintenta_antes_de_tiempo(monkeypatch):
    llamadas = []
    monkeypatch.setattr(gametdb, "_load_wiitdb_index",
                         lambda: llamadas.append(1) or {})

    reloj = [100.0]
    monkeypatch.setattr(gametdb.time, "monotonic", lambda: reloj[0])

    assert gametdb._ensure_wiitdb_index() == {}
    assert len(llamadas) == 1

    reloj[0] += 1  # bastante antes de _WIITDB_RETRY_SECONDS
    assert gametdb._ensure_wiitdb_index() == {}
    assert len(llamadas) == 1  # no reintentó

    reloj[0] += gametdb._WIITDB_RETRY_SECONDS + 1
    assert gametdb._ensure_wiitdb_index() == {}
    assert len(llamadas) == 2  # ahora sí


def test_ensure_wiitdb_index_reusa_el_indice_ya_armado(monkeypatch):
    llamadas = []
    monkeypatch.setattr(gametdb, "_load_wiitdb_index",
                         lambda: llamadas.append(1) or {"RMCP01": object()})
    assert gametdb._ensure_wiitdb_index()
    assert gametdb._ensure_wiitdb_index()
    assert len(llamadas) == 1


# -------------------------------------------------------------- _locale_texts
def test_locale_texts_ignora_etiqueta_vacia():
    xml = (b"<game>"
           b"<locale lang='KO'><synopsis></synopsis></locale>"
           b"<locale lang='EN'><synopsis>hi</synopsis></locale>"
           b"</game>")
    game_el = ET.fromstring(xml)
    assert gametdb._locale_texts(game_el, "synopsis") == {"EN": "hi"}


# -------------------------------------------------------------- _clean_synopsis
@pytest.mark.parametrize("texto,esperado", [
    (None, None),
    ("", None),
    ("   ", None),
    ("Uno\n\n\nDos", "Uno\n\nDos"),
    ("Line1 \n \nLine2", "Line1\n\nLine2"),
])
def test_clean_synopsis(texto, esperado):
    assert gametdb._clean_synopsis(texto) == esperado


# -------------------------------------------------------------- _parse_controls
def test_parse_controls_ignora_sin_type_dedup_y_required():
    xml = (b"<game><input players='2'>"
           b"<control/>"
           b"<control type='wheel' required='false'/>"
           b"<control type='WHEEL'/>"
           b"<control type='wiimote'/>"
           b"</input></game>")
    game_el = ET.fromstring(xml)
    controls = gametdb._parse_controls(game_el)
    assert [c.type for c in controls] == ["wheel", "wiimote"]
    assert controls[0].required is False
    assert controls[1].required is True


def test_parse_controls_sin_input_da_vacio():
    game_el = ET.fromstring(b"<game><id>X</id></game>")
    assert gametdb._parse_controls(game_el) == ()


# --------------------------------------------------------- get_game_extra_info
def test_get_game_extra_info_id_no_encontrado(monkeypatch):
    monkeypatch.setattr(gametdb, "_ensure_wiitdb_index", lambda: {})
    assert gametdb.get_game_extra_info("RMCP01") is None


def test_get_game_extra_info_todo_vacio_da_none(monkeypatch):
    root = ET.fromstring(_wiitdb_xml_bytes(GAME_XML_VACIO))
    game_el = root.find("game")
    monkeypatch.setattr(gametdb, "_ensure_wiitdb_index", lambda: {"AAAA01": game_el})
    assert gametdb.get_game_extra_info("AAAA01") is None


def test_get_game_extra_info_cae_a_ingles_solo_en_sinopsis(monkeypatch):
    """La sinopsis cae al inglés si el idioma pedido no la tiene; el título
    NO (mostrar el inglés como si fuera "el título en tu idioma" sería
    engañoso, ver comentario de la función)."""
    root = ET.fromstring(_wiitdb_xml_bytes(GAME_XML_SOLO_EN))
    game_el = root.find("game")
    monkeypatch.setattr(gametdb, "_ensure_wiitdb_index", lambda: {"BBBB01": game_el})

    info = gametdb.get_game_extra_info("BBBB01", language="ES")
    assert info.synopsis == "English synopsis."
    assert info.localized_title is None
    assert info.original_title == "Only English Title"


def test_get_game_extra_info_completo(monkeypatch):
    root = ET.fromstring(_wiitdb_xml_bytes(GAME_XML_COMPLETO))
    game_el = root.find("game")
    monkeypatch.setattr(gametdb, "_ensure_wiitdb_index", lambda: {"RMCP01": game_el})

    info = gametdb.get_game_extra_info("RMCP01", language="ES")
    assert info.genre == "Racing"
    assert info.players == "4"
    assert info.release_date == "2008-04-24"
    assert info.publisher == "Nintendo"
    assert info.developer == "Nintendo EAD"
    assert info.original_title == "Mario Kart Wii"
    assert info.localized_title == "Mario Kart Wii (ES)"
    assert "Corré con tus amigos" in info.synopsis
    assert [c.type for c in info.controls] == ["wheel", "wiimote"]


# ------------------------------------------------------- fetch_extra_info_async
def test_fetch_extra_info_async_id_invalido_no_consulta_indice(monkeypatch):
    monkeypatch.setattr(gametdb, "_ensure_wiitdb_index",
                         lambda: (_ for _ in ()).throw(
                             AssertionError("no debería consultar el índice")))
    resultados = []
    gametdb.fetch_extra_info_async("??????", on_done=resultados.append)
    assert resultados == [None]


def test_fetch_extra_info_async_sin_on_done_no_hace_nada(monkeypatch):
    monkeypatch.setattr(gametdb, "_ensure_wiitdb_index",
                         lambda: (_ for _ in ()).throw(
                             AssertionError("no debería consultar el índice")))
    gametdb.fetch_extra_info_async("RMCP01", on_done=None)
    assert gametdb.extra_info_in_flight() == 0


def test_fetch_extra_info_async_usa_resultado_cacheado_sin_recalcular(monkeypatch):
    """El segundo pedido igual se contesta en el acto y en el hilo que
    llama, sin volver a consultar el índice ni ocupar el worker."""
    cached_info = gametdb.GameExtraInfo(genre="Racing")
    llamadas = []
    monkeypatch.setattr(gametdb, "get_game_extra_info",
                         lambda gid, lang: llamadas.append(1) or cached_info)
    monkeypatch.setattr(gametdb, "wiitdb_index_available", lambda: True)

    primeros = []
    gametdb.fetch_extra_info_async("RMCP01", language="EN", on_done=primeros.append)
    assert _esperar(lambda: len(primeros) == 1)

    # Ahora consultar de nuevo sería un error: el resultado ya se recordó.
    monkeypatch.setattr(gametdb, "get_game_extra_info",
                         lambda *a, **kw: (_ for _ in ()).throw(
                             AssertionError("no debería recalcular")))
    resultados = []
    gametdb.fetch_extra_info_async("RMCP01", language="EN", on_done=resultados.append)

    assert resultados == [cached_info]
    assert len(llamadas) == 1


def test_fetch_extra_info_async_dedupe_pedidos_concurrentes(monkeypatch):
    llamadas = []
    iniciado = threading.Event()
    liberar = threading.Event()

    def fake_get_extra(game_id, language):
        llamadas.append((game_id, language))
        iniciado.set()
        assert liberar.wait(timeout=5), "el test no liberó a tiempo"
        return gametdb.GameExtraInfo(genre="Racing")

    monkeypatch.setattr(gametdb, "get_game_extra_info", fake_get_extra)
    monkeypatch.setattr(gametdb, "wiitdb_index_available", lambda: True)

    resultados = []
    lock = threading.Lock()

    def hacer_callback(nombre):
        def _cb(info):
            with lock:
                resultados.append((nombre, info))
        return _cb

    gametdb.fetch_extra_info_async("RMCP01", language="EN",
                                     on_done=hacer_callback("uno"))
    assert iniciado.wait(timeout=5)
    gametdb.fetch_extra_info_async("RMCP01", language="EN",
                                     on_done=hacer_callback("dos"))
    liberar.set()

    deadline = time.monotonic() + 5
    while gametdb.extra_info_in_flight() > 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(llamadas) == 1
    assert len(resultados) == 2
    assert resultados[0][1] is resultados[1][1]


# La regla de "qué resultado vale la pena recordar" es la única lógica de
# negocio que gametdb le pasa al registry compartido, así que se prueba
# como lo que es: una función suya, y después su cableado de punta a punta.
def test_result_is_final_con_info_encontrada(monkeypatch):
    monkeypatch.setattr(gametdb, "wiitdb_index_available", lambda: False)
    assert gametdb._extra_result_is_final(gametdb.GameExtraInfo(genre="Racing"))


def test_result_is_final_none_con_el_indice_disponible(monkeypatch):
    """None puede significar "GameTDB no tiene este juego" (definitivo) o
    "no se pudo bajar el volcado" (temporal). Con el índice disponible, es
    lo primero: se recuerda."""
    monkeypatch.setattr(gametdb, "wiitdb_index_available", lambda: True)
    assert gametdb._extra_result_is_final(None)


def test_result_is_final_none_sin_el_indice_disponible(monkeypatch):
    """Sin índice disponible (sin internet / servidor caído), el None es
    temporal: NO se recuerda, para poder reintentar cuando vuelva la
    conexión."""
    monkeypatch.setattr(gametdb, "wiitdb_index_available", lambda: False)
    assert not gametdb._extra_result_is_final(None)


def test_un_none_definitivo_se_recuerda_y_no_se_vuelve_a_consultar(monkeypatch):
    """Cableado de punta a punta de la regla anterior: con el índice
    disponible, el segundo pedido igual se contesta con lo recordado sin
    volver a consultar."""
    llamadas = []
    monkeypatch.setattr(gametdb, "get_game_extra_info",
                         lambda gid, lang: llamadas.append(1) or None)
    monkeypatch.setattr(gametdb, "wiitdb_index_available", lambda: True)

    primeros = []
    gametdb.fetch_extra_info_async("ZZZZ99", language="EN", on_done=primeros.append)
    assert _esperar(lambda: len(primeros) == 1)

    segundos = []
    gametdb.fetch_extra_info_async("ZZZZ99", language="EN", on_done=segundos.append)

    assert segundos == [None]      # contestado en el acto, sin pasar por el pool
    assert len(llamadas) == 1      # no se volvió a consultar


def test_un_none_temporal_no_se_recuerda_y_se_reintenta(monkeypatch):
    """La otra mitad: sin índice disponible el None no se recuerda, así
    que un pedido posterior vuelve a intentar de verdad."""
    llamadas = []
    monkeypatch.setattr(gametdb, "get_game_extra_info",
                         lambda gid, lang: llamadas.append(1) or None)
    monkeypatch.setattr(gametdb, "wiitdb_index_available", lambda: False)

    primeros = []
    gametdb.fetch_extra_info_async("ZZZZ99", language="EN", on_done=primeros.append)
    assert _esperar(lambda: len(primeros) == 1)

    segundos = []
    gametdb.fetch_extra_info_async("ZZZZ99", language="EN", on_done=segundos.append)
    assert _esperar(lambda: len(segundos) == 1)

    assert segundos == [None]
    assert len(llamadas) == 2      # se reintentó


def test_fetch_extra_info_async_excepcion_inesperada_da_none(monkeypatch):
    def fake_get_extra(gid, lang):
        raise RuntimeError("boom")

    monkeypatch.setattr(gametdb, "get_game_extra_info", fake_get_extra)
    monkeypatch.setattr(gametdb, "wiitdb_index_available", lambda: True)
    resultados = []

    gametdb.fetch_extra_info_async("RMCP01", language="EN", on_done=resultados.append)

    assert _esperar(lambda: len(resultados) == 1)
    assert resultados == [None]
    assert gametdb.extra_info_in_flight() == 0
