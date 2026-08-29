"""Primitivas de `atomicfs.py`: escritura atómica, apartar/devolver y
carpeta de staging.

Los tests de `atomic_write_target` vienen de `test_fsutil.py` -esa función
se llamaba `fsutil.atomic_target` y se mudó acá cuando se extrajeron las
primitivas compartidas-. Se movieron tal cual: mismas aserciones, mismo
comportamiento verificado, solo cambió el nombre con el que se la llama.
Eso es a propósito, porque son la prueba de que el refactor no cambió
nada de lo que ya funcionaba.

Lo que se agrega abajo son los tests de las primitivas que ANTES no
existían como tales, porque estaban enterradas dentro de
`DestinationGuard` y del instalador de Homebrew: `SetAside` y
`staged_directory`. Cada una se prueba por lo que garantiza el MECANISMO
-qué queda en disco después de cada tipo de fallo, y qué se reporta- sin
meterse con lo que cada módulo decide hacer con eso, que sigue probado en
`test_library.py` y `test_oscwii_installer_atomic_unit.py`.
"""
from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from wiibackup_manager import atomicfs


# ============================================== atomic_write_target --
def test_escritura_exitosa_deja_el_contenido(tmp_path):
    dest = tmp_path / "archivo.bin"
    with atomicfs.atomic_write_target(dest) as tmp:
        tmp.write_bytes(b"contenido nuevo")
    assert dest.read_bytes() == b"contenido nuevo"


def test_escritura_exitosa_no_deja_temporales(tmp_path):
    dest = tmp_path / "archivo.bin"
    with atomicfs.atomic_write_target(dest) as tmp:
        tmp.write_bytes(b"x")
    assert [p.name for p in tmp_path.iterdir()] == ["archivo.bin"]


def test_sobreescribe_el_destino_existente(tmp_path):
    dest = tmp_path / "archivo.bin"
    dest.write_bytes(b"viejo")
    with atomicfs.atomic_write_target(dest) as tmp:
        tmp.write_bytes(b"nuevo")
    assert dest.read_bytes() == b"nuevo"


def test_el_destino_viejo_queda_intacto_si_el_bloque_falla(tmp_path):
    """Lo que hace que valga la pena el temporal: un fallo a mitad de la
    escritura no puede dejar al usuario sin el archivo que ya tenía."""
    dest = tmp_path / "archivo.bin"
    dest.write_bytes(b"el respaldo bueno")

    with pytest.raises(OSError):
        with atomicfs.atomic_write_target(dest) as tmp:
            tmp.write_bytes(b"a medio escribir")
            raise OSError("unidad desconectada")

    assert dest.read_bytes() == b"el respaldo bueno"


def test_el_temporal_se_borra_si_el_bloque_falla(tmp_path):
    dest = tmp_path / "archivo.bin"
    with pytest.raises(OSError):
        with atomicfs.atomic_write_target(dest) as tmp:
            tmp.write_bytes(b"a medio escribir")
            raise OSError("unidad desconectada")
    assert list(tmp_path.iterdir()) == []


def test_el_destino_no_se_crea_si_el_bloque_falla(tmp_path):
    dest = tmp_path / "archivo.bin"
    with pytest.raises(RuntimeError):
        with atomicfs.atomic_write_target(dest) as tmp:
            tmp.write_bytes(b"x")
            raise RuntimeError("cualquier cosa")
    assert not dest.exists()


def test_una_excepcion_que_no_es_oserror_tambien_limpia(tmp_path):
    """El helper atrapa BaseException, no solo OSError: una cancelación o
    un error de programación tampoco pueden dejar basura en la unidad."""
    dest = tmp_path / "archivo.bin"
    with pytest.raises(KeyboardInterrupt):
        with atomicfs.atomic_write_target(dest) as tmp:
            tmp.write_bytes(b"x")
            raise KeyboardInterrupt()
    assert list(tmp_path.iterdir()) == []


