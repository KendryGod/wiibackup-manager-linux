"""Lista blanca de descarga (`oscwii_client`), y sus dos usuarios.

Ni `app.zip_url` ni `app.icon_url` las elige esta app: salen del catálogo
que devuelve la API de Open Shop Channel (o del caché en disco de ese
catálogo), o sea contenido de red. Si ese catálogo cambia -o queda
comprometido- una entrada puede apuntar a cualquier dominio, y antes de
este arreglo tanto el instalador (bajando el ZIP que después extrae) como
el cliente (bajando el ícono de la tarjeta) las pedían igual. Peor:
urllib sigue las redirecciones por su cuenta, así que ni siquiera hacía
falta que la URL del catálogo fuera la sospechosa, alcanzaba con un
`Location:`.

La validación es una sola (`oscwii_client.url_rejection_reason` +
`open_allowlisted`) justamente para que no haya dos listas que se
desincronicen; estas pruebas la ejercitan por los dos caminos.

Las pruebas de acá abajo levantan un servidor HTTP de verdad en 127.0.0.1
(con redirecciones de verdad) en vez de simular urllib: lo que se quiere
probar es justamente el comportamiento de urllib al seguir un salto, que
un mock reproduciría según lo que uno ya cree que hace. Para eso esos
tests amplían a mano las constantes de la lista blanca (127.0.0.1 y http)
-lo que se prueba es el mecanismo-; que la lista REAL sea solo https y
solo el host de OSC lo cubren los tests de `_url_rejection_reason`, que
corren contra las constantes tal como quedan en producción.
"""
from __future__ import annotations

import http.server
import threading
import zipfile

import pytest

from wiibackup_manager import config, oscwii_client, oscwii_installer
from wiibackup_manager.oscwii_client import HomebrewApp, UnsafeDownloadURL
from wiibackup_manager.oscwii_installer import InstallStatus

ZIP_ENTRIES = {
    "apps/TestApp/boot.dol": b"A" * 100,
    "apps/TestApp/meta.xml": b"<app/>",
}


def _zip_bytes(tmp_path) -> bytes:
    path = tmp_path / "fuente.zip"
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in ZIP_ENTRIES.items():
            zf.writestr(name, data)
    return path.read_bytes()


def _fake_app(zip_url: str = "https://hbb1.oscwii.org/api/contents/T/T.zip",
              icon_url: str = "") -> HomebrewApp:
    return HomebrewApp(slug="TestApp", name="Test App", zip_url=zip_url,
                       icon_url=icon_url)


# ------------------------------------------------ _url_rejection_reason --
# Contra las constantes REALES: acá no se toca la lista blanca.
def test_la_url_real_del_catalogo_se_acepta():
    assert oscwii_client.url_rejection_reason(
        "https://hbb1.oscwii.org/api/contents/WiiDonut/WiiDonut.zip") is None


def test_el_host_de_la_api_esta_en_la_lista():
    """La lista se deriva de `oscwii_client.OSC_API_BASE`: si algún día se
    apunta el cliente a otro servidor, la lista de descarga lo acompaña en
    vez de quedar desactualizada en silencio."""
    assert (oscwii_client.OSC_API_BASE.split("//")[1]
            in oscwii_client.ALLOWED_DOWNLOAD_HOSTS)


def test_http_sin_tls_se_rechaza():
    motivo = oscwii_client.url_rejection_reason(
        "http://hbb1.oscwii.org/api/contents/TestApp/TestApp.zip")
    assert motivo is not None
    assert "esquema" in motivo


def test_otro_host_se_rechaza():
    motivo = oscwii_client.url_rejection_reason(
        "https://atacante.example/api/contents/TestApp/TestApp.zip")
    assert motivo is not None
    assert "atacante.example" in motivo


def test_un_host_permitido_como_prefijo_de_otro_se_rechaza():
    """"hbb1.oscwii.org.atacante.example" empieza igual que el host bueno:
    la comparación es por host completo, no por prefijo ni por "termina
    en oscwii.org"."""
    assert oscwii_client.url_rejection_reason(
        "https://hbb1.oscwii.org.atacante.example/x.zip") is not None
    assert oscwii_client.url_rejection_reason(
        "https://noesoscwii.org/x.zip") is not None


