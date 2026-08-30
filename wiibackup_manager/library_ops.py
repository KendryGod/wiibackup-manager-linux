"""Las operaciones que modifican la biblioteca o la unidad de destino.

Renombrar al nombre estándar, y mandar un juego a la unidad del cliente
apartando antes lo que ya estaba por si hay que volver atrás. Todo lo de
acá ESCRIBE: es la contraparte de `transfer_plan`, que solo calcula.

Ojo con el nombre: `operations.py` es otra cosa (el `OperationManager`,
que coordina qué operación puede tocar qué recurso físico). Este módulo no
coordina nada, hace el trabajo.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from . import atomicfs, drives, wit_wrapper
from .fileops import _copy_with_progress, free_variant, rename_no_replace
from .game_model import Game, standard_filename
from .i18n import _
from .transfer_plan import game_dest_path, wbfs_group


# Límite duro de FAT32 por archivo (en realidad 2^32 - 1 bytes; usamos
# 4 GiB parejo como aproximación) para decidir si un WBFS ya existente
# entra sin dividir. Nota: esto es distinto de
# `wit_wrapper.FAT32_SPLIT_SIZE_BYTES` (4 GB, más chico a propósito), que es
# el tamaño de partición que le pedimos a `wit` cuando sí hace falta
# dividir — no hace falta que coincidan, uno decide "¿hace falta dividir?"
# y el otro "¿dónde corta wit cada parte?".
_FAT32_SIZE_LIMIT_BYTES = 4 * 1024 ** 3


# Cuántos nombres alternativos se prueban antes de darse por vencido.
_MAX_RENAME_ATTEMPTS = 100


def rename_to_standard(game: Game, dry_run: bool = False,
                        on_collision: str = "error") -> Path:
    """Renombra el archivo del juego a la convención 'Título [ID].ext'
    dentro de la misma carpeta. Devuelve la nueva ruta.

    `on_collision` decide qué pasa si ese nombre ya está tomado por otro
    archivo: "error" levanta `FileExistsError` (flujo de un juego suelto,
    donde el usuario ve el aviso y decide) y "suffix" busca una variante
    libre con sufijo (flujo en lote, donde frenar por cada choque no
    tendría sentido). En ninguno de los dos casos se pisa nada.

    Dos juegos con el mismo título y sin ID identificado dan el mismo
    nombre estándar: ese es el caso real de colisión, y en lote termina
    como 'Título.iso' y 'Título (2).iso'."""
    new_name = standard_filename(game)
    new_path = game.path.with_name(new_name)
    if new_path == game.path:
        return game.path

    if dry_run:
        if new_path.exists():
            if on_collision != "suffix":
                raise FileExistsError(_("Ya existe un archivo en {path}").format(path=new_path))
            new_path = free_variant(new_path)
        return new_path

    # Sin chequear-y-después-renombrar: `rename_no_replace` reserva el
    # nombre de forma atómica y avisa si estaba tomado, así que un archivo
    # que aparezca justo en el medio no se pierde.
    try:
        rename_no_replace(game.path, new_path)
    except FileExistsError:
        if on_collision != "suffix":
            raise FileExistsError(_("Ya existe un archivo en {path}").format(path=new_path))
        # Buscar una variante libre reintentando: si otro proceso se queda
        # con "Juego (2).wbfs" mientras tanto, se sigue con la siguiente.
        base = new_path
        for n in range(2, _MAX_RENAME_ATTEMPTS + 2):
            candidate = base.with_name(f"{base.stem} ({n}){base.suffix}")
            try:
                rename_no_replace(game.path, candidate)
            except FileExistsError:
                continue
            new_path = candidate
            break
        else:
            raise FileExistsError(
                _("No se encontró un nombre libre para {name}").format(name=base.name)
            )
    game.path = new_path
    return new_path


class DestinationExistsError(FileExistsError):
    """El archivo destino en la unidad WBFS ya existe y no se pidió
    sobrescribirlo. Es una condición esperable (el juego ya está en la
    unidad), no un error de la operación: quien llama decide si preguntar
    (flujo individual) o contarlo como omitido (flujo en lote)."""

    def __init__(self, dest: Path):
        super().__init__(f"Ya existe un archivo en {dest}")
        self.dest = dest


class MultiGameContainerError(RuntimeError):
    """`game.path` es un .wbfs que contiene más de un juego (detectado con
    `wit_wrapper.list_wbfs_container`). `identify_file` solo identifica el
    primero de la lista, así que una copia directa terminaría poniendo el
    contenedor entero -con todos los juegos que tenga adentro- en el
    destino de un solo juego (wbfs/<ID6>/<ID6>.wbfs), algo que los USB
    Loaders no van a reconocer como válido. Se aborta en vez de adivinar
    cuál de los juegos del contenedor es el que se quiso transferir."""

    def __init__(self, path: Path, contenido: list["wit_wrapper.DiscInfo"]):
        ids = ", ".join(juego.game_id for juego in contenido)
        super().__init__(
            f"{path} es un WBFS multi-juego ({ids}); no se puede copiar "
            "directo a un destino de un solo juego.")
        self.path = path
        self.contenido = contenido


class RollbackFailedError(RuntimeError):
    """`DestinationGuard._restore()` no pudo devolver TODOS los
    originales apartados a su lugar: al menos un respaldo temporal
    quedó sin restaurar.

    Nunca se levanta desde una operación exitosa: solo puede pasar
    adentro de `_restore()`, que a su vez solo corre después de que algo
    YA había fallado antes (la conversión, una cancelación, o el propio
    apartado del respaldo en `__enter__`). O sea que cuando esto se
    levanta hay DOS problemas encadenados, no uno -y `original_error`,
    si se lo pudo determinar, es el primero de los dos: quien atrape
    esta excepción tiene que nombrar ambos en el mensaje final, "algo
    falló" no alcanza para un archivo que puede haber quedado
    inservible.

    `pending` es la lista de `(original, respaldo)` que no volvieron a
    su lugar -en un WBFS dividido puede ser una sola de las tres partes-:
    el archivo de respaldo TODAVÍA existe en esa ruta, nada se perdió,
    pero alguien tiene que moverlo a mano."""

    def __init__(self, pending: list[tuple[Path, Path]],
                original_error: BaseException | None = None):
        self.pending = list(pending)
        self.original_error = original_error
        detalle = "; ".join(
            f"{original} (respaldo en {respaldo})"
            for original, respaldo in self.pending)
        super().__init__(
            "No se pudo restaurar el archivo original después de un "
            f"error: {detalle}")

    def user_message(self) -> str:
        """El mensaje para mostrarle al usuario -no `str(self)` a secas:
        acá hay DOS problemas (por qué se estaba restaurando, y que
        además la restauración falló) y hay que nombrar los dos, o el
        usuario ve "la conversión falló" sin enterarse de que el
        original puede haber quedado inservible. Si no se conoce el
        motivo original (`self.original_error is None`, el caso más raro:
        `_restore()` se llamó sin que nada hubiera fallado antes) se
        vuelve al mensaje de acá nomás."""
        if self.original_error is None:
            return str(self)
        return _(
            "La conversión falló ({motivo}) y además no se pudo "
            "restaurar completamente el archivo original: {detalle}"
        ).format(motivo=str(self.original_error), detalle=str(self))


# Marca del nombre oculto del respaldo: `.{nombre}.respaldo-{pid}`, en la
# misma carpeta que el original (ver `atomicfs.hidden_sibling`). El nombre
# tiene que ser reconocible: `_cleanup_partials` lo protege explícitamente
# de la limpieza de temporales de `wit`, que barre `.{nombre}.{lo que sea}`.
#
# Es público porque hay un segundo lector: `recovery_service` reconoce por
# esta misma marca los respaldos que quedaron de una sesión que se cortó a
# mitad. Que la escriba y la lea la MISMA constante es lo que evita que un
# día se renombre acá y el Recovery Manager deje de encontrarlos en
# silencio.
MARCA_RESPALDO = "respaldo"


class DestinationGuard:
    """Aparta lo que ya hay en el destino y lo devuelve si algo falla.

    Es para las operaciones que le pasan `--overwrite` a `wit`. El
    problema con dejar que `wit` sobrescriba solo es que no hay forma
    confiable de deshacerlo: `wit` escribe en temporales propios y los
    renombra a los nombres finales al terminar, así que si lo matan
    justo después de renombrar la primera parte, el nombre final ya
    quedó con contenido parcial y el limpiador no puede distinguirlo del
    archivo que el usuario tenía ahí desde antes.

    Con el respaldo explícito no hay nada que adivinar: los archivos que
    ya estaban se mueven a un nombre oculto (un rename dentro de la misma
    carpeta, instantáneo y sin copiar datos), `wit` escribe sobre nombres
    libres, y al final se borra el respaldo o se lo devuelve a su lugar
    según cómo haya salido.

    El precio es que mientras dura la operación conviven el respaldo y lo
    nuevo, o sea que hace falta espacio para los dos. Es el costo de
    poder volver atrás.

    Uso:

        with DestinationGuard(dest) as guard:
            ...escribir dest...
            guard.commit()      # solo si salió TODO bien
    """

    def __init__(self, dest: Path, enabled: bool = True):
        self.dest = Path(dest)
        self.enabled = enabled
        # El mecanismo de apartar/devolver/descartar es compartido
        # (`atomicfs.SetAside`); lo que esta clase pone encima es cuándo
        # hacerlo y qué significa cada fallo.
        self._aside = atomicfs.SetAside(MARCA_RESPALDO)
        self._committed = False
        self._outputs_before: set = set()
        # Respaldos que la operación terminó bien pero no se pudieron
        # borrar (ver `_discard`). Quien usa el guard los lee DESPUÉS del
        # `with` para avisarle al usuario y anotarlo en el historial.
        self.orphaned_backups: list = []

    @property
    def _saved(self) -> list:
        """Los pares `(original, respaldo)` todavía apartados. Vive en
        `SetAside`; sigue expuesto con este nombre porque es lo que
        `_cleanup_partials` protege de la limpieza y lo que viaja adentro
        de `RollbackFailedError`."""
        return self._aside.pairs

    def __enter__(self) -> "DestinationGuard":
        if not self.enabled:
            # Sin respaldo que apartar, pero la foto se toma igual: la
            # limpieza de un fallo tiene que poder distinguir lo que dejó
            # ESTA operación de lo que ya estaba.
            self._outputs_before = wit_wrapper.output_files(self.dest)
            return self
        for original in wbfs_group(self.dest):
            try:
                self._aside.move_aside(original)
            except OSError as e:
                # No se pudo apartar: se deshace lo ya apartado y se sale
                # sin tocar nada, mejor que quedar a mitad de camino. Si
                # ESE deshacer también falla, lo que se propaga es
                # `RollbackFailedError` (más grave: hay un original que
                # quedó movido a un nombre de respaldo, no solo un intento
                # de apartado que no arrancó) con este `OSError` como
                # motivo original.
                try:
                    self._restore()
                except RollbackFailedError as rollback_error:
                    rollback_error.original_error = e
                    raise
                raise
        # La foto va DESPUÉS de apartar el respaldo, no antes: el respaldo
        # se llama `.{nombre}.respaldo-PID` y cae dentro del mismo glob con
        # el que `wit_wrapper` reconoce sus temporales
        # (`.{nombre}.{lo que sea}`). Si la foto se tomara antes, el
        # respaldo aparecería como "archivo nuevo de esta operación" y la
        # limpieza de un fallo lo borraría — o sea, justo lo contrario de
        # para lo que existe esta clase.
        self._outputs_before = wit_wrapper.output_files(self.dest)
        return self

    def commit(self) -> None:
        """La operación salió bien: el respaldo ya no hace falta."""
        self._committed = True

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Ojo: acá NO se corta por `enabled`. Con el respaldo desactivado
        # no hay nada que restaurar (`_saved` está vacío y `_restore` /
        # `_discard` no hacen nada), pero sí hay que barrer lo que la
        # operación fallida dejó a medio escribir.
        if self._committed and exc_type is None:
            self._discard()
        else:
            self._cleanup_partials()
            try:
                self._restore()
            except RollbackFailedError as rollback_error:
                # `exc` es lo que haya fallado DENTRO del `with` (la
                # conversión, una cancelación) -el motivo por el que se
                # llegó a intentar restaurar en primer lugar. Si además
                # restaurar falla, quien atrape esto necesita los dos
                # datos: no alcanza con saber que el respaldo quedó a
                # medio volver, si no también por qué se estaba
                # restaurando.
                rollback_error.original_error = exc
                raise
        return False  # nunca se traga la excepción

    def _cleanup_partials(self) -> None:
        """Borra lo que la operación fallida haya alcanzado a dejar, para
        que el respaldo pueda volver a su lugar y no quede basura ocupando
        espacio.

        Hay que barrer dos cosas, no una: los nombres finales (`dest`,
        `dest.wbf1`, ...) y los temporales OCULTOS que `wit` usa mientras
        escribe (`.{dest}.{random}.tmp`, `.tmp.1`, ...). `wit_wrapper` ya
        limpia los suyos cuando cancela o cuando vence el timeout, pero si
        `wit` simplemente devuelve un código de error después de haber
        escrito medio archivo, esos temporales no los borraba nadie:
        quedaban huérfanos, invisibles para el usuario (empiezan con
        punto) y ocupando varios GB.

        `cleanup_new_output_files` cubre las dos familias y solo toca lo
        que apareció después de `__enter__`, así que no se lleva por
        delante lo que ya estaba."""
        # Los respaldos van explícitos en el conjunto protegido además de
        # estar en la foto: son lo único que no se puede perder acá, y no
        # depende de que la foto se haya tomado en el orden correcto.
        protegidos = self._outputs_before | {resp for _orig, resp in self._saved}
        wit_wrapper.cleanup_new_output_files(self.dest, protegidos)
        for parcial in wbfs_group(self.dest):
            try:
                parcial.unlink()
            except OSError:
                pass

    def _restore(self) -> None:
        """Devuelve cada respaldo a su nombre original. Se intenta con
        TODOS aunque alguno falle -en un WBFS dividido (wbfs/wbf1/wbf2)
        no tiene sentido dejar dos partes sin restaurar porque la
        tercera se atoró (permiso, disco desconectándose a medias)- y
        `self._saved` solo se vacía si TODOS volvieron a su lugar. Si
        algo falló, `self._saved` queda con exactamente lo pendiente (no
        vacío, no lo restaurado con éxito) y se levanta
        `RollbackFailedError`: silenciar esto acá dejaba un juego a medio
        restaurar sin que nadie -ni la app, ni el usuario- se enterara.

        `SetAside.restore` hace el trabajo -intenta con todos, en orden
        inverso, y deja pendiente exactamente lo que no pudo-; lo que
        decide esta clase es que eso vale una excepción propia, con un
        mensaje para el usuario."""
        pendientes = self._aside.restore()
        if pendientes:
            raise RollbackFailedError(pendientes)

    def _discard(self) -> None:
        """Borra los respaldos: la operación salió bien y ya no hacen
        falta.

        Un borrado que falla NO se ignora. El respaldo es un archivo
        oculto que puede pesar varios GB (un WBFS entero, y en un juego
        dividido son tres archivos), así que quedarse callado dejaba al
        usuario con la unidad llena por algo que no puede ver ni
        encontrar. Tampoco es un error de la operación -la conversión
        terminó bien y el resultado está donde tiene que estar-, así que
        no se levanta nada: se anota en `orphaned_backups` y quien usa el
        guard lo reporta (`format_orphaned_backups` +
        `oplog.record_orphaned_backup`)."""
        self.orphaned_backups = self._aside.discard()


def send_to_wbfs_drive(
    game: Game,
    drive_root: Path,
    wit_binary: str = "wit",
    bytes_progress_cb: Optional[Callable[[int], None]] = None,
    overwrite: bool = False,
    cancel: Optional["wit_wrapper.CancellationToken"] = None,
    scrub_update: bool = True,
) -> Path:
    """Copia `game` a la estructura estándar 'wbfs/<ID6>/<ID6>.wbfs' que
    reconocen los USB Loaders de Wii (USB Loader GX, CFG USB Loader, etc.)
    dentro de `drive_root`. Si el origen ya es WBFS y entra entero se copia
    tal cual; para cualquier otro formato (ISO/CISO/WDF), o si el destino
    puede necesitar dividir el archivo, se delega en `wit`, que es quien
    sabe empaquetar (y, si hace falta, partir) el WBFS correctamente.

    Antes de la copia directa de un WBFS se chequea, vía
    `wit_wrapper.list_wbfs_container`, que el contenedor tenga un solo
    juego: si tiene más, levanta `MultiGameContainerError` en vez de
    copiar el contenedor entero al destino de un solo juego.

    Si `game.console == "gc"` el destino es el que arma `gc_dest_path`
    (estructura de Nintendont) y SIEMPRE se copia directo con
    `_copy_with_progress`: Nintendont lee ISO y CISO de GameCube tal cual,
    así que no hay nada que convertir ni ninguna razón para pasar por
    `wit` (que además no sabe empaquetar GameCube en WBFS).

    FAT32 tiene un límite duro de ~4GiB por archivo, y hay discos Wii
    dual-layer que lo superan: si el filesystem del destino no se puede
    determinar con confianza, se asume que hace falta dividir (ver
    `drives.needs_wbfs_split`).

    `bytes_progress_cb`, si se pasa, se llama periódicamente con los bytes
    escritos hasta el momento hacia `dest` (copia directa o vía `wit`, ver
    `wit_wrapper.convert`), para poder mostrar progreso real dentro de la
    copia/conversión de un solo juego grande.

    Si el destino ya existe y no se pasa `overwrite=True`, levanta
    `DestinationExistsError` sin tocar nada: los dos caminos (copia
    directa con open("wb"), que trunca al instante, y `wit COPY
    --overwrite`) reemplazan el archivo sin vuelta atrás, así que la
    decisión de pisarlo es de quien llama, igual que ya pasaba al
    convertir.

    `cancel`, si se pasa, hace que cancelar corte esta copia/conversión en
    el momento (matando el `wit` en curso o abortando la copia directa) y
    levante `wit_wrapper.OperationCancelled`.

    `scrub_update` se le pasa tal cual a `wit_wrapper.convert` (ver ahí):
    solo importa cuando el camino termina pasando por `wit` -una copia
    directa de un WBFS que ya entra entero no convierte nada, así que no
    hay partición de actualización que descartar."""
    if cancel is not None and cancel.cancelled:
        raise wit_wrapper.OperationCancelled("Transferencia cancelada por el usuario.")

    dest = game_dest_path(game, drive_root)
    dest_dir = dest.parent

    if not overwrite and dest.exists():
        raise DestinationExistsError(dest)

    dest_dir.mkdir(parents=True, exist_ok=True)

    if game.console == "gc":
        _copy_with_progress(game.path, dest,
                            bytes_progress_cb or (lambda _n: None), cancel)
        return dest

    split = drives.needs_wbfs_split(dest_dir)

    # Copia directa solo si ya es WBFS Y sabemos que entra entero sin
    # dividir (si hiciera falta dividir, una copia plana no puede hacerlo:
    # hay que pasar por `wit COPY --split`).
    if game.fmt.upper() == "WBFS" and not (split and game.size_bytes >= _FAT32_SIZE_LIMIT_BYTES):
        # `identify_file` solo mira el primer juego de un .wbfs (ver
        # `wit_wrapper._find_id6_line`); si el contenedor tiene más de
        # uno, copiarlo tal cual al destino de un solo juego produce un
        # WBFS que ningún USB Loader va a reconocer como válido. Se
        # verifica acá, justo antes del atajo de copia directa, en vez de
        # confiar en que nadie mande un contenedor así -si `wit` no está
        # disponible no hay forma de chequear esto, igual que `identify_file`
        # tampoco podría haber identificado bien ese archivo.
        if wit_wrapper.is_available(wit_binary):
            contenido = wit_wrapper.list_wbfs_container(game.path, wit_binary)
            if len(contenido) > 1:
                raise MultiGameContainerError(game.path, contenido)

        # SIEMPRE por `_copy_with_progress`, aunque no haya progreso ni
        # cancelación que reportar: es el único camino que escribe en un
        # temporal y recién al final lo mueve encima del destino. La rama
        # "atajo" con `shutil.copy2` que había acá abría el destino con
        # "wb", o sea que lo vaciaba en el acto, y un fallo posterior
        # (crash, USB desenchufado, disco lleno) dejaba al usuario sin el
        # respaldo que ya tenía. El callback no-op no cuesta nada; tener
        # dos caminos de escritura, uno protegido y otro no, sí.
        _copy_with_progress(game.path, dest,
                            bytes_progress_cb or (lambda _n: None), cancel)
        return dest

    if not wit_wrapper.is_available(wit_binary):
        raise wit_wrapper.WitNotFoundError(wit_binary)

    # Si había algo en el destino, se lo aparta antes de dejar que `wit`
    # escriba: si la conversión falla o se cancela, vuelve a su lugar.
    with DestinationGuard(dest, enabled=bool(wbfs_group(dest))) as guard:
        # `overwrite=True` explícito: quien llama ya decidió pisar (lo
        # exige el `overwrite` de esta función) y el guard de arriba tiene
        # el respaldo apartado, así que si `wit` se encuentra algo con el
        # nombre final es basura de un intento anterior, no el juego del
        # usuario.
        result = wit_wrapper.convert(game.path, dest, "WBFS", wit_binary, split=split,
                                      bytes_progress_cb=bytes_progress_cb, cancel=cancel,
                                      overwrite=True, scrub_update=scrub_update)
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or _("Error desconocido al convertir con wit"))
        guard.commit()
    return dest
