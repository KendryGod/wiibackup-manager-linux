"""Nombres de archivo, escaneo y las operaciones que tocan el disco.

El foco está en lo que puede perder datos: renombrar sin pisar un archivo
ajeno y armar la ruta de destino de una unidad WBFS.

Cubre el área completa que antes vivía en `library.py` y hoy está repartida
entre `game_model`, `scanning`, `fileops`, `transfer_plan`, `library_ops` y
`formatting`. Se mantiene en un solo archivo a propósito: las pruebas están
agrupadas por lo que le puede pasar a un juego -renombrarlo, copiarlo,
mandarlo a una unidad- y varias cruzan más de un módulo (mandar un juego
usa `transfer_plan` para la ruta y `library_ops` para escribirla), así que
partirlas por módulo las separaría de su propio contexto.
"""
from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from wiibackup_manager import (atomicfs, fileops, formatting, game_model,
                               library_ops, scanning, transfer_plan)


# --------------------------------------------------------------- Formato --
@pytest.mark.parametrize("n,esperado", [
    (0, "0.0 MB"),
    (5 * 1024 ** 2, "5.0 MB"),
    (1024 ** 3, "1.0 GB"),
    (int(4.7 * 1024 ** 3), "4.7 GB"),
])
def test_format_size(n, esperado):
    assert formatting.format_size(n) == esperado


@pytest.mark.parametrize("segundos,esperado", [
    (0, "0s"), (45, "45s"), (60, "1m"), (135, "2m 15s"),
    (3600, "1h"), (3900, "1h 5m"),
    (-10, "0s"),   # un ETA negativo (reloj corrido) se muestra como 0
])
def test_format_eta(segundos, esperado):
    assert formatting.format_eta(segundos) == esperado


# ------------------------------------------------------------- Nombres --
@pytest.mark.parametrize("crudo,esperado", [
    ("Mario Kart Wii", "Mario Kart Wii"),
    ("Zelda: Skyward Sword", "Zelda Skyward Sword"),
    ("Juego/con/barras", "Juegoconbarras"),
    ("  espacios  ", "espacios"),
    ("puntos...", "puntos"),
])
def test_sanitize_filename(crudo, esperado):
    assert game_model.sanitize_filename(crudo) == esperado


def test_sanitize_filename_nunca_devuelve_vacio():
    """Un título que queda vacío tras limpiarlo no puede dar un nombre de
    archivo vacío: el renombrado lo usaría como nombre real."""
    assert game_model.sanitize_filename("///") == "untitled"
    assert game_model.sanitize_filename("") == "untitled"


def test_standard_filename(make_game):
    juego = make_game(name="cualquier-cosa.wbfs", title="Mario Kart Wii",
                      game_id="RMCP01")
    assert game_model.standard_filename(juego) == "Mario Kart Wii [RMCP01].wbfs"


def test_standard_filename_omite_el_id_si_no_es_valido(make_game):
    """Sin ID válido se omite el sufijo en vez de escribir '??????' en el
    nombre: esos caracteres no son válidos en FAT32."""
    juego = make_game(name="x.iso", title="Desconocido", game_id="??????")
    assert game_model.standard_filename(juego) == "Desconocido.iso"


def test_needs_rename(make_game):
    ya_esta = make_game(name="Mario Kart Wii [RMCP01].iso", title="Mario Kart Wii")
    falta = make_game(name="mkwii.iso", title="Mario Kart Wii")
    assert not game_model.needs_rename(ya_esta)
    assert game_model.needs_rename(falta)


def test_free_variant_va_encontrando_huecos(tmp_path):
    base = tmp_path / "Juego.wbfs"
    assert fileops.free_variant(base) == base       # libre: se usa tal cual
    base.write_bytes(b"x")
    assert fileops.free_variant(base) == tmp_path / "Juego (2).wbfs"
    (tmp_path / "Juego (2).wbfs").write_bytes(b"x")
    assert fileops.free_variant(base) == tmp_path / "Juego (3).wbfs"


# ------------------------------------------------- Renombrar sin pisar --
def test_rename_no_replace_mueve_el_archivo(tmp_path):
    src = tmp_path / "a.iso"
    src.write_bytes(b"contenido")
    dest = tmp_path / "b.iso"
    fileops.rename_no_replace(src, dest)
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
        fileops.rename_no_replace(src, dest)

    assert dest.read_bytes() == b"OTRO JUEGO QUE NO SE PUEDE PERDER"
    assert src.exists()      # el origen tampoco se perdió


def test_rename_to_standard(make_game):
    juego = make_game(name="mkwii.iso", title="Mario Kart Wii", game_id="RMCP01")
    nuevo = library_ops.rename_to_standard(juego)
    assert nuevo.name == "Mario Kart Wii [RMCP01].iso"
    assert nuevo.exists()
    assert juego.path == nuevo          # el Game queda apuntando al archivo real


