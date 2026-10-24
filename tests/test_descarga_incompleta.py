"""Una descarga de la Store que se corta se informa como tal.

Medido con Python 3.14.8 y un servidor que cierra la conexión antes de
mandar todo:

- Con `Content-Length`, `resp.read(n)` devuelve lo que llegó y después
  `b""`, sin ninguna excepción: `http.client` no compara contra el largo
  anunciado. La Store seguía con el ZIP truncado y decía "El archivo
  descargado está dañado".
- Con `Transfer-Encoding: chunked`, `read(n)` levanta
  `http.client.IncompleteRead`, que no es un `OSError`: caía en el
  `except Exception` final de `install_app` y la Store decía "Error al
  escribir en el destino", que manda a revisar la unidad.

Ahora las dos terminan en `INCOMPLETE_DOWNLOAD`: "Descarga incompleta,
reintentá". Lo esperado sale del `Content-Length` de la respuesta y no del
tamaño que dice el catálogo, que puede estar desactualizado.

Se prueba contra un servidor HTTP local de verdad: lo que importa es lo que
hace `http.client` con la conexión cortada, no lo que haría un doble.
"""
from __future__ import annotations

import gettext
import io
import socket
import threading
import types
import urllib.request
import zipfile
from pathlib import Path

import pytest

from wiibackup_manager import oscwii_installer
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallResult, InstallStatus

MB = 1024 * 1024


def _zip_valido() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("apps/App/meta.xml", b"<app/>")
    return buf.getvalue()


