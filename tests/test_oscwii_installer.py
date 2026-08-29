"""Pruebas de la validación de ZIP de `oscwii_installer.py`.

Se agregan junto con `golden_configs.py`: la investigación de esa tarea
encontró que el ZIP real de Nintendont en el catálogo de OSC trae una
carpeta "controllers/" fuera de "apps/", y que sin ampliar
`_ALLOWED_TOP_LEVEL_DIRS` el instalador rechazaba ese ZIP entero como "no
seguro" -o sea, Nintendont nunca se podía instalar de verdad, y el evento
que dispara la inyección de la config maestra no llegaba a ocurrir nunca.
Estas pruebas son la guarda de regresión de ese arreglo, más la
protección de zip-slip que ya existía."""
from __future__ import annotations

import zipfile

import pytest

from wiibackup_manager import oscwii_installer
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import InstallStatus


def _make_zip(path, entries: dict):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


# ------------------------------------------------------- _is_safe_member --
def test_entrada_dentro_de_apps_es_segura(tmp_path):
    assert oscwii_installer._is_safe_member("apps/WiiDonut/boot.dol", tmp_path)


def test_entrada_dentro_de_controllers_es_segura(tmp_path):
    """Caso real: el ZIP de Nintendont trae "controllers/" al mismo nivel
    que "apps/", no adentro (confirmado con los `subdirectories` que
    reporta la API real de OSC para ese slug)."""
    assert oscwii_installer._is_safe_member("controllers/gcadapter.ini", tmp_path)


def test_entrada_con_zip_slip_se_rechaza(tmp_path):
    assert not oscwii_installer._is_safe_member(
        "apps/../../../etc/passwd", tmp_path)


def test_entrada_con_ruta_absoluta_se_rechaza(tmp_path):
    assert not oscwii_installer._is_safe_member("/etc/passwd", tmp_path)


def test_entrada_fuera_de_carpetas_permitidas_se_rechaza(tmp_path):
    """Una carpeta de primer nivel que no está en la lista blanca (ni
    "apps" ni "controllers") se sigue rechazando: la ampliación del Paso 2
    de golden_configs no relaja la regla en general, solo le suma un
    segundo destino conocido."""
    assert not oscwii_installer._is_safe_member("roms/juego.gcm", tmp_path)


# --------------------------------------------------------- _validate_zip --
def test_validate_zip_acepta_apps_y_controllers_juntos(tmp_path):
    """Reproduce la forma real del ZIP de Nintendont: entradas en "apps/"
    y en "controllers/" en el mismo archivo."""
    zip_path = tmp_path / "Nintendont.zip"
    _make_zip(zip_path, {
        "apps/Nintendont/boot.dol": b"A" * 100,
        "apps/Nintendont/meta.xml": b"<app/>",
        "controllers/gcadapter.ini": b"[controller]\n",
    })

    dest_root = tmp_path / "usb"
    dest_root.mkdir()
    (dest_root / "apps").mkdir()

    zf, status, err = oscwii_installer._validate_zip(zip_path, dest_root)
    try:
        assert status is None, err
        assert zf is not None
    finally:
        if zf is not None:
            zf.close()


def test_validate_zip_rechaza_entrada_fuera_de_lo_permitido(tmp_path):
    zip_path = tmp_path / "malicioso.zip"
    _make_zip(zip_path, {
        "apps/Foo/boot.dol": b"A" * 10,
        "otra_carpeta/archivo.txt": b"x",
    })

    dest_root = tmp_path / "usb"
    dest_root.mkdir()
    (dest_root / "apps").mkdir()

    zf, status, err = oscwii_installer._validate_zip(zip_path, dest_root)
    assert zf is None
    assert status is InstallStatus.UNSAFE_ZIP
    assert "otra_carpeta" in err


# ----------------------------------------------------------- install_app --
def _fake_app(**overrides) -> HomebrewApp:
    kwargs = dict(slug="TestApp", name="Test App",
                 zip_url="https://hbb1.oscwii.org/api/contents/TestApp/TestApp.zip")
    kwargs.update(overrides)
    return HomebrewApp(**kwargs)