def test_rename_to_standard_dry_run_no_toca_el_disco(make_game):
    juego = make_game(name="mkwii.iso", title="Mario Kart Wii", game_id="RMCP01")
    original = juego.path
    propuesto = library_ops.rename_to_standard(juego, dry_run=True)
    assert propuesto.name == "Mario Kart Wii [RMCP01].iso"
    assert original.exists()            # sigue con el nombre viejo
    assert not propuesto.exists()


def test_rename_to_standard_con_colision_usa_sufijo(make_game, tmp_path):
    juego = make_game(name="mkwii.iso", title="Mario Kart Wii", game_id="RMCP01")
    ocupado = tmp_path / "Mario Kart Wii [RMCP01].iso"
    ocupado.write_bytes(b"OTRO ARCHIVO")

    nuevo = library_ops.rename_to_standard(juego, on_collision="suffix")

    assert nuevo.name == "Mario Kart Wii [RMCP01] (2).iso"
    assert ocupado.read_bytes() == b"OTRO ARCHIVO"     # intacto


def test_rename_to_standard_sin_sufijo_levanta_y_no_pisa(make_game, tmp_path):
    juego = make_game(name="mkwii.iso", title="Mario Kart Wii", game_id="RMCP01")
    ocupado = tmp_path / "Mario Kart Wii [RMCP01].iso"
    ocupado.write_bytes(b"OTRO ARCHIVO")

    with pytest.raises(FileExistsError):
        library_ops.rename_to_standard(juego, on_collision="error")

    assert ocupado.read_bytes() == b"OTRO ARCHIVO"


# ------------------------------------------------------ Destino WBFS --
def test_wbfs_dest_path(make_game, tmp_path):
    juego = make_game(game_id="RMCP01")
    destino = transfer_plan.wbfs_dest_path(juego, tmp_path)
    assert destino == tmp_path / "wbfs" / "RMCP01" / "RMCP01.wbfs"


def test_wbfs_dest_path_rechaza_un_id_que_se_escaparia_de_la_carpeta(make_game, tmp_path):
    """Sin esta validación, un header manipulado con '../..' escribiría
    fuera de la carpeta wbfs/ del pendrive."""
    juego = make_game(game_id="../../x")
    with pytest.raises(ValueError):
        transfer_plan.wbfs_dest_path(juego, tmp_path)


# --------------------------------------------------------- GameCube --
def test_gc_dest_path_usa_la_estructura_de_nintendont(make_game, tmp_path):
    juego = make_game(name="juego.iso", game_id="GZ2E01",
                      title="Twilight Princess", console="gc")
    destino = transfer_plan.gc_dest_path(juego, tmp_path)
    assert destino == tmp_path / "games" / "Twilight Princess [GZ2E01]" / "game.iso"


def test_gc_dest_path_conserva_la_extension_ciso(make_game, tmp_path):
    juego = make_game(name="juego.ciso", game_id="GZ2E01",
                      title="Twilight Princess", console="gc")
    destino = transfer_plan.gc_dest_path(juego, tmp_path)
    assert destino.name == "game.ciso"


def test_gc_dest_path_disco_2_va_a_la_misma_carpeta_como_disc2(make_game, tmp_path):
    disco1 = make_game(name="d1.iso", game_id="GZ2E01", title="RE4",
                       console="gc", disc_number=0)
    disco2 = make_game(name="d2.iso", game_id="GZ2E01", title="RE4",
                       console="gc", disc_number=1)
    dest1 = transfer_plan.gc_dest_path(disco1, tmp_path)
    dest2 = transfer_plan.gc_dest_path(disco2, tmp_path)
    assert dest1.parent == dest2.parent
    assert dest1.name == "game.iso"
    assert dest2.name == "disc2.iso"


def test_gc_dest_path_rechaza_un_id_invalido(make_game, tmp_path):
    juego = make_game(game_id="../../x", console="gc")
    with pytest.raises(ValueError):
        transfer_plan.gc_dest_path(juego, tmp_path)


def test_game_dest_path_enruta_segun_consola(make_game, tmp_path):
    wii = make_game(game_id="RMCP01", console="wii")
    gc = make_game(name="gc.iso", game_id="GZ2E01", console="gc")
    assert transfer_plan.game_dest_path(wii, tmp_path) == transfer_plan.wbfs_dest_path(wii, tmp_path)
    assert transfer_plan.game_dest_path(gc, tmp_path) == transfer_plan.gc_dest_path(gc, tmp_path)


def test_estimate_transfer_size_gc_es_el_tamano_de_origen(make_game):
    """GameCube nunca se convierte -Nintendont lee ISO/CISO tal cual-, así
    que no corresponde el margen de conversión a WBFS ni preguntarle a
    `wit`: lo que se escribe es exactamente lo que pesa el archivo."""
    juego = make_game(game_id="GZ2E01", console="gc", size=12345)
    assert transfer_plan.estimate_transfer_size(juego) == juego.size_bytes