def _servidor(respuesta: bytes) -> str:
    """Un servidor de una sola respuesta: manda `respuesta` tal cual (con
    la cabecera adentro) y cierra la conexión."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)

    def atender():
        conexion, _ = s.accept()
        conexion.recv(65536)
        conexion.sendall(respuesta)
        conexion.close()
        s.close()

    threading.Thread(target=atender, daemon=True).start()
    return f"http://127.0.0.1:{s.getsockname()[1]}/App.zip"


@pytest.fixture
def instalar_desde(monkeypatch, tmp_path):
    """Instala la app bajando el ZIP de un servidor local que responde
    `respuesta`. La lista blanca (https + hbb1.oscwii.org) se saltea solo
    para abrir ESA URL: lo que se prueba es lo que viene después."""
    def _instalar(respuesta: bytes, tamano_en_el_catalogo=None):
        url = _servidor(respuesta)
        monkeypatch.setattr(oscwii_installer, "open_allowlisted",
                            lambda _u, timeout, _q=None: urllib.request.urlopen(url, timeout=timeout))
        destino = tmp_path / "usb"
        destino.mkdir(exist_ok=True)
        app = HomebrewApp(slug="App", name="App",
                          zip_url="https://hbb1.oscwii.org/api/contents/App/App.zip",
                          zip_compressed_size=tamano_en_el_catalogo)
        return oscwii_installer.install_app(app, destino), destino
    return _instalar


def _archivos(raiz: Path) -> list:
    return sorted(str(p.relative_to(raiz)) for p in raiz.rglob("*") if p.is_file())


def test_con_content_length_y_la_conexion_cortada(instalar_desde):
    """Llegan 2 de 3 MB. Con más de la mitad recibida, lo que `http.client`
    dice que falta (1 MB) es menos que lo recibido: comparar contra eso, y
    no contra el largo anunciado, no lo vería."""
    respuesta = (b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % (3 * MB)
                 + b"PK\x03\x04" + b"x" * (2 * MB - 4))

    resultado, destino = instalar_desde(respuesta)

    assert resultado.status is InstallStatus.INCOMPLETE_DOWNLOAD, resultado
    assert "2.0 MB" in resultado.error and "3.0 MB" in resultado.error
    assert "Reintentá" in resultado.error
    assert _archivos(destino) == []


def test_con_chunked_y_la_conexion_cortada(instalar_desde):
    respuesta = (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
                 b"3e8\r\n" + b"x" * 100)

    resultado, destino = instalar_desde(respuesta)

    assert resultado.status is InstallStatus.INCOMPLETE_DOWNLOAD, resultado
    assert resultado.status is not InstallStatus.IO_ERROR
    assert "Reintentá" in resultado.error
    assert _archivos(destino) == []


def test_una_descarga_completa_se_instala(instalar_desde):
    cuerpo = _zip_valido()
    resultado, destino = instalar_desde(
        b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(cuerpo) + cuerpo)

    assert resultado.ok, resultado
    assert _archivos(destino) == ["apps/App/meta.xml"]


@pytest.mark.parametrize("cabecera", [
    b"Content-Length: %d\r\n",
    # Sin largo anunciado (hasta que cierra): no hay contra qué comparar.
    b"Connection: close\r\n",
])
def test_lo_esperado_es_el_content_length_y_no_el_tamano_del_catalogo(instalar_desde, cabecera):
    """El catálogo dice un tamaño y el servidor manda otro, entero: es una
    versión nueva del ZIP, no una descarga cortada."""
    cuerpo = _zip_valido()
    if b"%d" in cabecera:
        cabecera = cabecera % len(cuerpo)
    resultado, _destino = instalar_desde(
        b"HTTP/1.1 200 OK\r\n" + cabecera + b"\r\n" + cuerpo,
        tamano_en_el_catalogo=len(cuerpo) * 10)

    assert resultado.ok, resultado


def _tienda_de_mentira():
    from wiibackup_manager.operations import OperationManager
    toasts = []
    vista = types.SimpleNamespace(
        ops=OperationManager(), op_log=None, _show_toast=toasts.append,
        _install_states={}, _card_by_slug={})
    return vista, toasts


def _mostrar(resultado):
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView
    vista, toasts = _tienda_de_mentira()
    app = HomebrewApp(slug="App", name="App")
    HomebrewStoreView._on_install_done(vista, app, resultado, None, None)
    return vista._install_states["App"].message, toasts


def test_la_tarjeta_dice_descarga_incompleta():
    etiqueta, toasts = _mostrar(InstallResult(
        InstallStatus.INCOMPLETE_DOWNLOAD, "App", "Descarga incompleta: llegaron x de y."))

    assert etiqueta == "Descarga incompleta, reintentá"
    assert "escribir en el destino" not in etiqueta
    assert toasts and "Descarga incompleta" in toasts[0]


def test_en_ingles(monkeypatch, instalar_desde):
    from test_smoke_gui import _poner_el_catalogo_en_ingles
    from wiibackup_manager.widgets.homebrew_store_view import _INSTALL_STATUS_LABELS
    locale = Path(__file__).resolve().parent.parent / "data" / "locale"
    en = gettext.translation("wiibackup-manager", str(locale), languages=["en"])
    _poner_el_catalogo_en_ingles(monkeypatch, en)
    resultado, _destino = instalar_desde(
        b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % (3 * MB) + b"x" * MB)

    assert resultado.error.startswith("Incomplete download"), resultado.error
    # Las etiquetas de la tarjeta se arman al importar la vista, con el
    # idioma que haya en ese momento: acá se mira que estén en el catálogo.
    etiqueta = en.gettext(_INSTALL_STATUS_LABELS[InstallStatus.INCOMPLETE_DOWNLOAD])
    assert etiqueta.startswith("Incomplete download"), etiqueta


def test_una_respuesta_que_no_es_http_es_un_error_de_descarga(instalar_desde):
    """Lo mismo con cualquier `http.client.HTTPException` (acá
    `BadStatusLine`): tampoco es un `OSError`, y no tiene nada que ver con
    la unidad."""
    resultado, destino = instalar_desde(b"ESTO NO ES HTTP\r\n\r\n")

    assert resultado.status is InstallStatus.DOWNLOAD_ERROR, resultado
    assert _archivos(destino) == []
