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

from wiibackup_manager import oscwii_installer
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