def test_send_to_wbfs_drive_gc_no_evalua_needs_wbfs_split(make_game, tmp_path, monkeypatch):
    """El camino de GameCube en `send_to_wbfs_drive` corta antes de llegar
    a la lógica de split de FAT32 (que es cosa de Wii/`wit`): ni siquiera
    debería preguntarle a `drives.needs_wbfs_split` por el filesystem del
    destino."""
    def _no_deberia_llamarse(*_a, **_k):
        raise AssertionError(
            "needs_wbfs_split no debería evaluarse para un juego de GameCube")
    monkeypatch.setattr(library_ops.drives, "needs_wbfs_split", _no_deberia_llamarse)

    juego = make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                      console="gc", contenido=b"contenido de prueba gc")

    destino = library_ops.send_to_wbfs_drive(juego, tmp_path)

    assert destino == transfer_plan.gc_dest_path(juego, tmp_path)
    assert destino.read_bytes() == b"contenido de prueba gc"


def test_send_to_wbfs_drive_gc_nunca_divide_el_archivo(make_game, tmp_path, monkeypatch):
    """Aunque el destino sea (o parezca) FAT32, un juego de GameCube tiene
    que llegar entero: a diferencia de Wii, acá no hay conversión por
    `wit` ni `--split-size` de por medio, es una copia de archivo tal
    cual. `needs_wbfs_split` se fuerza a True para simular el peor caso
    (FAT32 real) y confirmar que igual no divide nada."""
    monkeypatch.setattr(library_ops.drives, "needs_wbfs_split", lambda path: True)

    juego = make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                      console="gc", contenido=b"contenido de prueba gc")

    destino = library_ops.send_to_wbfs_drive(juego, tmp_path)

    assert destino.read_bytes() == b"contenido de prueba gc"
    assert not destino.with_suffix(".wbf1").exists()


# ------------------------------------------------- WBFS multi-juego --
def test_send_to_wbfs_drive_wbfs_multi_juego_no_copia_directo(make_game, tmp_path, monkeypatch):
    """Un .wbfs cuyo contenedor trae más de un juego no se puede copiar
    tal cual al destino de un solo juego (wbfs/<ID6>/<ID6>.wbfs): eso es
    justo lo que `identify_file` no detecta (solo mira el primer ID6, ver
    `wit_wrapper._find_id6_line`), así que el chequeo tiene que pasar acá,
    antes del atajo de copia directa."""
    from wiibackup_manager.disc_header import DiscInfo

    contenido = [
        DiscInfo(game_id="RMCP01", title="Mario Kart Wii", source="wit"),
        DiscInfo(game_id="RSBE01", title="Super Smash Bros. Brawl", source="wit"),
    ]
    monkeypatch.setattr(library_ops.wit_wrapper, "is_available", lambda _binary: True)
    monkeypatch.setattr(library_ops.wit_wrapper, "list_wbfs_container",
                        lambda _path, _binary: contenido)

    juego = make_game(name="contenedor.wbfs", game_id="RMCP01",
                      title="Mario Kart Wii", fmt="WBFS",
                      contenido=b"contenedor multi-juego")

    with pytest.raises(library_ops.MultiGameContainerError):
        library_ops.send_to_wbfs_drive(juego, tmp_path)

    assert not transfer_plan.wbfs_dest_path(juego, tmp_path).exists()


def test_send_to_wbfs_drive_wbfs_un_solo_juego_copia_directo(make_game, tmp_path, monkeypatch):
    """Contraparte del test anterior: un contenedor de un solo juego sigue
    yendo por el atajo de copia directa de siempre."""
    from wiibackup_manager.disc_header import DiscInfo

    contenido = [DiscInfo(game_id="RMCP01", title="Mario Kart Wii", source="wit")]
    monkeypatch.setattr(library_ops.wit_wrapper, "is_available", lambda _binary: True)
    monkeypatch.setattr(library_ops.wit_wrapper, "list_wbfs_container",
                        lambda _path, _binary: contenido)

    juego = make_game(name="juego.wbfs", game_id="RMCP01", title="Mario Kart Wii",
                      fmt="WBFS", contenido=b"contenido de prueba wbfs")

    destino = library_ops.send_to_wbfs_drive(juego, tmp_path)

    assert destino.read_bytes() == b"contenido de prueba wbfs"


