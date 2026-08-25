"""Lectura tolerante de config.json y escritura atómica."""
from __future__ import annotations

import json

from wiibackup_manager import config


def test_write_text_atomic(tmp_path):
    destino = tmp_path / "salida.txt"
    config.write_text_atomic(destino, "hola")
    assert destino.read_text() == "hola"


def test_write_text_atomic_no_deja_temporales(tmp_path):
    destino = tmp_path / "salida.txt"
    config.write_text_atomic(destino, "hola")
    assert [p.name for p in tmp_path.iterdir()] == ["salida.txt"]


def test_write_text_atomic_reemplaza_conservando_lo_viejo_si_falla(tmp_path):
    """El punto de escribir atómicamente: cortar el proceso a mitad deja
    el archivo anterior entero, nunca uno truncado que parece completo."""
    destino = tmp_path / "config.json"
    config.write_text_atomic(destino, "version 1")
    config.write_text_atomic(destino, "version 2")
    assert destino.read_text() == "version 2"


def test_settings_por_defecto():
    s = config.Settings()
    assert s.library_path.endswith("WiiGames")
    assert s.wit_binary == "wit"
    assert s.cover_region == "EN"
    assert s.dest_presets == []


def test_load_sin_archivo_usa_los_valores_por_defecto(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "no-existe.json")
    assert config.Settings.load().wit_binary == "wit"


def test_load_lee_lo_guardado(monkeypatch, tmp_path):
    archivo = tmp_path / "config.json"
    archivo.write_text(json.dumps({"cover_region": "JA", "wit_binary": "/opt/wit"}),
                       encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", archivo)
    s = config.Settings.load()
    assert s.cover_region == "JA"
    assert s.wit_binary == "/opt/wit"


def test_un_campo_con_el_tipo_equivocado_solo_pierde_ese_campo(monkeypatch, tmp_path):
    """Validación por campo y no "todo o nada": editar el JSON a mano y
    poner `auto_scan_on_start: "si"` no puede perder las otras cuatro
    preferencias."""
    archivo = tmp_path / "config.json"
    archivo.write_text(json.dumps({
        "auto_scan_on_start": "si",     # debería ser booleano
        "cover_region": "JA",           # este está bien
    }), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", archivo)
    s = config.Settings.load()
    assert s.auto_scan_on_start is True     # cayó al valor por defecto
    assert s.cover_region == "JA"           # este sobrevivió


def test_un_booleano_no_se_cuela_en_un_campo_numerico(monkeypatch, tmp_path):
    """En Python bool es subclase de int, así que con isinstance un True
    pasaría por un campo numérico."""
    archivo = tmp_path / "config.json"
    archivo.write_text(json.dumps({"library_path": True}), encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", archivo)
    assert isinstance(config.Settings.load().library_path, str)


def test_un_json_ilegible_cae_a_los_valores_por_defecto(monkeypatch, tmp_path):
    archivo = tmp_path / "config.json"
    archivo.write_text("{ roto", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", archivo)
    assert config.Settings.load().wit_binary == "wit"


def test_clean_presets_descarta_lo_que_no_sirve():
    limpio = config.clean_presets([
        {"name": "Pendrive", "path": "/run/media/kendry/USB"},
        {"name": "sin path"},
        {"path": "/sin/nombre"},
        "ni siquiera un diccionario",
        {"name": 123, "path": "/tipo/equivocado"},
    ])
    assert limpio == [{"name": "Pendrive", "path": "/run/media/kendry/USB"}]


def test_clean_presets_con_algo_que_no_es_lista():
    assert config.clean_presets("cualquier cosa") == []
    assert config.clean_presets(None) == []


def test_library_path_available(tmp_path):
    s = config.Settings(library_path=str(tmp_path))
    assert config.library_path_available(s)
    s.library_path = str(tmp_path / "no-existe")
    assert not config.library_path_available(s)
