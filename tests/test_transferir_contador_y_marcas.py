"""Transferir: cuánto ocupa lo elegido y qué juegos ya están en la unidad.

Antes, para saber si lo elegido entraba había que sumar a mano el tamaño
de cada fila y compararlo con el espacio libre del destino; y para saber
si un juego ya estaba en la unidad, había que transferirlo y ver que la
cola lo marcaba "Ya estaba en el destino".

Ahora la pestaña lo dice mientras se tilda:

- "N seleccionados · X GB (quedarán Y GB libres)", en amarillo si entra
  justo y en rojo si no entra (`transfer_plan.estimate_selection`). Es la
  suma del tamaño de los archivos, sin `wit`: un juego que en FAT32 se
  divide en partes de 4 GB suma lo mismo, y los que ya están en la unidad
  no cuentan (la cola los saltea).
- "Ya en la unidad" en cada fila cuyo lugar en el destino ya está ocupado,
  con la misma pregunta que se hace antes de copiar
  (`transfer_plan.existing_destination`: en GameCube cuenta la otra
  extensión y la carpeta del juego por ID6). Se calcula en un hilo aparte,
  y una unidad que no se puede leer saca las marcas en vez de romper.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from wiibackup_manager import config, transfer_plan
from wiibackup_manager.game_model import Game

GB = 1024 ** 3


def _juego(nombre: str, size: int = GB, game_id: str = "RMCE01", console: str = "wii",
           titulo: str = "Juego", carpeta: Path = Path("/biblioteca")) -> Game:
    path = carpeta / nombre
    return Game(path=path, game_id=game_id, title=titulo,
                fmt=path.suffix.lstrip(".").upper(), size_bytes=size,
                identified_by="iso", console=console)


# ---------------------------------------------------------- La cuenta --
def test_suma_lo_elegido_y_dice_cuanto_queda():
    juegos = [_juego("a.iso", 2 * GB), _juego("b.wbfs", 3 * GB, "RSBE01")]

    e = transfer_plan.estimate_selection(juegos, free=10 * GB)

    assert (e.count, e.already, e.needed) == (2, 0, 5 * GB)
    assert e.remaining == 5 * GB
    assert e.level == "ok"


def test_si_no_entra_avisa_en_rojo_y_dice_cuanto_falta():
    juegos = [_juego("a.iso", 3 * GB), _juego("b.iso", 2 * GB, "RSBE01")]

    e = transfer_plan.estimate_selection(juegos, free=4 * GB)

    assert e.level == "full"
    assert e.remaining == -GB


@pytest.mark.parametrize("libres", [5 * GB, 5 * GB + 100 * 1024 ** 2],
                         ids=["exacto", "con-100-MB-de-sobra"])
def test_si_entra_justo_avisa_en_amarillo(libres):
    """Entra, pero con menos margen que lo que puede variar el tamaño real
    en la unidad (redondeo de bloques del WBFS): amarillo, no rojo."""
    e = transfer_plan.estimate_selection([_juego("a.wbfs", 5 * GB)], free=libres)

    assert e.level == "warn"
    assert e.remaining >= 0


def test_un_byte_de_mas_ya_no_entra():
    e = transfer_plan.estimate_selection([_juego("a.wbfs", 5 * GB)], free=5 * GB - 1)
    assert e.level == "full"


def test_con_mucho_lugar_no_hay_aviso():
    e = transfer_plan.estimate_selection([_juego("a.wbfs", 5 * GB)], free=6 * GB)
    assert e.level == "ok"


def test_sin_nada_elegido_no_hay_nada_que_avisar():
    e = transfer_plan.estimate_selection([], free=GB)

    assert (e.count, e.already, e.needed) == (0, 0, 0)
    assert e.level == "ok" and e.remaining == GB


def test_sin_destino_no_hay_aviso_ni_cuenta_de_lo_que_queda():
    e = transfer_plan.estimate_selection([_juego("a.iso", 3 * GB)], free=None)

    assert e.needed == 3 * GB
    assert e.level == "unknown" and e.remaining is None


def test_los_que_ya_estan_en_la_unidad_no_suman():
    ya = _juego("ya.iso", 4 * GB)
    nuevo = _juego("nuevo.iso", 2 * GB, "RSBE01")

    e = transfer_plan.estimate_selection([ya, nuevo], free=3 * GB, on_drive={ya.path})

    assert (e.count, e.already, e.needed) == (2, 1, 2 * GB)
    assert e.level == "ok"


def test_un_juego_que_en_fat32_se_divide_cuenta_su_tamano_y_nada_mas():
    """Más de 4 GB en una unidad FAT32 va en partes (`.wbfs` + `.wbf1`):
    son los mismos bytes, así que el total no cambia ni se rechaza."""
    e = transfer_plan.estimate_selection([_juego("grande.wbfs", 7 * GB)], free=8 * GB)

    assert e.needed == 7 * GB and e.level == "ok"


# ----------------------------------------------- Qué ya está en la unidad --
def _unidad(tmp_path: Path) -> Path:
    """Una unidad con un juego de Wii (RMCE01) y uno de GameCube (GALE01)
    guardado como CISO en una carpeta con otro título."""
    raiz = tmp_path / "usb"
    (raiz / "wbfs" / "RMCE01").mkdir(parents=True)
    (raiz / "wbfs" / "RMCE01" / "RMCE01.wbfs").write_bytes(b"w")
    gc = raiz / "games" / "Super Smash Bros Melee [GALE01]"
    gc.mkdir(parents=True)
    (gc / "game.ciso").write_bytes(b"c")
    return raiz


def test_marca_el_wii_y_el_gamecube_con_otra_extension_y_otro_titulo(tmp_path):
    raiz = _unidad(tmp_path)
    wii = _juego("Mario Kart Wii [RMCE01].iso")
    gc = _juego("Melee [GALE01].iso", game_id="GALE01", console="gc",
                titulo="SUPER SMASH BROS. MELEE")
    otro = _juego("Wii Sports [RSPE01].wbfs", game_id="RSPE01")

    presentes = transfer_plan.games_on_drive([wii, gc, otro], raiz)

    assert presentes == {wii.path, gc.path}


def test_un_id_invalido_no_se_marca_ni_rompe(tmp_path):
    raiz = _unidad(tmp_path)
    raro = _juego("Sin identificar.iso", game_id="??????")
    wii = _juego("Mario Kart Wii [RMCE01].iso")

    assert transfer_plan.games_on_drive([raro, wii], raiz) == {wii.path}


def test_un_juego_que_no_se_puede_mirar_no_se_marca(tmp_path, monkeypatch):
    raiz = _unidad(tmp_path)
    wii = _juego("Mario Kart Wii [RMCE01].iso")
    gc = _juego("Melee [GALE01].iso", game_id="GALE01", console="gc")
    real = transfer_plan.existing_destination

    def _falla_con_wii(game, root):
        if game.console == "wii":
            raise OSError(5, "Input/output error")
        return real(game, root)

    monkeypatch.setattr(transfer_plan, "existing_destination", _falla_con_wii)

    assert transfer_plan.games_on_drive([wii, gc], raiz) == {gc.path}


def test_una_unidad_que_no_esta_da_none_sin_mirar_juego_por_juego(tmp_path, monkeypatch):
    """Una unidad que se fue puede tardar en contestar cada lectura (un
    pendrive que falla): ni se empieza."""
    mirados = []
    monkeypatch.setattr(transfer_plan, "existing_destination",
                        lambda game, root: mirados.append(game))

    assert transfer_plan.games_on_drive([_juego("a.iso")], tmp_path / "no-esta") is None
    assert mirados == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root lee aunque no tenga permiso")
def test_una_unidad_que_no_se_puede_leer_da_none(tmp_path):
    raiz = _unidad(tmp_path)
    raiz.chmod(0)
    try:
        resultado = transfer_plan.games_on_drive([_juego("a.iso")], raiz)
    finally:
        raiz.chmod(0o755)

    assert resultado is None


def test_si_la_unidad_se_va_mientras_se_mira_da_none(tmp_path, monkeypatch):
    raiz = _unidad(tmp_path)
    real = transfer_plan.existing_destination

    def _se_va(game, root):
        resultado = real(game, root)
        shutil.rmtree(raiz)
        return resultado

    monkeypatch.setattr(transfer_plan, "existing_destination", _se_va)

    assert transfer_plan.games_on_drive([_juego("a.iso")], raiz) is None


def test_no_le_pregunta_nada_a_wit(tmp_path, monkeypatch):
    from wiibackup_manager import wit_wrapper

    def _no(*_a, **_k):
        raise AssertionError("no tenía que llamar a wit")

    for nombre in ("iso_size_bytes", "identify", "_run_cancellable"):
        monkeypatch.setattr(wit_wrapper, nombre, _no)
    raiz = _unidad(tmp_path)
    juegos = [_juego("Mario Kart Wii [RMCE01].wbfs"),
              _juego("Melee [GALE01].ciso", game_id="GALE01", console="gc")]

    assert transfer_plan.games_on_drive(juegos, raiz) == {j.path for j in juegos}
    transfer_plan.estimate_selection(juegos, free=GB)


# ------------------------------------------------------------- Con GTK --
@pytest.fixture(scope="module")
def gtk():
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        if os.environ.get("WBM_REQUIRE_GUI") == "1":
            pytest.fail("WBM_REQUIRE_GUI=1 pero no hay display")
        pytest.skip("sin display")
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    if not Gtk.init_check():
        pytest.skip("GTK no pudo inicializarse")
    Adw.init()
    return Gtk


def _esperar(condicion, segundos: float = 5.0) -> bool:
    """Procesa los avisos de GTK (los `GLib.idle_add` del hilo de fondo)
    hasta que `condicion()` se cumpla o se acabe el tiempo."""
    from gi.repository import GLib
    contexto = GLib.MainContext.default()
    limite = time.monotonic() + segundos
    while True:
        while contexto.iteration(False):
            pass
        if condicion() or time.monotonic() > limite:
            return condicion()
        time.sleep(0.01)


@pytest.fixture
def vista(gtk, monkeypatch, tmp_path):
    from wiibackup_manager import drives, gametdb
    from wiibackup_manager.widgets import transfer_view

    monkeypatch.setattr(drives, "list_removable_drives", lambda: [])
    monkeypatch.setattr(gametdb, "fetch_cover_async", lambda *a, **k: None)
    toasts = []
    v = transfer_view.TransferView(config.Settings(library_path=str(tmp_path)),
                                   toasts.append)
    ventana = gtk.Window()
    ventana.set_child(v)
    v.toasts = toasts
    yield v
    v.shutdown()
    ventana.destroy()


def _libres(monkeypatch, por_ruta: dict):
    """`shutil.disk_usage` con un espacio libre fijo por destino."""
    def _uso(ruta):
        libres = por_ruta.get(Path(ruta))
        if libres is None:
            raise FileNotFoundError(2, "No such file or directory", str(ruta))
        return SimpleNamespace(total=100 * GB, used=100 * GB - libres, free=libres)

    monkeypatch.setattr(shutil, "disk_usage", _uso)


def _elegir_destino(v, carpeta: Path):
    v._on_folder_picked(SimpleNamespace(
        select_folder_finish=lambda _r: SimpleNamespace(get_path=lambda: str(carpeta))),
        None)
    assert v._dest_path == carpeta


def _marcas(v) -> dict:
    return {fila.game.title: fila.on_drive_label.get_visible() for fila in v._game_rows}


def _marcas_listas(v) -> bool:
    return not v._marks_busy and v._marks_root == v._dest_path


def _juegos_de_prueba(tmp_path) -> list:
    biblioteca = tmp_path / "biblioteca"
    return [
        _juego("Mario Kart Wii [RMCE01].iso", 3 * GB, titulo="Mario Kart Wii",
               carpeta=biblioteca),
        _juego("Melee [GALE01].iso", GB, "GALE01", "gc", "Melee", biblioteca),
        _juego("Wii Sports [RSPE01].wbfs", 2 * GB, "RSPE01", titulo="Wii Sports",
               carpeta=biblioteca),
        _juego("Smash Brawl [RSBE01].wbfs", 4 * GB, "RSBE01", titulo="Smash Brawl",
               carpeta=biblioteca),
    ]


@pytest.mark.gui
def test_las_filas_que_ya_estan_en_la_unidad_lo_dicen(vista, tmp_path, monkeypatch):
    raiz = _unidad(tmp_path)
    _libres(monkeypatch, {raiz: 50 * GB})
    _elegir_destino(vista, raiz)

    vista.set_games(_juegos_de_prueba(tmp_path))

    assert _esperar(lambda: _marcas_listas(vista))
    assert _marcas(vista) == {"Mario Kart Wii": True, "Melee": True,
                              "Wii Sports": False, "Smash Brawl": False}
    fila = vista._game_rows[0]
    assert fila.on_drive_label.get_label() == "Ya en la unidad"


@pytest.mark.gui
def test_las_marcas_se_calculan_fuera_del_hilo_de_gtk(vista, tmp_path, monkeypatch):
    raiz = _unidad(tmp_path)
    _libres(monkeypatch, {raiz: 50 * GB})
    hilos = []
    real = transfer_plan.games_on_drive

    def _anotar(games, root):
        hilos.append(threading.current_thread() is threading.main_thread())
        return real(games, root)

    monkeypatch.setattr(transfer_plan, "games_on_drive", _anotar)
    _elegir_destino(vista, raiz)
    vista.set_games(_juegos_de_prueba(tmp_path))

    assert _esperar(lambda: _marcas_listas(vista) and _marcas(vista)["Melee"])
    assert hilos and not any(hilos)


@pytest.mark.gui
def test_el_contador_suma_lo_elegido_y_avisa_si_no_entra(vista, tmp_path, monkeypatch):
    raiz = _unidad(tmp_path)
    _libres(monkeypatch, {raiz: int(6.2 * GB)})
    _elegir_destino(vista, raiz)
    vista.set_games(_juegos_de_prueba(tmp_path))
    assert _esperar(lambda: _marcas_listas(vista))
    filas = {f.game.title: f for f in vista._game_rows}
    etiqueta = vista.selection_label

    assert not etiqueta.get_visible()

    filas["Wii Sports"].check.set_active(True)
    assert etiqueta.get_visible()
    assert etiqueta.get_label() == "1 seleccionado · 2.0 GB (quedarán 4.2 GB libres)"
    assert not etiqueta.has_css_class("warning") and not etiqueta.has_css_class("error")

    filas["Smash Brawl"].check.set_active(True)
    assert etiqueta.get_label() == ("2 seleccionados · 6.0 GB (entra justo: quedarán "
                                    "204.8 MB libres)")
    assert etiqueta.has_css_class("warning") and not etiqueta.has_css_class("error")

    # Los que ya están en la unidad no suman: la cola los saltea.
    filas["Mario Kart Wii"].check.set_active(True)
    assert etiqueta.get_label() == ("3 seleccionados (1 ya en la unidad) · 6.0 GB "
                                    "(entra justo: quedarán 204.8 MB libres)")

    filas["Smash Brawl"].game.size_bytes = 6 * GB
    vista._set_all_selected(True)
    assert etiqueta.get_label() == ("4 seleccionados (2 ya en la unidad) · 8.0 GB "
                                    "(no entra: faltan 1.8 GB)")
    assert etiqueta.has_css_class("error") and not etiqueta.has_css_class("warning")

    vista._set_all_selected(False)
    assert not etiqueta.get_visible()


@pytest.mark.gui
def test_cambiar_de_unidad_cambia_el_espacio_y_las_marcas(vista, tmp_path, monkeypatch):
    con_juegos = _unidad(tmp_path)
    vacia = tmp_path / "sd"
    vacia.mkdir()
    _libres(monkeypatch, {con_juegos: 50 * GB, vacia: 2 * GB})
    vista.set_games(_juegos_de_prueba(tmp_path))
    filas = {f.game.title: f for f in vista._game_rows}
    filas["Mario Kart Wii"].check.set_active(True)

    _elegir_destino(vista, con_juegos)
    assert _esperar(lambda: _marcas_listas(vista) and _marcas(vista)["Mario Kart Wii"])
    # Todo lo elegido ya está: no hay nada que copiar ni que sumar.
    assert vista.selection_label.get_label() == "1 seleccionado (1 ya en la unidad)"

    _elegir_destino(vista, vacia)
    assert _esperar(lambda: _marcas_listas(vista))
    assert not any(_marcas(vista).values())
    assert vista.selection_label.get_label() == ("1 seleccionado · 3.0 GB (no entra: "
                                                 "faltan 1.0 GB)")


@pytest.mark.gui
def test_si_la_unidad_se_va_se_quitan_las_marcas_sin_romper(vista, tmp_path,
                                                            monkeypatch):
    raiz = _unidad(tmp_path)
    _libres(monkeypatch, {raiz: 50 * GB})
    _elegir_destino(vista, raiz)
    vista.set_games(_juegos_de_prueba(tmp_path))
    assert _esperar(lambda: _marcas_listas(vista) and _marcas(vista)["Mario Kart Wii"])
    vista._game_rows[2].check.set_active(True)

    shutil.rmtree(raiz)
    vista._poll_drives()

    assert vista._dest_path is None
    assert _esperar(lambda: not vista._marks_busy)
    assert not any(_marcas(vista).values())
    assert vista.selection_label.get_label() == "1 seleccionado · 2.0 GB"


@pytest.mark.gui
def test_una_unidad_que_falla_al_leer_saca_las_marcas(vista, tmp_path, monkeypatch):
    raiz = _unidad(tmp_path)
    _libres(monkeypatch, {raiz: 50 * GB})
    _elegir_destino(vista, raiz)
    vista.set_games(_juegos_de_prueba(tmp_path))
    assert _esperar(lambda: _marcas_listas(vista) and _marcas(vista)["Mario Kart Wii"])

    def _ilegible(_games, _root):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(transfer_plan, "games_on_drive", _ilegible)
    vista._refresh_on_drive_marks()

    assert _esperar(lambda: not vista._marks_busy and not any(_marcas(vista).values()))


@pytest.mark.gui
def test_un_destino_que_no_existe_no_rompe(vista, tmp_path, monkeypatch):
    _libres(monkeypatch, {})
    vista.set_games(_juegos_de_prueba(tmp_path))
    vista._game_rows[0].check.set_active(True)

    vista._dest_path = tmp_path / "no-esta"
    vista._update_dest_space_label()
    vista._refresh_on_drive_marks()

    assert _esperar(lambda: not vista._marks_busy)
    assert not any(_marcas(vista).values())
    assert vista.selection_label.get_label() == "1 seleccionado · 3.0 GB"


@pytest.mark.gui
def test_un_juego_que_termina_de_copiarse_queda_marcado(vista, tmp_path, monkeypatch):
    from wiibackup_manager.queue_manager import JobStatus, TransferJob

    raiz = _unidad(tmp_path)
    _libres(monkeypatch, {raiz: 50 * GB})
    _elegir_destino(vista, raiz)
    vista.set_games(_juegos_de_prueba(tmp_path))
    assert _esperar(lambda: _marcas_listas(vista))
    sports = next(f for f in vista._game_rows if f.game.title == "Wii Sports")
    assert not sports.on_drive

    # La cola lo terminó de copiar.
    (raiz / "wbfs" / "RSPE01").mkdir()
    (raiz / "wbfs" / "RSPE01" / "RSPE01.wbfs").write_bytes(b"w")
    vista._on_job_changed(TransferJob(id=1, game=sports.game, dest_root=raiz,
                                      status=JobStatus.DONE))

    assert _esperar(lambda: sports.on_drive)


@pytest.mark.gui
def test_si_no_se_puede_leer_el_espacio_no_dice_cuanto_queda(vista, tmp_path,
                                                             monkeypatch):
    raiz = _unidad(tmp_path)
    _libres(monkeypatch, {raiz: 50 * GB})
    _elegir_destino(vista, raiz)
    vista.set_games(_juegos_de_prueba(tmp_path))
    vista._game_rows[2].check.set_active(True)
    assert vista.selection_label.get_label() == ("1 seleccionado · 2.0 GB (quedarán "
                                                 "48.0 GB libres)")

    _libres(monkeypatch, {})
    vista._update_dest_space_label()

    assert vista.selection_label.get_label() == "1 seleccionado · 2.0 GB"
