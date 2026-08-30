"""Recovery Manager: qué quedó tirado de una sesión que se cortó a mitad.

Para qué existe
---------------
Las primitivas de `atomicfs` están hechas para que un corte NUNCA deje un
archivo a medio escribir: se escribe al lado, y recién cuando está entero
se lo pone en su lugar. El precio de esa garantía es que, si el proceso
muere en el medio, lo que queda en el disco es el archivo de al lado -un
temporal, una staging, un respaldo- que nadie borró ni devolvió a su
lugar, porque el `try/finally` que iba a hacerlo se murió con el proceso.

Eso es exactamente lo que pasa cuando se cuelga la PC preparando el USB de
un cliente. La unidad queda con varios GB ocupados por archivos ocultos
que el usuario no puede ver ni encontrar, y -en el caso del respaldo- con
un juego suyo esperando a que alguien lo devuelva a su nombre real. Este
módulo los ENCUENTRA y dice qué se puede hacer con cada uno.

Qué hace este módulo y qué no
-----------------------------
Acá se BUSCA y se CLASIFICA. La ventana decide cuándo escanear, qué
mostrar y qué confirmar; `atomicfs.SetAside` hace el trabajo de mover
archivos. Este módulo es el que sabe leer un nombre oculto y decir "esto
es un respaldo de una conversión que no terminó, pesa 4.2 GB, el proceso
que lo dejó ya no existe, y su original volvería a llamarse `Juego.wbfs`".

No reimplementa el mecanismo de restaurar: `restore()` arma un
`SetAside.adopt` con el par que encontró y llama al MISMO `restore()` que
habría llamado `DestinationGuard` si el proceso hubiera llegado vivo hasta
el final. La única diferencia entre restaurar durante la operación y
restaurar tres días después es quién lo pide.

Las cuatro familias de restos
-----------------------------
Todas comparten el formato de `atomicfs.hidden_sibling`
(`.{nombre}.{marca}-{sufijo}`), y la marca es la que dice qué es y qué se
puede hacer:

- `respaldo` (`library.MARCA_RESPALDO`): lo que `DestinationGuard` apartó
  antes de dejar que `wit` escribiera encima. Es un archivo COMPLETO del
  usuario: se puede restaurar.
- `wbm-respaldo` (`oscwii_installer.MARCA_RESPALDO`): la versión anterior
  de una app de Homebrew, apartada antes de instalar la nueva. También
  está completa: se puede restaurar.
- `wbm-staging` (`oscwii_installer.MARCA_STAGING`): la carpeta donde se
  estaba armando la app NUEVA. Nunca llegó a ser la app final, así que no
  hay ningún "antes" al que volver: solo se puede descartar.
- `parcial` (`atomicfs.MARCA_PARCIAL`): un temporal de escritura a medio
  llenar. Mismo caso: no es la versión vieja de nada, es la nueva a
  medias. Solo se puede descartar.

El filtro que hace que esto sea seguro
--------------------------------------
Un resto y una operación en curso se ven IGUAL en el disco: los dos son un
archivo oculto con el nombre de un destino adentro. La diferencia no está
en el archivo sino en si su dueño sigue vivo, y por eso nada se ofrece sin
pasar antes por `_esta_abandonado`:

1. El PID del nombre tiene que estar muerto. Un `.Juego.wbfs.respaldo-4821`
   con el 4821 corriendo es una conversión pasando AHORA MISMO, y borrarlo
   o restaurarlo encima le arruinaría el archivo al usuario.
2. Los `parcial` no traen PID (`tempfile.mkstemp` les pone un sufijo al
   azar, ver `atomicfs.MARCA_PARCIAL`), así que para ellos se usa lo único
   que hay: la antigüedad. Un temporal que se tocó hace un minuto puede
   ser una copia en curso; uno de hace horas no lo es.
3. Ninguna operación registrada en el `OperationManager` puede estar
   ocupando ese lugar, sin importar de qué proceso sea. Es el mismo
   registro que impide expulsar un USB al que se le está copiando algo.

Los tres filtros fallan CERRADO: ante la duda -un PID que no se puede
consultar, un archivo que no se puede medir- el resto no se muestra. Un
huérfano que no se ofrece limpiar cuesta espacio en disco; uno que se
ofrece de más cuesta el juego de un cliente.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from . import atomicfs, drives, library, oscwii_installer
from .fsutil import path_size
from .i18n import _


class LeftoverKind(Enum):
    """Qué es un resto y, con eso, qué se puede hacer con él.

    El valor es la marca del nombre oculto tal como la escribió quien lo
    dejó: es la clave con la que se lo reconoce en el disco y por eso sale
    de la constante de cada módulo, no de un literal repetido acá."""

    #: Respaldo de `library.DestinationGuard`: el archivo del usuario,
    #: entero, apartado antes de que `wit` escribiera encima.
    BACKUP = library.MARCA_RESPALDO
    #: Respaldo del instalador de Homebrew: la versión anterior de la app.
    HOMEBREW_BACKUP = oscwii_installer.MARCA_RESPALDO
    #: Staging del instalador: la app nueva a medio armar.
    HOMEBREW_STAGING = oscwii_installer.MARCA_STAGING
    #: Temporal de escritura de `atomicfs`, a medio llenar.
    PARTIAL = atomicfs.MARCA_PARCIAL

    @property
    def restorable(self) -> bool:
        """Si tiene sentido ofrecer "Restaurar".

        Solo los respaldos: son lo que HABÍA y quedó apartado, así que
        devolverlos recupera algo. Una staging y un temporal son lo que
        IBA A HABER y quedó a medias -no hay ningún estado anterior
        guardado ahí adentro-, y ofrecer restaurarlos sería ofrecer poner
        un archivo incompleto en el lugar del bueno."""
        return self in (LeftoverKind.BACKUP, LeftoverKind.HOMEBREW_BACKUP)

    @property
    def has_pid(self) -> bool:
        """Si el sufijo del nombre es el PID de quien lo dejó.

        Los respaldos y la staging usan `atomicfs.hidden_sibling`, que
        pone el PID justamente para que el nombre sea reconocible. Los
        temporales salen de `tempfile.mkstemp`, que garantiza unicidad
        real con un sufijo al azar; el precio es que no se puede saber
        quién los dejó."""
        return self is not LeftoverKind.PARTIAL

    @property
    def label(self) -> str:
        """Qué es esto, en una línea, para quien lo va a ver en la lista.

        Se nombra por lo que el usuario perdió o ganaría, no por el
        mecanismo: "respaldo de un juego" y no "SetAside sin descartar"."""
        return {
            LeftoverKind.BACKUP: _("Respaldo de un juego"),
            LeftoverKind.HOMEBREW_BACKUP: _("Respaldo de una app de Homebrew"),
            LeftoverKind.HOMEBREW_STAGING: _("Instalación de Homebrew a medias"),
            LeftoverKind.PARTIAL: _("Archivo temporal incompleto"),
        }[self]

    @property
    def description(self) -> str:
        """Por qué está ahí y qué implica, para el diálogo de detalles."""
        return {
            LeftoverKind.BACKUP: _(
                "Copia del archivo original que se apartó antes de "
                "sobrescribirlo. Está completa: se puede devolver a su "
                "lugar."),
            LeftoverKind.HOMEBREW_BACKUP: _(
                "Versión anterior de la app, apartada antes de instalar la "
                "nueva. Está completa: se puede devolver a su lugar."),
            LeftoverKind.HOMEBREW_STAGING: _(
                "Carpeta donde se estaba armando la app nueva. Nunca llegó "
                "a instalarse, así que solo se puede eliminar."),
            LeftoverKind.PARTIAL: _(
                "Archivo a medio escribir de una copia que se cortó. No es "
                "una versión anterior de nada: solo se puede eliminar."),
        }[self]


# Las marcas, más largas primero: "wbm-respaldo" tiene "respaldo" adentro y
# la alternancia del regex se queda con la primera que coincide. El punto
# que va delante en el patrón ya las separa (`.wbm-respaldo-` no es
# `.respaldo-`), pero ordenarlas hace que eso no dependa de un detalle.
_MARCAS = sorted((k.value for k in LeftoverKind), key=len, reverse=True)

# `.{nombre}.{marca}-{sufijo}`, el formato que arma `atomicfs`. El nombre
# se captura codicioso y con backtracking porque puede tener puntos adentro
# (`RMCP01.wbfs`): lo que ancla el corte es el `\.` inmediatamente anterior
# a una marca conocida.
_PATRON = re.compile(
    r"^\.(?P<nombre>.+)\.(?P<marca>" + "|".join(re.escape(m) for m in _MARCAS)
    + r")-(?P<sufijo>.+)$"
)

# Cuánto tiene que hacer que nadie toca un `parcial` para considerarlo
# abandonado. Es la única defensa que tienen -no traen PID- y por eso el
# número se elige del lado seguro: media hora es muchísimo más que
# cualquier pausa de una copia sana (una copia escribe seguido, así que su
# mtime se mueve todo el tiempo) y sigue siendo poco para alguien que
# vuelve a abrir la app después de que se le colgó la PC.
PARTIAL_MIN_AGE_SECONDS = 30 * 60

# Hasta qué profundidad se baja desde cada raíz. Los restos son siempre
# HERMANOS de su destino (`atomicfs.hidden_sibling`), y los destinos que
# escribe la app viven, como mucho, en `wbfs/<ID6>/`, `games/<Título>/` o
# `apps/<App>/`: tres niveles alcanzan para todos. El límite no es una
# optimización menor -sin él, escanear un disco de 2 TB al arrancar la app
# recorrería el árbol entero del cliente.
MAX_DEPTH = 3


class RecoveryError(RuntimeError):
    """No se pudo hacer lo que se pidió sobre un resto.

    La POLÍTICA de este módulo, no de la primitiva: `atomicfs.SetAside`
    devuelve lo que no pudo hacer y no sabe nada de usuarios (ver su
    docstring). Acá eso se convierte en una excepción con un mensaje
    mostrable, que es lo que necesita la ventana."""


@dataclass(frozen=True)
class Leftover:
    """Un resto encontrado, con todo lo que hace falta para decidir.

    Es una FOTO del momento del escaneo: `size_bytes` y `mtime` son los de
    ese instante y no se vuelven a mirar. Por eso `restore()` y `delete()`
    trabajan contra el disco de verdad y reportan si algo cambió mientras
    tanto, en vez de confiar en estos números."""

    #: El resto en sí: el archivo o la carpeta oculta.
    path: Path
    #: A qué nombre volvería (si es restaurable) o a qué nombre iba a
    #: llegar (si no). Siempre es un hermano de `path`, sin el punto ni la
    #: marca.
    original: Path
    kind: LeftoverKind
    #: El PID que lo dejó, o None si la marca no lo lleva (`parcial`).
    pid: Optional[int]
    size_bytes: int
    mtime: float
    is_dir: bool
    #: Si en el nombre original hay algo AHORA. Restaurar encima de eso lo
    #: pisa, así que la interfaz tiene que avisarlo antes de hacerlo.
    original_exists: bool

    @property
    def restorable(self) -> bool:
        return self.kind.restorable

    def age_seconds(self, now: Optional[float] = None) -> float:
        """Cuánto hace que nadie lo toca. Nunca negativo: un reloj que se
        atrasó (o un archivo con fecha futura, que en FAT pasa) daría una
        antigüedad negativa y eso no significa nada para quien lo lee."""
        return max((now if now is not None else time.time()) - self.mtime, 0.0)


# ------------------------------------------------------- Leer un nombre --
def classify(path: Path) -> Optional[Leftover]:
    """Lee el NOMBRE de `path` y arma su `Leftover`, o None si no es un
    resto de esta app.

    Solo mira el nombre y hace un `lstat`: no decide si está abandonado
    (eso es `_esta_abandonado`, que necesita saber de PIDs y de operaciones
    en curso) ni si hay que mostrarlo. Separado así se lo puede probar
    contra nombres armados a mano, sin procesos ni relojes de por medio.

    Devuelve None -y no una excepción- para cualquier cosa que no encaje:
    la basura de macOS (`._Juego.wbfs`), un `.Trash-1000`, o un archivo que
    se esfumó entre que se lo listó y se lo quiso medir. En un escaneo que
    recorre miles de entradas, "esto no es asunto mío" es el caso normal."""
    path = Path(path)
    m = _PATRON.match(path.name)
    if m is None:
        return None

    kind = LeftoverKind(m.group("marca"))
    sufijo = m.group("sufijo")
    # El PID solo se lee de las marcas que lo llevan. Preguntárselo a un
    # `parcial` sería peor que no preguntar: `mkstemp` puede sacar un
    # sufijo que resulte ser todo dígitos, y ahí estaríamos consultando un
    # proceso que no tiene nada que ver con este archivo.
    pid = int(sufijo) if kind.has_pid and sufijo.isdigit() else None

    try:
        st = path.lstat()
    except OSError:
        return None

    original = path.with_name(m.group("nombre"))
    try:
        existe = original.exists()
    except OSError:
        # No poder confirmar que el lugar está libre se trata como que está
        # ocupado: hace que la interfaz pida confirmación antes de pisar
        # algo, que es el lado seguro de esta duda.
        existe = True

    return Leftover(
        path=path,
        original=original,
        kind=kind,
        pid=pid,
        size_bytes=path_size(path),
        mtime=st.st_mtime,
        is_dir=path.is_dir(),
        original_exists=existe,
    )


# -------------------------------------------------- ¿Sigue vivo el dueño? --
def process_is_alive(pid: Optional[int]) -> bool:
    """Si el proceso `pid` sigue corriendo en esta máquina.

    `os.kill(pid, 0)` no manda ninguna señal: solo hace que el kernel
    resuelva el PID y chequee permisos, que es justo lo que se quiere
    preguntar. Los tres desenlaces se leen distinto:

    - `ProcessLookupError`: no existe. Es el único caso en el que se
      responde "muerto".
    - `PermissionError`: existe, pero es de otro usuario. Existir es lo
      que importa acá.
    - cualquier otro `OSError`: no se pudo averiguar, y no saber no es lo
      mismo que saber que murió.

    Un PID que no se conoce (`None`) o que no es un PID de proceso
    (`<= 0`; el 0 es el grupo de procesos entero, preguntarle no
    significaría nada) también dan "vivo": son la respuesta conservadora,
    la que hace que el resto NO se ofrezca.

    Queda un caso que esto no distingue: que el sistema haya reciclado el
    número y ahora sea de un proceso sin relación. El resultado es un
    huérfano de verdad que no se ofrece limpiar -cuesta espacio en disco,
    no datos- y por eso se acepta en vez de agregarle al nombre una marca
    de tiempo de arranque que habría que mantener en las cuatro familias
    de restos."""
    if pid is None or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _esta_abandonado(leftover: Leftover, *, now: float,
                     is_alive: Callable[[Optional[int]], bool],
                     ops, min_age: float) -> bool:
    """Los tres filtros del docstring del módulo, en el orden en que
    conviene: primero los que se responden con el nombre y un `stat`, y al
    final el que toma el candado del `OperationManager`."""
    if leftover.kind.has_pid:
        if is_alive(leftover.pid):
            return False
    elif leftover.age_seconds(now) < min_age:
        # Sin PID no hay a quién preguntarle, así que un temporal reciente
        # se deja en paz: puede ser una copia escribiendo en este momento.
        return False

    if ops is not None and is_locked_by_operation(ops, leftover):
        return False
    return True


def is_locked_by_operation(ops, leftover: Leftover) -> bool:
    """Si alguna operación registrada en `ops` ocupa el lugar del resto.

    Se pregunta por el resto Y por su original: una conversión declara como
    `write_paths` el nombre FINAL (`Juego.wbfs`), no el del respaldo oculto
    que apartó, así que preguntar solo por el resto dejaría pasar
    exactamente el caso peligroso. `is_resource_busy` cubre además el punto
    de montaje y el disco físico, que es lo que declaran las
    transferencias, la instalación de Homebrew y Modo Fábrica.

    Es pública porque hace falta DOS veces: durante el escaneo, para no
    listar lo que está en uso, y otra vez justo antes de tocar el disco.
    Entre las dos cosas hay un diálogo abierto y una persona leyéndolo, y
    en ese rato pudo arrancar una transferencia sobre esa misma unidad
    -es el mismo motivo por el que existe `OperationManager.check`."""
    for ruta in (leftover.path, leftover.original):
        if ops.is_resource_busy(ruta) is not None:
            return True
        if ops.is_path_busy(ruta):
            return True
    return False


# ------------------------------------------------------------- Escaneo --
def _iter_entries(root: Path, depth: int) -> Iterator:
    """Recorre `root` hasta `MAX_DEPTH` niveles, sin bajar nunca a una
    carpeta oculta.

    No bajar a las ocultas no es solo velocidad: un resto es siempre
    hermano de un destino con nombre normal, así que adentro de una
    carpeta oculta no hay ninguno que encontrar. Lo que sí hay es la
    staging y el respaldo de Homebrew (que son carpetas y se reportan
    ENTERAS, no archivo por archivo) y la basura de otros sistemas
    (`.Trash-1000`, `.Spotlight-V100`), que no es asunto de esta app.

    Los symlinks no se siguen: un enlace a `/` convertiría el escaneo de
    arranque en un recorrido del disco entero."""
    try:
        with os.scandir(root) as it:
            entradas = list(it)
    except OSError:
        # Una carpeta ilegible -permisos, unidad desconectada a mitad del
        # escaneo- se salta en silencio. El escaneo es un aviso, no un
        # inventario: vale mucho más entregarlo incompleto que no
        # entregarlo.
        return
    for entry in entradas:
        yield entry
        if not entry.name.startswith(".") and depth < MAX_DEPTH:
            try:
                es_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if es_dir:
                yield from _iter_entries(Path(entry.path), depth + 1)


def scan(roots: Iterable[Path], *, ops=None,
         now: Optional[float] = None,
         is_alive: Callable[[Optional[int]], bool] = process_is_alive,
         min_age: float = PARTIAL_MIN_AGE_SECONDS) -> list[Leftover]:
    """Todos los restos ABANDONADOS bajo `roots`, del más grande al más
    chico.

    Nunca levanta por culpa del filesystem: una raíz que no existe, una
    carpeta sin permisos o un USB que se desconecta a mitad del recorrido
    aportan lo que se haya alcanzado a leer y nada más. Esto corre al
    arrancar la app, y un escaneo que explota impediría abrirla.

    `ops`, `now`, `is_alive` y `min_age` se inyectan para poder probar los
    filtros sin depender de qué procesos hay corriendo ni de qué hora es.
    Sin `ops` no se consulta el registro de operaciones: es lo correcto
    para una prueba o una herramienta suelta, no para la app -la ventana
    siempre lo pasa.

    El orden es por tamaño porque es el que le sirve a quien mira la
    lista: lo primero que quiere ver es qué le está comiendo los GB."""
    ahora = now if now is not None else time.time()
    encontrados: dict = {}
    for root in roots:
        root = Path(root)
        for entry in _iter_entries(root, 0):
            if not entry.name.startswith("."):
                continue
            leftover = classify(Path(entry.path))
            if leftover is None:
                continue
            if not _esta_abandonado(leftover, now=ahora, is_alive=is_alive,
                                    ops=ops, min_age=min_age):
                continue
            # Dos raíces se pueden solapar (la biblioteca guardada dentro
            # del USB que también se escanea), y el mismo resto no tiene
            # por qué aparecer dos veces en la lista.
            encontrados[_clave(leftover.path)] = leftover
    return sorted(encontrados.values(), key=lambda lo: lo.size_bytes,
                  reverse=True)


def _clave(path: Path):
    """Identidad de una ruta para deduplicar, resistente a que la misma
    carpeta se alcance por dos caminos (un symlink, dos raíces que se
    solapan). Si no se puede resolver, la ruta absoluta tal cual es mejor
    que descartar el resto."""
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def scan_roots(settings=None, *,
               candidate_drives: Callable = drives.list_candidate_drives,
               mount_points: Callable = drives.mount_points_of) -> list[Path]:
    """Dónde buscar: la biblioteca local y todas las unidades removibles
    montadas en este momento.

    Las unidades salen de `list_candidate_drives()` y no de los puntos de
    montaje a secas, para que este escaneo herede el BLINDAJE 1 de
    `drives`: solo entra lo que el kernel marca `removable=1`. Un disco
    interno no puede llegar acá por ningún camino, igual que no puede
    llegar al desplegable de Modo Fábrica. Encima de eso se descartan las
    rutas críticas del sistema (`is_critical_system_path`): si alguien
    montó un USB en `/boot`, el Recovery Manager no tiene nada que ofrecer
    ahí.

    Un disco conectado pero sin montar no aporta nada y no es un error:
    simplemente no hay carpeta que recorrer.

    Las raíces que se solapan NO se podan -si la biblioteca está adentro
    del USB, se recorren las dos-. Podar la de adentro parecería
    equivalente, pero el límite de profundidad se cuenta desde cada raíz:
    la biblioteca escaneada como raíz propia llega a sus `wbfs/<ID6>/`,
    y alcanzada desde la raíz del USB se quedaría un nivel corta. Lo que
    aparezca dos veces lo deduplica `scan`."""
    raices: list[Path] = []
    if settings is not None:
        raices.append(Path(settings.library_path))

    try:
        candidatos = candidate_drives()
    except OSError:
        candidatos = []
    for device in candidatos:
        try:
            raices.extend(mount_points(device.path))
        except OSError:
            continue

    vistas = set()
    resultado: list[Path] = []
    for raiz in raices:
        raiz = Path(raiz)
        try:
            if not raiz.is_dir():
                continue
        except OSError:
            continue
        if drives.is_critical_system_path(raiz):
            continue
        clave = _clave(raiz)
        if clave in vistas:
            continue
        vistas.add(clave)
        resultado.append(raiz)
    return resultado


# ------------------------------------------------------------ Acciones --
def restore(leftover: Leftover) -> None:
    """Devuelve un respaldo a su nombre original.

    El trabajo lo hace `atomicfs.SetAside.restore()`, el mismo que habría
    corrido `DestinationGuard` (o el instalador de Homebrew) si el proceso
    hubiera llegado vivo al final: un `os.replace` dentro de la misma
    carpeta, instantáneo y sin copiar los datos. Acá arriba está la
    política -qué se puede restaurar y qué significa que falle- que la
    primitiva a propósito no tiene.

    OJO con lo que esto pisa: si en el nombre original hay algo ahora
    (`Leftover.original_exists`), `os.replace` lo reemplaza sin preguntar.
    Es lo que se quiere -ese "algo" es lo que la operación interrumpida
    alcanzó a escribir, casi siempre incompleto- pero es destructivo, así
    que quien llama tiene que haberlo confirmado antes. La interfaz lo
    hace en `RecoveryDialog._on_restore`.

    Levanta `RecoveryError` si no era restaurable (una staging o un
    temporal: no hay ningún "antes" adentro) o si el rename falló."""
    if not leftover.restorable:
        raise RecoveryError(
            _("«{name}» no se puede restaurar: no es una copia de "
              "seguridad, es un archivo que quedó a medio escribir.")
            .format(name=leftover.original.name))

    aside = atomicfs.SetAside.adopt(leftover.kind.value,
                                    [(leftover.original, leftover.path)])
    pendientes = aside.restore()
    if pendientes:
        raise RecoveryError(
            _("No se pudo restaurar «{name}»: el archivo sigue en "
              "{ruta}.").format(name=leftover.original.name,
                                ruta=leftover.path))


def delete(leftover: Leftover) -> None:
    """Borra un resto. Sirve para los cuatro tipos: descartar un respaldo
    es tan válido como descartar una staging, y es lo que el usuario elige
    cuando lo que quiere es el espacio.

    Lo hace `atomicfs.SetAside.discard()`, que ya distingue carpeta de
    archivo y reporta lo que no pudo borrar; con `missing_ok` adentro, un
    resto que ya no está cuenta como borrado, que es la respuesta correcta
    para alguien que quería que dejara de ocupar lugar.

    Levanta `RecoveryError` si no se pudo, con la ruta adentro: sin el
    "dónde", el usuario se queda con una unidad llena por algo que no
    puede ver."""
    aside = atomicfs.SetAside.adopt(leftover.kind.value,
                                    [(leftover.original, leftover.path)])
    fallidos = aside.discard()
    if fallidos:
        raise RecoveryError(
            _("No se pudo eliminar {ruta}. Puede estar en uso o la unidad "
              "puede ser de solo lectura.").format(ruta=leftover.path))


def summary(leftovers: list) -> tuple:
    """(cuántos, cuánto ocupan) para el aviso corto de la ventana."""
    return len(leftovers), sum(lo.size_bytes for lo in leftovers)

