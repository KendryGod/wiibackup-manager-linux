"""Una biblioteca en un disco sin montar no se crea en el disco interno.

`config.ensure_dirs` (al arrancar y al guardar Preferencias) y agregar
juegos (`_start_import`) hacían `mkdir(parents=True)` de la carpeta de la
biblioteca. Con la biblioteca en un punto de montaje propio -por ejemplo
`~/Externo/Wii`, con `~/Externo` en fstab- y el disco sin montar, eso
creaba la carpeta en el disco interno, debajo del punto de montaje: no
aparecía el banner de "unidad no disponible", y lo que se agregara quedaba
escondido cuando se montaba el disco.

Ahora solo se crea sola la carpeta por defecto (la del primer arranque);
una elegida por el usuario que no está se trata como no disponible.
"""
from __future__ import annotations

import types

from wiibackup_manager import config


def test_una_biblioteca_elegida_que_no_esta_no_se_crea(tmp_path):
    montaje = tmp_path / "Externo"          # el punto de montaje, vacío
    montaje.mkdir()
    ajustes = config.Settings(library_path=str(montaje / "Wii"))

    config.ensure_dirs(ajustes)

    assert list(montaje.iterdir()) == []
    assert not config.library_path_available(ajustes)


def test_la_biblioteca_por_defecto_si_se_crea(tmp_path, monkeypatch):
    por_defecto = tmp_path / "home" / "WiiGames"
    monkeypatch.setattr(config, "DEFAULT_LIBRARY_PATH", str(por_defecto))
    ajustes = config.Settings(library_path=str(por_defecto))

    config.ensure_dirs(ajustes)

    assert por_defecto.is_dir()
    assert config.library_path_available(ajustes)


def test_una_biblioteca_que_ya_esta_no_se_toca(tmp_path):
    biblio = tmp_path / "Juegos"
    biblio.mkdir()
    (biblio / "Mario.iso").write_bytes(b"x")

    config.ensure_dirs(config.Settings(library_path=str(biblio)))

    assert [p.name for p in biblio.iterdir()] == ["Mario.iso"]


def test_agregar_juegos_no_crea_la_biblioteca_en_el_disco_interno(tmp_path):
    from wiibackup_manager.window import WiiBackupWindow

    montaje = tmp_path / "Externo"
    montaje.mkdir()
    juego = tmp_path / "Mario.iso"
    juego.write_bytes(b"x")
    toasts = []
    ventana = types.SimpleNamespace(
        settings=config.Settings(library_path=str(montaje / "Wii")),
        _show_toast=toasts.append, _games=[])

    WiiBackupWindow._start_import(ventana, [juego])

    assert list(montaje.iterdir()) == []
    [aviso] = toasts
    assert str(montaje / "Wii") in aviso
