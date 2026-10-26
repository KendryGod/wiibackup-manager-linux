"""Inyección de "Golden Configs": configuraciones maestras pre-hechas que
se copian automáticamente a la unidad de destino después de instalar
ciertas apps de homebrew puntuales desde la Homebrew Store.

Separado a propósito de `oscwii_installer.py`
-----------------------------------------------
`oscwii_installer.install_app` es infraestructura genérica: sabe bajar,
verificar y extraer CUALQUIER app del catálogo de OSC, y no tiene por qué
saber nada de qué app en particular es "Nintendont" ni de que existe una
carpeta `assets/configs/` en el repo. Esa regla de negocio (para ESTAS dos
apps puntuales, después de instalarlas, copiar ESTOS archivos) vive acá,
y quien orquesta la instalación (hoy, `widgets/homebrew_store_view.py`)
llama a `maybe_apply()` con el `InstallResult` que le devolvió
`install_app`, en el mismo hilo de fondo, apenas la extracción termina.

Investigación de los dos formatos (Paso 1, hecha contra fuentes primarias
antes de escribir una sola línea de esta lógica, no asumida):

GXGlobal.cfg (USB Loader GX)
----------------------------
- Texto plano, una asignación por línea ("clave = valor"), sin secciones.
  Confirmado leyendo el parser real
  (wiidev/usbloadergx, rama "enhanced", source/settings/CSettings.cpp):
  `ParseLine()` corta en el primer '=' y descarta la línea si no lo
  encuentra.
- Ruta real en el destino, confirmada en el mismo archivo (constructor de
  CSettings + `Save()`/`Load()`):
  `<destino>/apps/usbloader_gx/GXGlobal.cfg` -con guión bajo en el nombre
  de la carpeta, no "usbloadergx".
- USB Loader GX SÍ está en Open Shop Channel, con el slug `usbloader_gx`
  (verificado contra la API el 2026-10-18: versión 4.0 r1283). Esta nota
  decía lo contrario, y era cierto cuando se escribió; hoy esta rama de
  `GOLDEN_CONFIGS` se dispara al instalarlo desde la Homebrew Store.
- Su ZIP no trae GXGlobal.cfg, y no hace falta copiar ninguno: USB Loader
  GX arma el suyo. En su código (release v4.0-r1283), `FindConfig` crea el
  archivo, `SetDefault` pone los valores de fábrica (`GameCubePath =
  usb1:/games/` y las rutas de carátulas debajo de su carpeta) y
  `AppCleanUp` lo guarda completo con `Save`. Un archivo sin la línea
  "USB Loader GX R<revisión>" (revisión 1031 o más) lo ignora entero
  (`ValidVersion`).
- El `assets/configs/usbloadergx/GXGlobal.cfg` del repositorio es de
  relleno -está marcado PLACEHOLDER- y no se copia (ver `is_placeholder`).
  Si algún día se lo reemplaza por una configuración maestra de verdad, se
  copia como cualquier otra: solo en una unidad que no tenga la suya.

nincfg.bin (Nintendont)
------------------------
- Binario: la representación cruda del struct `NIN_CFG` de
  `common/include/CommonConfig.h` en FIX94/Nintendont. Confirmado leyendo
  ese header (dos veces, para descartar una alucinación del fetch):

      #define NIN_CFG_VERSION   0x0000000A
      #define NIN_CFG_MAXPAD    4

      typedef struct NIN_CFG {
          unsigned int  Magicbytes;      // 0x01070CF6
          unsigned int  Version;
          unsigned int  Config;
          unsigned int  VideoMode;
          unsigned int  Language;
          char          GamePath[255];
          char          CheatPath[255];
          unsigned int  MaxPads;
          unsigned int  GameID;
          unsigned char MemCardBlocks;
          signed char   VideoScale;
          signed char   VideoOffset;
          unsigned char NetworkProfile;
          unsigned int  WiiUGamepadSlot;
      } NIN_CFG;

  Wii/GameCube (Broadway, PowerPC) es big-endian, así que los `unsigned
  int` van en ese orden de bytes -mismo criterio que ya usa
  `disc_header.py` para los magic words de ISO.
- El header NO tiene ningún `#pragma pack`/`__attribute__((packed))`
  (se buscó explícitamente): el tamaño de 548 bytes que usa
  `NIN_CFG_STRUCT_SIZE` de acá abajo es un CÁLCULO con las reglas de
  alineación estándar de C (cada `unsigned int` alineado a 4 bytes,
  2 bytes de relleno antes de `MaxPads` porque los dos `char[255]`
  anteriores lo dejan en un offset impar), no algo confirmado byte a
  byte contra un nincfg.bin real volcado con un editor hexadecimal.
  Por eso NO se usa como validación estricta de tamaño (ver
  `_asset_is_valid`): además hay versiones más viejas del formato (v8,
  v9) de otro tamaño. Lo que sí se puede confirmar con certeza -y es lo
  que valida esta función- son los 4 bytes de `Magicbytes`.
- Ruta real en el destino: la RAÍZ del dispositivo montado, NO
  `apps/Nintendont/`. Confirmado en el código fuente real del propio
  loader (FIX94/Nintendont, loader/source/global.c):
  `f_open_char(&cfg, "/nincfg.bin", FA_READ|FA_OPEN_EXISTING)`.
- El `assets/configs/nintendont/nincfg.bin` del repositorio es sintético:
  548 bytes con solo `Magicbytes` (01 07 0C F6) y `Version` (10), y todo
  lo demás en cero. Nintendont lo carga como válido (`LoadNinCFG`,
  loader/source/global.c: tamaño, firma, versión y `MaxPads <= 4`) y queda
  con `MaxPads = 0`, que en el juego quiere decir que no lee ningún puerto
  de control de GameCube (`PADRead`, loader/source/ppc/PADReadGC/source/
  PADReadGC.c: `for (chan = 0; chan < MaxPads; ++chan)` sobre los
  registros del SI); además fuerza el idioma inglés (0) y una memoria de
  59 bloques. Sin archivo, Nintendont usa los suyos (`main`,
  loader/source/main.c): idioma automático, `MaxPads = 4` y 251 bloques.
  Por eso es de relleno y no se copia (ver `is_placeholder`): un binario
  no puede llevar la marca de texto, así que se lo reconoce por su sha256
  exacto. Un nincfg.bin de verdad -uno guardado por Nintendont- se copia
  como cualquier otra configuración maestra.

Bonus (no es parte de este archivo, pero salió de la misma
investigación): el ZIP real de Nintendont en el catálogo de OSC trae
también una carpeta "/controllers" fuera de "apps/" -confirmado con los
`subdirectories` que reporta la propia API-, así que
`oscwii_installer._ALLOWED_TOP_LEVEL_DIRS` se amplió para permitirla; sin
eso, el ZIP de Nintendont se rechazaba entero y esta inyección nunca
llegaba a dispararse con una instalación real.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Optional

from . import oplog
from .atomicfs import atomic_write_target
from .fsutil import installed_data_dirs
from .i18n import N_, _
from .oscwii_client import HomebrewApp

if TYPE_CHECKING:
    from .oscwii_installer import InstallResult


# --- Magic number real de NIN_CFG (ver el comentario del módulo). Wii es
# big-endian, así que 0x01070CF6 se lee de los primeros 4 bytes del
# archivo en ese orden. ---
NIN_CFG_MAGIC = bytes.fromhex("01070CF6")
NIN_CFG_VERSION = 0x0000000A

# Tamaño de NIN_CFG calculado con alineación estándar de C (ver el
# comentario del módulo): no se usa como mínimo estricto porque no está
# confirmado byte a byte y porque hay versiones más viejas más chicas.
# Se deja documentado acá, junto a la constante que sí se usa para
# validar de verdad.
NIN_CFG_STRUCT_SIZE_COMPUTED = 548

# Tamaños mínimos de sanidad para el archivo maestro: no pretenden ser el
# tamaño exacto real (para eso está la firma binaria, cuando existe), solo
# descartar un archivo vacío o truncado a casi nada.
_MIN_NINCFG_BYTES = 32
_MIN_GXGLOBAL_BYTES = 20


@dataclass(frozen=True)
class GoldenConfigSpec:
    """Qué copiar para una app puntual, y adónde."""

    label: str                    # nombre humano, para el log y los mensajes
    asset_relative: str           # ruta dentro de la carpeta de assets ("nintendont/nincfg.bin")
    dest_relative: PurePosixPath  # ruta relativa a dest_root
    min_size_bytes: int
    magic: Optional[bytes] = None  # firma binaria esperada al inicio del archivo, si se conoce


# Registro de qué app dispara qué inyección, por slug de OSC (el mismo
# campo que ya usa `oscwii_client.HomebrewApp.slug`). Ver el comentario
# del módulo para la investigación de cada ruta/formato.
GOLDEN_CONFIGS: dict = {
    # El nincfg.bin del repositorio es sintético y no se copia (ver el
    # comentario del módulo y `_PLACEHOLDER_SHA256`); uno de verdad, sí.
    "Nintendont": GoldenConfigSpec(
        label="Nintendont",
        asset_relative="nintendont/nincfg.bin",
        dest_relative=PurePosixPath("nincfg.bin"),
        min_size_bytes=_MIN_NINCFG_BYTES,
        magic=NIN_CFG_MAGIC,
    ),
    # El slug real de USB Loader GX en OSC, que coincide con su carpeta
    # ("usbloader_gx", con guion bajo). Mientras el archivo maestro sea el
    # de relleno no se copia nada (ver el comentario del módulo).
    "usbloader_gx": GoldenConfigSpec(
        label="USB Loader GX",
        asset_relative="usbloadergx/GXGlobal.cfg",
        dest_relative=PurePosixPath("apps/usbloader_gx/GXGlobal.cfg"),
        min_size_bytes=_MIN_GXGLOBAL_BYTES,
        magic=None,  # texto plano: no tiene firma binaria que chequear
    ),
}


class GoldenConfigStatus(Enum):
    APPLIED = "applied"
    # Ya había una configuración en el destino y se dejó como estaba (ver
    # `maybe_apply`). No es un error.
    KEPT_EXISTING = "kept_existing"
    # El archivo maestro del repositorio es de relleno (`is_placeholder`):
    # no se copió nada y la app arranca con su configuración de fábrica.
    # Tampoco es un error.
    SKIPPED_PLACEHOLDER = "skipped_placeholder"
    ASSET_MISSING = "asset_missing"
    ASSET_CORRUPT = "asset_corrupt"
    IO_ERROR = "io_error"
    SKIPPED_INSTALL_FAILED = "skipped_install_failed"

    @property
    def is_error(self) -> bool:
        return self in (GoldenConfigStatus.ASSET_MISSING,
                        GoldenConfigStatus.ASSET_CORRUPT,
                        GoldenConfigStatus.IO_ERROR)


@dataclass(frozen=True)
class GoldenConfigResult:
    status: GoldenConfigStatus
    app_slug: str
    label: str
    dest_path: Optional[Path] = None
    error: str = ""

    @property
    def applied(self) -> bool:
        return self.status is GoldenConfigStatus.APPLIED


# ------------------------------------------------- Ubicar assets/configs --
#
# Mismo problema -y ahora, literalmente, la misma solución- que el
# catálogo de traducciones de `i18n`: la app puede correr desde el repo
# clonado sin instalar, desde `pip install --user`, desde un venv, o desde
# una instalación de sistema, y en cada caso la carpeta de assets termina
# en un lugar distinto. La búsqueda vive en `fsutil.installed_data_dirs`;
# acá solo se dice qué rutas buscar. Ver `pyproject.toml`
# (`[tool.setuptools.data-files]`) para dónde queda cada caso al instalar.
def _candidate_asset_dirs() -> list:
    return installed_data_dirs("assets/configs", "wiibackup-manager/configs")


def find_asset(relative: str, local_dir: Optional[str] = None) -> Optional[Path]:
    """Ruta del archivo maestro `relative` (p. ej. "nintendont/nincfg.bin"),
    en el primer directorio candidato donde exista, o None si no está en
    ninguno.

    `local_dir` (`Settings.golden_configs_dir`, la carpeta del taller), si
    hay, va primero. Un archivo de relleno ahí no gana: se sigue buscando
    donde instala la app, como si no estuviera."""
    if local_dir:
        candidato = Path(local_dir) / relative
        if candidato.is_file() and not is_placeholder(candidato):
            return candidato
    for base in _candidate_asset_dirs():
        candidate = base / relative
        if candidate.is_file():
            return candidate
    return None


# ------------------------------------------------------------ Validación --
# La marca de un archivo maestro de relleno: va en su cabecera de
# comentarios. Una configuración que escribe la propia app nunca la trae
# (USB Loader GX no escribe comentarios propios, y nincfg.bin es binario).
PLACEHOLDER_MARKER = b"PLACEHOLDER"
_PLACEHOLDER_HEAD_BYTES = 4096

# Los de relleno que no pueden llevar la marca porque son binarios,
# reconocidos por su sha256 exacto. Exacto a propósito, y no "un nincfg.bin
# casi todo en cero": cualquier otro archivo -uno de verdad que guarde el
# taller- se copia, y no hay una regla que adivinar.
_PLACEHOLDER_SHA256 = frozenset({
    # assets/configs/nintendont/nincfg.bin: 548 bytes, solo Magicbytes y
    # Version; con él, Nintendont no lee los puertos de GameCube (ver el
    # comentario del módulo).
    "dd783c995a48a4b71a4b38aeb8b4768764d6e8a5431d43426ea12873949f2940",
})


def is_placeholder(asset_path: Path) -> bool:
    """True si el archivo maestro es de relleno: lleva `PLACEHOLDER_MARKER`
    en sus primeros bytes (los de texto) o es exactamente uno de
    `_PLACEHOLDER_SHA256` (los binarios). Un archivo así es de desarrollo
    y no se copia nunca a la unidad de un cliente. False si no se puede
    leer: de eso se ocupa `_asset_is_valid`."""
    try:
        with open(asset_path, "rb") as f:
            cabeza = f.read(_PLACEHOLDER_HEAD_BYTES)
            if PLACEHOLDER_MARKER in cabeza:
                return True
            suma = hashlib.sha256(cabeza)
            for bloque in iter(lambda: f.read(64 * 1024), b""):
                suma.update(bloque)
    except OSError:
        return False
    return suma.hexdigest() in _PLACEHOLDER_SHA256


def _asset_is_valid(spec: GoldenConfigSpec, asset_path: Path) -> tuple:
    """Chequeo básico de que el archivo maestro del propio repo no esté
    vacío ni truncado, antes de copiarlo a la unidad del cliente. Devuelve
    (True, "") si pasa, o (False, motivo) si no.

    No es (ni pretende ser) una validación completa del formato -para eso
    haría falta reimplementar el parser de cada app-, es la misma clase de
    chequeo "no está claramente roto" que ya hace el resto de la app con
    sus propios archivos cacheados (ver `gametdb._is_valid_cached_cover`):
    tamaño mínimo de sanidad, y la firma binaria cuando se conoce una."""
    try:
        size = asset_path.stat().st_size
    except OSError as e:
        return False, _("no se pudo leer el archivo maestro ({error})").format(error=e)

    if size < spec.min_size_bytes:
        return False, _("el archivo maestro mide {size} bytes; se esperaban al "
                        "menos {minimum}").format(size=size,
                                                   minimum=spec.min_size_bytes)

    if spec.magic is not None:
        try:
            with asset_path.open("rb") as f:
                header = f.read(len(spec.magic))
        except OSError as e:
            return False, _("no se pudo leer el archivo maestro ({error})").format(
                error=e)
        if header != spec.magic:
            return False, _("el archivo maestro no tiene la firma esperada "
                            "({found} != {expected})").format(
                                found=header.hex(), expected=spec.magic.hex())

    return True, ""


# --------------------------------------------------------------- Copia --
def _copy_atomic(src: Path, dest: Path) -> None:
    """Copia `src` a `dest` de forma atómica (ver
    `atomicfs.atomic_write_target`, el mismo helper que usa
    `gametdb._store_cover`). Levanta
    OSError si algo falla; quien llama lo convierte en un
    `GoldenConfigResult` de error."""
    with atomic_write_target(dest, mkparents=True) as tmp:
        shutil.copyfile(src, tmp)


# -------------------------------------------------------------------- API --
def maybe_apply(app: HomebrewApp, dest_root: Path,
                install_result: "InstallResult",
                op_log: Optional[oplog.OperationLog] = None,
                registry: Optional[dict] = None,
                local_dir: Optional[str] = None) -> Optional[GoldenConfigResult]:
    """Si `app.slug` tiene una config maestra registrada, la copia a
    `dest_root` después de una instalación exitosa.

    Se llama SIEMPRE, para toda app instalada -es quien llama, no esta
    función, quien decide si el slug importa-: si `app.slug` no está en
    `registry` (por defecto `GOLDEN_CONFIGS`), devuelve None de una sin
    tocar el disco ni el log. Eso es lo que garantiza que instalar
    cualquier otra app del catálogo nunca dispare ninguna copia.

    Si `install_result` no fue exitoso, tampoco se hace nada -no hay nada
    que inyectar sobre una app que ni siquiera terminó de extraerse-, pero
    a diferencia del caso anterior sí devuelve un resultado (`
    SKIPPED_INSTALL_FAILED`) por si quien llama quiere distinguir los dos
    casos.

    Antes de copiar se valida el archivo maestro del propio repo
    (`_asset_is_valid`): si falta o parece corrupto, se aborta la
    inyección con un error claro y NO se copia nada a la unidad del
    cliente -nunca un archivo a medias o inválido-, y el intento fallido
    queda igual en el historial de operaciones para que se pueda
    diagnosticar.

    `registry` es inyectable para poder probar esta función con specs de
    prueba, sin depender de los archivos reales de `assets/configs/` ni de
    su ruta instalada. `local_dir` es la carpeta del taller donde se busca
    primero (ver `find_asset`)."""
    if registry is None:
        registry = GOLDEN_CONFIGS

    spec = registry.get(app.slug)
    if spec is None:
        return None

    if not install_result.ok:
        return GoldenConfigResult(
            GoldenConfigStatus.SKIPPED_INSTALL_FAILED, app.slug, spec.label,
            error=_("la instalación de la app no terminó bien; no se tocó nada"))

    dest_path = Path(dest_root) / spec.dest_relative
    if os.path.lexists(dest_path):
        # Ya hay una configuración en el destino -la del cliente, o la que
        # dejó una instalación anterior-: no se pisa, tampoco con
        # "Reinstalar". Antes se reemplazaba siempre, y actualizar
        # Nintendont en una SD ya configurada borraba esa configuración.
        result = GoldenConfigResult(
            GoldenConfigStatus.KEPT_EXISTING, app.slug, spec.label,
            dest_path=dest_path)
        _log(op_log, result)
        return result

    asset_path = find_asset(spec.asset_relative, local_dir)
    if asset_path is None:
        result = GoldenConfigResult(
            GoldenConfigStatus.ASSET_MISSING, app.slug, spec.label,
            error=_("no se encontró el archivo maestro ({path})").format(
                path=spec.asset_relative))
        _log(op_log, result)
        return result

    if is_placeholder(asset_path):
        # El de relleno del repositorio: a la unidad del cliente no va. La
        # app arma la suya con los valores de fábrica (ver el comentario
        # del módulo).
        result = GoldenConfigResult(
            GoldenConfigStatus.SKIPPED_PLACEHOLDER, app.slug, spec.label)
        _log(op_log, result)
        return result

    ok, reason = _asset_is_valid(spec, asset_path)
    if not ok:
        result = GoldenConfigResult(
            GoldenConfigStatus.ASSET_CORRUPT, app.slug, spec.label,
            error=_("archivo maestro inválido, no se copió nada: {reason}").format(
                reason=reason))
        _log(op_log, result)
        return result

    try:
        _copy_atomic(asset_path, dest_path)
    except OSError as e:
        result = GoldenConfigResult(
            GoldenConfigStatus.IO_ERROR, app.slug, spec.label,
            dest_path=dest_path, error=str(e))
        _log(op_log, result)
        return result

    result = GoldenConfigResult(
        GoldenConfigStatus.APPLIED, app.slug, spec.label, dest_path=dest_path)
    _log(op_log, result)
    return result


# Nombre de la entrada del historial cuando se aplica la configuración
# maestra (o falla al aplicarse).
APPLIED_OPERATION = N_("Configuración maestra aplicada")

# Nombre de la nota que queda en el historial cuando la configuración
# maestra NO se aplica porque ya había una (sin traducir: la traducción se
# aplica al mostrarlo, igual que con `OperationKind.value`).
KEPT_OPERATION = N_("Configuración maestra no aplicada")


def _log(op_log: Optional[oplog.OperationLog], result: GoldenConfigResult) -> None:
    if op_log is None:
        return
    if result.status is GoldenConfigStatus.KEPT_EXISTING:
        op_log.record(KEPT_OPERATION, result.label, oplog.STATUS_OK,
                      _("ya había un {name} en la unidad y se dejó como "
                        "estaba").format(name=result.dest_path.name))
        return
    if result.status is GoldenConfigStatus.SKIPPED_PLACEHOLDER:
        op_log.record(KEPT_OPERATION, result.label, oplog.STATUS_OK,
                      _("el archivo maestro del repositorio es de relleno: no "
                        "se copió nada y {name} arranca con su configuración "
                        "de fábrica").format(name=result.label))
        return
    status = oplog.STATUS_ERROR if result.status.is_error else oplog.STATUS_OK
    detail = result.error if result.status.is_error else str(result.dest_path)
    # Nombre de operación sin traducir (mismo criterio que
    # `OperationKind.value`, ver operations.py): la traducción se aplica
    # al mostrarlo, no al guardarlo.
    op_log.record(APPLIED_OPERATION, result.label, status, detail)
