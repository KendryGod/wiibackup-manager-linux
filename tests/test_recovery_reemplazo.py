"""Recovery Manager para un reemplazo que se cortó.

Dos situaciones de la prueba de hardware con Super Smash Bros. Brawl
(`RSBE01.wbfs` + `RSBE01.wbf1`):

1. La que deja la app hoy si se desenchufa la unidad a mitad de pisar el
   juego: los dos respaldos `.RSBE01.wbfs.respaldo-<pid>` y el temporal
   de `wit` (`.RSBE01.wbfs.LtnLTy.tmp`), con la app TODAVÍA ABIERTA al
   reconectar.
2. La que se encontró en el pendrive: el original dentro de la papelera
   de la unidad (`.Trash-1000/files/RSBE01/`) y el temporal cortado en
   `wbfs/RSBE01/`.

En las dos, el Recovery Manager tiene que ofrecer "Restaurar" el juego
entero (las dos partes juntas) y "Eliminar" el temporal. Todo con datos y
rutas de prueba.
"""
from __future__ import annotations

import os
import time
import types
from pathlib import Path

import pytest

from wiibackup_manager import recovery_service as rs
from wiibackup_manager.operations import OperationKind, OperationManager
from wiibackup_manager.recovery_service import LeftoverKind

PID = os.getpid()
ORIGINAL = {"RSBE01.wbfs": b"ORIGINAL parte 0", "RSBE01.wbf1": b"ORIGINAL parte 1"}
VIEJO = time.time() - 600          # "hace 10 minutos": quieto hace rato