def test_install_app_rechaza_dest_root_critico(tmp_path, monkeypatch):
    """Defensa en profundidad (Blindaje 4 reusado): aunque hoy la UI nunca
    deje elegir una ruta crítica del sistema como destino, `install_app`
    tiene que rechazarla por su cuenta si algún día se la llama desde
    otro lugar sin ese filtro. Se simula la ruta crítica con
    `drives.CRITICAL_MOUNTPOINTS` (en vez de apuntar a "/" de verdad) para
    no depender de -ni arriesgar- el sistema real que corre la prueba."""
    monkeypatch.setattr(oscwii_installer.drives, "CRITICAL_MOUNTPOINTS",
                        frozenset({str(tmp_path)}))

    def _no_deberia_descargar(*_a, **_k):
        raise AssertionError(
            "install_app no debería llegar a descargar nada si dest_root "
            "ya se rechazó por ser una ruta crítica")
    monkeypatch.setattr(oscwii_installer, "_download_zip", _no_deberia_descargar)

    result = oscwii_installer.install_app(_fake_app(), tmp_path)

    assert not result.ok
    assert result.status is InstallStatus.UNSAFE_DEST_ROOT
    assert not (tmp_path / "apps").exists()


def test_install_app_con_destino_valido_instala_normal(tmp_path, monkeypatch):
    """No-regresión: un destino normal (no crítico) sigue instalando la
    app como siempre, atravesando la validación nueva sin que interfiera."""
    dest_root = tmp_path / "usb"
    dest_root.mkdir()

    zip_entries = {
        "apps/TestApp/boot.dol": b"A" * 100,
        "apps/TestApp/meta.xml": b"<app/>",
    }

    def _fake_download_zip(app, dest_path, cancel_event, on_progress):
        _make_zip(dest_path, zip_entries)
        return True, "", "d41d8cd98f00b204e9800998ecf8427e", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    monkeypatch.setattr(oscwii_installer, "_download_zip", _fake_download_zip)

    result = oscwii_installer.install_app(_fake_app(), dest_root)

    assert result.ok, result.error
    assert result.status is InstallStatus.OK
    assert (dest_root / "apps" / "TestApp" / "boot.dol").read_bytes() == b"A" * 100
    assert (dest_root / "apps" / "TestApp" / "meta.xml").read_bytes() == b"<app/>"


# ------------------------------------------------------- _extract_member --
def test_extraccion_cortada_no_deja_temporales_en_la_unidad(tmp_path, monkeypatch):
    """Una extracción que se corta a mitad (unidad desconectada, disco
    lleno) no puede dejar un `.boot.dol.parcial-<pid>` tirado en la unidad
    del usuario.

    Mientras `_extract_member` armaba el temporal a mano era el único de
    los cuatro lugares con este patrón que no limpiaba ante un error; al
    pasar a `fsutil.atomic_target` la limpieza quedó garantizada para
    todos. Esta prueba es la guarda de que no se vuelva a perder."""
    zip_path = tmp_path / "app.zip"
    _make_zip(zip_path, {"apps/TestApp/boot.dol": b"A" * 100})
    dest_root = tmp_path / "usb"
    (dest_root / "apps").mkdir(parents=True)

    def _falla_a_mitad(src, dst, *a, **kw):
        dst.write(b"AA")  # algo ya se escribió al temporal
        raise OSError("unidad desconectada")
    monkeypatch.setattr(oscwii_installer.shutil, "copyfileobj", _falla_a_mitad)

    with zipfile.ZipFile(zip_path) as zf:
        info = zf.infolist()[0]
        with pytest.raises(OSError):
            oscwii_installer._extract_member(zf, info, dest_root)

    quedaron = list((dest_root / "apps" / "TestApp").iterdir())
    assert quedaron == [], f"quedaron temporales huérfanos: {quedaron}"
