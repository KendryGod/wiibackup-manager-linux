"""Recovery Manager: encontrar lo que dejó una sesión que se cortó a mitad.

Las unidades de prueba se arman a mano en `tmp_path` con la misma
estructura que deja la app de verdad (`wbfs/<ID6>/`, `apps/<App>/`) y con
restos con nombres exactamente iguales a los que escriben
`atomicfs.hidden_sibling` y `tempfile.mkstemp`. Nada de mocks del
filesystem: lo que se está probando es justamente si se reconoce un
archivo real por su nombre y si moverlo lo deja donde tiene que quedar.

Los PID muertos salen de un subproceso que se lanza y se espera: cuando
`wait()` vuelve, ese PID no existe más, que es exactamente la situación
que el Recovery Manager tiene que detectar. El PID vivo es el de la propia
suite (`os.getpid()`), así que los dos casos ejercitan la implementación
real de `process_is_alive` y no una versión inyectada.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from wiibackup_manager import atomicfs, library_ops, oscwii_installer
from wiibackup_manager import recovery_service as rs
from wiibackup_manager.operations import OperationKind, OperationManager


# ------------------------------------------------------------ Fixtures --
@pytest.fixture
def pid_muerto():
    """Un PID que con seguridad ya no corre: se lanza un proceso que
    termina en el acto y se lo espera, así que el número queda libre."""
    p = subprocess.Popen([sys.executable, "-c", ""])
    p.wait()
    return p.pid


@pytest.fixture
def unidad(tmp_path):
    """Una unidad preparada como la deja la app: wbfs/, games/ y apps/."""
    raiz = tmp_path / "USB"
    for sub in ("wbfs/RMCP01", "games/Zelda [GZLE01]", "apps/WiiDonut"):
        (raiz / sub).mkdir(parents=True)
    return raiz


def _archivo(path: Path, tamaño: int = 64) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * tamaño)
    return path


def _envejecer(path: Path, segundos: float) -> None:
    """Corre el mtime hacia atrás, para simular un resto de hace rato sin
    tener que esperarlo."""
    cuando = time.time() - segundos
    os.utime(path, (cuando, cuando))


def _nombres(leftovers) -> set:
    return {lo.path.name for lo in leftovers}


# ------------------------------------------------- Leer el nombre nomás --
@pytest.mark.parametrize("nombre, kind, pid, original", [
    (".RMCP01.wbfs.respaldo-4821", rs.LeftoverKind.BACKUP, 4821, "RMCP01.wbfs"),
    (".WiiDonut.wbm-respaldo-77", rs.LeftoverKind.HOMEBREW_BACKUP, 77, "WiiDonut"),
    (".WiiDonut.wbm-staging-77", rs.LeftoverKind.HOMEBREW_STAGING, 77, "WiiDonut"),
    (".boot.dol.parcial-ab3f9x2q", rs.LeftoverKind.PARTIAL, None, "boot.dol"),
])
def test_classify_reconoce_cada_marca(tmp_path, nombre, kind, pid, original):
    """El nombre original puede tener puntos adentro ('RMCP01.wbfs'), así
    que lo que corta el nombre de la marca es el punto que va justo antes
    de una marca CONOCIDA, no el primer punto que aparezca."""
    lo = rs.classify(_archivo(tmp_path / nombre))
    assert lo is not None
    assert lo.kind is kind
    assert lo.pid == pid
    assert lo.original.name == original


def test_wbm_respaldo_no_se_confunde_con_respaldo(tmp_path):
    """'wbm-respaldo' contiene 'respaldo': si la alternancia del patrón se
    quedara con la marca corta, el respaldo de Homebrew se clasificaría
    como respaldo de un juego y se le ofrecería restaurar sobre un nombre
    equivocado."""
    lo = rs.classify(_archivo(tmp_path / ".App.wbm-respaldo-5"))
    assert lo.kind is rs.LeftoverKind.HOMEBREW_BACKUP
    assert lo.original.name == "App"


@pytest.mark.parametrize("nombre", [
    "Juego.iso",                # un archivo normal
    "._Juego.wbfs",             # basura de macOS
    ".Spotlight-V100",          # ídem
    ".Trash-1000",              # papelera de Linux
    ".RMCP01.wbfs.respaldo",    # sin sufijo: no lo escribió esta app
    ".RMCP01.wbfs.tmp-123",     # marca que no es nuestra
])
def test_classify_ignora_lo_que_no_es_nuestro(tmp_path, nombre):
    assert rs.classify(_archivo(tmp_path / nombre)) is None


def test_classify_avisa_si_el_nombre_original_esta_ocupado(tmp_path):
    _archivo(tmp_path / ".Juego.wbfs.respaldo-1")
    assert rs.classify(tmp_path / ".Juego.wbfs.respaldo-1").original_exists is False
    _archivo(tmp_path / "Juego.wbfs")
    assert rs.classify(tmp_path / ".Juego.wbfs.respaldo-1").original_exists is True


def test_las_marcas_salen_de_quien_las_escribe():
    """El valor de cada `LeftoverKind` tiene que ser la MISMA constante que
    usa el módulo que deja el resto. Si alguien renombra una marca allá y
    acá quedara una copia, el Recovery Manager dejaría de encontrarlos sin
    que nada falle."""
    assert rs.LeftoverKind.BACKUP.value == library_ops.MARCA_RESPALDO
    assert rs.LeftoverKind.HOMEBREW_BACKUP.value == oscwii_installer.MARCA_RESPALDO
    assert rs.LeftoverKind.HOMEBREW_STAGING.value == oscwii_installer.MARCA_STAGING
    assert rs.LeftoverKind.PARTIAL.value == atomicfs.MARCA_PARCIAL


def test_los_nombres_que_escribe_atomicfs_se_leen_de_vuelta(tmp_path):
    """La prueba de ida y vuelta: lo que arma `hidden_sibling` tiene que
    volver a leerse con la misma marca y el mismo original. Es lo que
    engancha el formato de escritura con el de lectura sin que ninguno de
    los dos lo asuma."""
    destino = tmp_path / "RMCP01.wbfs"
    oculto = _archivo(atomicfs.hidden_sibling(destino, library_ops.MARCA_RESPALDO))
    lo = rs.classify(oculto)
    assert lo.kind is rs.LeftoverKind.BACKUP
    assert lo.original == destino
    assert lo.pid == os.getpid()


# ------------------------------------------------------- ¿Vive el dueño? --
def test_process_is_alive_con_el_proceso_de_la_suite():
    assert rs.process_is_alive(os.getpid()) is True


def test_process_is_alive_con_un_pid_que_ya_termino(pid_muerto):
    assert rs.process_is_alive(pid_muerto) is False


@pytest.mark.parametrize("pid", [None, 0, -1])
def test_process_is_alive_falla_cerrado(pid):
    """Sin un PID que se pueda consultar, la respuesta es "vivo": es la que
    hace que el resto NO se ofrezca. Ante la duda, no tocar."""
    assert rs.process_is_alive(pid) is True


# --------------------------------------------------------------- Escaneo --
def test_encuentra_los_cuatro_tipos_de_resto(unidad, pid_muerto):
    """La unidad de un cliente después de que se colgó la PC: un respaldo
    de una conversión, los dos restos de una instalación de Homebrew y un
    temporal de copia."""
    _archivo(unidad / "wbfs/RMCP01" / f".RMCP01.wbfs.respaldo-{pid_muerto}", 400)
    _archivo(unidad / "apps" / f".WiiDonut.wbm-respaldo-{pid_muerto}", 300)
    (unidad / "apps" / f".WiiDonut.wbm-staging-{pid_muerto}").mkdir()
    _archivo(unidad / "apps" / f".WiiDonut.wbm-staging-{pid_muerto}" / "boot.dol", 200)
    parcial = _archivo(unidad / "games/Zelda [GZLE01]" / ".game.iso.parcial-x7k2", 100)
    _envejecer(parcial, rs.PARTIAL_MIN_AGE_SECONDS + 60)

    encontrados = rs.scan([unidad])

    assert _nombres(encontrados) == {
        f".RMCP01.wbfs.respaldo-{pid_muerto}",
        f".WiiDonut.wbm-respaldo-{pid_muerto}",
        f".WiiDonut.wbm-staging-{pid_muerto}",
        ".game.iso.parcial-x7k2",
    }
    # Del más grande al más chico: lo primero que quiere ver quien mira la
    # lista es qué le está comiendo el espacio.
    assert [lo.size_bytes for lo in encontrados] == sorted(
        [lo.size_bytes for lo in encontrados], reverse=True)


def test_solo_los_respaldos_se_pueden_restaurar(unidad, pid_muerto):
    _archivo(unidad / "wbfs/RMCP01" / f".RMCP01.wbfs.respaldo-{pid_muerto}")
    _archivo(unidad / "apps" / f".WiiDonut.wbm-respaldo-{pid_muerto}")
    (unidad / "apps" / f".WiiDonut.wbm-staging-{pid_muerto}").mkdir()
    parcial = _archivo(unidad / ".x.iso.parcial-abc")
    _envejecer(parcial, rs.PARTIAL_MIN_AGE_SECONDS + 60)

    por_tipo = {lo.kind: lo.restorable for lo in rs.scan([unidad])}
    assert por_tipo == {
        rs.LeftoverKind.BACKUP: True,
        rs.LeftoverKind.HOMEBREW_BACKUP: True,
        rs.LeftoverKind.HOMEBREW_STAGING: False,
        rs.LeftoverKind.PARTIAL: False,
    }


def test_un_resto_de_un_proceso_vivo_no_aparece(unidad, pid_muerto):
    """El caso que hace que esto sea seguro: el respaldo con el PID de un
    proceso VIVO es una conversión pasando ahora mismo, no un huérfano.
    Ofrecerlo sería ofrecer arruinarle el archivo al usuario mientras se
    escribe."""
    vivo = _archivo(unidad / "wbfs/RMCP01" / f".RMCP01.wbfs.respaldo-{os.getpid()}")
    muerto = _archivo(unidad / "wbfs/RMCP01" / f".Otro.wbfs.respaldo-{pid_muerto}")

    encontrados = rs.scan([unidad])

    assert _nombres(encontrados) == {muerto.name}
    assert vivo.exists(), "no se toca lo que tiene dueño vivo"


def test_una_staging_de_un_proceso_vivo_tampoco(unidad):
    (unidad / "apps" / f".WiiDonut.wbm-staging-{os.getpid()}").mkdir()
    assert rs.scan([unidad]) == []


def test_un_parcial_recien_tocado_no_aparece(unidad):
    """Los temporales de `atomicfs` no traen PID (`mkstemp` les pone un
    sufijo al azar), así que lo único que los separa de una copia en curso
    es hace cuánto que nadie los toca."""
    _archivo(unidad / "wbfs/RMCP01" / ".RMCP01.wbfs.parcial-q1w2e3")
    assert rs.scan([unidad]) == []


def test_un_parcial_viejo_si_aparece(unidad):
    parcial = _archivo(unidad / "wbfs/RMCP01" / ".RMCP01.wbfs.parcial-q1w2e3")
    _envejecer(parcial, rs.PARTIAL_MIN_AGE_SECONDS + 1)
    assert _nombres(rs.scan([unidad])) == {".RMCP01.wbfs.parcial-q1w2e3"}


def test_un_parcial_con_sufijo_de_digitos_no_se_lee_como_pid(unidad):
    """`mkstemp` puede sacar un sufijo que sea todo dígitos. Si eso se
    interpretara como un PID, el temporal se juzgaría por un proceso que no
    tiene nada que ver con él -y con el PID de la propia suite, encima,
    quedaría escondido para siempre."""
    parcial = _archivo(unidad / f".x.iso.parcial-{os.getpid()}")
    _envejecer(parcial, rs.PARTIAL_MIN_AGE_SECONDS + 1)

    encontrados = rs.scan([unidad])

    assert len(encontrados) == 1
    assert encontrados[0].pid is None


def test_un_escaneo_limpio_no_devuelve_nada(unidad):
    """Una unidad sana: juegos de verdad, una app instalada, y la basura
    que dejan otros sistemas operativos. Nada de eso es un resto, así que
    no hay nada que avisar -ni banner, ni diálogo."""
    _archivo(unidad / "wbfs/RMCP01/RMCP01.wbfs")
    _archivo(unidad / "games/Zelda [GZLE01]/game.iso")
    _archivo(unidad / "apps/WiiDonut/boot.dol")
    _archivo(unidad / "._RMCP01.wbfs")
    (unidad / ".Trash-1000").mkdir()

    encontrados = rs.scan([unidad])

    assert encontrados == []
    assert rs.summary(encontrados) == (0, 0)


def test_no_baja_adentro_de_una_staging(unidad, pid_muerto):
    """La staging se reporta ENTERA, como una sola cosa. Si el escaneo
    bajara adentro, un `.boot.dol.parcial-xxx` que quedó ahí dentro
    aparecería como un resto suelto y el usuario vería dos filas para un
    solo problema -y borrar la de adentro no liberaría casi nada."""
    staging = unidad / "apps" / f".WiiDonut.wbm-staging-{pid_muerto}"
    staging.mkdir()
    adentro = _archivo(staging / ".boot.dol.parcial-zzz")
    _envejecer(adentro, rs.PARTIAL_MIN_AGE_SECONDS + 60)

    encontrados = rs.scan([unidad])

    assert _nombres(encontrados) == {staging.name}
    assert encontrados[0].is_dir is True


def test_no_baja_mas_alla_del_limite_de_profundidad(unidad, pid_muerto):
    """El límite existe para que arrancar la app no recorra el disco de 2 TB
    de un cliente. Los restos que la app puede dejar viven dentro de
    `wbfs/<ID6>/`, `games/<Título>/` o `apps/<App>/`, así que entran; lo
    que esté más abajo es de otro programa."""
    hondo = unidad / "a" / "b" / "c" / "d"
    hondo.mkdir(parents=True)
    _archivo(hondo / f".x.iso.respaldo-{pid_muerto}")
    _archivo(unidad / "wbfs/RMCP01" / f".RMCP01.wbfs.respaldo-{pid_muerto}")

    assert _nombres(rs.scan([unidad])) == {f".RMCP01.wbfs.respaldo-{pid_muerto}"}


def test_no_sigue_symlinks(tmp_path, unidad, pid_muerto):
    """Un enlace a otra carpeta dentro de la unidad convertiría el escaneo
    de arranque en un paseo por donde apunte el enlace -en el peor caso,
    la raíz del sistema."""
    afuera = tmp_path / "afuera"
    afuera.mkdir()
    _archivo(afuera / f".x.iso.respaldo-{pid_muerto}")
    (unidad / "atajo").symlink_to(afuera, target_is_directory=True)

    assert rs.scan([unidad]) == []


def test_una_raiz_que_no_existe_no_rompe_el_escaneo(unidad, pid_muerto):
    """El USB que se desconectó entre que se armó la lista de raíces y
    corrió el escaneo. Lo que sí se pudo leer se entrega igual."""
    _archivo(unidad / f".x.iso.respaldo-{pid_muerto}")
    encontrados = rs.scan([unidad / "no-existe", unidad])
    assert len(encontrados) == 1


def test_dos_raices_solapadas_no_duplican_el_mismo_resto(unidad, pid_muerto):
    """La biblioteca guardada adentro del USB: las dos raíces se escanean
    (el límite de profundidad se cuenta desde cada una) pero el resto que
    aparece por los dos caminos es uno solo."""
    biblioteca = unidad / "wbfs"
    _archivo(biblioteca / "RMCP01" / f".RMCP01.wbfs.respaldo-{pid_muerto}")

    encontrados = rs.scan([unidad, biblioteca])

    assert len(encontrados) == 1


# ----------------------------------------- El filtro del OperationManager --
def test_un_resto_con_una_operacion_encima_no_se_ofrece(unidad, pid_muerto):
    """Aunque el PID del nombre esté muerto, si hay una operación
    registrada ocupando esa unidad no se toca nada: puede ser otra
    instancia de la app, o esta misma escribiendo ahí con otro nombre."""
    _archivo(unidad / "wbfs/RMCP01" / f".RMCP01.wbfs.respaldo-{pid_muerto}")
    ops = OperationManager()

    assert len(rs.scan([unidad], ops=ops)) == 1, "sin operaciones sí aparece"

    op = ops.start(OperationKind.TRANSFERRING, resources=[unidad])
    assert rs.scan([unidad], ops=ops) == []

    ops.finish(op)
    assert len(rs.scan([unidad], ops=ops)) == 1, "al terminar vuelve a aparecer"


def test_tambien_cuenta_una_operacion_sobre_el_nombre_original(unidad, pid_muerto):
    """Una conversión declara el nombre FINAL ('RMCP01.wbfs'), no el del
    respaldo oculto que apartó. Preguntar solo por el resto dejaría pasar
    justo el caso peligroso."""
    destino = unidad / "wbfs/RMCP01/RMCP01.wbfs"
    _archivo(destino.with_name(f".RMCP01.wbfs.respaldo-{pid_muerto}"))
    ops = OperationManager()
    ops.start(OperationKind.CONVERTING, write=[destino])

    assert rs.scan([unidad], ops=ops) == []


def test_una_operacion_en_otra_unidad_no_estorba(tmp_path, unidad, pid_muerto):
    """El filtro tiene que ser preciso: preparar el pendrive de un cliente
    no puede esconder los restos que quedaron en el de otro."""
    otra = tmp_path / "otro-usb"
    otra.mkdir()
    _archivo(unidad / f".x.iso.respaldo-{pid_muerto}")
    ops = OperationManager()
    ops.start(OperationKind.TRANSFERRING, resources=[otra])

    assert len(rs.scan([unidad], ops=ops)) == 1


def test_is_locked_by_operation_revalida_despues_del_escaneo(unidad, pid_muerto):
    """Entre el escaneo y el click del usuario hay un diálogo abierto, y en
    ese rato puede arrancar una transferencia. Es lo que consulta el
    diálogo antes de tocar el disco."""
    _archivo(unidad / f".x.iso.respaldo-{pid_muerto}")
    ops = OperationManager()
    lo = rs.scan([unidad], ops=ops)[0]

    assert rs.is_locked_by_operation(ops, lo) is False
    ops.start(OperationKind.TRANSFERRING, resources=[unidad])
    assert rs.is_locked_by_operation(ops, lo) is True


# ------------------------------------------------------------ Restaurar --
def test_restaurar_devuelve_el_archivo_a_su_nombre(unidad, pid_muerto):
    destino = unidad / "wbfs/RMCP01/RMCP01.wbfs"
    respaldo = _archivo(destino.with_name(f".RMCP01.wbfs.respaldo-{pid_muerto}"))
    respaldo.write_bytes(b"el juego del cliente")

    lo = rs.scan([unidad])[0]
    rs.restore(lo)

    assert destino.read_bytes() == b"el juego del cliente"
    assert not respaldo.exists()


def test_restaurar_pisa_lo_que_dejo_la_operacion_interrumpida(unidad, pid_muerto):
    """El caso completo de una conversión que se cortó: quedó el respaldo
    (el archivo bueno, apartado) y el nombre final ocupado por lo que `wit`
    alcanzó a escribir. Restaurar tiene que dejar el bueno -por eso el
    diálogo lo confirma antes."""
    destino = _archivo(unidad / "wbfs/RMCP01/RMCP01.wbfs")
    destino.write_bytes(b"a medio convertir")
    respaldo = destino.with_name(f".RMCP01.wbfs.respaldo-{pid_muerto}")
    respaldo.write_bytes(b"el juego entero")

    lo = rs.scan([unidad])[0]
    assert lo.original_exists is True

    rs.restore(lo)

    assert destino.read_bytes() == b"el juego entero"
    assert not respaldo.exists()


def test_restaurar_una_app_de_homebrew_devuelve_la_carpeta(unidad, pid_muerto):
    """Un respaldo de Homebrew es una CARPETA entera, y restaurarla es el
    mismo `os.replace` que con un archivo: `SetAside` no distingue."""
    anterior = unidad / "apps" / f".WiiDonut.wbm-respaldo-{pid_muerto}"
    anterior.mkdir()
    _archivo(anterior / "boot.dol")

    lo = rs.scan([unidad])[0]
    rs.restore(lo)

    assert (unidad / "apps/WiiDonut/boot.dol").exists()
    assert not anterior.exists()


def test_restaurar_una_staging_no_se_puede(unidad, pid_muerto):
    """Una staging es la app NUEVA a medio armar: no hay ningún estado
    anterior guardado ahí adentro, así que "restaurar" no significa nada.
    Y el `RecoveryError` tiene que llegar sin haber movido un archivo."""
    staging = unidad / "apps" / f".WiiDonut.wbm-staging-{pid_muerto}"
    staging.mkdir()
    _archivo(staging / "boot.dol")

    lo = rs.scan([unidad])[0]
    with pytest.raises(rs.RecoveryError):
        rs.restore(lo)

    assert staging.exists()


def test_restaurar_un_parcial_no_se_puede(unidad):
    parcial = _archivo(unidad / ".RMCP01.wbfs.parcial-abc")
    _envejecer(parcial, rs.PARTIAL_MIN_AGE_SECONDS + 1)

    lo = rs.scan([unidad])[0]
    with pytest.raises(rs.RecoveryError):
        rs.restore(lo)

    assert parcial.exists()


def test_restaurar_algo_que_ya_no_esta_avisa(unidad, pid_muerto):
    """El resto que alguien borró por afuera (desde el gestor de archivos)
    entre el escaneo y el click. Tiene que avisar, no fallar en silencio ni
    explotar con un traceback."""
    respaldo = _archivo(unidad / f".x.iso.respaldo-{pid_muerto}")
    lo = rs.scan([unidad])[0]
    respaldo.unlink()

    with pytest.raises(rs.RecoveryError) as e:
        rs.restore(lo)
    assert str(respaldo) in str(e.value)


# ------------------------------------------------------------- Eliminar --
def test_eliminar_libera_el_archivo(unidad, pid_muerto):
    respaldo = _archivo(unidad / "wbfs/RMCP01" / f".RMCP01.wbfs.respaldo-{pid_muerto}")

    lo = rs.scan([unidad])[0]
    rs.delete(lo)

    assert not respaldo.exists()


def test_eliminar_una_staging_borra_la_carpeta_entera(unidad, pid_muerto):
    staging = unidad / "apps" / f".WiiDonut.wbm-staging-{pid_muerto}"
    staging.mkdir()
    _archivo(staging / "boot.dol")
    _archivo(staging / "data" / "config.ini")

    lo = rs.scan([unidad])[0]
    rs.delete(lo)

    assert not staging.exists()


def test_eliminar_no_toca_el_nombre_original(unidad, pid_muerto):
    """Eliminar el respaldo descarta la copia vieja, no lo que hay en el
    nombre real. Es la diferencia entre liberar espacio y borrarle el juego
    al cliente."""
    destino = _archivo(unidad / "wbfs/RMCP01/RMCP01.wbfs")
    _archivo(destino.with_name(f".RMCP01.wbfs.respaldo-{pid_muerto}"))

    lo = rs.scan([unidad])[0]
    rs.delete(lo)

    assert destino.exists()


def test_eliminar_algo_que_ya_no_esta_no_es_un_error(unidad, pid_muerto):
    """Quien pidió eliminar quería que dejara de ocupar lugar. Si ya no
    está, eso ya pasó: molestarlo con un error sería raro."""
    respaldo = _archivo(unidad / f".x.iso.respaldo-{pid_muerto}")
    lo = rs.scan([unidad])[0]
    respaldo.unlink()

    rs.delete(lo)  # no levanta


# ------------------------------------------------- Dónde se busca (raíces) --
class _DiscoDeMentira:
    def __init__(self, path):
        self.path = Path(path)


def test_scan_roots_toma_la_biblioteca_y_las_unidades_removibles(tmp_path):
    biblioteca = tmp_path / "biblioteca"
    biblioteca.mkdir()
    usb = tmp_path / "usb"
    usb.mkdir()

    class _Settings:
        library_path = str(biblioteca)

    raices = rs.scan_roots(
        _Settings(),
        candidate_drives=lambda: [_DiscoDeMentira("/dev/sdz")],
        mount_points=lambda _dev: [usb])

    assert raices == [biblioteca, usb]


def test_scan_roots_ignora_un_disco_conectado_pero_sin_montar(tmp_path):
    """Sin punto de montaje no hay carpeta que recorrer. No es un error."""
    raices = rs.scan_roots(
        None,
        candidate_drives=lambda: [_DiscoDeMentira("/dev/sdz")],
        mount_points=lambda _dev: [])
    assert raices == []


def test_scan_roots_ignora_rutas_criticas_del_sistema(tmp_path):
    """Si alguien montó algo removible en /boot, el Recovery Manager no
    tiene nada que ofrecer ahí. Es la misma lista de rutas que protege a
    Modo Fábrica."""
    raices = rs.scan_roots(
        None,
        candidate_drives=lambda: [_DiscoDeMentira("/dev/sdz")],
        mount_points=lambda _dev: [Path("/boot"), Path("/")])
    assert raices == []


def test_scan_roots_ignora_una_biblioteca_desconectada(tmp_path):
    class _Settings:
        library_path = str(tmp_path / "usb-que-no-esta")

    assert rs.scan_roots(_Settings(), candidate_drives=lambda: []) == []


def test_scan_roots_no_repite_la_misma_carpeta(tmp_path):
    """La biblioteca configurada en el mismo punto de montaje del USB."""
    usb = tmp_path / "usb"
    usb.mkdir()

    class _Settings:
        library_path = str(usb)

    raices = rs.scan_roots(
        _Settings(),
        candidate_drives=lambda: [_DiscoDeMentira("/dev/sdz")],
        mount_points=lambda _dev: [usb])

    assert raices == [usb]


# -------------------------------------------------------- Lo que se lee --
def test_summary_suma_cuantos_y_cuanto(unidad, pid_muerto):
    _archivo(unidad / f".a.iso.respaldo-{pid_muerto}", 1000)
    _archivo(unidad / f".b.iso.respaldo-{pid_muerto}", 500)

    assert rs.summary(rs.scan([unidad])) == (2, 1500)


def test_el_tamaño_de_una_staging_es_el_de_todo_su_contenido(unidad, pid_muerto):
    """Lo que le interesa a quien mira la lista es cuánto espacio recupera
    borrándola, no cuánto ocupa la entrada de directorio."""
    staging = unidad / "apps" / f".WiiDonut.wbm-staging-{pid_muerto}"
    staging.mkdir()
    _archivo(staging / "boot.dol", 700)
    _archivo(staging / "data" / "x.bin", 300)

    assert rs.scan([unidad])[0].size_bytes == 1000


def test_la_antiguedad_nunca_es_negativa(tmp_path):
    """En FAT -el formato de estas unidades- un archivo puede quedar con
    fecha futura si el reloj de la PC estaba adelantado. "hace -3 horas" no
    significa nada para quien lo lee."""
    resto = _archivo(tmp_path / ".x.iso.respaldo-1")
    futuro = time.time() + 3600
    os.utime(resto, (futuro, futuro))

    assert rs.classify(resto).age_seconds() == 0.0


# ------------------------------------------------ Lo que muestra el aviso --
def test_el_resumen_dice_cuantos_y_cuanto_ocupan(unidad, pid_muerto):
    from wiibackup_manager.widgets.recovery_dialog import summary_text

    _archivo(unidad / f".a.iso.respaldo-{pid_muerto}", 2 * 1024 ** 3)
    _archivo(unidad / f".b.iso.respaldo-{pid_muerto}", 1024 ** 3)

    texto = summary_text(rs.scan([unidad]))

    assert "2 restos" in texto
    assert "3.0 GB" in texto


def test_el_resumen_de_un_solo_resto_va_en_singular(unidad, pid_muerto):
    from wiibackup_manager.widgets.recovery_dialog import summary_text

    _archivo(unidad / f".a.iso.respaldo-{pid_muerto}")
    assert "1 resto de" in summary_text(rs.scan([unidad]))


@pytest.mark.parametrize("segundos, esperado", [
    (5, "recién"),
    (90, "hace 1 minuto"),
    (60 * 5, "hace 5 minutos"),
    (60 * 60, "hace 1 hora"),
    (60 * 60 * 5, "hace 5 horas"),
    (60 * 60 * 24, "hace 1 día"),
    (60 * 60 * 24 * 3, "hace 3 días"),
])
def test_la_antiguedad_se_dice_en_la_unidad_que_se_entiende(segundos, esperado):
    from wiibackup_manager.widgets.recovery_dialog import format_age

    assert format_age(segundos) == esperado


# ------------------- Lo que está corriendo no tiene que esconder restos --
#
# Al abrir la ventana arrancan JUNTOS el escaneo de la biblioteca y el de
# restos. El primero declara la carpeta entera como recurso ocupado, y el
# segundo descarta todo resto que esté en un lugar ocupado -para no listar
# lo que otra operación está usando. Con esas dos reglas juntas, TODO resto
# que viviera en la biblioteca desaparecía del aviso mientras durara el
# escaneo, que es justo el momento en que se lo busca. Y el escaneo de
# restos corre UNA sola vez al arrancar: no hay segunda oportunidad.
#
# El usuario veía la app abrirse sin ningún aviso, que es exactamente lo
# que se ve cuando no hay nada que recuperar.


def _resto_en(carpeta: Path, pid_muerto: int) -> Path:
    ruta = carpeta / f".Juego.wbfs.respaldo-{pid_muerto}"
    _archivo(ruta)
    return ruta


@pytest.mark.parametrize("kind", [OperationKind.SCANNING, OperationKind.VERIFYING])
def test_una_operacion_que_solo_lee_no_esconde_los_restos(unidad, pid_muerto, kind):
    """Un escaneo no escribe nada: que esté corriendo no vuelve peligroso
    ni siquiera MIRAR un resto, y mucho menos es motivo para ocultarlo."""
    _resto_en(unidad, pid_muerto)
    ops = OperationManager()
    ops.start(kind, resources=[unidad])

    assert len(rs.scan([unidad], ops=ops)) == 1, (
        f"{kind.name} escondió un resto sin llegar a escribir nada")


@pytest.mark.parametrize("kind", [
    OperationKind.TRANSFERRING, OperationKind.CONVERTING,
    OperationKind.FORMATTING, OperationKind.INSTALLING_HOMEBREW,
])
def test_una_operacion_que_escribe_si_esconde_los_restos(unidad, pid_muerto, kind):
    """La otra mitad, que es la que no hay que romper arreglando la
    primera: mientras algo escribe en ese lugar, lo que parece un resto
    puede ser el archivo que esa operación está creando en este momento."""
    _resto_en(unidad, pid_muerto)
    ops = OperationManager()
    ops.start(kind, resources=[unidad])

    assert rs.scan([unidad], ops=ops) == [], (
        f"{kind.name} está escribiendo ahí y aun así se listó el resto")


def test_tocar_el_disco_sigue_siendo_conservador(unidad, pid_muerto):
    """Listar y ACTUAR no piden lo mismo.

    El diálogo revalida con `is_locked_by_operation` antes de mover o
    borrar nada, y ahí se queda estricto: cuenta también lo que solo lee.
    Puede entonces mostrar un resto sobre el que después se niegue a
    actuar -"ahora hay una operación usando esa ubicación"-, que es una
    respuesta honesta, y mucho mejor que la anterior, que era no mostrar
    nada."""
    ruta = _resto_en(unidad, pid_muerto)
    ops = OperationManager()
    ops.start(OperationKind.SCANNING, resources=[unidad])

    leftover = rs.classify(ruta)
    # Al listar, el escaneo lo deja pasar...
    assert rs.is_locked_by_operation(ops, leftover, ignore_read_only=True) is False
    # ...pero justo antes de tocarlo, no.
    assert rs.is_locked_by_operation(ops, leftover) is True


# ------------------------- Cuando el escaneo del arranque se cae solo --
#
# El escaneo automático corre en un hilo y atrapa TODO: nada de lo que le
# pase puede impedir que la app se abra. Pero atrapar no es dar por bueno.
# Antes esa rama dejaba la lista vacía, que es exactamente lo que dice un
# disco limpio, así que un escaneo roto se veía igual que "no hay nada que
# recuperar": el usuario se quedaba sin el aviso de los restos justo
# cuando algo andaba mal, y sin nada que mirar para saber por qué.
#
# Se ejercita el código real de `WiiBackupWindow` con un `self` de
# mentira, como en `test_queue_manager.py`: importar el módulo no abre
# ninguna ventana, así que esto corre en cualquier terminal y no solo bajo
# Xvfb, que es donde vive el resto de lo que toca GTK.


class _BannerDeMentira:
    """Lo mínimo de `Adw.Banner` que usa `_update_recovery_banner`."""

    def __init__(self):
        self.title = None
        self.revealed = None
        self.button_label = "Ver detalles"

    def set_title(self, texto):
        self.title = texto

    def set_revealed(self, valor):
        self.revealed = valor

    def set_button_label(self, texto):
        self.button_label = texto


class _VentanaDeMentira:
    def __init__(self):
        self.settings = object()
        self.ops = object()
        self._recovery_banner = _BannerDeMentira()
        self._recovery_leftovers: list = []
        self._recovery_ignored: set = set()
        self._recovery_scan_error = ""


def _correr_escaneo_del_arranque(monkeypatch):
    """Corre `_start_recovery_scan` de verdad -con su hilo- y despacha a
    mano lo que haya quedado encolado para el hilo de GTK, que en un test
    no existe. Devuelve la ventana falsa ya actualizada."""
    from wiibackup_manager import window
    from wiibackup_manager.widgets import gtk_helpers

    monkeypatch.setattr(gtk_helpers, "widget_is_alive", lambda w: True)
    pendientes: list = []
    monkeypatch.setattr(window.GLib, "idle_add",
                        lambda func, *args: pendientes.append((func, args)))

    ventana = _VentanaDeMentira()
    # Los tres métodos REALES de la ventana, atados al `self` de mentira:
    # el hilo llama al callback por `self`, y el callback al banner por
    # `self`, así que sin esto se probaría media cadena. Lo único simulado
    # es el widget del final.
    for nombre in ("_start_recovery_scan", "_on_recovery_scan_done",
                   "_update_recovery_banner"):
        setattr(ventana, nombre,
                types.MethodType(getattr(window.WiiBackupWindow, nombre), ventana))

    ventana._start_recovery_scan()

    limite = time.monotonic() + 10
    while not pendientes and time.monotonic() < limite:
        time.sleep(0.01)
    assert pendientes, "el escaneo no avisó nunca al hilo de GTK"
    func, args = pendientes.pop(0)
    func(*args)
    return ventana


@pytest.mark.parametrize("rompe", ["scan_roots", "scan"])
def test_un_escaneo_de_restos_que_se_cae_avisa_y_no_dice_que_esta_limpio(
        monkeypatch, capsys, rompe):
    """Lo que este arreglo vino a evitar: que un fallo del escaneo se vea
    igual que un disco sin restos.

    Se prueba rompiendo cada una de las dos llamadas que cubre el `try`,
    porque las dos dejan al usuario en la misma situación -no se sabe si
    hay restos- y las dos tienen que avisar igual."""
    def _explota(*_a, **_k):
        raise OSError("la unidad se desconectó a mitad del escaneo")
    monkeypatch.setattr(rs, rompe, _explota)
    # La otra llamada tiene que poder correr: lo que se prueba es que
    # rompa la de esta vuelta, no las dos a la vez.
    if rompe == "scan_roots":
        monkeypatch.setattr(rs, "scan", lambda *_a, **_k: [])
    else:
        monkeypatch.setattr(rs, "scan_roots", lambda *_a, **_k: [])

    ventana = _correr_escaneo_del_arranque(monkeypatch)

    # 1. El aviso se ve, y es el de "no pude mirar", no el de "no hay nada".
    assert ventana._recovery_banner.revealed is True, (
        "un escaneo caído se vio igual que un disco limpio: sin ningún aviso")
    assert "No se pudo completar el escaneo" in ventana._recovery_banner.title
    # Y dice qué implica, que es lo que lo hace distinto de "todo limpio".
    assert "sin detectar" in ventana._recovery_banner.title
    # 2. Sin botón: "Ver detalles" abriría el diálogo vacío, que es
    #    justamente el mensaje que este banner viene a desmentir.
    assert ventana._recovery_banner.button_label == ""
    # 3. El motivo quedó registrado para poder diagnosticarlo.
    err = capsys.readouterr().err
    assert "falló el escaneo de restos" in err
    assert "la unidad se desconectó a mitad del escaneo" in err
    assert "OSError" in err
    # 4. Y no se inventaron restos que nadie llegó a ver.
    assert ventana._recovery_leftovers == []


def test_un_escaneo_que_no_encuentra_nada_no_muestra_ningun_aviso(monkeypatch,
                                                                  capsys):
    """La otra mitad, sin la cual la anterior no prueba nada: cuando el
    escaneo SÍ termina y no hay restos, el banner sigue callado y no se
    ensucia stderr. Los dos casos tienen que verse distinto."""
    monkeypatch.setattr(rs, "scan_roots", lambda *_a, **_k: [])
    monkeypatch.setattr(rs, "scan", lambda *_a, **_k: [])

    ventana = _correr_escaneo_del_arranque(monkeypatch)

    assert ventana._recovery_banner.revealed is False
    assert ventana._recovery_banner.title is None
    assert ventana._recovery_scan_error == ""
    assert capsys.readouterr().err == ""