# ----------------------------------------------------------- Escaneo --
def test_find_game_files_encuentra_por_extension(tmp_path):
    (tmp_path / "a.iso").write_bytes(b"x")
    (tmp_path / "b.wbfs").write_bytes(b"x")
    (tmp_path / "c.ciso").write_bytes(b"x")
    (tmp_path / "d.wdf").write_bytes(b"x")
    (tmp_path / "leeme.txt").write_bytes(b"x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "e.iso").write_bytes(b"x")

    encontrados = {p.name for p in scanning.find_game_files(tmp_path)}
    assert encontrados == {"a.iso", "b.wbfs", "c.ciso", "d.wdf", "e.iso"}


def test_find_game_files_ignora_mayusculas_de_la_extension(tmp_path):
    (tmp_path / "GRITADO.ISO").write_bytes(b"x")
    assert [p.name for p in scanning.find_game_files(tmp_path)] == ["GRITADO.ISO"]


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
        encontrados = scanning.find_game_files(tmp_path, saltadas)
        assert [p.name for p in encontrados] == ["visible.iso"]
        assert saltadas, "la carpeta ilegible tiene que quedar anotada"
    finally:
        prohibida.chmod(0o755)


def test_identify_file_por_header(tmp_path, iso_bytes):
    iso = tmp_path / "sin-nombre.iso"
    iso.write_bytes(iso_bytes())
    juego = scanning.identify_file(iso)
    assert juego is not None
    assert juego.game_id == "RMCP01"
    assert juego.title == "MARIO KART WII"
    assert juego.fmt == "ISO"
    assert juego.identified_by == "iso"


def test_identify_file_ignora_extensiones_ajenas(tmp_path):
    otro = tmp_path / "notas.txt"
    otro.write_bytes(b"x")
    assert scanning.identify_file(otro) is None


def test_identify_file_detecta_gamecube_por_header(tmp_path, iso_bytes):
    iso = tmp_path / "sin-nombre.iso"
    iso.write_bytes(iso_bytes(game_id=b"GZ2E01", title=b"TWILIGHT PRINCESS",
                              console="gc"))
    juego = scanning.identify_file(iso)
    assert juego is not None
    assert juego.game_id == "GZ2E01"
    assert juego.console == "gc"
    assert juego.disc_number == 0


def test_identify_file_propaga_el_numero_de_disco_de_gamecube(tmp_path, iso_bytes):
    iso = tmp_path / "disco2.iso"
    iso.write_bytes(iso_bytes(game_id=b"GZ2E01", console="gc", disc_number=1))
    juego = scanning.identify_file(iso)
    assert juego.disc_number == 1


# ---------------------------------------------------------- Exportar --
def test_export_text(make_game):
    juegos = [make_game(name="a.iso", title="Juego A", size=2 * 1024 ** 3),
              make_game(name="b.iso", title="Juego B", size=1024 ** 3)]
    texto = formatting.export_games(juegos, formatting.EXPORT_TEXT)
    assert "Juego A — 2.0 GB" in texto
    assert "Juego B — 1.0 GB" in texto
    assert "2 juegos · 3.0 GB" in texto


def test_export_text_en_singular(make_game):
    texto = formatting.export_games([make_game(title="Solo")], formatting.EXPORT_TEXT)
    assert "1 juego · " in texto


def test_export_csv_lleva_encabezado_y_una_fila_por_juego(make_game):
    juegos = [make_game(name="a.iso", title="Zelda: Skyward Sword", game_id="RVLE01")]
    csv_texto = formatting.export_games(juegos, formatting.EXPORT_CSV)
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
    csv_texto = formatting.export_games(juegos, formatting.EXPORT_CSV)
    assert "\n=1+1" not in csv_texto
    assert ",=1+1" not in csv_texto


# --------------------------------------------------------- DestinationGuard --
def test_destination_guard_restaura_todo_si_la_operacion_falla(tmp_path):
    """Caso ya existente, sin cambios de comportamiento: si todo se
    puede restaurar, `_saved` queda vacío y no se levanta nada -la
    excepción de la operación (acá, el `RuntimeError` simulado) es la
    única que se propaga."""
    a = tmp_path / "juego.wbfs"
    b = tmp_path / "juego.wbf1"
    a.write_bytes(b"original-a")
    b.write_bytes(b"original-b")

    with pytest.raises(RuntimeError, match="la conversión falló"):
        with library_ops.DestinationGuard(a) as guard:
            # Simula lo que deja `wit` a mitad de una conversión que
            # después falla: los nombres finales ya tienen contenido
            # nuevo (parcial, corrupto, lo que sea).
            a.write_bytes(b"nuevo-a")
            b.write_bytes(b"nuevo-b")
            raise RuntimeError("la conversión falló")

    assert a.read_bytes() == b"original-a"
    assert b.read_bytes() == b"original-b"
    assert guard._saved == []


def test_destination_guard_restore_exitoso_no_deja_respaldos_sueltos(tmp_path):
    a = tmp_path / "juego.wbfs"
    a.write_bytes(b"original-a")

    with pytest.raises(RuntimeError):
        with library_ops.DestinationGuard(a) as guard:
            respaldo = guard._saved[0][1]
            assert respaldo.exists()  # apartado, listo para restaurar
            raise RuntimeError("falló")

    assert not respaldo.exists()  # se lo movió de vuelta, no quedó duplicado
    assert a.exists()


def test_destination_guard_restore_con_una_parte_que_falla_levanta_rollback_failed(
        tmp_path, monkeypatch):
    """El caso central: un WBFS dividido (wbfs/wbf1) donde restaurar UNA
    de las partes falla. Antes esto se ignoraba en silencio y `_saved`
    se vaciaba igual, como si hubiera salido bien -acá se confirma que
    ahora se levanta `RollbackFailedError` con el detalle correcto, que
    la parte que SÍ se pudo restaurar vuelve a su lugar igual (no todo o
    nada), y que `_saved` conserva justo lo pendiente."""
    a = tmp_path / "juego.wbfs"
    b = tmp_path / "juego.wbf1"
    a.write_bytes(b"original-a")
    b.write_bytes(b"original-b")

    real_replace = os.replace

    def _replace_que_falla_para_b(origen, destino):
        if Path(destino) == b:
            raise OSError("simulado: no se pudo restaurar juego.wbf1")
        return real_replace(origen, destino)

    monkeypatch.setattr(atomicfs.os, "replace", _replace_que_falla_para_b)

    with pytest.raises(library_ops.RollbackFailedError) as exc_info:
        with library_ops.DestinationGuard(a) as guard:
            a.write_bytes(b"nuevo-a")
            b.write_bytes(b"nuevo-b")
            raise RuntimeError("la conversión falló")

    error = exc_info.value

    # La parte que SÍ se pudo restaurar (a) volvió a su lugar. La que
    # falló (b) queda SIN el nombre público: `_cleanup_partials` ya
    # había borrado el contenido nuevo que dejó la conversión fallida
    # (para dejarle el lugar libre al respaldo) y el `os.replace` que
    # tenía que traer de vuelta el respaldo es justo el que falló -así
    # que el juego queda directamente inexistente, no solo corrupto.
    # No se pierde nada igual: el respaldo (ver más abajo) sigue intacto.
    assert a.read_bytes() == b"original-a"
    assert not b.exists()

    # `_saved` conserva EXACTAMENTE lo pendiente, no se vacía.
    assert guard._saved == [(b, guard._saved[0][1])]
    assert error.pending == guard._saved

    # El respaldo de la parte que falló sigue existiendo, en la ruta que
    # informa el error: es lo que permite rescatarlo a mano.
    original_pendiente, respaldo_pendiente = error.pending[0]
    assert original_pendiente == b
    assert respaldo_pendiente.exists()
    assert respaldo_pendiente.read_bytes() == b"original-b"

    # El original que sí se restauró NO aparece en pending.
    assert a not in [orig for orig, _resp in error.pending]

    assert str(b) in str(error)
    assert str(respaldo_pendiente) in str(error)


def test_rollback_failed_error_encadena_el_error_original(tmp_path, monkeypatch):
    """`__exit__` tiene que engancharle a `RollbackFailedError` la
    excepción que estaba activa DENTRO del `with` (la de la conversión
    fallida) como `original_error`: sin esto, `user_message()` no puede
    distinguir "la conversión falló" de "encima no se pudo restaurar"."""
    a = tmp_path / "juego.wbfs"
    a.write_bytes(b"original-a")

    real_replace = os.replace

    def _falla_solo_al_restaurar(origen, destino):
        # Apartar (`__enter__`) mueve el ORIGINAL a un nombre oculto
        # (".juego.wbfs.respaldo-N"); restaurar mueve ese oculto DE
        # VUELTA al nombre público. Fallar solo cuando el origen ya es
        # el oculto es lo que aísla el fallo a la restauración -si
        # fallara también al apartar, ni siquiera se llegaría a armar
        # el `with` para probar esto.
        if Path(origen).name.startswith("."):
            raise OSError("no se pudo restaurar")
        return real_replace(origen, destino)

    monkeypatch.setattr(atomicfs.os, "replace", _falla_solo_al_restaurar)

    with pytest.raises(library_ops.RollbackFailedError) as exc_info:
        with library_ops.DestinationGuard(a):
            raise RuntimeError("la conversión falló feo")

    error = exc_info.value
    assert isinstance(error.original_error, RuntimeError)
    assert str(error.original_error) == "la conversión falló feo"

    mensaje = error.user_message()
    assert "la conversión falló feo" in mensaje
    assert "no se pudo restaurar" in mensaje.lower() or "restaurar" in mensaje.lower()


def test_rollback_failed_error_sin_original_error_usa_el_mensaje_base():
    error = library_ops.RollbackFailedError(
        [(Path("/a/juego.wbfs"), Path("/a/.juego.wbfs.respaldo-1"))])
    assert error.original_error is None
    assert error.user_message() == str(error)


# ------------------------------------------ Copia atómica con progreso --
# `_copy_with_progress` es por donde pasan los ISO/WBFS de verdad
# (`copy_atomic`, `copy_no_replace` y las transferencias a GameCube), y no
# tenía pruebas propias. Se agregan junto con el cambio de nombre del
# temporal a `tempfile.mkstemp`: lo que más importa verificar es que ese
# cambio no se llevó puesto nada de lo que esta función ya garantizaba
# -bajar a disco antes del intercambio, cortar al cancelar, dejar el
# destino viejo intacto ante cualquier fallo, y copiar los permisos del
# origen como hace `shutil.copy2`.

class _EnElChunk:
    """Se hace pasar por un `CancellationToken` para meterse en medio de
    la copia.

    `_copy_with_progress` consulta `.cancelled` antes de leer cada bloque,
    así que es el único punto de la función donde un test puede pararse a
    mitad de camino sin parchear nada de adentro. `progress_cb` no sirve
    para esto: solo se llama una vez por segundo, y la última vez ya es
    después del `os.replace`.

    `en_consulta` es en qué consulta actuar. Para un origen de N bloques
    exactos, la consulta N+1 es la que ve el archivo agotado: ahí el
    temporal ya tiene TODO el contenido escrito y todavía no se hizo el
    intercambio, que es justo el instante que interesa."""

    def __init__(self, en_consulta: int, accion=None, cancelar_en=None):
        self.en_consulta = en_consulta
        self.accion = accion
        self.cancelar_en = cancelar_en
        self.consultas = 0

    @property
    def cancelled(self) -> bool:
        self.consultas += 1
        if self.cancelar_en is not None and self.consultas >= self.cancelar_en:
            return True
        if self.accion is not None and self.consultas == self.en_consulta:
            self.accion()
        return False


def _consultas_hasta_el_final(tamaño: int) -> int:
    """Cuántas veces se consulta `.cancelled` hasta que el origen se
    agota: una por bloque más la que se encuentra con el archivo
    terminado."""
    bloques, resto = divmod(tamaño, fileops._COPY_CHUNK_BYTES)
    return bloques + (1 if resto else 0) + 1


def test_copia_exitosa_deja_el_contenido_y_ningun_temporal(tmp_path):
    src = tmp_path / "Juego.iso"
    src.write_bytes(b"A" * 4096)
    dest = tmp_path / "destino" / "Juego.iso"
    dest.parent.mkdir()

    fileops.copy_atomic(src, dest)

    assert dest.read_bytes() == b"A" * 4096
    assert [p.name for p in dest.parent.iterdir()] == ["Juego.iso"]


def test_dos_copias_concurrentes_al_mismo_destino_no_se_pisan(tmp_path):
    """El caso que el nombre de temporal por PID no cubría.

    Dos threads del MISMO proceso copiando al mismo destino calculaban el
    mismo `.<nombre>.parcial-<pid>`, así que el segundo abría con "wb" el
    archivo que el primero estaba escribiendo -truncándole GB de ISO a
    mitad de camino- y el `os.replace` del primero terminaba moviendo
    datos de los dos mezclados, o directamente fallaba por no encontrar
    su temporal.

    Las dos barreras son para mirar el instante exacto: `listos` espera a
    que los DOS threads tengan su contenido entero escrito (última
    consulta de `.cancelled`, con el origen ya agotado) y todavía no
    hayan hecho el intercambio; ahí el hilo principal inspecciona la
    carpeta; recién entonces `seguir` los suelta."""
    chunk = fileops._COPY_CHUNK_BYTES
    origenes = {}
    for clave, byte in (("A", b"A"), ("B", b"B")):
        src = tmp_path / f"origen-{clave}.iso"
        src.write_bytes(byte * (2 * chunk))
        origenes[clave] = src

    dest = tmp_path / "destino" / "Juego.iso"
    dest.parent.mkdir()

    listos = threading.Barrier(3)
    seguir = threading.Barrier(3)
    errores: list = []

    def esperar_al_otro() -> None:
        listos.wait(timeout=30)
        seguir.wait(timeout=30)

    def copiar(clave: str) -> None:
        try:
            token = _EnElChunk(_consultas_hasta_el_final(2 * chunk),
                               accion=esperar_al_otro)
            fileops._copy_with_progress(origenes[clave], dest,
                                        lambda _n: None, cancel=token)
        except BaseException as e:  # noqa: BLE001 - se revisa abajo
            errores.append(e)
            listos.abort()
            seguir.abort()

    hilos = [threading.Thread(target=copiar, args=(c,)) for c in origenes]
    for h in hilos:
        h.start()

    # Los dos threads están parados entre las dos barreras: contenido
    # completo en sus temporales, ningún `os.replace` hecho todavía.
    listos.wait(timeout=30)
    temporales = sorted(p for p in dest.parent.iterdir() if p.name != dest.name)
    contenidos = {p: p.read_bytes() for p in temporales}
    seguir.wait(timeout=30)

    for h in hilos:
        h.join(timeout=60)
        assert not h.is_alive(), "un thread quedó colgado"

    assert not errores, errores
    assert len(temporales) == 2, f"compartieron el temporal: {temporales}"
    # Cada temporal tiene SU contenido entero: ninguno truncó ni mezcló
    # los bytes del otro.
    assert sorted(contenidos.values()) == sorted(
        [b"A" * (2 * chunk), b"B" * (2 * chunk)])
    # Gana el que haga el `os.replace` último, pero el destino queda con
    # uno de los dos originales entero, nunca con una mezcla.
    assert dest.read_bytes() in (b"A" * (2 * chunk), b"B" * (2 * chunk))
    assert [p.name for p in dest.parent.iterdir()] == ["Juego.iso"]


def test_los_permisos_del_destino_los_da_el_origen(tmp_path):
    """`mkstemp` crea en 0600, pero acá el modo final lo fija
    `shutil.copystat` copiando el del origen -es lo que hace a esta
    función equivalente a `shutil.copy2`-, así que el 0600 no llega nunca
    al archivo final."""
    src = tmp_path / "Juego.iso"
    src.write_bytes(b"x" * 128)
    os.chmod(src, 0o640)
    dest = tmp_path / "Juego-copia.iso"

    fileops.copy_atomic(src, dest)

    assert stat.S_IMODE(dest.stat().st_mode) == 0o640


def test_el_temporal_es_oculto_y_hermano_del_destino(tmp_path):
    """Oculto para que no lo tome un escaneo de la biblioteca (ni lo vea
    el usuario en la unidad), y hermano del destino porque el
    `os.replace` final solo es atómico dentro del mismo filesystem."""
    src = tmp_path / "Juego.iso"
    src.write_bytes(b"x" * fileops._COPY_CHUNK_BYTES)
    dest = tmp_path / "destino" / "Juego.iso"
    dest.parent.mkdir()

    visto: list = []
    token = _EnElChunk(_consultas_hasta_el_final(fileops._COPY_CHUNK_BYTES),
                       accion=lambda: visto.extend(
                           p for p in dest.parent.iterdir()))
    fileops._copy_with_progress(src, dest, lambda _n: None, cancel=token)

    assert len(visto) == 1
    assert visto[0].name.startswith(".Juego.iso.parcial-")
    assert visto[0].parent == dest.parent


def test_un_origen_vacio_produce_un_destino_vacio(tmp_path):
    """A diferencia de `atomicfs.atomic_write_target` -donde pasar a
    `mkstemp` cambió el caso "no se escribió nada" de OSError a destino
    vacío- acá no cambia nada: el temporal siempre se creó por adelantado (antes con
    `open(tmp, "wb")`, ahora con `mkstemp`), así que un origen vacío
    siempre produjo un destino vacío, que es lo correcto."""
    src = tmp_path / "vacio.iso"
    src.write_bytes(b"")
    dest = tmp_path / "destino.iso"

    fileops.copy_atomic(src, dest)

    assert dest.exists()
    assert dest.read_bytes() == b""


def test_la_cancelacion_corta_y_no_deja_rastro(tmp_path):
    """La cancelación se revisa entre bloques y sigue funcionando igual:
    corta en el momento, no crea el destino y no deja el temporal."""
    src = tmp_path / "Juego.iso"
    src.write_bytes(b"x" * (3 * fileops._COPY_CHUNK_BYTES))
    dest = tmp_path / "destino" / "Juego.iso"
    dest.parent.mkdir()

    token = _EnElChunk(0, cancelar_en=2)  # cancela con la copia empezada

    with pytest.raises(library_ops.wit_wrapper.OperationCancelled):
        fileops._copy_with_progress(src, dest, lambda _n: None, cancel=token)

    assert not dest.exists()
    assert list(dest.parent.iterdir()) == []


def test_el_destino_viejo_queda_intacto_si_la_copia_falla(tmp_path):
    """Lo que justifica todo el rodeo del temporal: un fallo a mitad de
    camino no puede dejar al cliente sin el respaldo que ya tenía."""
    src = tmp_path / "Juego.iso"
    src.write_bytes(b"nuevo" * 100)
    dest = tmp_path / "Juego.iso.dest"
    dest.write_bytes(b"el respaldo bueno")

    def _falla(_n):
        raise OSError("USB desenchufado")

    token = _EnElChunk(1, accion=lambda: _falla(0))
    with pytest.raises(OSError):
        fileops._copy_with_progress(src, dest, lambda _n: None, cancel=token)

    assert dest.read_bytes() == b"el respaldo bueno"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Juego.iso",
                                                          "Juego.iso.dest"]


