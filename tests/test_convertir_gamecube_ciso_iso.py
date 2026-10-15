"""Convertir un juego de GameCube entre CISO e ISO.

Nintendont lee los dos formatos; convertir sirve para pasar de uno al otro.
Medido con el `wit` real (v3.05a):

- CISO → ISO sin `--trunc` da un ISO de 4 699 979 776 bytes (el tamaño de
  un DVD de Wii, todo relleno), que no entra en FAT32. Con `--trunc` sale
  del tamaño del disco de GameCube: 1 459 978 240 bytes en Ocarina of Time
  / Master Quest, 1 459 978 220 en Twilight Princess (recorta hasta el
  último dato, no a un tamaño fijo).
- La ida y vuelta ISO → CISO → ISO da el mismo sha1.
- `wit CMP` da 0 si dos imágenes tienen el mismo contenido y 2 si no
  (detecta un solo byte cambiado en los datos del juego).

Se convierte con la misma protección que la conversión de Wii (destino
existente apartado y devuelto si algo falla, lo nuevo bajado a disco antes
de descartar lo viejo), y además, antes de darla por buena, el resultado
tiene que tener el mismo ID6, ser de GameCube, coincidir en contenido con
el original (`wit CMP`) y, si es ISO, no pasar de 1 459 978 240 bytes.
La conversión a WBFS sigue rechazada.
"""
from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import types
from pathlib import Path

import pytest

from wiibackup_manager import (library_ops, oplog, transfer_plan, wit_wrapper)
from wiibackup_manager.disc_header import DiscInfo
from wiibackup_manager.game_model import Game
from wiibackup_manager.operations import OperationKind, OperationManager

ORIGEN = b"el CISO original"
GC = 1_459_978_240      # un disco de GameCube entero


# ------------------------------------------------------------- Arnés --
class _Hilo:
    def __init__(self, target=None, daemon=None, **_kw):
        self._target = target

    def start(self):
        self._target()


def _juego(biblio: Path, nombre: str, game_id="D43E01", console="gc", datos=ORIGEN):
    p = biblio / nombre
    p.write_bytes(datos)
    return Game(path=p, game_id=game_id, title=p.stem, fmt=p.suffix.lstrip(".").upper(),
                size_bytes=len(datos), identified_by="wit", console=console)


@pytest.fixture
def biblio(tmp_path):
    d = tmp_path / "biblioteca"
    d.mkdir()
    return d


@pytest.fixture
def wit_gc(monkeypatch):
    """El `wit` de mentira. `estado` decide qué contesta: con qué ID se
    identifica lo convertido, si `wit CMP` dice que coincide, si la
    conversión se cancela, cuánto pesa lo que escribe."""
    estado = types.SimpleNamespace(llamadas=[], ids={}, consolas={}, coincide=True,
                                   cancelar=False, tamano=None, libres=10 ** 12)

    def convert(src, dest, fmt, binary, **kw):
        dest = Path(dest)
        estado.llamadas.append((Path(src).name, dest.name, kw))
        tmp = dest.parent / f".{dest.name}.AbCdEf.tmp"
        tmp.write_bytes(b"a medias")
        if estado.cancelar:
            raise wit_wrapper.OperationCancelled("cancelado")
        tmp.replace(dest)
        if estado.tamano is not None:
            with open(dest, "r+b") as f:
                f.truncate(estado.tamano)
        else:
            dest.write_bytes(b"convertido")
        return subprocess.CompletedProcess([], 0, "", "")

    def identify(path, binary="wit"):
        gid = estado.ids.get(Path(path).stem, "D43E01")
        return DiscInfo(gid, "Zelda", "wit", estado.consolas.get(Path(path).name, "gc"))

    monkeypatch.setattr(wit_wrapper, "convert", convert)
    monkeypatch.setattr(wit_wrapper, "identify", identify)
    monkeypatch.setattr(wit_wrapper, "images_match",
                        lambda a, b, binary="wit", cancel=None: estado.coincide)
    monkeypatch.setattr(wit_wrapper, "is_available", lambda _b="wit": True)
    monkeypatch.setattr(transfer_plan, "free_space", lambda _p: estado.libres)
    return estado


