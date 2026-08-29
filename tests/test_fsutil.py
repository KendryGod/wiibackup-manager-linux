"""Pruebas de `fsutil.py`, el módulo compartido de escritura atómica y
ubicación de datos instalados.

Vale la pena testearlo aparte y no solo a través de sus cuatro usuarios
(`gametdb._store_cover`, `oscwii_client.get_icon_path`,
`golden_configs._copy_atomic`, `oscwii_installer._extract_member`):
ahora el manejo de errores de los cuatro está definido en un solo lugar,
así que una regresión acá se propaga a todos a la vez. Lo que más importa
verificar es el contrato de fallo -el destino no se toca y el temporal no
queda tirado- porque es justamente lo que cada copia hacía distinto (o,
en el caso del extractor de ZIP, no hacía)."""
from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

import pytest

from wiibackup_manager import fsutil


# ======================================================== atomic_target --
def test_escritura_exitosa_deja_el_contenido(tmp_path):
    dest = tmp_path / "archivo.bin"
    with fsutil.atomic_target(dest) as tmp:
        tmp.write_bytes(b"contenido nuevo")
    assert dest.read_bytes() == b"contenido nuevo"


def test_escritura_exitosa_no_deja_temporales(tmp_path):
    dest = tmp_path / "archivo.bin"
    with fsutil.atomic_target(dest) as tmp:
        tmp.write_bytes(b"x")
    assert [p.name for p in tmp_path.iterdir()] == ["archivo.bin"]


def test_sobreescribe_el_destino_existente(tmp_path):
    dest = tmp_path / "archivo.bin"
    dest.write_bytes(b"viejo")
    with fsutil.atomic_target(dest) as tmp:
        tmp.write_bytes(b"nuevo")
    assert dest.read_bytes() == b"nuevo"


def test_el_destino_viejo_queda_intacto_si_el_bloque_falla(tmp_path):
    """Lo que hace que valga la pena el temporal: un fallo a mitad de la
    escritura no puede dejar al usuario sin el archivo que ya tenía."""
    dest = tmp_path / "archivo.bin"
    dest.write_bytes(b"el respaldo bueno")

    with pytest.raises(OSError):
        with fsutil.atomic_target(dest) as tmp:
            tmp.write_bytes(b"a medio escribir")
            raise OSError("unidad desconectada")

    assert dest.read_bytes() == b"el respaldo bueno"


def test_el_temporal_se_borra_si_el_bloque_falla(tmp_path):
    dest = tmp_path / "archivo.bin"
    with pytest.raises(OSError):
        with fsutil.atomic_target(dest) as tmp:
            tmp.write_bytes(b"a medio escribir")
            raise OSError("unidad desconectada")
    assert list(tmp_path.iterdir()) == []


def test_el_destino_no_se_crea_si_el_bloque_falla(tmp_path):
    dest = tmp_path / "archivo.bin"
    with pytest.raises(RuntimeError):
        with fsutil.atomic_target(dest) as tmp:
            tmp.write_bytes(b"x")
            raise RuntimeError("cualquier cosa")
    assert not dest.exists()


def test_una_excepcion_que_no_es_oserror_tambien_limpia(tmp_path):
    """El helper atrapa BaseException, no solo OSError: una cancelación o
    un error de programación tampoco pueden dejar basura en la unidad."""
    dest = tmp_path / "archivo.bin"
    with pytest.raises(KeyboardInterrupt):
        with fsutil.atomic_target(dest) as tmp:
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
    with fsutil.atomic_target(dest):
        pass
    assert dest.read_bytes() == b""
    assert [p.name for p in tmp_path.iterdir()] == ["archivo.bin"]


def test_falla_de_replace_borra_el_temporal(tmp_path, monkeypatch):
    dest = tmp_path / "archivo.bin"

    def boom_replace(src, dst):
        raise OSError("no se pudo renombrar")

    monkeypatch.setattr(fsutil.os, "replace", boom_replace)
    with pytest.raises(OSError):
        with fsutil.atomic_target(dest) as tmp:
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
        with fsutil.atomic_target(dest) as tmp:
            tmp.write_bytes(b"x")
            raise RuntimeError("el error de verdad")


