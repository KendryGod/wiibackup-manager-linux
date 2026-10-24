"""El Recovery Manager encuentra los restos de una instalación de Homebrew
cortada dentro de las carpetas de datos de las apps.

La Store va a escribir en carpetas de datos fuera de `apps/` (`smw/`,
`usr/local/lib/timidity/`...), archivo por archivo y sin borrar lo que el
usuario tenga ahí. Los restos de una transacción cortada (la versión
anterior apartada, lo nuevo a medio preparar) son HERMANOS de su destino,
y esos destinos llegan a 7 niveles (`smw/gfx/packs/Classic/tilesets/SMB1/
…`). El escaneo de arranque baja solo 3 (`MAX_DEPTH`) desde cada raíz: no
los veía.

Ahora cada carpeta de datos que exista en la unidad se recorre como raíz
propia hasta el nivel más hondo que la Store puede escribir
(`oscwii_installer.MAX_COMPONENTES_DATOS`, que `_clasificar` hace cumplir),
y solo esas: el resto de la unidad se sigue recorriendo hasta 3.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from wiibackup_manager import oscwii_installer, recovery_service
from wiibackup_manager.operations import OperationManager
from wiibackup_manager.oscwii_installer import Destino

PID_MUERTO = 999_999_999
MAX = oscwii_installer.MAX_COMPONENTES_DATOS


def _respaldo_de(archivo: Path) -> Path:
    """El respaldo de `archivo` como lo deja `StagedSwap` (su versión
    anterior apartada), de un proceso que ya no existe."""
    resp = archivo.with_name(
        f".{archivo.name}.{oscwii_installer.MARCA_RESPALDO}-{PID_MUERTO}")
    resp.parent.mkdir(parents=True, exist_ok=True)
    resp.write_bytes(b"version anterior")
    return resp


def _encontrados(raiz: Path) -> set:
    return {lo.path for lo in recovery_service.scan([raiz], ops=OperationManager())}


@pytest.mark.parametrize("relativa", [
    "smw/gfx/packs/Classic/tilesets/SMB1/tileset.png",       # el más hondo del catálogo
    "/".join(["smw"] + [f"n{i}" for i in range(MAX - 2)] + ["x.png"]),
    "usr/local/lib/timidity/Drum_000/035_Kick_1.pat",
    "/".join(["usr", "local", "lib", "timidity"]
             + [f"n{i}" for i in range(MAX - 5)] + ["x.pat"]),
    "WiiFlow/plugins/Hatari-alt/CFG/hatari.ini",             # con otras mayúsculas
    "data/supersonicwii/assets/environment/skyboxes/flare/top.png",
])
def test_un_respaldo_hondo_en_una_carpeta_de_datos_se_encuentra(tmp_path, relativa):
    assert len(Path(relativa).parts) <= MAX
    assert oscwii_installer._clasificar(relativa) is Destino.DATOS
    resp = _respaldo_de(tmp_path / relativa)

    assert resp in _encontrados(tmp_path)


def test_fuera_de_las_carpetas_de_datos_se_sigue_bajando_solo_3(tmp_path):
    """Recorrer hondo toda la unidad haría lento el arranque: solo las
    carpetas de datos, que es donde la Store escribe hondo."""
    resp = _respaldo_de(tmp_path / "roms/a/b/c/d/juego.bin")

    assert resp not in _encontrados(tmp_path)


def test_una_carpeta_de_datos_que_no_existe_no_molesta(tmp_path):
    resp = _respaldo_de(tmp_path / "apps/App/boot.dol")

    assert _encontrados(tmp_path) == {resp}


def test_el_mismo_resto_no_aparece_dos_veces(tmp_path):
    """`smw/x` está al alcance del recorrido normal y del de `smw/`."""
    resp = _respaldo_de(tmp_path / "smw/x.wav")

    restos = recovery_service.scan([tmp_path], ops=OperationManager())

    assert [lo.path for lo in restos] == [resp]


def test_la_store_no_escribe_mas_hondo_de_lo_que_recovery_recorre():
    hondo = "/".join(["smw"] + [f"n{i}" for i in range(MAX - 1)] + ["x.png"])
    assert len(Path(hondo).parts) == MAX + 1

    assert oscwii_installer._clasificar(hondo) is None


@pytest.mark.parametrize("prefijo", [["smw"], ["usr", "local", "lib", "timidity"]])
def test_no_baja_mas_hondo_de_lo_que_la_store_escribe(tmp_path, prefijo):
    """El tope se cuenta desde la raíz de la unidad, no desde la carpeta de
    datos: debajo de `usr/local/lib/timidity/` quedan 4 niveles, no los 7
    de `smw/`."""
    relativa = "/".join(prefijo + [f"n{i}" for i in range(MAX - len(prefijo))] + ["x"])
    assert len(Path(relativa).parts) == MAX + 1
    resp = _respaldo_de(tmp_path / relativa)

    assert resp not in _encontrados(tmp_path)