class _Ventana:
    def __init__(self, tmp_path):
        from wiibackup_manager.window import WiiBackupWindow
        self.op_log = oplog.OperationLog(tmp_path / "historial.json")
        self.ops = OperationManager(log=self.op_log)
        self.settings = types.SimpleNamespace(wit_binary="wit")
        self.progress_bar = types.SimpleNamespace(set_fraction=lambda _f: None,
                                                  set_text=lambda _t: None)
        self.toasts = []
        self.resultado_lote = None
        for nombre in ("_on_convert_requested", "_start_convert", "_on_batch_convert"):
            setattr(self, nombre, types.MethodType(getattr(WiiBackupWindow, nombre), self))
        self._describe_target = WiiBackupWindow._describe_target

    def _begin_cancellable_progress(self, *_a):
        return wit_wrapper.CancellationToken()

    def _reject_if_busy(self, *_a, **_k):
        return False

    def _hide_progress(self):
        pass

    def _show_toast(self, mensaje):
        self.toasts.append(mensaje)

    def rescan_library(self):
        pass

    def _on_batch_done(self, title, ok, errors, skipped, op, target, cancelled):
        self.resultado_lote = types.SimpleNamespace(ok=ok, errors=errors, skipped=skipped,
                                                    cancelled=cancelled)
        self.ops.finish(op)


@pytest.fixture
def ventana(tmp_path, monkeypatch):
    from wiibackup_manager import window
    monkeypatch.setattr(window.threading, "Thread", _Hilo)
    monkeypatch.setattr(window.GLib, "idle_add", lambda f, *a: f(*a) and False)
    monkeypatch.setattr(window.gtk_helpers, "confirm_overwrite",
                        lambda parent, body, accion: accion())
    return _Ventana(tmp_path)


def _convertir(ventana, juego):
    ventana._on_convert_requested(types.SimpleNamespace(game=juego))


def _ocultos(d: Path) -> list:
    return sorted(p.name for p in d.iterdir() if p.name.startswith("."))


def _conversion_en_historial(ventana):
    [e] = [e for e in ventana.op_log.entries()
           if e.operation == OperationKind.CONVERTING.value]
    return e


# ------------------------------------------------------- Desde la fila --
def test_ciso_a_iso_usa_trunc_y_deja_el_original(ventana, biblio, wit_gc):
    juego = _juego(biblio, "Zelda.ciso")

    _convertir(ventana, juego)

    [(origen, destino, kw)] = wit_gc.llamadas
    assert (origen, destino) == ("Zelda.ciso", "Zelda.iso")
    assert kw.get("trunc") is True
    assert kw.get("scrub_update") is False
    # El lugar se mira antes (en FAT32 la reserva de `wit` escribe ceros).
    assert kw.get("prealloc") is False
    assert (biblio / "Zelda.iso").read_bytes() == b"convertido"
    assert (biblio / "Zelda.ciso").read_bytes() == ORIGEN
    assert _ocultos(biblio) == []
    assert ventana.toasts[-1] == "Convertido a Zelda.iso"
    assert _conversion_en_historial(ventana).status == oplog.STATUS_OK


def test_iso_a_ciso_sin_trunc(ventana, biblio, wit_gc):
    juego = _juego(biblio, "Zelda.iso")

    _convertir(ventana, juego)

    [(_o, destino, kw)] = wit_gc.llamadas
    assert destino == "Zelda.ciso"
    assert not kw.get("trunc")
    assert (biblio / "Zelda.ciso").read_bytes() == b"convertido"
    assert (biblio / "Zelda.iso").read_bytes() == ORIGEN


@pytest.mark.parametrize("problema", ["otro-id", "no-es-gamecube", "contenido-distinto",
                                      "iso-de-mas"])
def test_si_el_resultado_no_es_correcto_se_descarta(ventana, biblio, wit_gc, problema):
    if problema == "otro-id":
        wit_gc.ids["Zelda"] = "GZ2E01"
    elif problema == "no-es-gamecube":
        wit_gc.consolas["Zelda.iso"] = "wii"
    elif problema == "contenido-distinto":
        wit_gc.coincide = False
    else:
        wit_gc.tamano = GC + 1          # el ISO de relleno que no entra en FAT32
    juego = _juego(biblio, "Zelda.ciso")

    _convertir(ventana, juego)

    assert sorted(p.name for p in biblio.iterdir()) == ["Zelda.ciso"]
    assert (biblio / "Zelda.ciso").read_bytes() == ORIGEN
    # El mensaje entero es de `convert_gamecube` (traducido), sin el
    # "Error al convertir:" de un error de `wit`.
    assert ventana.toasts[-1].startswith("Zelda.iso no quedó bien (")
    assert _conversion_en_historial(ventana).status == oplog.STATUS_ERROR


