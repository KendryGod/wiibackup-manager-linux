"""Respaldo de una app de Homebrew con la app ya instalada: solo se elimina.

El instalador pone en el destino solo una staging completa
(`atomicfs.staged_directory`), así que un respaldo con la app instalada al
lado es la versión ANTERIOR, que quedó porque se cortó (o falló) su
borrado después de un intercambio que sí salió bien -y por eso mismo
puede estar a medio borrar-. Restaurarlo encima cambiaría una versión
completa por una que quizás no lo está.

Antes se ofrecía "Restaurar" igual, el `os.replace` fallaba con ENOTEMPTY
y el aviso decía "No se pudo restaurar… el archivo sigue en…", después de
una confirmación que llamaba "probablemente incompleto" a la app recién
instalada.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from wiibackup_manager import oscwii_installer, recovery_service
from wiibackup_manager.operations import OperationManager

PID_MUERTO = 999_999_999


def _respaldo(apps: Path) -> Path:
    resp = apps / f".WiiDonut.{oscwii_installer.MARCA_RESPALDO}-{PID_MUERTO}"
    resp.mkdir(parents=True)
    (resp / "boot.dol").write_bytes(b"version anterior")
    return resp


def _instalar(apps: Path) -> Path:
    app = apps / "WiiDonut"
    app.mkdir(parents=True)
    (app / "boot.dol").write_bytes(b"version nueva, completa")
    return app


def _el_resto(raiz: Path):
    [resto] = recovery_service.scan([raiz], ops=OperationManager())
    assert resto.kind is recovery_service.LeftoverKind.HOMEBREW_BACKUP
    return resto


@pytest.fixture
def apps(tmp_path):
    return tmp_path / "apps"


def test_con_la_app_instalada_restaurar_no_toca_nada_y_lo_dice(apps, tmp_path):
    app, resp = _instalar(apps), _respaldo(apps)
    resto = _el_resto(tmp_path)

    with pytest.raises(recovery_service.RecoveryError) as info:
        recovery_service.restore(resto)

    mensaje = str(info.value)
    assert "ya está instalada" in mensaje
    assert "incomplet" not in mensaje
    assert "no es una copia" not in mensaje
    assert (app / "boot.dol").read_bytes() == b"version nueva, completa"
    assert (resp / "boot.dol").read_bytes() == b"version anterior"


def test_si_la_app_se_instalo_despues_del_escaneo_tampoco(apps, tmp_path):
    """La lista del Recovery Manager es la del último escaneo (al arrancar o
    al montar la unidad): la app pudo instalarse después. Se mira el disco
    en el momento de restaurar, no la foto."""
    resp = _respaldo(apps)
    resto = _el_resto(tmp_path)
    assert resto.original_exists is False
    app = _instalar(apps)

    with pytest.raises(recovery_service.RecoveryError) as info:
        recovery_service.restore(resto)

    assert "ya está instalada" in str(info.value)
    assert (app / "boot.dol").read_bytes() == b"version nueva, completa"
    assert (resp / "boot.dol").read_bytes() == b"version anterior"


def test_sin_la_app_instalada_se_restaura_como_siempre(apps, tmp_path):
    resp = _respaldo(apps)
    resto = _el_resto(tmp_path)

    recovery_service.restore(resto)

    assert (apps / "WiiDonut" / "boot.dol").read_bytes() == b"version anterior"
    assert not resp.exists()
