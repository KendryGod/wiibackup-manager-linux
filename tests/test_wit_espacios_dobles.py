"""`wit` reescribe el destino de `COPY --dest` y el segundo archivo de `CMP`.

Medido con el binario real (wit v3.05a r8638): esas dos rutas pasan por el
mismo filtro que `wit` usa para armar nombres, que junta los espacios
seguidos, quita los del principio y el final del nombre, cambia
`* ; ? @ \\ ^ ` | ~` y los caracteres de control por un espacio, `:` por
" - " y expande las secuencias `%`. El origen de `COPY`, el primer archivo
de `CMP` y las rutas de `LIST`, `ISOSIZE` y `VERIFY` llegan tal cual.

La app arma carpetas de GameCube con dos espacios seguidos: el título de
D43E01 es "The Legend of Zelda: Ocarina of Time / Master Quest", y al
sacarle la "/" queda "...of Time  Master Quest [D43E01]". Convertir ese
juego en su carpeta fallaba (`COPY` con rc 80, "CAN'T CREATE FILE": la
carpeta con un solo espacio no existe) y compararlo daba "no coincide"
(`CMP` con rc 2, "CAN'T OPEN FILE"). Lo mismo con una unidad cuya etiqueta
tenga dos espacios.

Arreglo: `wit` corre con la carpeta del destino como directorio de
trabajo y recibe solo el nombre. Y si el NOMBRE es de los que `wit`
reescribe, no se lo llama: escribiría otro archivo -y con `--overwrite`
podía pisar uno que ya estaba con ese otro nombre-.

Se prueba con `fake_wit`, un `wit` ejecutable que imita esas reglas (ver
su docstring); `nombre_respetado_por_wit` lleva la tabla medida.
"""
from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from fake_wit import instalar
from wiibackup_manager import library_ops, transfer_plan, wit_wrapper
from wiibackup_manager.game_model import Game

TITULO_D43E01 = "The Legend of Zelda: Ocarina of Time / Master Quest"


@pytest.fixture
def wit_falso(tmp_path, monkeypatch):
    # Desde `tmp_path`: si algo llegara a pasarle a `wit` un destino
    # relativo sin la carpeta de trabajo, lo escribe acá y no en el repo.
    monkeypatch.chdir(tmp_path)
    return instalar(tmp_path / "bin")


def _gamecube_en_la_unidad(unidad: Path, contenido: bytes = b"D43E01 disco de prueba"):
    """Un CISO de D43E01 en la carpeta que arma la propia app."""
    provisorio = Game(path=Path("game.ciso"), game_id="D43E01", title=TITULO_D43E01,
                      fmt="CISO", size_bytes=0, identified_by="wit", console="gc")
    origen = transfer_plan.gc_dest_path(provisorio, unidad)
    origen.parent.mkdir(parents=True)
    origen.write_bytes(contenido)
    return Game(path=origen, game_id="D43E01", title=TITULO_D43E01, fmt="CISO",
                size_bytes=len(contenido), identified_by="wit", console="gc")


def test_la_carpeta_de_d43e01_tiene_dos_espacios(tmp_path):
    """El caso real: así arma la app la carpeta de este juego en la unidad
    (y así la tienen las unidades ya preparadas, que no se renombran)."""
    juego = _gamecube_en_la_unidad(tmp_path / "unidad")
    assert juego.path.parent.name == "The Legend of Zelda Ocarina of Time  Master Quest [D43E01]"


def test_convertir_gamecube_en_una_carpeta_con_doble_espacio(tmp_path, wit_falso, monkeypatch):
    monkeypatch.setattr(transfer_plan, "estimate_output_size", lambda g, e, b: 10)
    juego = _gamecube_en_la_unidad(tmp_path / "unidad")
    destino = juego.path.with_suffix(".iso")

    library_ops.convert_gamecube(juego, destino, ".iso", str(wit_falso.binario))

    assert destino.read_bytes() == juego.path.read_bytes()
    assert sorted(p.name for p in juego.path.parent.iterdir()) == ["game.ciso", "game.iso"]
    # Ni una carpeta "hermana" con un solo espacio ni nada fuera de la del juego.
    assert [p.name for p in (tmp_path / "unidad" / "games").iterdir()] == [juego.path.parent.name]