def test_si_el_bloque_no_escribe_nada_el_destino_queda_vacio(tmp_path):
    """Cambio de contrato al pasar a `mkstemp`, anotado a propósito.

    Antes el temporal no existía hasta que el bloque lo creaba, así que un
    bloque que no escribía nada hacía fallar el `os.replace` con OSError y
    el destino no se creaba. Ahora `mkstemp` crea el archivo por
    adelantado -que es justamente de dónde sale la unicidad garantizada
    del nombre-, así que "no escribir nada" produce un destino vacío en
    vez de un error.

    Es el resultado correcto para el único caso real que llega acá: un
    miembro de ZIP de 0 bytes tiene que instalarse como un archivo de 0
    bytes, no hacer fracasar la instalación entera. Lo que sí sigue
    valiendo es que no quede ningún temporal tirado."""
    dest = tmp_path / "archivo.bin"
    with atomicfs.atomic_write_target(dest):
        pass
    assert dest.read_bytes() == b""
    assert [p.name for p in tmp_path.iterdir()] == ["archivo.bin"]


def test_falla_de_replace_borra_el_temporal(tmp_path, monkeypatch):
    dest = tmp_path / "archivo.bin"

    def boom_replace(src, dst):
        raise OSError("no se pudo renombrar")

    monkeypatch.setattr(atomicfs.os, "replace", boom_replace)
    with pytest.raises(OSError):
        with atomicfs.atomic_write_target(dest) as tmp:
            tmp.write_bytes(b"x")
    assert list(tmp_path.iterdir()) == []


def test_una_limpieza_que_falla_no_tapa_el_error_original(tmp_path, monkeypatch):
    """Si tampoco se puede borrar el temporal, quien llama tiene que
    seguir viendo el error de verdad y no un error del borrado."""
    dest = tmp_path / "archivo.bin"

    def boom_unlink(self, *a, **kw):
        raise OSError("tampoco se puede borrar")

    monkeypatch.setattr(Path, "unlink", boom_unlink)

    with pytest.raises(RuntimeError, match="el error de verdad"):
        with atomicfs.atomic_write_target(dest) as tmp:
            tmp.write_bytes(b"x")
            raise RuntimeError("el error de verdad")


def test_el_temporal_es_oculto_y_hermano_del_destino(tmp_path):
    """Contrato con `tools/manual_queue_e2e.py`, que busca los temporales
    huérfanos por `rglob(".*")`, y con el escaneo de la biblioteca, que
    ignora los archivos ocultos. Hermano del destino, además, porque el
    `os.replace` final solo es atómico dentro del mismo filesystem."""
    dest = tmp_path / "Juego.iso"
    visto = {}
    with atomicfs.atomic_write_target(dest) as tmp:
        visto["nombre"] = tmp.name
        visto["carpeta"] = tmp.parent
        tmp.write_bytes(b"x")

    assert visto["nombre"].startswith(".Juego.iso.parcial-")
    assert visto["carpeta"] == tmp_path  # hermano del destino, mismo filesystem


def test_dos_llamadas_al_mismo_destino_usan_temporales_distintos(tmp_path):
    """El nombre lo da `mkstemp` (O_CREAT|O_EXCL), no una convención con
    el PID adentro: dos llamadas al MISMO destino no pueden coincidir ni
    aunque salgan del mismo proceso."""
    dest = tmp_path / "archivo.bin"
    with atomicfs.atomic_write_target(dest) as a, atomicfs.atomic_write_target(dest) as b:
        assert a != b
        a.write_bytes(b"a")
        b.write_bytes(b"b")


def test_dos_threads_al_mismo_destino_no_se_pisan_el_temporal(tmp_path):
    """El caso concreto que el esquema por PID no cubría.

    Dos threads del MISMO proceso escribiendo al mismo destino calculaban
    el mismo nombre de temporal (`.<nombre>.parcial-<pid>`), así que el
    segundo truncaba y sobrescribía lo que el primero todavía estaba
    escribiendo, y el `os.replace` del segundo se encontraba sin archivo.

    La barrera del medio es lo que hace que el test sirva: fuerza a que
    los dos threads estén DENTRO del bloque, con lo suyo ya escrito, al
    mismo tiempo. Recién ahí cada uno relee su temporal: si lo
    compartieran, uno de los dos leería el contenido del otro."""
    dest = tmp_path / "archivo.bin"
    contenidos = {"A": b"A" * 4096, "B": b"B" * 4096}
    barrera = threading.Barrier(2)
    lock = threading.Lock()
    temporales: list = []
    errores: list = []

    def escribir(clave: str) -> None:
        try:
            with atomicfs.atomic_write_target(dest) as tmp:
                with lock:
                    temporales.append(tmp)
                tmp.write_bytes(contenidos[clave])
                # Los dos ya escribieron: a partir de acá, cualquier
                # mezcla entre ellos es visible.
                barrera.wait(timeout=10)
                assert tmp.read_bytes() == contenidos[clave], (
                    f"el temporal de {clave} lo pisó el otro thread")
        except BaseException as e:  # noqa: BLE001 - se revisa abajo
            errores.append(e)
            barrera.abort()

    hilos = [threading.Thread(target=escribir, args=(c,)) for c in contenidos]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join(timeout=20)
        assert not h.is_alive(), "un thread quedó colgado"

    assert not errores, errores
    assert len(set(temporales)) == 2, f"compartieron el temporal: {temporales}"
    # Gana uno de los dos -cuál, depende de quién haga el `os.replace`
    # último-, pero el destino queda con UNO de los dos contenidos entero,
    # nunca con una mezcla.
    assert dest.read_bytes() in contenidos.values()
    assert [p.name for p in tmp_path.iterdir()] == ["archivo.bin"]