def test_cancelar_no_deja_nada_y_no_toca_el_original(ventana, biblio, wit_gc):
    wit_gc.cancelar = True
    juego = _juego(biblio, "Zelda.ciso")

    _convertir(ventana, juego)

    assert sorted(p.name for p in biblio.iterdir()) == ["Zelda.ciso"]
    assert (biblio / "Zelda.ciso").read_bytes() == ORIGEN
    assert _conversion_en_historial(ventana).status == oplog.STATUS_CANCELLED


def test_sin_espacio_no_arranca(ventana, biblio, wit_gc):
    wit_gc.libres = 100 * 1024 * 1024    # menos que un disco de GameCube
    juego = _juego(biblio, "Zelda.ciso")

    _convertir(ventana, juego)

    assert wit_gc.llamadas == []
    assert sorted(p.name for p in biblio.iterdir()) == ["Zelda.ciso"]
    assert ventana.toasts[-1].startswith("No entra en el destino: necesita ")


def test_un_iso_que_ya_estaba_vuelve_si_la_conversion_falla(ventana, biblio, wit_gc):
    (biblio / "Zelda.iso").write_bytes(b"el ISO de antes")
    wit_gc.coincide = False
    juego = _juego(biblio, "Zelda.ciso")

    _convertir(ventana, juego)

    assert (biblio / "Zelda.iso").read_bytes() == b"el ISO de antes"
    assert (biblio / "Zelda.ciso").read_bytes() == ORIGEN
    assert _ocultos(biblio) == []


def test_un_iso_que_ya_estaba_se_reemplaza_si_sale_bien(ventana, biblio, wit_gc):
    (biblio / "Zelda.iso").write_bytes(b"el ISO de antes")
    juego = _juego(biblio, "Zelda.ciso")

    _convertir(ventana, juego)

    assert (biblio / "Zelda.iso").read_bytes() == b"convertido"
    assert _ocultos(biblio) == []


def test_no_toca_las_partes_de_un_wbfs_que_se_llama_igual(ventana, biblio, wit_gc):
    """`Zelda.wbf1` es la segunda parte de OTRO juego (un WBFS de Wii
    dividido): convertir `Zelda.ciso` a `Zelda.iso` no la aparta ni la
    borra."""
    (biblio / "Zelda.wbfs").write_bytes(b"wii parte 1")
    (biblio / "Zelda.wbf1").write_bytes(b"wii parte 2")
    juego = _juego(biblio, "Zelda.ciso")

    _convertir(ventana, juego)

    assert (biblio / "Zelda.iso").read_bytes() == b"convertido"
    assert (biblio / "Zelda.wbfs").read_bytes() == b"wii parte 1"
    assert (biblio / "Zelda.wbf1").read_bytes() == b"wii parte 2"
    assert _ocultos(biblio) == []


def test_si_no_se_puede_bajar_a_la_unidad_vuelve_lo_que_habia(ventana, biblio, wit_gc,
                                                              monkeypatch):
    """Lo nuevo se baja a la unidad ANTES de descartar lo viejo: si eso
    falla, el ISO que ya estaba vuelve a su lugar."""
    from wiibackup_manager import fileops

    def _falla(*_a, **_k):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(fileops, "flush_and_drop_cache", _falla)
    (biblio / "Zelda.iso").write_bytes(b"el ISO de antes")
    juego = _juego(biblio, "Zelda.ciso")

    _convertir(ventana, juego)

    assert (biblio / "Zelda.iso").read_bytes() == b"el ISO de antes"
    assert (biblio / "Zelda.ciso").read_bytes() == ORIGEN
    assert _ocultos(biblio) == []
    assert _conversion_en_historial(ventana).status == oplog.STATUS_ERROR