def test_credenciales_embebidas_no_disfrazan_el_host():
    """"https://hbb1.oscwii.org@atacante.example/x.zip" va a
    atacante.example aunque a ojo parezca del host bueno."""
    motivo = oscwii_client.url_rejection_reason(
        "https://hbb1.oscwii.org@atacante.example/x.zip")
    assert motivo is not None


def test_esquemas_raros_se_rechazan():
    for url in ("file:///etc/passwd", "ftp://hbb1.oscwii.org/x.zip",
                "data:application/zip;base64,UEsDBA==", "hbb1.oscwii.org/x.zip",
                ""):
        assert oscwii_client.url_rejection_reason(url) is not None, url


def test_el_punto_final_del_host_no_rechaza():
    """"hbb1.oscwii.org." es el mismo host (FQDN absoluto) y se acepta."""
    assert oscwii_client.url_rejection_reason(
        "https://hbb1.oscwii.org./api/contents/TestApp/TestApp.zip") is None


# ------------------------------------------- Servidor HTTP de verdad --
PNG_DE_MENTIRA = b"\x89PNG\r\n\x1a\n" + b"contenido-de-mentira"
PNG_DEL_IMPOSTOR = b"\x89PNG\r\n\x1a\n" + b"vengo-de-otro-host"


class _Handler(http.server.BaseHTTPRequestHandler):
    """Sirve el ZIP en /TestApp.zip, el ícono en /icon.png, y dos
    redirecciones: una que se queda en el mismo host y otra que se va a un
    dominio que no está en la lista."""

    zip_bytes = b""

    def _otro_host(self, ruta: str) -> str:
        """Una URL al MISMO servidor pero por un nombre de host que no
        está en la lista blanca. Es lo que hace fuerte a los tests de
        redirección: el destino existe y contesta, así que si la lista no
        se aplicara la descarga saldría bien y el test fallaría. Con un
        host inventado (que no resuelve) el rechazo se confundiría con un
        error de DNS -eso se prueba aparte, en el test del `.invalid`."""
        return f"http://localhost:{self.server.server_address[1]}{ruta}"

    def do_GET(self):
        if self.path == "/TestApp.zip":
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(self.zip_bytes)))
            self.end_headers()
            self.wfile.write(self.zip_bytes)
        elif self.path == "/icon.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG_DE_MENTIRA)))
            self.end_headers()
            self.wfile.write(PNG_DE_MENTIRA)
        elif self.path == "/redir-interno":
            self.send_response(302)
            self.send_header("Location", "/TestApp.zip")
            self.end_headers()
        elif self.path == "/icon-del-impostor.png":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG_DEL_IMPOSTOR)))
            self.end_headers()
            self.wfile.write(PNG_DEL_IMPOSTOR)
        elif self.path == "/redir-externo":
            self.send_response(302)
            self.send_header("Location", self._otro_host("/TestApp.zip"))
            self.end_headers()
        elif self.path == "/redir-icono-externo":
            self.send_response(302)
            self.send_header("Location",
                             self._otro_host("/icon-del-impostor.png"))
            self.end_headers()
        elif self.path == "/redir-inalcanzable":
            self.send_response(302)
            self.send_header("Location",
                             "http://host-no-permitido.invalid/TestApp.zip")
            self.end_headers()
        else:
            self.send_error(404)

    def log_message(self, *args):  # sin ruido en la salida de pytest
        pass