def _archivo(path: Path, datos: bytes, mtime: float = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(datos)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _nadie_lo_tiene_abierto():
    return set()


def _scan(raiz, **kw):
    kw.setdefault("ops", OperationManager())
    kw.setdefault("open_files", _nadie_lo_tiene_abierto)
    return rs.scan([raiz], **kw)


@pytest.fixture
def reemplazo_cortado(tmp_path):
    """Lo que queda en la unidad después de desenchufarla a mitad de
    reemplazar el juego, con esta misma app (este PID) todavía abierta."""
    raiz = tmp_path / "WII_USB"
    carpeta = raiz / "wbfs" / "RSBE01"
    for nombre, datos in ORIGINAL.items():
        _archivo(carpeta / f".{nombre}.respaldo-{PID}", datos, VIEJO)
    _archivo(carpeta / ".RSBE01.wbfs.LtnLTy.tmp", b"juego nuevo a medias", VIEJO)
    return raiz


# ======================================= 1. Respaldo de la app abierta --
def test_con_la_app_abierta_ofrece_el_juego_entero_y_el_temporal(
        reemplazo_cortado):
    restos = _scan(reemplazo_cortado)
    tipos = sorted(r.kind.name for r in restos)
    assert tipos == ["BACKUP", "WIT_TEMP"]

    juego = next(r for r in restos if r.kind is LeftoverKind.BACKUP)
    # Una sola entrada para las dos partes: restaurar una sola dejaría un
    # juego que no arranca.
    assert juego.original.name == "RSBE01.wbfs"
    assert [o.name for o, _r in juego.parts] == ["RSBE01.wbf1"]
    assert juego.size_bytes == sum(len(d) for d in ORIGINAL.values())
    assert juego.restorable
    assert juego.title == "RSBE01.wbfs (+1 parte)"
    assert "quedó guardado" in juego.kind.description

    temporal = next(r for r in restos if r.kind is LeftoverKind.WIT_TEMP)
    assert not temporal.restorable


def test_restaurar_devuelve_las_dos_partes_y_eliminar_borra_el_temporal(
        reemplazo_cortado):
    carpeta = reemplazo_cortado / "wbfs" / "RSBE01"
    restos = _scan(reemplazo_cortado)
    rs.restore(next(r for r in restos if r.kind is LeftoverKind.BACKUP))
    rs.delete(next(r for r in restos if r.kind is LeftoverKind.WIT_TEMP))
    assert {p.name: p.read_bytes() for p in carpeta.iterdir()} == ORIGINAL


def test_un_respaldo_de_esta_app_en_uso_no_se_ofrece(reemplazo_cortado):
    """Si la operación que lo dejó SIGUE corriendo (está en `ops`), es un
    reemplazo en curso: tocarlo le arruinaría el juego al usuario."""
    ops = OperationManager()
    destino = reemplazo_cortado / "wbfs" / "RSBE01" / "RSBE01.wbfs"
    op = ops.start(OperationKind.TRANSFERRING, write=[destino])
    try:
        restos = _scan(reemplazo_cortado, ops=ops)
        assert all(r.kind is not LeftoverKind.BACKUP for r in restos)
    finally:
        ops.finish(op)


def test_sin_registro_de_operaciones_un_respaldo_propio_no_se_ofrece(
        reemplazo_cortado):
    """Sin `ops` no hay forma de saber si es de una operación en curso."""
    restos = rs.scan([reemplazo_cortado], ops=None,
                     open_files=_nadie_lo_tiene_abierto)
    assert all(r.kind is not LeftoverKind.BACKUP for r in restos)


# ========================================== El temporal de `wit` --
def test_un_temporal_abierto_por_un_proceso_no_se_ofrece(reemplazo_cortado):
    """Una copia viva tiene su temporal abierto."""
    tmp = reemplazo_cortado / "wbfs" / "RSBE01" / ".RSBE01.wbfs.LtnLTy.tmp"
    st = tmp.stat()
    restos = _scan(reemplazo_cortado,
                   open_files=lambda: {(st.st_dev, st.st_ino)})
    assert all(r.kind is not LeftoverKind.WIT_TEMP for r in restos)


def test_un_temporal_que_cambio_recien_no_se_ofrece(tmp_path):
    raiz = tmp_path / "usb"
    _archivo(raiz / "wbfs" / "RSBE01" / ".RSBE01.wbfs.Ab12Cd.tmp", b"x")
    assert _scan(raiz) == []


def test_sin_proc_un_temporal_viejo_se_ofrece_igual(tmp_path):
    """Si /proc no se puede mirar queda el criterio de siempre: media hora
    sin que nadie lo toque."""
    raiz = tmp_path / "usb"
    viejisimo = time.time() - 2 * rs.PARTIAL_MIN_AGE_SECONDS
    _archivo(raiz / "wbfs" / "RSBE01" / ".RSBE01.wbfs.Ab12Cd.tmp", b"x", viejisimo)
    _archivo(raiz / "wbfs" / "RMCP01" / ".RMCP01.wbfs.Zz99Yy.tmp", b"x", VIEJO)
    restos = _scan(raiz, open_files=lambda: None)
    assert [r.original.name for r in restos] == ["RSBE01.wbfs"]


def test_las_partes_del_temporal_de_wit_van_juntas(tmp_path):
    raiz = tmp_path / "usb"
    carpeta = raiz / "wbfs" / "RSBE01"
    _archivo(carpeta / ".RSBE01.wbfs.Ab12Cd.tmp", b"a" * 10, VIEJO)
    _archivo(carpeta / ".RSBE01.wbfs.Ab12Cd.tmp.1", b"b" * 5, VIEJO)
    (resto,) = _scan(raiz)
    assert resto.size_bytes == 15
    rs.delete(resto)
    assert list(carpeta.iterdir()) == []


@pytest.mark.parametrize("nombre", [
    ".config.tmp",                 # no es una imagen de juego
    ".RSBE01.wbfs.tmp",            # falta el sufijo al azar de `wit`
    ".notas.txt.Ab12Cd.tmp",       # no es una imagen de juego
    ".RSBE01.wbfs.Ab12Cd.tmpx",
])
def test_el_patron_de_wit_no_toma_archivos_ajenos(tmp_path, nombre):
    assert rs.classify(_archivo(tmp_path / nombre, b"x")) is None


# ========================== 2. El original en la papelera de la unidad --
def _papelera(raiz: Path, path_en_info: str, nombre="RSBE01") -> Path:
    papelera = raiz / f".Trash-{os.getuid()}"
    for parte, datos in ORIGINAL.items():
        _archivo(papelera / "files" / nombre / parte, datos)
    info = papelera / "info" / f"{nombre}.trashinfo"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_text("[Trash Info]\nPath=%s\nDeletionDate=2026-10-07T08:05:10\n"
                    % path_en_info)
    return papelera


@pytest.fixture
def original_en_la_papelera(tmp_path):
    """Lo que había en el pendrive de la prueba de hardware."""
    raiz = tmp_path / "WII_USB"
    _papelera(raiz, "wbfs/RSBE01")
    _archivo(raiz / "wbfs" / "RSBE01" / ".RSBE01.wbfs.LtnLTy.tmp",
             b"juego nuevo a medias", VIEJO)
    return raiz


def test_ofrece_restaurar_el_juego_de_la_papelera_y_eliminar_el_temporal(
        original_en_la_papelera):
    restos = _scan(original_en_la_papelera)
    assert sorted(r.kind.name for r in restos) == ["TRASHED", "WIT_TEMP"]
    juego = next(r for r in restos if r.kind is LeftoverKind.TRASHED)
    assert juego.restorable
    assert juego.original == original_en_la_papelera / "wbfs" / "RSBE01"
    # La carpeta del juego existe (la recreó la copia cortada, con solo el
    # temporal adentro), pero restaurar no pisa nada.
    assert not juego.original_exists
    assert "papelera" in juego.kind.description


def test_restaurar_desde_la_papelera_deja_todo_como_antes(
        original_en_la_papelera):
    raiz = original_en_la_papelera
    restos = _scan(raiz)
    rs.restore(next(r for r in restos if r.kind is LeftoverKind.TRASHED))
    rs.delete(next(r for r in restos if r.kind is LeftoverKind.WIT_TEMP))

    carpeta = raiz / "wbfs" / "RSBE01"
    assert {p.name: p.read_bytes() for p in carpeta.iterdir()} == ORIGINAL
    papelera = raiz / f".Trash-{os.getuid()}"
    # Ni el elemento ni su .trashinfo: la papelera no queda con un
    # fantasma que apunte a un juego que ya volvió.
    assert list((papelera / "files").iterdir()) == []
    assert list((papelera / "info").iterdir()) == []
    assert _scan(raiz) == []


def test_si_la_carpeta_no_existe_la_papelera_la_devuelve_entera(tmp_path):
    """Un temporal de `atomicfs` en `wbfs/` (no adentro de la carpeta del
    juego) también es señal de copia cortada."""
    raiz = tmp_path / "usb"
    _papelera(raiz, "wbfs/RSBE01")
    _archivo(raiz / "wbfs" / ".RSBE01.parcial-ab12cd", b"x", VIEJO)
    juego = next(r for r in _scan(raiz) if r.kind is LeftoverKind.TRASHED)
    # Sin temporal ADENTRO de RSBE01 no hay señal en ese lugar...
    assert juego is not None
    rs.restore(juego)
    assert {p.name for p in (raiz / "wbfs" / "RSBE01").iterdir()} == set(ORIGINAL)


def test_un_juego_que_alguien_borro_a_proposito_no_se_ofrece(tmp_path):
    """Sin un temporal de copia cortada donde estaba, es un borrado
    normal: no es asunto de esta app."""
    raiz = tmp_path / "usb"
    _papelera(raiz, "wbfs/RSBE01")
    assert _scan(raiz) == []


def test_lo_que_no_estaba_en_wbfs_ni_games_no_se_ofrece(tmp_path):
    raiz = tmp_path / "usb"
    _papelera(raiz, "fotos/RSBE01")
    _archivo(raiz / "fotos" / "RSBE01" / ".RSBE01.wbfs.LtnLTy.tmp", b"x", VIEJO)
    assert all(r.kind is not LeftoverKind.TRASHED for r in _scan(raiz))


def test_la_ruta_de_la_papelera_puede_venir_absoluta_y_codificada(tmp_path):
    """La spec permite `Path=` absoluto, y siempre va con %-escapes."""
    raiz = tmp_path / "usb"
    carpeta = raiz / "games" / "Metroid Prime [GM8E01]"
    papelera = raiz / f".Trash-{os.getuid()}"
    _archivo(papelera / "files" / "Metroid Prime [GM8E01]" / "game.iso", b"gc")
    (papelera / "info").mkdir(parents=True)
    (papelera / "info" / "Metroid Prime [GM8E01].trashinfo").write_text(
        "[Trash Info]\nPath=%s\n" % str(carpeta).replace(" ", "%20")
        .replace("[", "%5B").replace("]", "%5D"))
    _archivo(carpeta / ".game.iso.parcial-zz11", b"x", VIEJO)
    juego = next(r for r in _scan(raiz) if r.kind is LeftoverKind.TRASHED)
    assert juego.original == carpeta


def test_restaurar_de_la_papelera_no_pisa_un_juego_que_ya_esta(
        original_en_la_papelera):
    carpeta = original_en_la_papelera / "wbfs" / "RSBE01"
    _archivo(carpeta / "RSBE01.wbfs", b"OTRO juego completo")
    juego = next(r for r in _scan(original_en_la_papelera)
                 if r.kind is LeftoverKind.TRASHED)
    assert juego.original_exists
    with pytest.raises(rs.RecoveryError):
        rs.restore(juego)
    assert (carpeta / "RSBE01.wbfs").read_bytes() == b"OTRO juego completo"
    assert (juego.path / "RSBE01.wbfs").exists()


# ============================================ Reescaneo al reconectar --
def test_montar_una_unidad_pide_un_escaneo_sin_pisar_el_que_corre():
    from wiibackup_manager.window import WiiBackupWindow

    llamadas = []
    vista = types.SimpleNamespace(_recovery_scanning=True,
                                  _recovery_rescan_pending=False)
    WiiBackupWindow._schedule_recovery_rescan(vista)
    assert vista._recovery_rescan_pending is True
    assert llamadas == []