def test_el_temporal_es_oculto_y_hermano_del_destino(tmp_path):
    """Contrato con `tools/manual_queue_e2e.py`, que busca los temporales
    huérfanos por `rglob(".*")`, y con el escaneo de la biblioteca, que
    ignora los archivos ocultos. Hermano del destino, además, porque el
    `os.replace` final solo es atómico dentro del mismo filesystem."""
    dest = tmp_path / "Juego.iso"
    visto = {}
    with fsutil.atomic_target(dest) as tmp:
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
    with fsutil.atomic_target(dest) as a, fsutil.atomic_target(dest) as b:
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
            with fsutil.atomic_target(dest) as tmp:
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
    with fsutil.atomic_target(dest) as tmp:
        tmp.write_bytes(b"x")

    assert (stat.S_IMODE(dest.stat().st_mode)
            == stat.S_IMODE(referencia.stat().st_mode))


def test_mkparents_crea_la_carpeta_del_destino(tmp_path):
    dest = tmp_path / "sub" / "carpeta" / "archivo.bin"
    with fsutil.atomic_target(dest, mkparents=True) as tmp:
        tmp.write_bytes(b"x")
    assert dest.read_bytes() == b"x"


def test_sin_mkparents_una_carpeta_inexistente_falla(tmp_path):
    dest = tmp_path / "no-existe" / "archivo.bin"
    with pytest.raises(OSError):
        with fsutil.atomic_target(dest) as tmp:
            tmp.write_bytes(b"x")
    assert not dest.exists()


def test_acepta_una_ruta_en_texto(tmp_path):
    dest = tmp_path / "archivo.bin"
    with fsutil.atomic_target(str(dest)) as tmp:
        tmp.write_bytes(b"x")
    assert dest.read_bytes() == b"x"


# =================================================== installed_data_dirs --
def test_el_primer_candidato_es_el_repo_clonado():
    dirs = fsutil.installed_data_dirs("data/locale", "locale")
    paquete = Path(fsutil.__file__).resolve().parent
    assert dirs[0] == paquete.parent / "data" / "locale"


def test_no_devuelve_candidatos_repetidos():
    dirs = fsutil.installed_data_dirs("data/locale", "locale")
    assert len(dirs) == len(set(dirs))


def test_incluye_las_rutas_de_sistema():
    dirs = fsutil.installed_data_dirs("assets/configs", "wiibackup-manager/configs")
    assert Path("/usr/share/wiibackup-manager/configs") in dirs
    assert Path("/usr/local/share/wiibackup-manager/configs") in dirs
    assert Path.home() / ".local/share/wiibackup-manager/configs" in dirs


def test_la_cola_share_se_aplica_a_todos_los_prefijos():
    dirs = fsutil.installed_data_dirs("data/locale", "locale")
    # Todos menos el primero (el repo) cuelgan de algún .../share/locale
    for d in dirs[1:]:
        assert d.parent.name == "share"
        assert d.name == "locale"


def test_todas_las_rutas_son_absolutas_salvo_lo_que_dependa_del_repo():
    dirs = fsutil.installed_data_dirs("data/locale", "locale")
    assert all(d.is_absolute() for d in dirs)


def test_instalado_en_site_packages_deduce_el_prefijo(monkeypatch):
    """La parte sutil de esta función: con `pip install --user`,
    `sys.prefix` sigue siendo /usr, así que sin deducir el prefijo del
    propio paquete un catálogo viejo del sistema le ganaría al recién
    instalado. Corriendo desde el repo clonado esta rama no se ejecuta
    nunca, así que se simula moviendo el `__file__` del módulo."""
    monkeypatch.setattr(
        fsutil, "__file__",
        "/fake/prefijo/lib/python3.14/site-packages/wiibackup_manager/fsutil.py")

    dirs = fsutil.installed_data_dirs("data/locale", "locale")

    # Va inmediatamente después del repo y ANTES de sys.prefix y del resto.
    assert dirs[1] == Path("/fake/prefijo/share/locale")


def test_instalado_en_dist_packages_tambien_deduce_el_prefijo(monkeypatch):
    """Debian/Ubuntu usan "dist-packages" donde Fedora usa
    "site-packages"; las dos tienen que funcionar igual."""
    monkeypatch.setattr(
        fsutil, "__file__",
        "/fake/prefijo/lib/python3/dist-packages/wiibackup_manager/fsutil.py")

    dirs = fsutil.installed_data_dirs("assets/configs", "wiibackup-manager/configs")

    assert dirs[1] == Path("/fake/prefijo/share/wiibackup-manager/configs")
