"""Packs de taller en Transferir.

Un pack es una lista con nombre de juegos que se cargan juntos ("Pack
Mario", "Pack niños"). Se guarda en config.json bajo la clave nueva
`packs`, como {nombre: [ID6, ...]}: por ID6 y no por archivo, así sirve
aunque la biblioteca se reorganice o se convierta.

- Marcar un pack tilda los juegos de la lista que están en la biblioteca
  (uno por disco, aunque el mismo juego esté dos veces) y avisa cuántos
  no están; el contador de Transferir muestra cuánto ocupa.
- "Guardar selección como pack…" pide un nombre. Vacío o repetido (sin
  distinguir mayúsculas ni espacios de más): aviso y no se guarda, nunca
  se pisa otro pack en silencio.
- Renombrar y borrar: borrar un pack borra solo la lista.
- Cargar config.json tolera `packs` ausente (una config de 0.2.2), con
  otro tipo, IDs que no son texto o no son ID6, y nombres raros, sin
  perder el resto ni avisar "configuración dañada".
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wiibackup_manager import config, packs
from wiibackup_manager.game_model import Game

GB = 1024 ** 3


@pytest.fixture
def archivo(tmp_path, monkeypatch):
    ruta = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_FILE", ruta)
    return ruta


def _apartados(archivo: Path) -> list:
    return sorted(archivo.parent.glob("config.json.corrupto-*"))


# Lo que escribía 0.2.2: todas las claves de entonces, sin `packs`.
CONFIG_022 = {
    "library_path": "/home/taller/WiiGames",
    "wbfs_drive_path": "/run/media/taller/USB",
    "cover_region": "ES",
    "wit_binary": "/usr/local/bin/wit",
    "auto_scan_on_start": False,
    "color_scheme": "dark",
    "dest_presets": [{"name": "SD cliente", "path": "/run/media/taller/SD"}],
    "scrub_update": False,
    "verify_after_copy": True,
    "shop_name": "Mi taller",
    "shop_slogan": "Juegos & más",
    "shop_location": "Tegucigalpa",
    "shop_whatsapp": "50488880000",
    "shop_logo_path": "/home/taller/logo.png",
    "shop_accent_color": "#112233",
    "ticket_theme": "light",
}


def _cargar(archivo: Path, datos: dict) -> config.Settings:
    archivo.write_text(json.dumps(datos), encoding="utf-8")
    return config.Settings.load()


def _sin_packs(s: config.Settings) -> dict:
    """Lo cargado, sin los campos que una config de 0.2.2 no tiene: esos
    entran con su valor por defecto (ver sus propios tests)."""
    from dataclasses import asdict
    datos = asdict(s)
    datos.pop("packs", None)
    datos.pop("verify_gamecube_copies", None)
    datos.pop("golden_configs_dir", None)
    return datos


# ------------------------------------------------------------- Config --
def test_una_config_de_0_2_2_sin_packs_sigue_funcionando(archivo):
    s = _cargar(archivo, CONFIG_022)

    assert s.packs == {}
    assert _sin_packs(s) == CONFIG_022
    assert not s.config_was_damaged and _apartados(archivo) == []


@pytest.mark.parametrize("valor", [[], "Pack Mario", 5, None, True, [["RMCE01"]]],
                         ids=["lista", "texto", "numero", "null", "bool", "lista-de-listas"])
def test_packs_con_otro_tipo_se_ignora_sin_tocar_el_resto(archivo, valor):
    s = _cargar(archivo, {**CONFIG_022, "packs": valor})

    assert s.packs == {}
    assert _sin_packs(s) == CONFIG_022
    assert not s.config_was_damaged and _apartados(archivo) == []


def test_ids_que_no_son_texto_o_no_son_id6_se_descartan(archivo):
    s = _cargar(archivo, {**CONFIG_022, "packs": {
        "Pack Mario": ["RMCE01", 5, None, ["SMNE01"], {"id": "RMCE01"}, True,
                       "rmce01", " SMNE01 ", "malo", "RMC E01", "RMCE011", "", "SMNE01"],
    }})

    assert s.packs == {"Pack Mario": ["RMCE01", "SMNE01"]}
    assert _sin_packs(s) == CONFIG_022 and not s.config_was_damaged


def test_un_pack_que_no_es_una_lista_se_descarta(archivo):
    s = _cargar(archivo, {**CONFIG_022, "packs": {
        "Bueno": ["RMCE01"], "Texto": "RMCE01", "Objeto": {"a": 1}, "Nulo": None,
        "Vacio": [],
    }})

    assert s.packs == {"Bueno": ["RMCE01"], "Vacio": []}
    assert not s.config_was_damaged


def test_nombres_raros(archivo):
    s = _cargar(archivo, {**CONFIG_022, "packs": {
        "  Pack   Mario  ": ["RMCE01"],
        "": ["SMNE01"],
        "   ": ["SMNE01"],
        "\t\n": ["SMNE01"],
        "Tab\tNombre": ["SMNE01"],
        "con\u0000nulo​": ["SMNE01"],
        "pack mario": ["SB4E01"],          # el mismo nombre: queda el primero
        "Pack ñandú 🎮 <b>&</b>": ["RSBE01"],
        "x" * 300: ["RSBE01"],
    }})

    assert list(s.packs) == ["Pack Mario", "Tab Nombre", "con nulo",
                             "Pack ñandú 🎮 <b>&</b>", "x" * 300]
    assert s.packs["Pack Mario"] == ["RMCE01"]
    assert not s.config_was_damaged and _apartados(archivo) == []


def test_guardar_un_pack_no_pierde_las_demas_claves(archivo):
    s = _cargar(archivo, CONFIG_022)

    s.packs = {"Pack Mario": ["RMCE01", "SMNE01"]}
    s.save()

    en_disco = json.loads(archivo.read_text(encoding="utf-8"))
    # Lo que 0.2.2 no tenía se guarda con su valor por defecto.
    assert en_disco == {**CONFIG_022, "packs": {"Pack Mario": ["RMCE01", "SMNE01"]},
                        "verify_gamecube_copies": True, "golden_configs_dir": ""}
    assert config.Settings.load().packs == {"Pack Mario": ["RMCE01", "SMNE01"]}


def test_un_pack_danado_no_impide_guardar_el_resto(archivo):
    s = _cargar(archivo, {**CONFIG_022, "packs": {"Malo": 7, "Bueno": ["RMCE01", 3]}})
    s.shop_name = "Otro taller"
    s.save()

    en_disco = json.loads(archivo.read_text(encoding="utf-8"))
    assert en_disco["packs"] == {"Bueno": ["RMCE01"]}
    assert en_disco["shop_name"] == "Otro taller"
    assert en_disco["dest_presets"] == CONFIG_022["dest_presets"]


# ------------------------------------------------------- Lógica pura --
def _juego(nombre, game_id, size=GB, console="wii", disc=0) -> Game:
    path = Path("/biblioteca") / nombre
    return Game(path=path, game_id=game_id, title=nombre, fmt=path.suffix[1:].upper(),
                size_bytes=size, identified_by="iso", console=console, disc_number=disc)


def test_marcar_elige_los_que_estan_y_dice_cuales_faltan():
    biblioteca = [_juego("mario.iso", "RMCE01"), _juego("nsmb.wbfs", "SMNE01"),
                  _juego("melee1.iso", "GALE01", console="gc"),
                  _juego("melee2.iso", "GALE01", console="gc", disc=1)]

    elegidos, faltan = packs.match(["RMCE01", "GALE01", "RSBE01", "SB4E01"], biblioteca)

    assert [j.path.name for j in elegidos] == ["mario.iso", "melee1.iso", "melee2.iso"]
    assert faltan == ["RSBE01", "SB4E01"]


def test_un_juego_que_esta_dos_veces_en_la_biblioteca_se_marca_una_vez():
    biblioteca = [_juego("mario.iso", "RMCE01"), _juego("mario.wbfs", "RMCE01")]

    elegidos, faltan = packs.match(["RMCE01"], biblioteca)

    assert [j.path.name for j in elegidos] == ["mario.iso"] and faltan == []


def test_los_ids_de_la_seleccion_sin_repetir_ni_sin_identificar():
    seleccion = [_juego("sin id.iso", "??????"), _juego("nsmb.wbfs", "SMNE01"),
                 _juego("mario.iso", "rmce01"), _juego("mario.wbfs", "RMCE01"),
                 _juego("melee2.iso", "GALE01", console="gc", disc=1)]

    assert packs.ids_from_selection(seleccion) == ["SMNE01", "RMCE01", "GALE01"]


@pytest.mark.parametrize("texto, esperado", [
    ("", ("", packs.EMPTY)),
    ("   \t ", ("", packs.EMPTY)),
    ("pack   MARIO", ("pack MARIO", packs.DUPLICATE)),
    ("  Pack Zelda ", ("Pack Zelda", None)),
])
def test_nombre_vacio_o_repetido(texto, esperado):
    assert packs.check_name({"Pack Mario": ["RMCE01"]}, texto) == esperado


def test_al_renombrar_se_puede_cambiar_solo_las_mayusculas_pero_no_pisar_otro():
    actuales = {"Pack Mario": ["RMCE01"], "Pack Zelda": ["RZDE01"]}

    assert packs.check_name(actuales, "PACK MARIO", renaming="Pack Mario") == \
        ("PACK MARIO", None)
    assert packs.check_name(actuales, "pack zelda", renaming="Pack Mario") == \
        ("pack zelda", packs.DUPLICATE)


def test_un_nombre_muy_largo_se_recorta_solo_para_mostrarlo():
    assert packs.short_name("x" * 300) == "x" * 39 + "…"
    assert packs.short_name("Pack Mario") == "Pack Mario"


def test_renombrar_conserva_el_orden_y_los_juegos():
    actuales = {"A": ["RMCE01"], "B": ["SMNE01"], "C": ["GALE01"]}

    nuevos = packs.renamed(actuales, "B", "Pack Nuevo")

    assert list(nuevos.items()) == [("A", ["RMCE01"]), ("Pack Nuevo", ["SMNE01"]),
                                    ("C", ["GALE01"])]
    assert actuales == {"A": ["RMCE01"], "B": ["SMNE01"], "C": ["GALE01"]}


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


@pytest.fixture
def vista(gtk, monkeypatch, tmp_path, archivo):
    from wiibackup_manager import drives, gametdb
    from wiibackup_manager.widgets import transfer_view

    monkeypatch.setattr(drives, "list_removable_drives", lambda: [])
    monkeypatch.setattr(gametdb, "fetch_cover_async", lambda *a, **k: None)
    s = _cargar(archivo, {**CONFIG_022, "packs": {
        "Pack Mario": ["RMCE01", "SMNE01", "SB4E01"],
        "Pack_GC": ["GALE01"],
    }})
    toasts = []
    v = transfer_view.TransferView(s, toasts.append)
    ventana = gtk.Window()
    ventana.set_child(v)
    v.toasts = toasts
    v.set_games([
        _juego("Mario Kart Wii.iso", "RMCE01", 3 * GB),
        _juego("New Super Mario Bros.wbfs", "SMNE01", 2 * GB),
        _juego("Melee 1.iso", "GALE01", GB, "gc"),
        _juego("Melee 2.iso", "GALE01", GB, "gc", 1),
        _juego("Wii Sports.wbfs", "RSPE01", GB),
    ])
    yield v
    v.shutdown()
    ventana.destroy()


def _tildados(v) -> list:
    return [f.game.path.name for f in v._game_rows if f.check.get_active()]


def _en_disco(archivo: Path) -> dict:
    return json.loads(archivo.read_text(encoding="utf-8"))


def _etiquetas_del_menu(modelo) -> list:
    etiquetas = []
    for i in range(modelo.get_n_items()):
        valor = modelo.get_item_attribute_value(i, "label")
        if valor is not None:
            etiquetas.append(valor.get_string())
        for enlace in ("section", "submenu"):
            hijo = modelo.get_item_link(i, enlace)
            if hijo is not None:
                etiquetas += _etiquetas_del_menu(hijo)
    return etiquetas


def _acciones_del_menu(modelo) -> list:
    acciones = []
    for i in range(modelo.get_n_items()):
        valor = modelo.get_item_attribute_value(i, "action")
        if valor is not None:
            destino = modelo.get_item_attribute_value(i, "target")
            acciones.append((valor.get_string(),
                             destino.get_string() if destino is not None else None))
        for enlace in ("section", "submenu"):
            hijo = modelo.get_item_link(i, enlace)
            if hijo is not None:
                acciones += _acciones_del_menu(hijo)
    return acciones


def _accion(v, nombre: str, pack: str | None = None):
    """Como la activa el menú: por nombre completo desde el botón, que la
    busca en los grupos de acciones de la vista."""
    from gi.repository import GLib
    assert v.packs_button.activate_action(
        f"transfer.{nombre}", GLib.Variant("s", pack) if pack is not None else None)


@pytest.mark.gui
def test_el_boton_packs_esta_junto_a_seleccionar_todos(vista):
    boton = vista.packs_button
    hermano = boton.get_next_sibling()

    assert boton.get_label() == "Packs"
    assert hermano is not None and hermano.get_label() == "Seleccionar todos"


@pytest.mark.gui
def test_el_menu_lista_los_packs_y_guardar(vista):
    vista.settings.packs = {**vista.settings.packs, "x" * 300: ["RSPE01"]}
    vista._rebuild_packs_menu()

    etiquetas = _etiquetas_del_menu(vista.packs_button.get_menu_model())

    # El "_" de un nombre no es un atajo de teclado del menú.
    assert "Pack Mario (3 juegos)" in etiquetas and "Pack__GC (1 juego)" in etiquetas
    assert "x" * 39 + "… (1 juego)" in etiquetas
    assert "Guardar selección como pack…" in etiquetas
    assert {"Marcar sus juegos", "Renombrar…", "Borrar…"} <= set(etiquetas)


@pytest.mark.gui
def test_cada_opcion_del_menu_va_a_una_accion_que_existe(vista):
    acciones = _acciones_del_menu(vista.packs_button.get_menu_model())

    assert ("transfer.save-pack", None) in acciones
    for pack in ("Pack Mario", "Pack_GC"):
        for nombre in ("apply-pack", "rename-pack", "delete-pack"):
            assert (f"transfer.{nombre}", pack) in acciones
    for accion, _pack in acciones:
        assert vista._pack_actions.has_action(accion.removeprefix("transfer."))


@pytest.mark.gui
def test_marcar_un_pack_tilda_los_que_estan_y_avisa_los_que_faltan(vista):
    _accion(vista, "apply-pack", "Pack Mario")

    assert _tildados(vista) == ["Mario Kart Wii.iso", "New Super Mario Bros.wbfs"]
    assert vista.toasts[-1] == ("Pack 'Pack Mario': se marcaron 2 juegos. 1 de la lista "
                                "no está en la biblioteca.")
    # El contador de lo elegido dice cuánto ocupa.
    assert vista.selection_label.get_label() == "2 seleccionados · 5.0 GB"


@pytest.mark.gui
def test_marcar_suma_a_lo_que_ya_estaba_tildado(vista):
    vista._game_rows[4].check.set_active(True)

    _accion(vista, "apply-pack", "Pack_GC")

    assert _tildados(vista) == ["Melee 1.iso", "Melee 2.iso", "Wii Sports.wbfs"]
    assert vista.toasts[-1] == "Pack 'Pack_GC': se marcaron 2 juegos."


@pytest.mark.gui
def test_un_pack_sin_ningun_juego_en_la_biblioteca_lo_dice(vista):
    vista.settings.packs = {**vista.settings.packs, "Lejano": ["RZDE01"]}

    _accion(vista, "apply-pack", "Lejano")

    assert _tildados(vista) == []
    assert vista.toasts[-1] == "Ninguno de los juegos del pack 'Lejano' está en la biblioteca."


@pytest.mark.gui
def test_guardar_la_seleccion_como_pack(vista, archivo):
    for fila in vista._game_rows[2:]:
        fila.check.set_active(True)

    dialogo, entrada, aviso = vista._pack_name_dialog("", "", "", "Guardar")
    entrada.set_text("  Pack   Fiesta ")
    vista._on_save_pack_response(dialogo, "ok", entrada,
                                 packs.ids_from_selection(vista._selected_games()))

    assert vista.settings.packs["Pack Fiesta"] == ["GALE01", "RSPE01"]
    assert _en_disco(archivo)["packs"]["Pack Fiesta"] == ["GALE01", "RSPE01"]
    assert _en_disco(archivo)["shop_name"] == "Mi taller"
    assert "Pack Fiesta (2 juegos)" in _etiquetas_del_menu(
        vista.packs_button.get_menu_model())
    assert vista.toasts[-1] == "Pack 'Pack Fiesta' guardado con 2 juegos."


@pytest.mark.gui
def test_guardar_sin_nada_elegido_avisa_y_no_abre_nada(vista, archivo):
    antes = archivo.read_bytes()

    _accion(vista, "save-pack")

    assert vista.toasts[-1].startswith("Elegí primero los juegos del pack")
    assert archivo.read_bytes() == antes


@pytest.mark.gui
@pytest.mark.parametrize("nombre, aviso", [
    ("   ", "El pack necesita un nombre."),
    ("pack  MARIO", "Ya hay un pack que se llama 'Pack Mario': elegí otro nombre."),
])
def test_un_nombre_vacio_o_repetido_no_guarda_y_avisa(vista, archivo, nombre, aviso):
    antes = archivo.read_bytes()
    vista._game_rows[4].check.set_active(True)

    dialogo, entrada, _etiqueta = vista._pack_name_dialog("", "", "", "Guardar")
    entrada.set_text(nombre)
    # El botón se apaga mientras el nombre no sirve…
    assert not dialogo.get_response_enabled("ok")
    # …y si igual llega la respuesta, no se guarda ni se pisa nada.
    vista._on_save_pack_response(dialogo, "ok", entrada, ["RSPE01"])

    assert vista.toasts[-1] == aviso
    assert vista.settings.packs["Pack Mario"] == ["RMCE01", "SMNE01", "SB4E01"]
    assert archivo.read_bytes() == antes


@pytest.mark.gui
def test_el_dialogo_dice_que_el_nombre_ya_existe_mientras_se_escribe(vista):
    dialogo, entrada, aviso = vista._pack_name_dialog("", "", "", "Guardar")

    entrada.set_text("Pack Zelda")
    assert dialogo.get_response_enabled("ok")

    entrada.set_text("pack mario")
    assert not dialogo.get_response_enabled("ok")
    assert aviso.get_label() == "Ya hay un pack que se llama 'Pack Mario': elegí otro nombre."
    assert aviso.has_css_class("error")

    entrada.set_text("")
    assert not dialogo.get_response_enabled("ok")
    assert aviso.get_label() == "El pack necesita un nombre."


@pytest.mark.gui
def test_renombrar_un_pack(vista, archivo):
    dialogo, entrada, aviso = vista._pack_name_dialog("", "", "Pack Mario", "Renombrar",
                                               renaming="Pack Mario")
    entrada.set_text("Pack Nintendo")
    vista._on_rename_pack_response(dialogo, "ok", entrada, "Pack Mario")

    assert list(vista.settings.packs) == ["Pack Nintendo", "Pack_GC"]
    assert _en_disco(archivo)["packs"]["Pack Nintendo"] == ["RMCE01", "SMNE01", "SB4E01"]
    etiquetas = _etiquetas_del_menu(vista.packs_button.get_menu_model())
    assert "Pack Nintendo (3 juegos)" in etiquetas
    assert "Pack Mario (3 juegos)" not in etiquetas
    assert vista.toasts[-1] == "Pack 'Pack Mario' renombrado a 'Pack Nintendo'."


@pytest.mark.gui
def test_renombrar_a_un_nombre_que_ya_existe_no_pisa_el_otro(vista, archivo):
    antes = archivo.read_bytes()
    dialogo, entrada, aviso = vista._pack_name_dialog("", "", "Pack Mario", "Renombrar",
                                               renaming="Pack Mario")
    entrada.set_text("PACK_GC")
    vista._on_rename_pack_response(dialogo, "ok", entrada, "Pack Mario")

    assert vista.settings.packs == {"Pack Mario": ["RMCE01", "SMNE01", "SB4E01"],
                                    "Pack_GC": ["GALE01"]}
    assert archivo.read_bytes() == antes
    assert vista.toasts[-1] == "Ya hay un pack que se llama 'Pack_GC': elegí otro nombre."


@pytest.mark.gui
def test_borrar_un_pack_borra_solo_la_lista(vista, archivo, tmp_path):
    biblioteca = tmp_path / "biblioteca"
    biblioteca.mkdir()
    (biblioteca / "mario.iso").write_bytes(b"juego")

    vista._on_delete_pack_response(None, "delete", "Pack Mario")

    assert vista.settings.packs == {"Pack_GC": ["GALE01"]}
    assert _en_disco(archivo)["packs"] == {"Pack_GC": ["GALE01"]}
    assert (biblioteca / "mario.iso").read_bytes() == b"juego"
    assert "Pack Mario (3 juegos)" not in _etiquetas_del_menu(
        vista.packs_button.get_menu_model())
    assert vista.toasts[-1] == "Se borró el pack 'Pack Mario'."


@pytest.mark.gui
def test_cancelar_el_borrado_no_borra(vista, archivo):
    antes = archivo.read_bytes()

    vista._on_delete_pack_response(None, "cancel", "Pack Mario")

    assert "Pack Mario" in vista.settings.packs and archivo.read_bytes() == antes


@pytest.mark.gui
def test_si_no_se_pudo_guardar_no_dice_que_se_guardo(vista, monkeypatch):
    monkeypatch.setattr(config, "try_save", lambda _s: "Disco lleno")
    vista._game_rows[4].check.set_active(True)

    dialogo, entrada, aviso = vista._pack_name_dialog("", "", "", "Guardar")
    entrada.set_text("Pack Sports")
    vista._on_save_pack_response(dialogo, "ok", entrada, ["RSPE01"])

    assert vista.toasts[-1] == "No se pudo guardar la configuración: Disco lleno"
    assert not any(t.startswith("Pack 'Pack Sports' guardado") for t in vista.toasts)