def test_el_destino_conserva_los_permisos_de_siempre(tmp_path):
    """`mkstemp` crea en 0600, pero el temporal de este helper termina
    SIENDO el archivo final: sin corregirlo, la caché, las configs
    maestras y las apps instaladas habrían cambiado de permisos en
    silencio. La referencia es un archivo creado con un `open()` normal,
    o sea con el mismo umask que regía antes."""
    referencia = tmp_path / "referencia.bin"
    referencia.write_bytes(b"x")

    dest = tmp_path / "archivo.bin"
    with atomicfs.atomic_write_target(dest) as tmp:
        tmp.write_bytes(b"x")

    assert (stat.S_IMODE(dest.stat().st_mode)
            == stat.S_IMODE(referencia.stat().st_mode))


def test_mkparents_crea_la_carpeta_del_destino(tmp_path):
    dest = tmp_path / "sub" / "carpeta" / "archivo.bin"
    with atomicfs.atomic_write_target(dest, mkparents=True) as tmp:
        tmp.write_bytes(b"x")
    assert dest.read_bytes() == b"x"


def test_sin_mkparents_una_carpeta_inexistente_falla(tmp_path):
    dest = tmp_path / "no-existe" / "archivo.bin"
    with pytest.raises(OSError):
        with atomicfs.atomic_write_target(dest) as tmp:
            tmp.write_bytes(b"x")
    assert not dest.exists()


def test_acepta_una_ruta_en_texto(tmp_path):
    dest = tmp_path / "archivo.bin"
    with atomicfs.atomic_write_target(str(dest)) as tmp:
        tmp.write_bytes(b"x")
    assert dest.read_bytes() == b"x"


# ================================================ atomic_write_stream --
# La variante de "archivo abierto", para quien escribe de a bloques.
def test_stream_escribe_y_deja_el_contenido(tmp_path):
    dest = tmp_path / "archivo.bin"
    with atomicfs.atomic_write_stream(dest) as (f, tmp):
        f.write(b"parte uno ")
        f.write(b"parte dos")
        assert not dest.exists(), "el destino no se toca hasta el intercambio"
    assert dest.read_bytes() == b"parte uno parte dos"
    assert [p.name for p in tmp_path.iterdir()] == ["archivo.bin"]


def test_stream_no_toca_el_destino_viejo_si_falla(tmp_path):
    dest = tmp_path / "archivo.bin"
    dest.write_bytes(b"el respaldo bueno")

    with pytest.raises(OSError):
        with atomicfs.atomic_write_stream(dest) as (f, _tmp):
            f.write(b"a medio escribir")
            raise OSError("unidad desconectada")

    assert dest.read_bytes() == b"el respaldo bueno"
    assert [p.name for p in tmp_path.iterdir()] == ["archivo.bin"]


def test_stream_cierra_el_descriptor_aunque_el_bloque_falle(tmp_path):
    """El descriptor lo entrega `mkstemp` y queda a cargo del `with`: si
    el bloque revienta -por ejemplo al no poder abrir el origen de una
    copia- no puede quedar filtrado."""
    dest = tmp_path / "archivo.bin"
    antes = len(os.listdir("/proc/self/fd"))

    for _ in range(50):
        with pytest.raises(FileNotFoundError):
            with atomicfs.atomic_write_stream(dest) as (_f, _tmp):
                open(tmp_path / "no-existe.bin", "rb")

    assert len(os.listdir("/proc/self/fd")) == antes
    assert list(tmp_path.iterdir()) == []


