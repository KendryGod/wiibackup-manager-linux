"""Una instalación de la Store que no llega a instalar no deja nada en la
unidad.

`install_app` creaba `apps/` en el destino ANTES de descargar, para fallar
rápido si no se podía escribir. Si después la descarga fallaba, el paquete
venía roto o no entraba, la carpeta quedaba vacía en la unidad del
cliente. Ahora se comprueba que el destino sea una carpeta escribible sin
crear nada, y las carpetas las crea la transacción (`atomicfs.StagedSwap`)
cuando ya hay algo que poner, y las quita si se deshace.
"""
from __future__ import annotations

import os
import zipfile

import pytest

from homebrew_falso import dol
from wiibackup_manager import oscwii_installer
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import CancelRequested, InstallStatus


def _app() -> HomebrewApp:
    return HomebrewApp(slug="App", name="App",
                       zip_url="https://hbb1.oscwii.org/api/contents/App/App.zip")


def _descarga(monkeypatch, contenido=None, *, falla=None):
    """`_download_zip` escribe `contenido` (un dict de entradas o bytes
    sueltos) en vez de bajar nada; con `falla`, devuelve ese error o lo
    levanta si es una excepción."""
    llamadas = []

    def _falsa(app, destino, cancel_event, on_progress):
        llamadas.append(destino)
        if isinstance(falla, BaseException):
            raise falla
        if falla is not None:
            return False, falla, "", ""
        if isinstance(contenido, bytes):
            destino.write_bytes(contenido)
        else:
            with zipfile.ZipFile(destino, "w") as zf:
                for nombre, datos in contenido.items():
                    zf.writestr(nombre, datos)
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _falsa)
    return llamadas


@pytest.fixture
def unidad(tmp_path):
    raiz = tmp_path / "usb"
    raiz.mkdir()
    return raiz


@pytest.mark.parametrize("caso, contenido, falla, estado", [
    ("descarga", None, "sin conexión", InstallStatus.DOWNLOAD_ERROR),
    ("cancelada", None, CancelRequested(), InstallStatus.CANCELLED),
    ("zip roto", b"esto no es un zip", None, InstallStatus.BAD_ZIP),
    ("fuera de lo permitido", {"apps/App/boot.dol": dol(b"A"),
                               "otra_carpeta/x.txt": b"x"}, None,
     InstallStatus.UNSAFE_ZIP),
    ("ejecutable dañado", {"apps/App/boot.dol": b"404: Not Found"}, None,
     InstallStatus.BAD_CONTENT),
])
def test_un_fallo_antes_de_instalar_no_deja_nada(monkeypatch, unidad, caso,
                                                  contenido, falla, estado):
    _descarga(monkeypatch, contenido, falla=falla)

    resultado = oscwii_installer.install_app(_app(), unidad)

    assert resultado.status is estado, resultado
    assert os.listdir(unidad) == [], caso


def test_sin_espacio_no_deja_nada(monkeypatch, unidad):
    _descarga(monkeypatch, {"apps/App/boot.dol": dol(b"A")})
    monkeypatch.setattr(oscwii_installer.transfer_plan, "free_space", lambda _p: 0)

    resultado = oscwii_installer.install_app(_app(), unidad)

    assert resultado.status is InstallStatus.NO_SPACE, resultado
    assert os.listdir(unidad) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root escribe igual sin permisos")
def test_un_destino_sin_permiso_falla_antes_de_descargar(monkeypatch, unidad):
    llamadas = _descarga(monkeypatch, {"apps/App/boot.dol": dol(b"A")})
    unidad.chmod(0o555)
    try:
        resultado = oscwii_installer.install_app(_app(), unidad)
    finally:
        unidad.chmod(0o755)

    assert resultado.status is InstallStatus.IO_ERROR, resultado
    assert str(unidad) in resultado.error
    assert llamadas == []
    assert os.listdir(unidad) == []


def test_un_destino_que_no_existe_falla_antes_de_descargar(monkeypatch, tmp_path):
    """Antes se creaba entero (`mkdir(parents=True)`) y la app se instalaba
    en una carpeta nueva del disco de la PC. La carpeta elegida que ya no
    está es, en la Store, una unidad que se desenchufó
    (`drives.device_is_gone`)."""
    llamadas = _descarga(monkeypatch, {"apps/App/boot.dol": dol(b"A")})
    destino = tmp_path / "no-existe"

    resultado = oscwii_installer.install_app(_app(), destino)

    assert resultado.status is InstallStatus.DEVICE_DISCONNECTED, resultado
    assert llamadas == []
    assert not destino.exists()


def test_una_instalacion_buena_crea_apps(monkeypatch, unidad):
    _descarga(monkeypatch, {"apps/App/boot.dol": dol(b"A"),
                            "apps/App/meta.xml": b"<app/>"})

    resultado = oscwii_installer.install_app(_app(), unidad)

    assert resultado.ok, resultado
    assert sorted(os.listdir(unidad / "apps" / "App")) == ["boot.dol", "meta.xml"]
    assert os.listdir(unidad) == ["apps"]


def test_un_destino_que_es_un_archivo_falla_antes_de_descargar(monkeypatch, tmp_path):
    """Con permiso de escritura y de ejecución, `os.access` solo no lo
    distingue de una carpeta."""
    llamadas = _descarga(monkeypatch, {"apps/App/boot.dol": dol(b"A")})
    destino = tmp_path / "archivo"
    destino.write_bytes(b"x")
    destino.chmod(0o755)

    resultado = oscwii_installer.install_app(_app(), destino)

    assert not resultado.ok, resultado
    assert "no es una carpeta" in resultado.error
    assert llamadas == []