def test_wii_sigue_igual(ventana, biblio, wit_gc):
    """La conversión de Wii no pasa por nada de esto: mismas opciones de
    `wit` que antes (ni `trunc` ni `scrub_update`) y sin `wit CMP`."""
    wit_gc.coincide = False             # si se usara, la haría fallar
    juego = _juego(biblio, "Brawl.iso", game_id="RSBE01", console="wii")

    _convertir(ventana, juego)

    [(_o, destino, kw)] = wit_gc.llamadas
    assert destino == "Brawl.wbfs"
    assert "trunc" not in kw and "scrub_update" not in kw
    assert (biblio / "Brawl.wbfs").read_bytes() == b"convertido"


# --------------------------------------------------------------- Lote --
def test_en_lote_convierte_gamecube_entre_ciso_e_iso_y_wii_a_wbfs(ventana, biblio, wit_gc):
    zelda = _juego(biblio, "Zelda.ciso")
    tp = _juego(biblio, "TP.iso", game_id="GZ2E01")
    wit_gc.ids["TP"] = "GZ2E01"
    brawl = _juego(biblio, "Brawl.iso", game_id="RSBE01", console="wii")
    viejo = _juego(biblio, "Viejo.wbfs")          # un WBFS de GameCube: se saltea
    ventana._selected_games = lambda: [zelda, tp, brawl, viejo]

    ventana._on_batch_convert()

    hechos = {(o, d): kw for o, d, kw in wit_gc.llamadas}
    assert set(hechos) == {("Zelda.ciso", "Zelda.iso"), ("TP.iso", "TP.ciso"),
                           ("Brawl.iso", "Brawl.wbfs")}
    assert hechos[("Zelda.ciso", "Zelda.iso")].get("trunc") is True
    assert not hechos[("TP.iso", "TP.ciso")].get("trunc")
    assert "trunc" not in hechos[("Brawl.iso", "Brawl.wbfs")]
    assert ventana.resultado_lote.ok == 3
    assert any("Viejo" in s for s in ventana.resultado_lote.skipped)
    assert _ocultos(biblio) == []


def test_en_lote_un_resultado_incorrecto_se_descarta_y_sigue(ventana, biblio, wit_gc):
    zelda = _juego(biblio, "Zelda.ciso")
    brawl = _juego(biblio, "Brawl.iso", game_id="RSBE01", console="wii")
    wit_gc.ids["Zelda"] = "GZ2E01"
    ventana._selected_games = lambda: [zelda, brawl]

    ventana._on_batch_convert()

    assert sorted(p.name for p in biblio.iterdir()) == ["Brawl.iso", "Brawl.wbfs",
                                                        "Zelda.ciso"]
    assert ventana.resultado_lote.ok == 1
    assert any("Zelda" in e for e in ventana.resultado_lote.errors)


# ------------------------------------- Tamaño (barra y chequeo de espacio) --
def test_lo_que_se_espera_que_pese(biblio):
    """Es una cota: el ISO sale recortado y no pasa del disco entero; el
    CISO puede pesar un poco más que el ISO de origen (la cabecera de
    32 KiB y el último bloque de 2 MiB guardado entero)."""
    zelda = _juego(biblio, "Zelda.ciso")
    tp = _juego(biblio, "TP.iso", datos=b"x" * 1000)
    assert transfer_plan.estimate_output_size(zelda, ".iso") == GC
    cota_ciso = 1000 + 0x8000 + 2 * 1024 * 1024
    assert cota_ciso <= transfer_plan.estimate_output_size(tp, ".ciso") < cota_ciso + 8 * 1024 * 1024


