"""El escaneo de la biblioteca no entra en carpetas ocultas ni toma
archivos ocultos como juegos.

Con la biblioteca en la raíz de un disco externo, "Eliminar" manda el
juego a la papelera de esa unidad (`.Trash-1000/files/`, la pone GIO), y
el escaneo lo volvía a encontrar ahí: el juego borrado seguía en la lista.
Un pendrive que pasó por una Mac trae `._Juego.iso` (4 KB de metadatos),
que aparecía como un juego sin identificar.

Ahora se saltean las carpetas ocultas POR DEBAJO de la raíz y los
archivos que empiezan con punto. La raíz misma puede ser oculta. Y el
Recovery Manager, que busca justamente restos que empiezan con punto
(`.Juego.wbfs.respaldo-<pid>`, `.Juego.iso.parcial-…`), tiene su propio
recorrido y los sigue encontrando.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from wiibackup_manager import recovery_service, scanning


def _iso_wii(path: Path, game_id: bytes = b"RMCE01") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cabecera = bytearray(0x100)
    cabecera[0:6] = game_id
    cabecera[0x18:0x1C] = b"\x5d\x1c\x9e\xa3"
    cabecera[0x20:0x26] = b"JUEGO "
    path.write_bytes(bytes(cabecera))
    return path


def _nombres(raiz: Path, rutas) -> list:
    return sorted(str(p.relative_to(raiz)) for p in rutas)


def test_no_entra_en_carpetas_ocultas_ni_toma_archivos_ocultos(tmp_path):
    biblio = tmp_path / "biblioteca"
    _iso_wii(biblio / "Mario [RMCE01].iso")
    _iso_wii(biblio / "sub" / "Brawl [RSBE01].iso", b"RSBE01")
    _iso_wii(biblio / ".Trash-1000" / "files" / "Borrado [RSBE01].iso", b"RSBE01")
    _iso_wii(biblio / "sub" / ".oculta" / "Otro [RMCE01].iso")
    (biblio / "._Mario [RMCE01].iso").write_bytes(b"\0" * 4096)   # de macOS
    (biblio / ".Mario [RMCE01].iso.parcial-ab12cd").write_bytes(b"a medias")

    encontrados = scanning.find_game_files(biblio)

    assert _nombres(biblio, encontrados) == ["Mario [RMCE01].iso",
                                             "sub/Brawl [RSBE01].iso"]
    assert sorted(g.game_id for g in scanning.scan_library(biblio, "no-hay-wit")) == [
        "RMCE01", "RSBE01"]


def test_una_biblioteca_en_una_carpeta_oculta_sigue_funcionando(tmp_path):
    biblio = tmp_path / ".juegos-wii"
    _iso_wii(biblio / "Mario [RMCE01].iso")
    _iso_wii(biblio / "sub" / "Brawl [RSBE01].iso", b"RSBE01")

    assert _nombres(biblio, scanning.find_game_files(biblio)) == [
        "Mario [RMCE01].iso", "sub/Brawl [RSBE01].iso"]


def test_recovery_sigue_encontrando_los_restos_ocultos(tmp_path):
    """Lo que el escaneo de juegos ahora saltea es justo lo que busca el
    Recovery Manager: tienen que seguir apareciendo ahí, en la raíz y en
    una subcarpeta."""
    p = subprocess.Popen([sys.executable, "-c", ""])
    p.wait()
    biblio = tmp_path / "biblioteca"
    respaldo = biblio / f".Mario [RMCE01].iso.respaldo-{p.pid}"
    parcial = biblio / "sub" / ".Brawl [RSBE01].iso.parcial-q1w2e3"
    for resto in (respaldo, parcial):
        resto.parent.mkdir(parents=True, exist_ok=True)
        resto.write_bytes(b"\0" * 64)
    viejo = time.time() - recovery_service.PARTIAL_MIN_AGE_SECONDS - 1
    os.utime(parcial, (viejo, viejo))

    encontrados = {lo.path.name for lo in recovery_service.scan([biblio])}

    assert encontrados == {respaldo.name, parcial.name}
    assert scanning.find_game_files(biblio) == []
