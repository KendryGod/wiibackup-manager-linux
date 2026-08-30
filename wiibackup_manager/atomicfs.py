"""Primitivas para dejar algo en su lugar sin pasar por un estado a medias.

Tres partes de la app resuelven el mismo problema -"reemplazar X sin que
exista un instante en el que X esté a medio escribir, y sin perder lo que
había si algo falla"- sobre cosas distintas:

- `gametdb`, `oscwii_client`, `golden_configs` y el extractor de ZIP de
  `oscwii_installer` reemplazan UN ARCHIVO chico que se escribe de una.
- `fileops._copy_with_progress` reemplaza UN ARCHIVO grande que se copia
  de a bloques, con progreso, cancelación y `fsync`.
- `oscwii_installer` reemplaza UNA CARPETA entera (la app de Homebrew),
  y `library_ops.DestinationGuard` aparta UN GRUPO de archivos (las partes de
  un WBFS dividido) mientras `wit` escribe encima.

Los tres terminaban reimplementando el mismo mecanismo: armar un nombre
oculto hermano del destino, escribir/apartar ahí, intercambiar con
`os.replace` -atómico solo dentro del mismo filesystem, de ahí lo de
"hermano"- y limpiar o devolver las cosas a su lugar cuando algo falla.
Acá vive ese mecanismo, una sola vez.

Lo que NO vive acá
------------------
Las decisiones. Qué excepción se levanta cuando no se puede devolver un
respaldo a su lugar, qué mensaje ve el usuario, si un respaldo huérfano es
un error o un aviso, si conviene reintentar: eso es política de cada
módulo y se queda en cada módulo. Estas primitivas hacen el trabajo sucio
y REPORTAN lo que salió mal (devolviendo los fallos, o levantando
`SwapRollbackFailed`, que no sabe nada de usuarios ni de mensajes); quien
llama decide qué significa.

Por eso, por ejemplo, `SetAside.restore()` devuelve la lista de los que no
pudo devolver en vez de levantar algo: `DestinationGuard` convierte eso en
`library_ops.RollbackFailedError` con su mensaje para el usuario, y podría
convertirlo en otra cosa sin que este módulo se entere.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

# --------------------------------------------------------------- Nombres --
# Marca de los temporales de escritura (`.{nombre}.parcial-<sufijo>`).
# Constante y no un literal suelto porque tiene un segundo lector además
# del `mkstemp` de acá: `recovery_service`, que reconoce por esta marca los
# temporales que quedaron de una escritura interrumpida.
#
# Ojo con el sufijo: el de `hidden_sibling` es el PID, pero el de estos
# temporales lo elige `tempfile.mkstemp` al azar (ver `_temp_sibling`, que
# explica por qué). O sea que un `.parcial-` NO trae PID adentro y no se
# puede preguntar por su dueño: es la razón por la que `recovery_service`
# los juzga por antigüedad.
MARCA_PARCIAL = "parcial"


def hidden_sibling(target: Path, marca: str) -> Path:
    """Ruta hermana y oculta de `target`: `.{nombre}.{marca}-{pid}`.

    Hermana (misma carpeta) porque el `os.replace` final solo es atómico
    dentro del mismo filesystem. Oculta (empieza con punto) porque el
    escaneo de la biblioteca ignora esos archivos y porque
    `tools/manual_queue_e2e.py` los busca así para detectar temporales
    huérfanos.

    El PID adentro separa a dos PROCESOS que trabajen sobre el mismo
    destino. Para los temporales de escritura eso no alcanza -dos threads
    del mismo proceso calcularían el mismo nombre- y por eso
    `atomic_write_target` usa `tempfile.mkstemp`, que da unicidad de
    verdad. Acá el PID sí alcanza: apartar un destino y volver a ponerlo
    es parte de una operación que la app ya serializa (`OperationManager`
    no deja dos operaciones sobre el mismo archivo), y el nombre tiene que
    ser PREDECIBLE para poder reconocerlo -es lo que permite que
    `DestinationGuard` proteja sus propios respaldos de la limpieza de
    temporales de `wit`."""
    return target.with_name(f".{target.name}.{marca}-{os.getpid()}")


# ------------------------------------------------------ Permisos del temporal --
_permisos_temporal: "int | None" = None


def _permisos_por_defecto() -> int:
    """Permisos que habría tenido el temporal si lo hubiera creado un
    `open(..., "wb")` normal: 0666 recortado por el umask del proceso.

    Hace falta porque `mkstemp` crea siempre en 0600 -lo correcto para un
    temporal que se queda temporal, pero acá el temporal PASA A SER el
    archivo final-. Sin esto, la caché de carátulas e íconos, las configs
    maestras y las apps instaladas habrían quedado en 0600 de un día para
    el otro: un cambio silencioso de permisos que el esquema anterior no
    hacía.

    El umask se lee de /proc y no con `os.umask()`, porque la única forma
    de consultarlo con `os.umask` es fijarlo y volverlo a poner, y eso es
    una carrera entre threads. Si /proc no está (o no trae el campo), se
    asume el umask más común."""
    global _permisos_temporal
    if _permisos_temporal is None:
        umask = 0o022
        try:
            with open("/proc/self/status", "r", encoding="ascii") as f:
                for linea in f:
                    if linea.startswith("Umask:"):
                        umask = int(linea.split()[1], 8)
                        break
        except (OSError, ValueError, IndexError):
            pass
        _permisos_temporal = 0o666 & ~umask
    return _permisos_temporal


def _ajustar_permisos(tmp: Path) -> None:
    """Mejor esfuerzo: en FAT/exFAT -el destino habitual de esta app- los
    permisos los fija el montaje y `chmod` puede fallar, y eso no tiene
    por qué hacer fracasar la escritura."""
    try:
        os.chmod(tmp, _permisos_por_defecto())
    except OSError:
        pass


# ------------------------------------------------- Escritura de un archivo --
@contextmanager
def _temp_sibling(dest: Path, mkparents: bool) -> Iterator[tuple]:
    """Núcleo compartido de la escritura atómica: crea el temporal, lo cede
    como `(fd, ruta)` y, si el bloque termina sin excepción, lo mueve
    encima de `dest`. Ante CUALQUIER excepción -incluida una que levante el
    propio `os.replace`- borra el temporal y la deja propagar tal cual.

    El temporal lo crea `tempfile.mkstemp` en la MISMA carpeta que `dest`,
    con nombre `.{nombre}.parcial-<sufijo aleatorio>`: único garantizado
    por el sistema operativo (`O_CREAT|O_EXCL` con reintentos), no por
    convención del código, así que ni dos procesos ni dos threads pueden
    coincidir.

    Quién se queda con el descriptor lo decide quien envuelve a esta
    función: `atomic_write_target` lo cierra y cede la ruta,
    `atomic_write_stream` lo abre como archivo y cede el archivo."""
    dest = Path(dest)
    if mkparents:
        dest.parent.mkdir(parents=True, exist_ok=True)

    fd, nombre = tempfile.mkstemp(dir=dest.parent,
                                  prefix=f".{dest.name}.{MARCA_PARCIAL}-")
    tmp = Path(nombre)
    _ajustar_permisos(tmp)
    try:
        yield fd, tmp
        os.replace(tmp, dest)
    except BaseException:
        # Borrar el temporal es "mejor esfuerzo": si tampoco se puede
        # (permisos, unidad desconectada a mitad de camino), lo que
        # importa es que la excepción original llegue a quien llama sin
        # que esta la tape.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@contextmanager
def atomic_write_target(dest: Path, *, mkparents: bool = False) -> Iterator[Path]:
    """Cede la RUTA de un temporal hermano de `dest` y, si el bloque
    termina sin excepción, lo mueve encima de `dest` con `os.replace`.

    Así `dest` nunca existe a medio escribir: o está el contenido viejo
    entero, o el nuevo entero, nunca una mezcla. Es la primitiva para
    quien escribe el archivo de una (`write_bytes`, `copyfile`, copiar un
    miembro de un ZIP); quien necesita el archivo abierto -para escribir
    de a bloques, o para bajarlo a disco antes de intercambiar- tiene
    `atomic_write_stream`.

    `mkparents=True` crea la carpeta de `dest` antes de empezar, para
    quienes escriben a una ruta que puede no existir todavía (el extractor
    de ZIP, la copia de configs maestras).

    NO hace `fsync`: para lo que se guarda con esto -cachés que se pueden
    volver a bajar y copias de archivos chicos- alcanza con la atomicidad
    del rename. Quien además necesita durabilidad ante un corte de luz usa
    `atomic_write_stream(fsync=True)`.

    Un bloque que no escribe nada deja un `dest` VACÍO, no un error: el
    temporal lo crea `mkstemp` por adelantado (de ahí sale la unicidad del
    nombre). Es el resultado correcto para el único caso real que llega
    acá, un miembro de ZIP de 0 bytes."""
    with _temp_sibling(dest, mkparents) as (fd, tmp):
        # Los usuarios de esta forma abren la ruta ellos mismos, así que
        # el descriptor se cierra en el acto. El archivo -y con él el
        # nombre reservado- sigue existiendo: lo que se suelta es el
        # descriptor, no la exclusividad.
        os.close(fd)
        yield tmp


@contextmanager
def atomic_write_stream(dest: Path, *, mkparents: bool = False,
                        fsync: bool = False,
                        before_replace: Optional[Callable[[Path], None]] = None
                        ) -> Iterator[tuple]:
    """Igual que `atomic_write_target`, pero cede el temporal YA ABIERTO
    para escritura binaria, como `(archivo, ruta)`.

    Es lo que necesita quien escribe de a bloques: usar el descriptor que
    devolvió `mkstemp` en vez de reabrir la ruta cierra la ventana entre
    crear el temporal y abrirlo, y el descriptor queda a cargo del `with`
    -si el bloque falla, se cierra igual, sin filtrarse.

    `fsync=True` baja los datos a disco ANTES del intercambio: sin eso el
    rename puede quedar registrado mientras los datos siguen en cache, y
    un tirón del cable dejaría el destino nuevo incompleto y el viejo ya
    borrado. `before_replace(tmp)` corre con el archivo ya cerrado y justo
    antes del `os.replace`, para lo que tenga que quedar aplicado sobre el
    temporal y no sobre el destino (copiar la metadata del origen, por
    ejemplo). Si levanta, el temporal se borra y `dest` no se toca."""
    with _temp_sibling(dest, mkparents) as (fd, tmp):
        with os.fdopen(fd, "wb") as f:
            yield f, tmp
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        if before_replace is not None:
            before_replace(tmp)


# ------------------------------------------------------- Apartar y devolver --
class SetAside:
    """Aparta rutas que ya existen a un nombre oculto hermano, para poder
    devolverlas si algo sale mal.

    Sirve tanto para archivos (las partes de un WBFS dividido, en
    `library_ops.DestinationGuard`) como para carpetas (la versión anterior de
    una app de Homebrew): apartar es un `os.replace` dentro de la misma
    carpeta, instantáneo y sin copiar datos, en los dos casos.

    Es mecanismo puro: `restore()` y `discard()` DEVUELVEN lo que no
    pudieron hacer en vez de levantar excepciones o escribir mensajes.
    Cada módulo decide qué significa eso -`DestinationGuard` levanta
    `library_ops.RollbackFailedError` con un mensaje para el usuario, el
    instalador de Homebrew reporta los respaldos huérfanos en su
    `InstallResult`- y esta clase no tiene por qué saberlo."""

    def __init__(self, marca: str) -> None:
        self.marca = marca
        self._pairs: list = []

    @classmethod
    def adopt(cls, marca: str, pairs) -> "SetAside":
        """Un `SetAside` sobre pares `(original, respaldo)` que YA existen
        en el disco, apartados por otra corrida de la app.

        Es la puerta para invocar el mecanismo a mano. El uso normal es
        apartar y devolver dentro de la misma operación -por eso
        `move_aside` es lo que llena `_pairs`- pero `recovery_service`
        llega DESPUÉS: encuentra un `.{nombre}.respaldo-<pid>` de un
        proceso que ya no está y necesita devolverlo a su lugar con
        exactamente el mismo `os.replace` que habría hecho la operación
        original si hubiera podido terminar.

        Sin esto, el Recovery Manager habría reimplementado el rename -y
        con él la decisión de en qué orden se intenta y qué se reporta
        cuando falla-, que es justo lo que este módulo existe para que no
        pase. No se verifica que los respaldos existan: `restore()` y
        `discard()` ya reportan lo que no pudieron hacer, que es la misma
        respuesta que corresponde para un resto que se esfumó entre que
        se lo listó y se lo quiso tocar."""
        aside = cls(marca)
        aside._pairs = [(Path(original), Path(respaldo))
                        for original, respaldo in pairs]
        return aside

    @property
    def pairs(self) -> list:
        """Los pares `(original, respaldo)` todavía apartados."""
        return list(self._pairs)

    def move_aside(self, original: Path) -> Path:
        """Aparta `original` y devuelve la ruta del respaldo. Deja
        propagar el `OSError` si no se pudo: no haber podido apartar es
        justamente lo que quien llama tiene que decidir cómo manejar (y
        para eso están `restore`/`pairs`, que reflejan lo que sí quedó
        apartado hasta ahí)."""
        original = Path(original)
        respaldo = hidden_sibling(original, self.marca)
        os.replace(original, respaldo)
        self._pairs.append((original, respaldo))
        return respaldo

    def restore(self) -> list:
        """Devuelve cada respaldo a su nombre original y reporta los que
        NO se pudieron devolver, como pares `(original, respaldo)`.

        Se intenta con TODOS aunque alguno falle -en un WBFS dividido no
        tiene sentido dejar dos partes sin restaurar porque la tercera se
        atoró- y se restaura en orden inverso al que se apartaron. Los que
        fallaron quedan en `pairs` (en el orden en que se apartaron, no en
        el que se intentaron): son exactamente lo que sigue pendiente."""
        fallidos: list = []
        for original, respaldo in reversed(self._pairs):
            try:
                os.replace(respaldo, original)
            except OSError:
                fallidos.append((original, respaldo))
        self._pairs = [par for par in self._pairs if par in fallidos]
        return list(self._pairs)

    def discard(self) -> list:
        """Borra los respaldos -ya no hacen falta- y devuelve los que no
        se pudieron borrar.

        Un borrado que falla no es un error de la operación, pero tampoco
        se ignora: el respaldo es un archivo (o una carpeta) oculto que
        puede pesar varios GB, y quedarse callado deja al usuario con la
        unidad llena por algo que no puede ver ni encontrar. Quien llama
        decide cómo avisarlo."""
        huerfanos: list = []
        for _original, respaldo in self._pairs:
            try:
                if respaldo.is_dir():
                    shutil.rmtree(respaldo)
                else:
                    respaldo.unlink(missing_ok=True)
            except OSError:
                huerfanos.append(respaldo)
        self._pairs = []
        return huerfanos


# --------------------------------------------------- Carpeta entera (staging) --
class SwapRollbackFailed(Exception):
    """El intercambio final falló Y ADEMÁS no se pudo devolver el respaldo
    a su lugar: el destino puede haber quedado directamente inexistente.

    Es la excepción de la PRIMITIVA, sin mensajes para el usuario: trae
    los pares pendientes y el error que empezó todo para que quien llama
    la traduzca a lo suyo (`library_ops.RollbackFailedError`, en los dos
    usuarios de hoy)."""

    def __init__(self, pending: list, original_error: BaseException) -> None:
        super().__init__(f"no se pudo restaurar: {pending}")
        self.pending = pending
        self.original_error = original_error


@dataclass
class StagedDirectory:
    """Lo que `staged_directory` cede: la carpeta que hay que llenar y,
    cuando el intercambio ya pasó, los respaldos que no se pudieron
    borrar."""

    path: Path
    orphaned_backups: list = field(default_factory=list)


@contextmanager
def staged_directory(target_dir: Path, *, staging_marca: str = "staging",
                     backup_marca: str = "respaldo"
                     ) -> Iterator[StagedDirectory]:
    """Arma una carpeta de staging hermana de `target_dir` para llenarla, y
    al salir sin excepción la deja EN EL LUGAR de `target_dir`.

    El orden importa y es lo que hace que valga la pena: la staging se
    llena ENTERA primero y el destino no se toca hasta que eso salió bien.
    Si el bloque falla -o se cancela- se borra la staging y `target_dir`
    queda exactamente como estaba, sin haber sido tocado ni un instante.

    El intercambio, ya con la staging completa:

    1. Si `target_dir` no existía, la staging pasa a ocupar su lugar
       directo.
    2. Si existía, se lo aparta primero a un respaldo oculto
       (`SetAside`), entra la staging, y el respaldo se borra recién si
       todo salió bien. Los respaldos que no se puedan borrar quedan en
       `.orphaned_backups` -la operación salió bien igual, pero hay
       espacio ocupado que hay que avisar.
    3. Si el intercambio falla DESPUÉS de apartar el respaldo, se intenta
       devolverlo. Si eso también falla se levanta `SwapRollbackFailed` y
       la staging se deja INTACTA a propósito, junto con el respaldo: dos
       candidatos rescatables a mano es mejor que borrar alguno de los
       dos por las dudas.
    """
    target_dir = Path(target_dir)
    parent = target_dir.parent
    parent.mkdir(parents=True, exist_ok=True)

    staging = hidden_sibling(target_dir, staging_marca)
    # Por si quedó una staging huérfana de un intento anterior con el
    # mismo PID: no debería pasar, pero `rmtree` de algo que no existe no
    # es un error (`ignore_errors=True`).
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()

    handle = StagedDirectory(path=staging)
    try:
        yield handle
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    if not target_dir.exists():
        try:
            os.replace(staging, target_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return

    aside = SetAside(backup_marca)
    try:
        aside.move_aside(target_dir)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    try:
        os.replace(staging, target_dir)
    except OSError as e:
        pendientes = aside.restore()
        if pendientes:
            raise SwapRollbackFailed(pendientes, e) from e
        # Se pudo restaurar la versión anterior: lo nuevo fracasó pero el
        # destino quedó funcional, así que no hace falta conservar la
        # staging.
        shutil.rmtree(staging, ignore_errors=True)
        raise

    handle.orphaned_backups = aside.discard()