# ---------------------------------------------------------- wit de verdad --
def _imagen_gc(path: Path, game_id=b"GTST01", nombre=b"PRUEBA SINTETICA",
               relleno=b"\xab", fst_corta=False):
    """Un disco de GameCube mínimo que `wit` reconoce: cabecera, apploader,
    un DOL chico y una FST con un archivo de 4 KiB después de ella, así
    `--trunc` corta al final de ese archivo (0x31000). Disperso: ocupa unos
    KB.

    `fst_corta=True` arma una FST que declara 0x20 bytes y trae solo la
    raíz (12). Medido: `wit COPY --trunc` corta en el byte 12 de la FST y
    después el propio `wit` no puede leer ese ISO ("Can't read FST")."""
    if fst_corta:
        fst = struct.pack(">III", 0x01000000, 0, 1)
        fst_declarada = 0x20
    else:
        fst = (struct.pack(">III", 0x01000000, 0, 2)
               + struct.pack(">III", 0, 0x30000, 0x1000) + b"a.bin\0")
        fst_declarada = len(fst)
    with open(path, "wb") as f:
        f.truncate(GC)
        cab = bytearray(0x440)
        cab[0:6] = game_id
        cab[0x1C:0x20] = struct.pack(">I", 0xC2339F3D)
        cab[0x20:0x20 + len(nombre)] = nombre
        struct.pack_into(">IIII", cab, 0x420, 0x10000, 0x20000, fst_declarada, fst_declarada)
        f.seek(0)
        f.write(cab)
        f.seek(0x2440)
        f.write(b"2004/01/01\0\0\0\0\0\0" + struct.pack(">III", 0x81200000, 0x100, 0))
        f.write(b"\x60\x00\x00\x00" * 0x40)
        dol = bytearray(0x100)
        struct.pack_into(">I", dol, 0x00, 0x100)
        struct.pack_into(">I", dol, 0x48, 0x80003100)
        struct.pack_into(">I", dol, 0x90, 0x20)
        struct.pack_into(">I", dol, 0xE0, 0x80003100)
        f.seek(0x10000)
        f.write(bytes(dol) + b"\x4e\x80\x00\x20" * 8)
        f.seek(0x20000)
        f.write(fst)
        if not fst_corta:
            f.seek(0x30000)
            f.write(relleno * 0x1000)


def _sha1(p: Path) -> str:
    h = hashlib.sha1()
    with open(p, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 22), b""):
            h.update(bloque)
    return h.hexdigest()


@pytest.mark.skipif(shutil.which("wit") is None, reason="wit no está instalado")
def test_ida_y_vuelta_con_wit_real(tmp_path):
    """ISO → CISO → ISO con el `wit` real y la validación real (`wit LIST`
    y `wit CMP`). El ISO de vuelta es idéntico al que da `wit COPY --trunc`
    sobre el original, y es el original byte a byte hasta su último dato
    (después, en `_imagen_gc`, solo hay relleno de ceros)."""
    origen = tmp_path / "origen"
    origen.mkdir()
    original = origen / "Prueba.iso"
    _imagen_gc(original)
    referencia = tmp_path / "ref" / "Prueba.iso"
    referencia.parent.mkdir()
    subprocess.run(["wit", "COPY", "--trunc", str(original), "--dest", str(referencia)],
                   check=True, capture_output=True)
    juego_iso = Game(path=original, game_id="GTST01", title="Prueba", fmt="ISO",
                     size_bytes=original.stat().st_size, identified_by="iso",
                     console="gc")

    ciso = tmp_path / "Prueba.ciso"
    library_ops.convert_gamecube(juego_iso, ciso, ".ciso", "wit")
    juego_ciso = Game(path=ciso, game_id="GTST01", title="Prueba", fmt="CISO",
                      size_bytes=ciso.stat().st_size, identified_by="wit", console="gc")
    vuelta = tmp_path / "Prueba.iso"
    library_ops.convert_gamecube(juego_ciso, vuelta, ".iso", "wit")

    assert _sha1(vuelta) == _sha1(referencia)
    assert vuelta.stat().st_size == 0x31000          # recortado tras el último dato
    with open(original, "rb") as f:
        assert hashlib.sha1(f.read(0x31000)).hexdigest() == _sha1(vuelta)
    assert not [p.name for p in tmp_path.rglob(".*")]


@pytest.mark.skipif(shutil.which("wit") is None, reason="wit no está instalado")
def test_wit_cmp_real_distingue_otro_disco(tmp_path):
    iso, ciso = tmp_path / "a.iso", tmp_path / "a.ciso"
    otra, otro_dato = tmp_path / "otra.iso", tmp_path / "otro-dato.iso"
    _imagen_gc(iso)
    _imagen_gc(otra, nombre=b"OTRA COSA")
    _imagen_gc(otro_dato, relleno=b"\xac")
    subprocess.run(["wit", "COPY", str(iso), "--dest", str(ciso)],
                   check=True, capture_output=True)

    assert wit_wrapper.images_match(iso, ciso) is True
    assert wit_wrapper.images_match(otra, ciso) is False
    assert wit_wrapper.images_match(otro_dato, ciso) is False