def test_stream_con_fsync_baja_a_disco_antes_de_intercambiar(tmp_path, monkeypatch):
    """Durabilidad: sin el fsync previo, el rename puede quedar registrado
    mientras los datos siguen en cache, y un tirón del cable deja el
    destino nuevo incompleto y el viejo ya borrado."""
    dest = tmp_path / "archivo.bin"
    orden = []
    fsync_real, replace_real = atomicfs.os.fsync, atomicfs.os.replace
    monkeypatch.setattr(atomicfs.os, "fsync",
                        lambda fd: (orden.append("fsync"), fsync_real(fd))[1])
    monkeypatch.setattr(atomicfs.os, "replace",
                        lambda a, b: (orden.append("replace"), replace_real(a, b))[1])

    with atomicfs.atomic_write_stream(dest, fsync=True) as (f, _tmp):
        f.write(b"x")

    assert orden == ["fsync", "replace"]


def test_stream_sin_fsync_no_lo_llama(tmp_path, monkeypatch):
    """El fsync cuesta tiempo real en un USB: solo lo paga quien lo pide."""
    llamadas = []
    monkeypatch.setattr(atomicfs.os, "fsync", lambda fd: llamadas.append(fd))

    with atomicfs.atomic_write_stream(tmp_path / "archivo.bin") as (f, _tmp):
        f.write(b"x")

    assert llamadas == []


def test_before_replace_corre_sobre_el_temporal_y_antes_del_intercambio(tmp_path):
    """Es lo que necesita `library._copy_with_progress` para su
    `copystat`: aplicar algo sobre el temporal -no sobre el destino- justo
    antes de que ocupe el lugar."""
    dest = tmp_path / "archivo.bin"
    visto = {}

    def _antes(tmp):
        visto["ruta"] = tmp
        visto["existia_el_destino"] = dest.exists()
        os.chmod(tmp, 0o640)

    with atomicfs.atomic_write_stream(dest, before_replace=_antes) as (f, tmp):
        f.write(b"x")
        esperada = tmp

    assert visto["ruta"] == esperada
    assert visto["existia_el_destino"] is False
    assert stat.S_IMODE(dest.stat().st_mode) == 0o640


def test_si_before_replace_falla_el_destino_no_se_toca(tmp_path):
    dest = tmp_path / "archivo.bin"
    dest.write_bytes(b"lo viejo")

    def _revienta(_tmp):
        raise OSError("no se pudo copiar la metadata")

    with pytest.raises(OSError):
        with atomicfs.atomic_write_stream(dest, before_replace=_revienta) as (f, _t):
            f.write(b"lo nuevo")

    assert dest.read_bytes() == b"lo viejo"
    assert [p.name for p in tmp_path.iterdir()] == ["archivo.bin"]


# ========================================================= hidden_sibling --
def test_el_nombre_apartado_es_oculto_y_hermano(tmp_path):
    """Hermano para que el `os.replace` sea atómico (mismo filesystem), y
    oculto para que no lo tome el escaneo de la biblioteca."""
    ruta = atomicfs.hidden_sibling(tmp_path / "sub" / "Juego.wbfs", "respaldo")
    assert ruta.parent == tmp_path / "sub"
    assert ruta.name.startswith(".Juego.wbfs.respaldo-")
    assert str(os.getpid()) in ruta.name


# ================================================================ SetAside --
def test_setaside_aparta_y_devuelve(tmp_path):
    a = tmp_path / "juego.wbfs"
    a.write_bytes(b"original")

    aside = atomicfs.SetAside("respaldo")
    respaldo = aside.move_aside(a)

    assert not a.exists(), "el nombre público quedó libre"
    assert respaldo.read_bytes() == b"original"
    assert aside.pairs == [(a, respaldo)]

    assert aside.restore() == []
    assert a.read_bytes() == b"original"
    assert not respaldo.exists()
    assert aside.pairs == []


def test_setaside_descarta_y_no_deja_rastro(tmp_path):
    a = tmp_path / "juego.wbfs"
    a.write_bytes(b"original")
    aside = atomicfs.SetAside("respaldo")
    aside.move_aside(a)

    assert aside.discard() == []
    assert list(tmp_path.iterdir()) == []
    assert aside.pairs == []


