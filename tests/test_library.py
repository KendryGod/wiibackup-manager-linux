"""Nombres de archivo, escaneo y las operaciones que tocan el disco.

El foco está en lo que puede perder datos: renombrar sin pisar un archivo
ajeno y armar la ruta de destino de una unidad WBFS.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from wiibackup_manager import library


# --------------------------------------------------------------- Formato --
@pytest.mark.parametrize("n,esperado", [
    (0, "0.0 MB"),
    (5 * 1024 ** 2, "5.0 MB"),
    (1024 ** 3, "1.0 GB"),
    (int(4.7 * 1024 ** 3), "4.7 GB"),
])
def test_format_size(n, esperado):
    assert library.format_size(n) == esperado


@pytest.mark.parametrize("segundos,esperado", [
    (0, "0s"), (45, "45s"), (60, "1m"), (135, "2m 15s"),
    (3600, "1h"), (3900, "1h 5m"),
    (-10, "0s"),   # un ETA negativo (reloj corrido) se muestra como 0
])
def test_format_eta(segundos, esperado):
    assert library.format_eta(segundos) == esperado


# ------------------------------------------------------------- Nombres --
@pytest.mark.parametrize("crudo,esperado", [
    ("Mario Kart Wii", "Mario Kart Wii"),
    ("Zelda: Skyward Sword", "Zelda Skyward Sword"),
    ("Juego/con/barras", "Juegoconbarras"),
    ("  espacios  ", "espacios"),
    ("puntos...", "puntos"),
])
def test_sanitize_filename(crudo, esperado):
    assert library.sanitize_filename(crudo) == esperado


def test_sanitize_filename_nunca_devuelve_vacio():
    """Un título que queda vacío tras limpiarlo no puede dar un nombre de
    archivo vacío: el renombrado lo usaría como nombre real."""
    assert library.sanitize_filename("///") == "untitled"
    assert library.sanitize_filename("") == "untitled"


def test_standard_filename(make_game):
    juego = make_game(name="cualquier-cosa.wbfs", title="Mario Kart Wii",
                      game_id="RMCP01")
    assert library.standard_filename(juego) == "Mario Kart Wii [RMCP01].wbfs"


def test_standard_filename_omite_el_id_si_no_es_valido(make_game):
    """Sin ID válido se omite el sufijo en vez de escribir '??????' en el
    nombre: esos caracteres no son válidos en FAT32."""
    juego = make_game(name="x.iso", title="Desconocido", game_id="??????")
    assert library.standard_filename(juego) == "Desconocido.iso"


def test_needs_rename(make_game):
    ya_esta = make_game(name="Mario Kart Wii [RMCP01].iso", title="Mario Kart Wii")
    falta = make_game(name="mkwii.iso", title="Mario Kart Wii")
    assert not library.needs_rename(ya_esta)
    assert library.needs_rename(falta)


def test_free_variant_va_encontrando_huecos(tmp_path):
    base = tmp_path / "Juego.wbfs"
    assert library.free_variant(base) == base       # libre: se usa tal cual
    base.write_bytes(b"x")
    assert library.free_variant(base) == tmp_path / "Juego (2).wbfs"
    (tmp_path / "Juego (2).wbfs").write_bytes(b"x")
    assert library.free_variant(base) == tmp_path / "Juego (3).wbfs"


# ------------------------------------------------- Renombrar sin pisar --
def test_rename_no_replace_mueve_el_archivo(tmp_path):
    src = tmp_path / "a.iso"
    src.write_bytes(b"contenido")
    dest = tmp_path / "b.iso"
    library.rename_no_replace(src, dest)
    assert not src.exists()
    assert dest.read_bytes() == b"contenido"


def test_rename_no_replace_no_pisa_un_archivo_ajeno(tmp_path):
    """El bug que esto cubre: Path.rename reemplaza el destino en silencio,
    así que renombrar sobre un juego existente lo borraba sin aviso."""
    src = tmp_path / "a.iso"
    src.write_bytes(b"el que se mueve")
    dest = tmp_path / "b.iso"
    dest.write_bytes(b"OTRO JUEGO QUE NO SE PUEDE PERDER")

    with pytest.raises(FileExistsError):
        library.rename_no_replace(src, dest)

    assert dest.read_bytes() == b"OTRO JUEGO QUE NO SE PUEDE PERDER"
    assert src.exists()      # el origen tampoco se perdió


def test_rename_to_standard(make_game):
    juego = make_game(name="mkwii.iso", title="Mario Kart Wii", game_id="RMCP01")
    nuevo = library.rename_to_standard(juego)
    assert nuevo.name == "Mario Kart Wii [RMCP01].iso"
    assert nuevo.exists()
    assert juego.path == nuevo          # el Game queda apuntando al archivo real


def test_rename_to_standard_dry_run_no_toca_el_disco(make_game):
    juego = make_game(name="mkwii.iso", title="Mario Kart Wii", game_id="RMCP01")
    original = juego.path
    propuesto = library.rename_to_standard(juego, dry_run=True)
    assert propuesto.name == "Mario Kart Wii [RMCP01].iso"
    assert original.exists()            # sigue con el nombre viejo
    assert not propuesto.exists()


def test_rename_to_standard_con_colision_usa_sufijo(make_game, tmp_path):
    juego = make_game(name="mkwii.iso", title="Mario Kart Wii", game_id="RMCP01")
    ocupado = tmp_path / "Mario Kart Wii [RMCP01].iso"
    ocupado.write_bytes(b"OTRO ARCHIVO")

    nuevo = library.rename_to_standard(juego, on_collision="suffix")

    assert nuevo.name == "Mario Kart Wii [RMCP01] (2).iso"
    assert ocupado.read_bytes() == b"OTRO ARCHIVO"     # intacto


def test_rename_to_standard_sin_sufijo_levanta_y_no_pisa(make_game, tmp_path):
    juego = make_game(name="mkwii.iso", title="Mario Kart Wii", game_id="RMCP01")
    ocupado = tmp_path / "Mario Kart Wii [RMCP01].iso"
    ocupado.write_bytes(b"OTRO ARCHIVO")

    with pytest.raises(FileExistsError):
        library.rename_to_standard(juego, on_collision="error")

    assert ocupado.read_bytes() == b"OTRO ARCHIVO"


# ------------------------------------------------------ Destino WBFS --
def test_wbfs_dest_path(make_game, tmp_path):
    juego = make_game(game_id="RMCP01")
    destino = library.wbfs_dest_path(juego, tmp_path)
    assert destino == tmp_path / "wbfs" / "RMCP01" / "RMCP01.wbfs"


def test_wbfs_dest_path_rechaza_un_id_que_se_escaparia_de_la_carpeta(make_game, tmp_path):
    """Sin esta validación, un header manipulado con '../..' escribiría
    fuera de la carpeta wbfs/ del pendrive."""
    juego = make_game(game_id="../../x")
    with pytest.raises(ValueError):
        library.wbfs_dest_path(juego, tmp_path)


# --------------------------------------------------------- GameCube --
def test_gc_dest_path_usa_la_estructura_de_nintendont(make_game, tmp_path):
    juego = make_game(name="juego.iso", game_id="GZ2E01",
                      title="Twilight Princess", console="gc")
    destino = library.gc_dest_path(juego, tmp_path)
    assert destino == tmp_path / "games" / "Twilight Princess [GZ2E01]" / "game.iso"


def test_gc_dest_path_conserva_la_extension_ciso(make_game, tmp_path):
    juego = make_game(name="juego.ciso", game_id="GZ2E01",
                      title="Twilight Princess", console="gc")
    destino = library.gc_dest_path(juego, tmp_path)
    assert destino.name == "game.ciso"


def test_gc_dest_path_disco_2_va_a_la_misma_carpeta_como_disc2(make_game, tmp_path):
    disco1 = make_game(name="d1.iso", game_id="GZ2E01", title="RE4",
                       console="gc", disc_number=0)
    disco2 = make_game(name="d2.iso", game_id="GZ2E01", title="RE4",
                       console="gc", disc_number=1)
    dest1 = library.gc_dest_path(disco1, tmp_path)
    dest2 = library.gc_dest_path(disco2, tmp_path)
    assert dest1.parent == dest2.parent
    assert dest1.name == "game.iso"
    assert dest2.name == "disc2.iso"


def test_gc_dest_path_rechaza_un_id_invalido(make_game, tmp_path):
    juego = make_game(game_id="../../x", console="gc")
    with pytest.raises(ValueError):
        library.gc_dest_path(juego, tmp_path)


def test_game_dest_path_enruta_segun_consola(make_game, tmp_path):
    wii = make_game(game_id="RMCP01", console="wii")
    gc = make_game(name="gc.iso", game_id="GZ2E01", console="gc")
    assert library.game_dest_path(wii, tmp_path) == library.wbfs_dest_path(wii, tmp_path)
    assert library.game_dest_path(gc, tmp_path) == library.gc_dest_path(gc, tmp_path)


def test_estimate_transfer_size_gc_es_el_tamano_de_origen(make_game):
    """GameCube nunca se convierte -Nintendont lee ISO/CISO tal cual-, así
    que no corresponde el margen de conversión a WBFS ni preguntarle a
    `wit`: lo que se escribe es exactamente lo que pesa el archivo."""
    juego = make_game(game_id="GZ2E01", console="gc", size=12345)
    assert library.estimate_transfer_size(juego) == juego.size_bytes


def test_send_to_wbfs_drive_gc_no_evalua_needs_wbfs_split(make_game, tmp_path, monkeypatch):
    """El camino de GameCube en `send_to_wbfs_drive` corta antes de llegar
    a la lógica de split de FAT32 (que es cosa de Wii/`wit`): ni siquiera
    debería preguntarle a `drives.needs_wbfs_split` por el filesystem del
    destino."""
    def _no_deberia_llamarse(*_a, **_k):
        raise AssertionError(
            "needs_wbfs_split no debería evaluarse para un juego de GameCube")
    monkeypatch.setattr(library.drives, "needs_wbfs_split", _no_deberia_llamarse)

    juego = make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                      console="gc", contenido=b"contenido de prueba gc")

    destino = library.send_to_wbfs_drive(juego, tmp_path)

    assert destino == library.gc_dest_path(juego, tmp_path)
    assert destino.read_bytes() == b"contenido de prueba gc"


def test_send_to_wbfs_drive_gc_nunca_divide_el_archivo(make_game, tmp_path, monkeypatch):
    """Aunque el destino sea (o parezca) FAT32, un juego de GameCube tiene
    que llegar entero: a diferencia de Wii, acá no hay conversión por
    `wit` ni `--split-size` de por medio, es una copia de archivo tal
    cual. `needs_wbfs_split` se fuerza a True para simular el peor caso
    (FAT32 real) y confirmar que igual no divide nada."""
    monkeypatch.setattr(library.drives, "needs_wbfs_split", lambda path: True)

    juego = make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                      console="gc", contenido=b"contenido de prueba gc")

    destino = library.send_to_wbfs_drive(juego, tmp_path)

    assert destino.read_bytes() == b"contenido de prueba gc"
    assert not destino.with_suffix(".wbf1").exists()


# ----------------------------------------------------------- Escaneo --
def test_find_game_files_encuentra_por_extension(tmp_path):
    (tmp_path / "a.iso").write_bytes(b"x")
    (tmp_path / "b.wbfs").write_bytes(b"x")
    (tmp_path / "c.ciso").write_bytes(b"x")
    (tmp_path / "d.wdf").write_bytes(b"x")
    (tmp_path / "leeme.txt").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "e.iso").write_bytes(b"x")

    encontrados = {p.name for p in library.find_game_files(tmp_path)}
    assert encontrados == {"a.iso", "b.wbfs", "c.ciso", "d.wdf", "e.iso"}


def test_find_game_files_ignora_mayusculas_de_la_extension(tmp_path):
    (tmp_path / "GRITADO.ISO").write_bytes(b"x")
    assert [p.name for p in library.find_game_files(tmp_path)] == ["GRITADO.ISO"]


def test_find_game_files_reporta_las_carpetas_sin_permiso(tmp_path):
    """Una carpeta ilegible no puede hacer fallar el escaneo entero: se
    anota y el resto de la biblioteca aparece igual."""
    (tmp_path / "visible.iso").write_bytes(b"x")
    prohibida = tmp_path / "prohibida"
    prohibida.mkdir()
    (prohibida / "escondido.iso").write_bytes(b"x")
    prohibida.chmod(0o000)
    try:
        saltadas: list = []
        encontrados = library.find_game_files(tmp_path, saltadas)
        assert [p.name for p in encontrados] == ["visible.iso"]
        assert saltadas, "la carpeta ilegible tiene que quedar anotada"
    finally:
        prohibida.chmod(0o755)


def test_identify_file_por_header(tmp_path, iso_bytes):
    iso = tmp_path / "sin-nombre.iso"
    iso.write_bytes(iso_bytes())
    juego = library.identify_file(iso)
    assert juego is not None
    assert juego.game_id == "RMCP01"
    assert juego.title == "MARIO KART WII"
    assert juego.fmt == "ISO"
    assert juego.identified_by == "iso"


def test_identify_file_ignora_extensiones_ajenas(tmp_path):
    otro = tmp_path / "notas.txt"
    otro.write_bytes(b"x")
    assert library.identify_file(otro) is None


def test_identify_file_detecta_gamecube_por_header(tmp_path, iso_bytes):
    iso = tmp_path / "sin-nombre.iso"
    iso.write_bytes(iso_bytes(game_id=b"GZ2E01", title=b"TWILIGHT PRINCESS",
                              console="gc"))
    juego = library.identify_file(iso)
    assert juego is not None
    assert juego.game_id == "GZ2E01"
    assert juego.console == "gc"
    assert juego.disc_number == 0


def test_identify_file_propaga_el_numero_de_disco_de_gamecube(tmp_path, iso_bytes):
    iso = tmp_path / "disco2.iso"
    iso.write_bytes(iso_bytes(game_id=b"GZ2E01", console="gc", disc_number=1))
    juego = library.identify_file(iso)
    assert juego.disc_number == 1


# ---------------------------------------------------------- Exportar --
def test_export_text(make_game):
    juegos = [make_game(name="a.iso", title="Juego A", size=2 * 1024 ** 3),
              make_game(name="b.iso", title="Juego B", size=1024 ** 3)]
    texto = library.export_games(juegos, library.EXPORT_TEXT)
    assert "Juego A — 2.0 GB" in texto
    assert "Juego B — 1.0 GB" in texto
    assert "2 juegos · 3.0 GB" in texto


def test_export_text_en_singular(make_game):
    texto = library.export_games([make_game(title="Solo")], library.EXPORT_TEXT)
    assert "1 juego · " in texto


def test_export_csv_lleva_encabezado_y_una_fila_por_juego(make_game):
    juegos = [make_game(name="a.iso", title="Zelda: Skyward Sword", game_id="RVLE01")]
    csv_texto = library.export_games(juegos, library.EXPORT_CSV)
    lineas = csv_texto.strip().splitlines()
    assert lineas[0].startswith("Título,ID,Formato")
    assert "RVLE01" in lineas[1]
    # El título trae dos puntos y una coma potencial: el módulo csv lo
    # entrecomilla solo si hace falta, y en cualquier caso tiene que
    # volver a leerse como un único campo.
    import csv as _csv
    filas = list(_csv.reader(lineas))
    assert filas[1][0] == "Zelda: Skyward Sword"


def test_export_csv_neutraliza_formulas(make_game):
    """Un título que arranca con '=' lo interpretaría Excel/LibreOffice
    como fórmula al abrir la lista exportada."""
    juegos = [make_game(title="=1+1")]
    csv_texto = library.export_games(juegos, library.EXPORT_CSV)
    assert "\n=1+1" not in csv_texto
    assert ",=1+1" not in csv_texto