def test_baja_a_disco_antes_de_intercambiar(tmp_path, monkeypatch):
    """Durabilidad: sin el `fsync` previo, el rename puede quedar
    registrado mientras los datos siguen en cache y un tirón del cable
    deja el destino nuevo incompleto y el viejo ya borrado. El cambio de
    temporal no puede haberse llevado eso puesto."""
    src = tmp_path / "Juego.iso"
    src.write_bytes(b"x" * 4096)
    dest = tmp_path / "destino.iso"

    orden: list = []
    fsync_real, replace_real = atomicfs.os.fsync, atomicfs.os.replace

    def fsync_espia(fd):
        orden.append("fsync")
        return fsync_real(fd)

    def replace_espia(a, b):
        orden.append("replace")
        return replace_real(a, b)

    monkeypatch.setattr(atomicfs.os, "fsync", fsync_espia)
    monkeypatch.setattr(atomicfs.os, "replace", replace_espia)

    fileops.copy_atomic(src, dest)

    assert orden == ["fsync", "replace"]


# ------------------------------------------ Respaldos que quedan huérfanos --
# Cuando la operación sale bien, `DestinationGuard` borra el respaldo. Si
# ESE borrado falla, antes se ignoraba en silencio: el respaldo es un
# archivo oculto que puede pesar varios GB (y en un WBFS dividido son
# tres), así que el usuario se quedaba con la unidad llena por algo que no
# podía ver ni encontrar.

