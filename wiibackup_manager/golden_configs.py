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
- ASUNCIÓN A REVISAR, importante: USB Loader GX NO se distribuye a través
  de Open Shop Channel (ni aparece en el catálogo real de la API por
  nombre ni por slug; su propio README dice que se instala bajando el ZIP
  de sus releases de GitHub). Así que hoy, con datos reales de
  `oscwii_client`, esta rama de `GOLDEN_CONFIGS` queda registrada pero
  inalcanzable: ninguna app que devuelva la API tiene ese slug. Se deja
  lista (sin costo, y lista para el día que cambie) en vez de omitida.

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

Bonus (no es parte de este archivo, pero salió de la misma
investigación): el ZIP real de Nintendont en el catálogo de OSC trae
también una carpeta "/controllers" fuera de "apps/" -confirmado con los
`subdirectories` que reporta la propia API-, así que
`oscwii_installer._ALLOWED_TOP_LEVEL_DIRS` se amplió para permitirla; sin
eso, el ZIP de Nintendont se rechazaba entero y esta inyección nunca
llegaba a dispararse con una instalación real.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Optional

from . import oplog
from .fsutil import atomic_target, installed_data_dirs
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
    "Nintendont": GoldenConfigSpec(
        label="Nintendont",
        asset_relative="nintendont/nincfg.bin",
        dest_relative=PurePosixPath("nincfg.bin"),
        min_size_bytes=_MIN_NINCFG_BYTES,
        magic=NIN_CFG_MAGIC,
    ),
    # Slug especulativo (ver el comentario del módulo: USB Loader GX no
    # está en el catálogo real de OSC hoy). Se arma con la misma
    # convención de carpeta que usa la app real ("usbloader_gx"), no con
    # el nombre de pantalla, por si el día de mañana el catálogo lo suma
    # con ese slug.
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


def find_asset(relative: str) -> Optional[Path]:
    """Ruta del archivo maestro `relative` (p. ej. "nintendont/nincfg.bin"),
    en el primer directorio candidato donde exista, o None si no está en
    ninguno."""
    for base in _candidate_asset_dirs():
        candidate = base / relative
        if candidate.is_file():
            return candidate
    return None


# ------------------------------------------------------------ Validación --
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
        return False, f"no se pudo leer el archivo maestro ({e})"

    if size < spec.min_size_bytes:
        return False, (f"el archivo maestro mide {size} bytes; se esperaban "
                       f"al menos {spec.min_size_bytes}")

    if spec.magic is not None:
        try:
            with asset_path.open("rb") as f:
                header = f.read(len(spec.magic))
        except OSError as e:
            return False, f"no se pudo leer el archivo maestro ({e})"
        if header != spec.magic:
            return False, (f"el archivo maestro no tiene la firma esperada "
                           f"({header.hex()} != {spec.magic.hex()})")

    return True, ""


# --------------------------------------------------------------- Copia --
def _copy_atomic(src: Path, dest: Path) -> None:
    """Copia `src` a `dest` de forma atómica (ver `fsutil.atomic_target`,
    el mismo helper que usan `oscwii_installer._extract_member` y
    `gametdb._store_cover`). Levanta OSError si algo falla; quien llama lo
    convierte en un `GoldenConfigResult` de error."""
    with atomic_target(dest, mkparents=True) as tmp:
        shutil.copyfile(src, tmp)


# -------------------------------------------------------------------- API --
def maybe_apply(app: HomebrewApp, dest_root: Path,
                install_result: "InstallResult",
                op_log: Optional[oplog.OperationLog] = None,
                registry: Optional[dict] = None) -> Optional[GoldenConfigResult]:
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
    su ruta instalada."""
    if registry is None:
        registry = GOLDEN_CONFIGS

    spec = registry.get(app.slug)
    if spec is None:
        return None

    if not install_result.ok:
        return GoldenConfigResult(
            GoldenConfigStatus.SKIPPED_INSTALL_FAILED, app.slug, spec.label,
            error="la instalación de la app no terminó bien; no se tocó nada")

    asset_path = find_asset(spec.asset_relative)
    if asset_path is None:
        result = GoldenConfigResult(
            GoldenConfigStatus.ASSET_MISSING, app.slug, spec.label,
            error=f"no se encontró el archivo maestro ({spec.asset_relative})")
        _log(op_log, result)
        return result

    ok, reason = _asset_is_valid(spec, asset_path)
    if not ok:
        result = GoldenConfigResult(
            GoldenConfigStatus.ASSET_CORRUPT, app.slug, spec.label,
            error=f"archivo maestro inválido, no se copió nada: {reason}")
        _log(op_log, result)
        return result

    dest_path = Path(dest_root) / spec.dest_relative
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


def _log(op_log: Optional[oplog.OperationLog], result: GoldenConfigResult) -> None:
    if op_log is None:
        return
    status = oplog.STATUS_ERROR if result.status.is_error else oplog.STATUS_OK
    detail = result.error if result.status.is_error else str(result.dest_path)
    # Nombre de operación sin traducir (mismo criterio que
    # `OperationKind.value`, ver operations.py): la traducción se aplica
    # al mostrarlo, no al guardarlo.
    op_log.record("Configuración maestra aplicada", result.label, status, detail)
