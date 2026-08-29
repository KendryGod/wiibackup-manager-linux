"""Pruebas de `oscwii_client.py`.

Mismo criterio que `test_gametdb.py`: no existía ningún test dedicado a
este módulo (`HomebrewApp` solo se usaba como fixture en otros archivos),
así que se cubre desde cero priorizando el manejo de errores -timeouts,
JSON/ZIP mal formados, caída de la API con fallback a caché, dedupe de
descargas concurrentes- por sobre el camino feliz.

Nada toca la red de verdad: se fakea `urllib.request.urlopen` con objetos
simples (sin `unittest.mock`), y `config.CACHE_DIR` se aísla a `tmp_path`
en cada test."""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from wiibackup_manager import config, oscwii_client
from wiibackup_manager.oscwii_client import HomebrewApp


# --------------------------------------------------------------- fixtures --
@pytest.fixture(autouse=True)
def _sandbox_oscwii_client(tmp_path, monkeypatch):
    """Aísla la caché en disco y el estado en memoria del módulo (los
    registries de pedidos en vuelo son módulo-nivel y persisten entre
    tests dentro del mismo proceso si no se limpian)."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    oscwii_client._list_jobs.forget()
    oscwii_client._icon_jobs.forget()
    yield
    oscwii_client._list_jobs.forget()
    oscwii_client._icon_jobs.forget()


# ------------------------------------------------------------- utilidades --
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


def _api_dict(**overrides) -> dict:
    """Elemento de la respuesta de la API, con la forma documentada en el
    docstring del módulo. `overrides` reemplaza claves de primer nivel
    enteras (ej. `url={...}` reemplaza todo el sub-dict `url`)."""
    d = {
        "slug": "WiiDonut",
        "name": "Wii Donut",
        "author": "jornmann, Andy Sloane",
        "category": "demos",
        "description": {"short": "corto", "long": "largo"},
        "file_size": {"binary": 430656, "icon": 2404,
                      "zip_compressed": 226230, "zip_uncompressed": 433665},
        "release_date": 1644364800,
        "supported_platforms": ["wii", "vwii", "wii_mini"],
        "peripherals": ["Wii Remote"],
        "url": {"icon": "https://hbb1.oscwii.org/api/contents/WiiDonut/icon.png",
                "zip": "https://hbb1.oscwii.org/api/contents/WiiDonut/WiiDonut.zip"},
        "version": "1.0.0",
    }
    d.update(overrides)
    return d


def _app(slug="WiiDonut",
         icon_url="https://hbb1.oscwii.org/api/contents/WiiDonut/icon.png") -> HomebrewApp:
    return HomebrewApp(slug=slug, name=slug, icon_url=icon_url)


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


# ============================================================================
# Bloque A: HomebrewApp.from_api_dict / _parse_apps
# ============================================================================

def test_from_api_dict_no_es_dict():
    assert HomebrewApp.from_api_dict("no soy un dict") is None
    assert HomebrewApp.from_api_dict(["lista"]) is None
    assert HomebrewApp.from_api_dict(None) is None


def test_from_api_dict_sin_slug():
    d = _api_dict()
    del d["slug"]
    assert HomebrewApp.from_api_dict(d) is None


def test_from_api_dict_slug_vacio_o_espacios():
    assert HomebrewApp.from_api_dict(_api_dict(slug="   ")) is None


def test_from_api_dict_sin_name():
    d = _api_dict()
    del d["name"]
    assert HomebrewApp.from_api_dict(d) is None


def test_from_api_dict_name_vacio():
    assert HomebrewApp.from_api_dict(_api_dict(name="")) is None


def test_from_api_dict_url_no_es_dict():
    assert HomebrewApp.from_api_dict(_api_dict(url="no soy un dict")) is None


def test_from_api_dict_sin_zip_url():
    """Sin URL de ZIP la app no se puede instalar: no vale mostrarla."""
    d = _api_dict(url={"icon": "https://x/icon.png"})
    assert HomebrewApp.from_api_dict(d) is None


def test_from_api_dict_zip_url_vacio():
    assert HomebrewApp.from_api_dict(_api_dict(url={"zip": "   "})) is None


def test_from_api_dict_campos_opcionales_con_tipo_incorrecto_usan_default():
    d = _api_dict(author=123, category=None, description="no es dict",
                  file_size="tampoco es dict")
    app = HomebrewApp.from_api_dict(d)
    assert app is not None
    assert app.author == ""
    assert app.category == ""
    assert app.short_description == ""
    assert app.long_description == ""
    assert app.zip_compressed_size is None
    assert app.zip_uncompressed_size is None


def test_from_api_dict_release_date_bool_no_es_valido():
    """bool es subclase de int en Python: un `true`/`false` de la API no
    puede colarse como epoch (mismo caso que `config.Settings.load`)."""
    app = HomebrewApp.from_api_dict(_api_dict(release_date=True))
    assert app.release_date is None


def test_from_api_dict_supported_platforms_no_lista_da_vacio():
    app = HomebrewApp.from_api_dict(_api_dict(supported_platforms="wii"))
    assert app.supported_platforms == ()


def test_from_api_dict_supported_platforms_filtra_elementos_no_string():
    app = HomebrewApp.from_api_dict(
        _api_dict(supported_platforms=["wii", 123, None, "vwii"]))
    assert app.supported_platforms == ("wii", "vwii")


def test_from_api_dict_completo():
    app = HomebrewApp.from_api_dict(_api_dict())
    assert app.slug == "WiiDonut"
    assert app.name == "Wii Donut"
    assert app.zip_url.endswith("WiiDonut.zip")
    assert app.zip_compressed_size == 226230
    assert app.zip_uncompressed_size == 433665
    assert app.supported_platforms == ("wii", "vwii", "wii_mini")
    assert app.peripherals == ("Wii Remote",)
    assert app.short_description == "corto"
    assert app.long_description == "largo"


def test_to_cache_dict_round_trip():
    original = HomebrewApp.from_api_dict(_api_dict())
    reconstruido = HomebrewApp.from_api_dict(original.to_cache_dict())
    assert reconstruido == original


def test_parse_apps_json_invalido():
    apps, err = oscwii_client._parse_apps(b"{esto no es json")
    assert apps is None
    assert "JSON inv" in err


def test_parse_apps_no_es_lista():
    apps, err = oscwii_client._parse_apps(json.dumps({"no": "es lista"}).encode())
    assert apps is None
    assert "lista" in err


def test_parse_apps_lista_vacia_es_valida():
    """Distinto de "corrupto": un catálogo vacío de verdad se acepta."""
    apps, err = oscwii_client._parse_apps(b"[]")
    assert apps == ()
    assert err is None


def test_parse_apps_ningun_elemento_valido():
    raw = json.dumps([{"foo": "bar"}, {"slug": "", "name": "x"}]).encode()
    apps, err = oscwii_client._parse_apps(raw)
    assert apps is None
    assert "ningún elemento" in err


def test_parse_apps_mezcla_de_validos_e_invalidos():
    raw = json.dumps([_api_dict(slug="A"), {"foo": "bar"},
                       _api_dict(slug="B")]).encode()
    apps, err = oscwii_client._parse_apps(raw)
    assert err is None
    assert [a.slug for a in apps] == ["A", "B"]


# ============================================================================
# Bloque B: _fetch_remote / _load_cache / _store_cache / list_apps
# ============================================================================

def test_fetch_remote_http_error(monkeypatch):
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda *a, **kw: (_ for _ in ()).throw(
            urllib.error.HTTPError("http://x", 500, "boom", None, None)))
    apps, err = oscwii_client._fetch_remote()
    assert apps is None
    assert err == "HTTP 500"


@pytest.mark.parametrize("excepcion", [
    urllib.error.URLError("sin red"),
    TimeoutError("se colgó"),
    OSError("desconectado"),
])
def test_fetch_remote_error_de_red(monkeypatch, excepcion):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: (_ for _ in ()).throw(excepcion))
    apps, err = oscwii_client._fetch_remote()
    assert apps is None
    assert err


def test_fetch_remote_status_no_200(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(503, b""))
    apps, err = oscwii_client._fetch_remote()
    assert apps is None
    assert "503" in err


def test_fetch_remote_json_malformado(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, b"no es json"))
    apps, err = oscwii_client._fetch_remote()
    assert apps is None
    assert "JSON inv" in err


def test_fetch_remote_exitoso(monkeypatch):
    raw = json.dumps([_api_dict()]).encode()
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, raw))
    apps, err = oscwii_client._fetch_remote()
    assert err is None
    assert len(apps) == 1
    assert apps[0].slug == "WiiDonut"


def test_load_cache_sin_archivo():
    apps, err = oscwii_client._load_cache()
    assert apps is None
    assert "no hay" in err


def test_load_cache_falla_lectura(monkeypatch):
    path = oscwii_client.contents_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"[]")

    def boom_read_bytes(self, *a, **kw):
        raise OSError("permiso denegado")

    monkeypatch.setattr(Path, "read_bytes", boom_read_bytes)
    apps, err = oscwii_client._load_cache()
    assert apps is None
    assert "permiso" in err


def test_load_cache_json_corrupto():
    path = oscwii_client.contents_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"esto no es json")
    apps, err = oscwii_client._load_cache()
    assert apps is None
    assert "JSON inv" in err


def test_load_cache_exitoso():
    path = oscwii_client.contents_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps([_api_dict()]).encode())
    apps, err = oscwii_client._load_cache()
    assert err is None
    assert len(apps) == 1


def test_store_cache_falla_escritura_no_propaga(monkeypatch):
    app = HomebrewApp.from_api_dict(_api_dict())

    def boom_write_text_atomic(path, payload):
        raise OSError("disco lleno")

    monkeypatch.setattr(config, "write_text_atomic", boom_write_text_atomic)
    oscwii_client._store_cache((app,))  # no debe lanzar


def test_store_cache_exitoso_deja_el_archivo():
    app = HomebrewApp.from_api_dict(_api_dict())
    oscwii_client._store_cache((app,))
    data = json.loads(oscwii_client.contents_cache_path().read_bytes())
    assert data == [app.to_cache_dict()]


def test_list_apps_api_ok_actualiza_cache(monkeypatch):
    app = HomebrewApp.from_api_dict(_api_dict())
    monkeypatch.setattr(oscwii_client, "_fetch_remote", lambda: ((app,), None))

    result = oscwii_client.list_apps()

    assert result.status == oscwii_client.FetchStatus.OK
    assert result.apps == (app,)
    cached = json.loads(oscwii_client.contents_cache_path().read_bytes())
    assert cached == [app.to_cache_dict()]


def test_list_apps_api_ok_con_lista_vacia_no_es_error(monkeypatch):
    """`()` es un catálogo real, no un fallo: no debe disparar el
    fallback a caché."""
    monkeypatch.setattr(oscwii_client, "_fetch_remote", lambda: ((), None))
    monkeypatch.setattr(
        oscwii_client, "_load_cache",
        lambda: (_ for _ in ()).throw(AssertionError("no debería consultar la caché")))

    result = oscwii_client.list_apps()
    assert result.status == oscwii_client.FetchStatus.OK
    assert result.apps == ()


def test_list_apps_api_falla_usa_cache_valida(monkeypatch):
    cacheada = HomebrewApp.from_api_dict(_api_dict(slug="Cacheada"))
    monkeypatch.setattr(oscwii_client, "_fetch_remote", lambda: (None, "sin red"))
    monkeypatch.setattr(oscwii_client, "_load_cache", lambda: ((cacheada,), None))

    result = oscwii_client.list_apps()
    assert result.status == oscwii_client.FetchStatus.STALE_CACHE
    assert result.apps == (cacheada,)
    assert result.error == "sin red"  # el motivo de la API, no el de la caché


def test_list_apps_sin_api_ni_cache_da_error(monkeypatch):
    monkeypatch.setattr(oscwii_client, "_fetch_remote", lambda: (None, "sin red"))
    monkeypatch.setattr(oscwii_client, "_load_cache", lambda: (None, "no hay caché"))

    result = oscwii_client.list_apps()
    assert result.status == oscwii_client.FetchStatus.ERROR
    assert result.apps == ()
    assert result.error == "sin red"


def test_list_apps_fetch_remote_excepcion_inesperada(monkeypatch):
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(oscwii_client, "_fetch_remote", boom)
    monkeypatch.setattr(oscwii_client, "_load_cache", lambda: (None, "no hay caché"))

    result = oscwii_client.list_apps()
    assert result.status == oscwii_client.FetchStatus.ERROR
    assert "boom" in result.error


def test_list_apps_load_cache_excepcion_inesperada(monkeypatch):
    monkeypatch.setattr(oscwii_client, "_fetch_remote", lambda: (None, "sin red"))

    def boom():
        raise RuntimeError("boom-cache")

    monkeypatch.setattr(oscwii_client, "_load_cache", boom)

    result = oscwii_client.list_apps()
    assert result.status == oscwii_client.FetchStatus.ERROR
    assert result.error == "sin red"


# ============================================================================
# Bloque C: fetch_apps_async
# ============================================================================

def test_fetch_apps_async_sin_on_done_no_dispara_nada(monkeypatch):
    monkeypatch.setattr(
        oscwii_client, "list_apps",
        lambda: (_ for _ in ()).throw(AssertionError("no debería llamarse")))
    oscwii_client.fetch_apps_async(on_done=None)
    assert oscwii_client._list_jobs.in_flight() == 0


def test_fetch_apps_async_dedupe_pedidos_concurrentes(monkeypatch):
    llamadas = []
    iniciado = threading.Event()
    liberar = threading.Event()
    resultado = oscwii_client.AppListResult(status=oscwii_client.FetchStatus.OK, apps=())

    def fake_list_apps():
        llamadas.append(1)
        iniciado.set()
        assert liberar.wait(timeout=5), "el test no liberó a tiempo"
        return resultado

    monkeypatch.setattr(oscwii_client, "list_apps", fake_list_apps)

    resultados = []
    lock = threading.Lock()

    def hacer_callback(nombre):
        def _cb(r):
            with lock:
                resultados.append((nombre, r))
        return _cb

    oscwii_client.fetch_apps_async(on_done=hacer_callback("uno"))
    assert iniciado.wait(timeout=5)
    oscwii_client.fetch_apps_async(on_done=hacer_callback("dos"))
    liberar.set()

    deadline = time.monotonic() + 5
    while len(resultados) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(llamadas) == 1
    assert resultados[0][1] is resultado
    assert resultados[1][1] is resultado


def test_list_error_result_mapea_la_excepcion(monkeypatch):
    """El cableado propio de este módulo: a diferencia de las carátulas y
    los íconos (que se resuelven como None), un fallo inesperado de
    `list_apps` tiene que llegar a la interfaz como un `AppListResult` de
    error con el motivo adentro."""
    resultado = oscwii_client._list_error_result(RuntimeError("boom"))
    assert resultado.status == oscwii_client.FetchStatus.ERROR
    assert resultado.apps == ()
    assert "boom" in resultado.error


def test_fetch_apps_async_excepcion_inesperada_da_error(monkeypatch):
    """Y el mismo cableado de punta a punta: `list_apps` ya captura por su
    cuenta todo lo previsto, así que esto cubre un error de programación
    que se escape, que no puede quedar perdido en un hilo del pool."""
    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(oscwii_client, "list_apps", boom)
    resultados = []

    oscwii_client.fetch_apps_async(on_done=resultados.append)

    assert _esperar(lambda: len(resultados) == 1)
    assert resultados[0].status == oscwii_client.FetchStatus.ERROR
    assert "boom" in resultados[0].error
    assert oscwii_client._list_jobs.in_flight() == 0


# ============================================================================
# Bloque D: íconos
# ============================================================================

def test_icon_cache_path_sanea_slug_peligroso():
    p = oscwii_client.icon_cache_path("../../etc/passwd")
    assert p.name == "etcpasswd.png"
    assert p.parent == oscwii_client.icons_cache_dir()


def test_icon_cache_path_slug_vacio_usa_app():
    p = oscwii_client.icon_cache_path("")
    assert p.name == "app.png"


def test_get_icon_path_sin_icon_url_no_toca_red(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: (_ for _ in ()).throw(
                             AssertionError("no debería llamar a la red")))
    assert oscwii_client.get_icon_path(_app(icon_url="")) is None


@pytest.mark.parametrize("excepcion", [
    urllib.error.HTTPError("http://x", 404, "not found", None, None),
    urllib.error.URLError("sin red"),
    TimeoutError(),
    OSError("desconectado"),
])
def test_get_icon_path_error_de_red(monkeypatch, excepcion):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: (_ for _ in ()).throw(excepcion))
    assert oscwii_client.get_icon_path(_app()) is None


def test_get_icon_path_status_no_200(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(503, b""))
    assert oscwii_client.get_icon_path(_app()) is None


def test_get_icon_path_respuesta_no_es_png(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, b"<html>404</html>"))
    app = _app()
    result = oscwii_client.get_icon_path(app)
    assert result is None
    assert not oscwii_client.icon_cache_path(app.slug).exists()


def test_get_icon_path_exitoso_guarda_y_devuelve(monkeypatch):
    data = oscwii_client.PNG_MAGIC + b"contenido-de-mentira"
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, data))
    result = oscwii_client.get_icon_path(_app())
    assert result.read_bytes() == data


def test_get_icon_path_usa_cache_sin_pedir_red(monkeypatch):
    app = _app()
    cache_path = oscwii_client.icon_cache_path(app.slug)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(oscwii_client.PNG_MAGIC + b"ya cacheado")
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: (_ for _ in ()).throw(
                             AssertionError("no debería llamar a la red")))
    result = oscwii_client.get_icon_path(app)
    assert result == cache_path


def test_get_icon_path_force_ignora_cache_existente(monkeypatch):
    app = _app()
    cache_path = oscwii_client.icon_cache_path(app.slug)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(oscwii_client.PNG_MAGIC + b"viejo")
    nuevo = oscwii_client.PNG_MAGIC + b"nuevo"
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, nuevo))
    result = oscwii_client.get_icon_path(app, force=True)
    assert result.read_bytes() == nuevo


def test_get_icon_path_falla_escritura_temporal(monkeypatch):
    data = oscwii_client.PNG_MAGIC + b"x"
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, data))

    def boom_write_bytes(self, *a, **kw):
        raise OSError("disco lleno")

    monkeypatch.setattr(Path, "write_bytes", boom_write_bytes)
    assert oscwii_client.get_icon_path(_app()) is None


def test_get_icon_path_falla_escritura_y_tambien_falla_la_limpieza(monkeypatch):
    data = oscwii_client.PNG_MAGIC + b"x"
    monkeypatch.setattr(urllib.request, "urlopen",
                         lambda *a, **kw: FakeResponse(200, data))

    def boom_write_bytes(self, *a, **kw):
        raise OSError("disco lleno")

    def boom_unlink(self, *a, **kw):
        raise OSError("también falla el borrado")

    monkeypatch.setattr(Path, "write_bytes", boom_write_bytes)
    monkeypatch.setattr(Path, "unlink", boom_unlink)
    assert oscwii_client.get_icon_path(_app()) is None


def test_fetch_icon_async_sin_on_done_no_hace_nada(monkeypatch):
    monkeypatch.setattr(oscwii_client, "get_icon_path",
                         lambda *a, **kw: (_ for _ in ()).throw(
                             AssertionError("no debería descargar")))
    oscwii_client.fetch_icon_async(_app(), on_done=None)
    assert oscwii_client._icon_jobs.in_flight() == 0


def test_fetch_icon_async_sin_icon_url_da_none_sincronico(monkeypatch):
    monkeypatch.setattr(oscwii_client, "get_icon_path",
                         lambda *a, **kw: (_ for _ in ()).throw(
                             AssertionError("no debería descargar")))
    resultados = []
    oscwii_client.fetch_icon_async(_app(icon_url=""), on_done=resultados.append)
    assert resultados == [None]


def test_fetch_icon_async_usa_cache_sincronico(monkeypatch):
    app = _app()
    cache_path = oscwii_client.icon_cache_path(app.slug)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(oscwii_client.PNG_MAGIC + b"ya cacheado")
    monkeypatch.setattr(oscwii_client, "get_icon_path",
                         lambda *a, **kw: (_ for _ in ()).throw(
                             AssertionError("no debería descargar de nuevo")))
    resultados = []
    oscwii_client.fetch_icon_async(app, on_done=resultados.append)
    assert resultados == [cache_path]


def test_fetch_icon_async_dedupe_pedidos_concurrentes(monkeypatch, tmp_path):
    app = _app()
    llamadas = []
    iniciado = threading.Event()
    liberar = threading.Event()
    destino = tmp_path / "icono.png"

    def fake_get_icon_path(a, force=False):
        llamadas.append(a.slug)
        iniciado.set()
        assert liberar.wait(timeout=5), "el test no liberó a tiempo"
        return destino

    monkeypatch.setattr(oscwii_client, "get_icon_path", fake_get_icon_path)

    resultados = []
    lock = threading.Lock()

    def hacer_callback(nombre):
        def _cb(path):
            with lock:
                resultados.append((nombre, path))
        return _cb

    oscwii_client.fetch_icon_async(app, on_done=hacer_callback("uno"))
    assert iniciado.wait(timeout=5)
    oscwii_client.fetch_icon_async(app, on_done=hacer_callback("dos"))
    liberar.set()

    deadline = time.monotonic() + 5
    while len(resultados) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert len(llamadas) == 1
    assert resultados[0][1] == destino
    assert resultados[1][1] == destino


def test_fetch_icon_async_excepcion_inesperada_da_none(monkeypatch):
    """El ícono es cosmético: un fallo inesperado se resuelve como "no hay
    ícono" (la tarjeta se queda con el placeholder), nunca como una
    excepción perdida en un hilo del pool."""
    def boom(app, force=False):
        raise RuntimeError("boom")

    monkeypatch.setattr(oscwii_client, "get_icon_path", boom)
    resultados = []

    oscwii_client.fetch_icon_async(_app(), on_done=resultados.append)

    assert _esperar(lambda: len(resultados) == 1)
    assert resultados == [None]
    assert oscwii_client._icon_jobs.in_flight() == 0