def test_comparar_con_doble_espacio_en_la_carpeta(tmp_path, wit_falso):
    juego = _gamecube_en_la_unidad(tmp_path / "unidad")
    copia = juego.path.with_suffix(".iso")
    copia.write_bytes(juego.path.read_bytes())

    assert wit_wrapper.images_match(juego.path, copia, str(wit_falso.binario))
    # Y sigue viendo cuando NO coinciden: no es que ahora diga que sí a todo.
    copia.write_bytes(b"D43E01 otro contenido")
    assert not wit_wrapper.images_match(juego.path, copia, str(wit_falso.binario))


def test_copiar_a_una_unidad_cuya_etiqueta_tiene_doble_espacio(tmp_path, wit_falso):
    """Un WBFS a `wbfs/<ID6>/` de una unidad montada como "MI  USB"."""
    origen = tmp_path / "biblioteca" / "Brawl.iso"
    origen.parent.mkdir()
    origen.write_bytes(b"RSBE01 disco de wii")
    destino = tmp_path / "MI  USB" / "wbfs" / "RSBE01" / "RSBE01.wbfs"
    destino.parent.mkdir(parents=True)

    resultado = wit_wrapper.convert(origen, destino, "WBFS", str(wit_falso.binario),
                                    split=True, overwrite=True)

    assert resultado.returncode == 0, resultado.stderr
    assert destino.read_bytes() == origen.read_bytes()


def test_wit_corre_en_la_carpeta_del_destino_con_el_nombre_relativo(tmp_path, wit_falso):
    origen = tmp_path / "biblioteca" / "Brawl.iso"
    origen.parent.mkdir()
    origen.write_bytes(b"RSBE01 disco de wii")
    destino = tmp_path / "unidad" / "RSBE01.wbfs"
    destino.parent.mkdir()

    wit_wrapper.convert(origen, destino, "WBFS", str(wit_falso.binario))

    (carpeta, args), = wit_falso.llamadas()
    assert carpeta == str(destino.parent)
    assert args[args.index("--dest") + 1] == "RSBE01.wbfs"
    # El origen va con su ruta absoluta: no depende de la carpeta de trabajo.
    assert args[args.index("--dest") - 1] == str(origen)


def test_un_origen_relativo_sigue_funcionando(tmp_path, wit_falso, monkeypatch):
    """`wit` ya no corre en la carpeta de la app: un origen relativo se pasa
    resuelto, si no `wit` lo buscaría en la carpeta del destino."""
    (tmp_path / "biblioteca").mkdir()
    (tmp_path / "biblioteca" / "Brawl.iso").write_bytes(b"RSBE01 disco de wii")
    (tmp_path / "unidad").mkdir()
    monkeypatch.chdir(tmp_path)

    resultado = wit_wrapper.convert(Path("biblioteca/Brawl.iso"), Path("unidad/RSBE01.wbfs"),
                                    "WBFS", str(wit_falso.binario))

    assert resultado.returncode == 0, resultado.stderr
    assert (tmp_path / "unidad" / "RSBE01.wbfs").read_bytes() == b"RSBE01 disco de wii"


def test_comparar_con_un_primer_archivo_relativo(tmp_path, wit_falso, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "game.ciso").write_bytes(b"D43E01 x")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "game.iso").write_bytes(b"D43E01 x")
    monkeypatch.chdir(tmp_path)

    assert wit_wrapper.images_match(Path("a/game.ciso"), Path("b/game.iso"),
                                    str(wit_falso.binario))


def test_el_comando_que_queda_en_el_log_se_puede_pegar_tal_cual(tmp_path, wit_falso, capsys):
    """El log dice desde qué carpeta corrió `wit`: sin eso, el nombre
    relativo del destino no sirve para repetir el comando a mano."""
    origen = tmp_path / "biblioteca" / "Brawl.iso"
    origen.parent.mkdir()
    origen.write_bytes(b"RSBE01 disco de wii")
    destino = tmp_path / "MI  USB" / "RSBE01.wbfs"
    destino.parent.mkdir()

    wit_wrapper.convert(origen, destino, "WBFS", str(wit_falso.binario))

    linea = capsys.readouterr().err.split("[wiibackup-manager] wit: ", 1)[1].splitlines()[0]
    (carpeta, args), = wit_falso.llamadas()
    assert shlex.split(linea) == ["cd", carpeta, "&&", str(wit_falso.binario), *args]