def test_setaside_descarta_carpetas_tambien(tmp_path):
    """El respaldo de una app de Homebrew es una carpeta entera, no un
    archivo: descartarlo tiene que borrar el árbol."""
    carpeta = tmp_path / "App"
    (carpeta / "sub").mkdir(parents=True)
    (carpeta / "sub" / "boot.dol").write_bytes(b"x")

    aside = atomicfs.SetAside("wbm-respaldo")
    respaldo = aside.move_aside(carpeta)
    assert respaldo.is_dir()

    assert aside.discard() == []
    assert list(tmp_path.iterdir()) == []


def test_setaside_reporta_lo_que_no_pudo_devolver(tmp_path, monkeypatch):
    """Se intenta con TODOS aunque alguno falle -en un WBFS dividido no
    tiene sentido dejar dos partes sin restaurar porque la tercera se
    atoró- y lo que queda pendiente es exactamente lo que falló."""
    a = tmp_path / "juego.wbfs"
    b = tmp_path / "juego.wbf1"
    a.write_bytes(b"parte-a")
    b.write_bytes(b"parte-b")

    aside = atomicfs.SetAside("respaldo")
    aside.move_aside(a)
    respaldo_b = aside.move_aside(b)

    real = atomicfs.os.replace
    monkeypatch.setattr(
        atomicfs.os, "replace",
        lambda o, d: (_ for _ in ()).throw(OSError("no se pudo"))
        if Path(d) == b else real(o, d))

    pendientes = aside.restore()

    assert pendientes == [(b, respaldo_b)]
    assert aside.pairs == pendientes
    assert a.read_bytes() == b"parte-a", "el que sí se pudo volvió a su lugar"
    assert respaldo_b.read_bytes() == b"parte-b", "el que falló sigue rescatable"


def test_setaside_reporta_lo_que_no_pudo_borrar(tmp_path, monkeypatch):
    """Un respaldo que no se puede borrar no es un error de la operación,
    pero tampoco se ignora: son varios GB ocupados en un archivo oculto."""
    a = tmp_path / "juego.wbfs"
    a.write_bytes(b"original")
    aside = atomicfs.SetAside("respaldo")
    respaldo = aside.move_aside(a)

    monkeypatch.setattr(Path, "unlink",
                        lambda self, *args, **kw: (_ for _ in ()).throw(
                            OSError("unidad de solo lectura")))

    assert aside.discard() == [respaldo]
    assert respaldo.exists()


def test_setaside_tolera_un_respaldo_que_ya_no_esta(tmp_path):
    """Descartar algo que ya no existe no es un fallo que reportar."""
    a = tmp_path / "juego.wbfs"
    a.write_bytes(b"original")
    aside = atomicfs.SetAside("respaldo")
    respaldo = aside.move_aside(a)
    respaldo.unlink()

    assert aside.discard() == []


# ======================================================= staged_directory --
def test_staging_se_promueve_al_salir_bien(tmp_path):
    destino = tmp_path / "apps" / "App"

    with atomicfs.staged_directory(destino) as staging:
        assert staging.path.is_dir()
        assert staging.path.parent == destino.parent
        assert not destino.exists(), "el destino no existe hasta el intercambio"
        (staging.path / "boot.dol").write_bytes(b"nuevo")

    assert (destino / "boot.dol").read_bytes() == b"nuevo"
    assert staging.orphaned_backups == []
    assert [p.name for p in destino.parent.iterdir()] == ["App"]


def test_una_falla_al_llenar_no_toca_el_destino(tmp_path):
    """La garantía central: la staging se llena ENTERA antes de tocar
    nada, así que un fallo a mitad deja lo que había exactamente como
    estaba."""
    destino = tmp_path / "apps" / "App"
    destino.mkdir(parents=True)
    (destino / "boot.dol").write_bytes(b"la version vieja")

    with pytest.raises(RuntimeError):
        with atomicfs.staged_directory(destino) as staging:
            (staging.path / "boot.dol").write_bytes(b"a medio extraer")
            raise RuntimeError("el ZIP estaba cortado")

    assert (destino / "boot.dol").read_bytes() == b"la version vieja"
    assert [p.name for p in destino.parent.iterdir()] == ["App"]