@pytest.fixture
def servidor(tmp_path, monkeypatch):
    """Servidor HTTP real en 127.0.0.1, con la lista blanca ampliada para
    aceptarlo (ver el docstring del módulo). Devuelve la base de la URL."""
    _Handler.zip_bytes = _zip_bytes(tmp_path)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    hilo = threading.Thread(target=httpd.serve_forever, daemon=True)
    hilo.start()

    # Un proxy configurado en el entorno del que corre la suite mandaría
    # estos pedidos a otro lado (urllib respeta http_proxy).
    for var in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("no_proxy", "*")

    monkeypatch.setattr(oscwii_client, "ALLOWED_URL_SCHEMES",
                        frozenset({"http"}))
    monkeypatch.setattr(oscwii_client, "ALLOWED_DOWNLOAD_HOSTS",
                        frozenset({"127.0.0.1"}))
    # La caché de íconos vive bajo `config.CACHE_DIR`; aislarla es lo
    # mismo que hace el fixture de test_oscwii_client.py.
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        hilo.join(timeout=5)


def test_descarga_desde_un_host_permitido_funciona(servidor, tmp_path):
    """No-regresión: con la URL permitida, la instalación sigue siendo la
    de siempre -incluida la descarga de verdad por la red."""
    dest_root = tmp_path / "usb"
    dest_root.mkdir()

    result = oscwii_installer.install_app(
        _fake_app(f"{servidor}/TestApp.zip"), dest_root)

    assert result.ok, result.error
    assert (dest_root / "apps" / "TestApp" / "boot.dol").read_bytes() == b"A" * 100
    assert (dest_root / "apps" / "TestApp" / "meta.xml").read_bytes() == b"<app/>"


def test_una_redireccion_dentro_de_la_lista_se_sigue(servidor, tmp_path):
    """Redirigir NO está prohibido: lo que importa es a dónde. Un salto
    que termina en un host permitido tiene que funcionar igual que si la
    URL hubiera sido la final desde el principio (OSC podría redirigir a
    un mirror suyo en cualquier momento)."""
    dest_root = tmp_path / "usb"
    dest_root.mkdir()

    result = oscwii_installer.install_app(
        _fake_app(f"{servidor}/redir-interno"), dest_root)

    assert result.ok, result.error
    assert (dest_root / "apps" / "TestApp" / "boot.dol").read_bytes() == b"A" * 100


def test_una_redireccion_fuera_de_la_lista_se_rechaza(servidor, tmp_path):
    """La URL inicial es del host bueno y solo el destino del salto es el
    de afuera: sin validar cada redirección, esto pasaba entero.

    El salto apunta al MISMO servidor por otro nombre de host, o sea que
    contesta con un ZIP perfectamente válido: si la lista blanca no se
    aplicara a la redirección, esta instalación terminaría en OK."""
    dest_root = tmp_path / "usb"
    dest_root.mkdir()

    result = oscwii_installer.install_app(
        _fake_app(f"{servidor}/redir-externo"), dest_root)

    assert not result.ok
    assert result.status is InstallStatus.UNSAFE_URL
    assert "localhost" in result.error
    assert not (dest_root / "apps" / "TestApp").exists()


def test_la_redireccion_se_corta_antes_de_conectarse_al_destino(servidor, tmp_path):
    """El salto se rechaza en `redirect_request`, o sea ANTES de pedirle
    nada al host nuevo: por eso el destino puede ser un dominio .invalid
    (que ni siquiera resuelve) y el error igual es el de la lista blanca y
    no uno de DNS."""
    dest_root = tmp_path / "usb"
    dest_root.mkdir()

    result = oscwii_installer.install_app(
        _fake_app(f"{servidor}/redir-inalcanzable"), dest_root)

    assert result.status is InstallStatus.UNSAFE_URL
    assert "host-no-permitido.invalid" in result.error


# ----------------------------------------- Rechazo antes de tocar nada --
def test_install_app_rechaza_un_host_no_permitido_sin_escribir_nada(tmp_path,
                                                                    monkeypatch):
    """Con las constantes reales: una entrada del catálogo que apunta a
    otro dominio se corta antes de la descarga y antes de crear siquiera
    `dest_root/apps`."""
    dest_root = tmp_path / "usb"
    dest_root.mkdir()

    def _no_deberia_descargar(*_a, **_k):
        raise AssertionError("no se tendría que haber intentado ninguna descarga")
    monkeypatch.setattr(oscwii_installer, "_download_zip", _no_deberia_descargar)

    result = oscwii_installer.install_app(
        _fake_app("https://atacante.example/api/contents/TestApp/TestApp.zip"),
        dest_root)

    assert not result.ok
    assert result.status is InstallStatus.UNSAFE_URL
    assert "atacante.example" in result.error
    assert list(dest_root.iterdir()) == []