def test_un_commit_normal_no_deja_respaldos_huerfanos(tmp_path):
    dest = tmp_path / "juego.wbfs"
    dest.write_bytes(b"lo que ya estaba")

    with library_ops.DestinationGuard(dest) as guard:
        dest.write_bytes(b"lo nuevo")
        guard.commit()

    assert guard.orphaned_backups == []
    assert dest.read_bytes() == b"lo nuevo"
    assert [p.name for p in tmp_path.iterdir()] == ["juego.wbfs"]


def test_un_respaldo_que_no_se_puede_borrar_queda_registrado(tmp_path, monkeypatch):
    """La operación salió bien igual -el resultado está donde tiene que
    estar- así que no se levanta ninguna excepción: el respaldo que quedó
    se anota para que quien usa el guard lo reporte."""
    dest = tmp_path / "juego.wbfs"
    dest.write_bytes(b"lo que ya estaba")

    def _unlink_que_falla(self, *a, **kw):
        raise OSError("unidad de solo lectura")
    monkeypatch.setattr(Path, "unlink", _unlink_que_falla)

    with library_ops.DestinationGuard(dest) as guard:
        dest.write_bytes(b"lo nuevo")
        guard.commit()

    assert len(guard.orphaned_backups) == 1
    respaldo = guard.orphaned_backups[0]
    assert respaldo.name.startswith(".juego.wbfs.respaldo-")
    assert respaldo.exists(), "el respaldo sigue ahí: por eso hay que avisar"
    assert dest.read_bytes() == b"lo nuevo"