def test_al_actualizar_desaparece_lo_que_la_version_nueva_no_trae(tmp_path):
    """Es lo que compra intercambiar la carpeta entera en vez de escribir
    archivo por archivo: no queda una mezcla de las dos versiones."""
    destino = tmp_path / "App"
    destino.mkdir()
    (destino / "boot.dol").write_bytes(b"vieja")
    (destino / "solo-en-la-vieja.txt").write_bytes(b"obsoleto")

    with atomicfs.staged_directory(destino) as staging:
        (staging.path / "boot.dol").write_bytes(b"nueva")

    assert (destino / "boot.dol").read_bytes() == b"nueva"
    assert not (destino / "solo-en-la-vieja.txt").exists()


def test_el_respaldo_que_no_se_pudo_borrar_se_reporta(tmp_path, monkeypatch):
    destino = tmp_path / "App"
    destino.mkdir()
    (destino / "boot.dol").write_bytes(b"vieja")

    real = atomicfs.shutil.rmtree

    def _rmtree(path, ignore_errors=False, **kw):
        if ".respaldo-" in str(path):
            if ignore_errors:
                return
            raise OSError("unidad de solo lectura")
        return real(path, ignore_errors=ignore_errors, **kw)

    monkeypatch.setattr(atomicfs.shutil, "rmtree", _rmtree)

    with atomicfs.staged_directory(destino) as staging:
        (staging.path / "boot.dol").write_bytes(b"nueva")

    assert (destino / "boot.dol").read_bytes() == b"nueva", "el intercambio salió bien"
    assert len(staging.orphaned_backups) == 1
    assert staging.orphaned_backups[0].is_dir()


def test_si_falla_el_intercambio_vuelve_la_version_anterior(tmp_path, monkeypatch):
    destino = tmp_path / "App"
    destino.mkdir()
    (destino / "boot.dol").write_bytes(b"vieja")

    real = atomicfs.os.replace
    monkeypatch.setattr(
        atomicfs.os, "replace",
        lambda o, d: (_ for _ in ()).throw(OSError("no se pudo promover"))
        if ".staging-" in Path(o).name else real(o, d))

    with pytest.raises(OSError):
        with atomicfs.staged_directory(destino) as staging:
            (staging.path / "boot.dol").write_bytes(b"nueva")

    assert (destino / "boot.dol").read_bytes() == b"vieja"
    assert [p.name for p in tmp_path.iterdir()] == ["App"]


def test_si_tampoco_se_puede_restaurar_se_conservan_los_dos(tmp_path, monkeypatch):
    """El caso catastrófico. Se levanta `SwapRollbackFailed` -sin mensajes
    para el usuario: eso lo pone quien llama- y NO se borra nada: la
    staging tiene la versión nueva completa y el respaldo la anterior, y
    dos candidatos rescatables a mano son mejor que borrar alguno por las
    dudas."""
    destino = tmp_path / "App"
    destino.mkdir()
    (destino / "boot.dol").write_bytes(b"vieja")

    real = atomicfs.os.replace

    def _replace(origen, dest):
        nombre = Path(origen).name
        if ".staging-" in nombre or ".respaldo-" in nombre:
            raise OSError("simulado: falla también al restaurar")
        return real(origen, dest)

    monkeypatch.setattr(atomicfs.os, "replace", _replace)

    with pytest.raises(atomicfs.SwapRollbackFailed) as exc_info:
        with atomicfs.staged_directory(destino) as staging:
            (staging.path / "boot.dol").write_bytes(b"nueva")

    error = exc_info.value
    assert len(error.pending) == 1
    original, respaldo = error.pending[0]
    assert original == destino
    assert isinstance(error.original_error, OSError)

    assert not destino.exists(), "el destino quedó sin su nombre público"
    assert (respaldo / "boot.dol").read_bytes() == b"vieja"
    assert (staging.path / "boot.dol").read_bytes() == b"nueva"


def test_una_staging_huerfana_del_mismo_pid_no_estorba(tmp_path):
    """El nombre lleva el PID, así que un intento anterior abortado del
    mismo proceso podría haber dejado una staging con ese nombre."""
    destino = tmp_path / "App"
    huerfana = atomicfs.hidden_sibling(destino, "staging")
    huerfana.mkdir(parents=True)
    (huerfana / "basura.txt").write_bytes(b"de un intento anterior")

    with atomicfs.staged_directory(destino) as staging:
        assert staging.path == huerfana
        assert list(staging.path.iterdir()) == [], "arranca vacía"
        (staging.path / "boot.dol").write_bytes(b"nueva")

    assert [p.name for p in destino.iterdir()] == ["boot.dol"]