@pytest.mark.skipif(shutil.which("wit") is None, reason="wit no está instalado")
def test_con_wit_real_un_iso_recortado_ilegible_se_descarta(tmp_path):
    """Lo que la validación está para atrapar: `wit` termina bien pero el
    ISO recortado no sirve (acá, el propio `wit` no puede leerlo). Se
    descarta y el CISO queda como estaba."""
    origen = tmp_path / "origen"
    origen.mkdir()
    _imagen_gc(origen / "Prueba.iso", fst_corta=True)
    ciso = tmp_path / "Prueba.ciso"
    subprocess.run(["wit", "COPY", str(origen / "Prueba.iso"), "--dest", str(ciso)],
                   check=True, capture_output=True)
    antes = _sha1(ciso)
    juego = Game(path=ciso, game_id="GTST01", title="Prueba", fmt="CISO",
                 size_bytes=ciso.stat().st_size, identified_by="wit", console="gc")

    with pytest.raises(library_ops.GameCubeConversionError, match="no quedó bien"):
        library_ops.convert_gamecube(juego, tmp_path / "Prueba.iso", ".iso", "wit")

    assert sorted(p.name for p in tmp_path.iterdir()) == ["Prueba.ciso", "origen"]
    assert _sha1(ciso) == antes


@pytest.mark.skipif(shutil.which("wit") is None, reason="wit no está instalado")
def test_con_wit_real_un_resultado_de_otro_juego_se_descarta(tmp_path):
    """La validación de verdad: el juego dice ser GXXX01 y el disco es
    GTST01. No queda nada nuevo, ni oculto."""
    original = tmp_path / "Prueba.iso"
    _imagen_gc(original)
    juego = Game(path=original, game_id="GXXX01", title="Prueba", fmt="ISO",
                 size_bytes=original.stat().st_size, identified_by="iso", console="gc")

    with pytest.raises(library_ops.GameCubeConversionError, match="GTST01"):
        library_ops.convert_gamecube(juego, tmp_path / "Prueba.ciso", ".ciso", "wit")

    assert sorted(p.name for p in tmp_path.iterdir()) == ["Prueba.iso"]


# ------------------------------------------- Texto de la barra al comparar --
# Mientras corre `wit CMP` (lee los dos discos enteros) la barra quedaba en
# 97 % sin decir nada. Ahora dice "Comprobando…" y vuelve al porcentaje al
# terminar, con éxito o no.
def _textos_al_comparar(monkeypatch, ventana):
    textos, vistos = [], []
    ventana.progress_bar = types.SimpleNamespace(set_fraction=lambda _f: None,
                                                 set_text=textos.append)
    real = wit_wrapper.images_match

    def _cmp(a, b, binary="wit", cancel=None):
        vistos.append(list(textos))
        return real(a, b, binary, cancel)

    monkeypatch.setattr(wit_wrapper, "images_match", _cmp)
    return textos, vistos


def test_la_barra_dice_comprobando_mientras_compara(ventana, biblio, wit_gc, monkeypatch):
    textos, vistos = _textos_al_comparar(monkeypatch, ventana)

    _convertir(ventana, _juego(biblio, "Zelda.ciso"))

    assert vistos == [["Comprobando…"]]
    assert textos[-1] is None


def test_si_la_comparacion_falla_la_barra_tambien_vuelve(ventana, biblio, wit_gc,
                                                        monkeypatch):
    wit_gc.coincide = False
    textos, vistos = _textos_al_comparar(monkeypatch, ventana)

    _convertir(ventana, _juego(biblio, "Zelda.ciso"))

    assert vistos == [["Comprobando…"]] and textos[-1] is None


def test_en_lote_la_barra_dice_comprobando_en_cada_juego(ventana, biblio, wit_gc,
                                                        monkeypatch):
    zelda = _juego(biblio, "Zelda.ciso")
    tp = _juego(biblio, "TP.iso", game_id="GZ2E01")
    wit_gc.ids["TP"] = "GZ2E01"
    ventana._selected_games = lambda: [zelda, tp]
    textos, vistos = _textos_al_comparar(monkeypatch, ventana)

    ventana._on_batch_convert()

    assert [v[-1] for v in vistos] == ["Comprobando…", "Comprobando…"]
    assert textos[-1] is None