def test_un_wbfs_dividido_reporta_todos_los_respaldos(tmp_path, monkeypatch):
    """Un juego dividido son varios archivos (wbfs/wbf1/wbf2), o sea
    varios respaldos: el aviso tiene que nombrarlos a todos."""
    for nombre in ("juego.wbfs", "juego.wbf1", "juego.wbf2"):
        (tmp_path / nombre).write_bytes(b"x" * 16)

    monkeypatch.setattr(Path, "unlink",
                        lambda self, *a, **kw: (_ for _ in ()).throw(
                            OSError("unidad de solo lectura")))

    with library_ops.DestinationGuard(tmp_path / "juego.wbfs") as guard:
        guard.commit()

    assert len(guard.orphaned_backups) == 3


def test_el_aviso_de_respaldo_huerfano_dice_cuanto_y_donde(tmp_path):
    """Sin la ruta el aviso no sirve: son archivos ocultos, así que el
    usuario ve la unidad más llena y no tiene cómo encontrar qué la
    ocupa."""
    respaldo = tmp_path / ".juego.wbfs.respaldo-123"
    respaldo.write_bytes(b"x" * (3 * 1024 * 1024))

    aviso = formatting.format_orphaned_backups([respaldo])

    assert str(respaldo) in aviso
    assert "3.0 MB" in aviso


def test_el_aviso_con_varios_respaldos_suma_el_total(tmp_path):
    rutas = []
    for i in range(3):
        r = tmp_path / f".parte{i}.respaldo-123"
        r.write_bytes(b"x" * (1024 * 1024))
        rutas.append(r)

    aviso = formatting.format_orphaned_backups(rutas)

    assert "3" in aviso
    assert "3.0 MB" in aviso
    for r in rutas:
        assert str(r) in aviso


def test_sin_respaldos_huerfanos_no_hay_aviso():
    assert formatting.format_orphaned_backups([]) == ""