def test_download_zip_no_crea_el_archivo_destino_si_la_url_no_pasa(tmp_path):
    """Defensa en profundidad: aunque a `_download_zip` se la llame
    directo (sin el chequeo temprano de `install_app`), corta antes de
    abrir el socket y antes de crear el archivo de destino."""
    destino = tmp_path / "TestApp.zip"

    with pytest.raises(UnsafeDownloadURL):
        oscwii_installer._download_zip(
            _fake_app("https://atacante.example/TestApp.zip"), destino,
            None, None)

    assert not destino.exists()


# ============================================================================
# La misma lista blanca por el otro camino: los íconos del catálogo
# ============================================================================
# `get_icon_path` baja `app.icon_url`, que sale del mismo catálogo que
# `zip_url`. Un ícono es cosmético -si falla, la tarjeta se queda con el
# placeholder- así que acá el rechazo se ve como un None; lo que importa
# es que la descarga no ocurra.

def test_icono_desde_un_host_permitido_se_baja(servidor):
    """No-regresión: con la URL permitida, el ícono se baja y se cachea
    igual que siempre."""
    app = _fake_app(icon_url=f"{servidor}/icon.png")

    resultado = oscwii_client.get_icon_path(app)

    assert resultado is not None
    assert resultado.read_bytes() == PNG_DE_MENTIRA


def test_icono_con_redireccion_fuera_de_la_lista_no_se_baja(servidor):
    """La URL inicial es del host bueno y solo el salto se va afuera:
    mismo agujero que tenía la descarga del ZIP, cerrado por el mismo
    handler.

    Igual que en el test del ZIP, el destino del salto contesta de verdad
    -y con un PNG válido, así que pasaría la validación de firma-: sin la
    lista blanca este ícono se bajaría y se cachearía."""
    app = _fake_app(icon_url=f"{servidor}/redir-icono-externo")

    assert oscwii_client.get_icon_path(app) is None
    assert not oscwii_client.icon_cache_path(app.slug).exists()


def test_icono_de_un_host_no_permitido_no_toca_la_red(tmp_path, monkeypatch):
    """Con las constantes reales: una `icon_url` a otro dominio se corta
    antes de abrir el socket y sin crear siquiera la carpeta de caché."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")

    def _no_deberia_abrir(*_a, **_k):
        raise AssertionError("no se tendría que haber intentado ninguna descarga")
    monkeypatch.setattr(oscwii_client, "open_allowlisted", _no_deberia_abrir)

    app = _fake_app(icon_url="https://atacante.example/api/contents/T/icon.png")

    assert oscwii_client.get_icon_path(app) is None
    assert not oscwii_client.icons_cache_dir().exists()


def test_icono_por_http_sin_tls_no_se_baja(tmp_path, monkeypatch):
    """El mismo host de OSC pero sin TLS tampoco pasa: un ícono en claro
    lo puede cambiar cualquiera en el camino."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(oscwii_client, "open_allowlisted",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("no debería llamar a la red")))

    app = _fake_app(icon_url="http://hbb1.oscwii.org/api/contents/T/icon.png")

    assert oscwii_client.get_icon_path(app) is None


def test_el_icono_cacheado_se_sigue_devolviendo(tmp_path, monkeypatch):
    """No-regresión: el chequeo va después del acierto de caché, así que
    un ícono ya bajado se sigue mostrando sin salir a la red."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(oscwii_client, "open_allowlisted",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            AssertionError("no debería llamar a la red")))

    app = _fake_app(icon_url="https://hbb1.oscwii.org/api/contents/T/icon.png")
    cache_path = oscwii_client.icon_cache_path(app.slug)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(PNG_DE_MENTIRA)

    assert oscwii_client.get_icon_path(app) == cache_path
