"""El archivo AppStream (`metainfo.xml`) de la app.

Sin él, GNOME Software y Flathub no tienen de dónde sacar la descripción,
las capturas, la licencia ni las versiones: Flathub lo exige. Se instala
en `share/metainfo/` y describe lo mismo que ya dicen `pyproject.toml`,
el `.desktop` y `LICENSE`, así que el test cruza los datos para que no
se separen:

- el `id` es el de la app y el `launchable` apunta al `.desktop` real;
- la licencia del proyecto y la de los metadatos son MIT, como `LICENSE`;
- la versión es la misma en `pyproject.toml`, `wiibackup_manager/__init__.py`
  (la que muestra "Acerca de"), la release más nueva de `<releases>` y el
  badge del README: al subir la versión hay que cambiar las cuatro;
- las capturas apuntan a archivos que existen en `docs/screenshots/`;
- el texto está en inglés (el idioma base de AppStream) y en español;
- `appstreamcli validate` no encuentra problemas, si está instalado.
"""
from __future__ import annotations

import datetime
import re
import shutil
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from wiibackup_manager import __version__, config

RAIZ = Path(__file__).resolve().parent.parent
METAINFO = RAIZ / "data" / f"{config.APP_ID}.metainfo.xml"
DESKTOP = RAIZ / "data" / f"{config.APP_ID}.desktop"
README = RAIZ / "README.md"
LANG = "{http://www.w3.org/XML/1998/namespace}lang"


@pytest.fixture(scope="module")
def raiz():
    return ET.parse(METAINFO).getroot()


def _proyecto() -> dict:
    return tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))


def test_existe_y_es_una_app_de_escritorio(raiz):
    assert raiz.tag == "component"
    assert raiz.get("type") == "desktop-application"
    assert raiz.findtext("id") == config.APP_ID


def test_el_launchable_es_el_desktop_de_la_app(raiz):
    launchable = raiz.find("launchable")

    assert launchable.get("type") == "desktop-id"
    assert launchable.text == DESKTOP.name
    assert DESKTOP.is_file()


def test_la_licencia_es_mit_como_el_proyecto(raiz):
    assert raiz.findtext("metadata_license") == "MIT"
    assert raiz.findtext("project_license") == "MIT"
    assert _proyecto()["project"]["license"] == {"text": "MIT"}
    assert (RAIZ / "LICENSE").read_text(encoding="utf-8").startswith("MIT License")


def test_el_desarrollador_y_el_binario_son_los_del_proyecto(raiz):
    proyecto = _proyecto()["project"]

    assert raiz.findtext("developer/name") == proyecto["authors"][0]["name"]
    assert raiz.findtext("provides/binary") in proyecto["scripts"]


def _version_del_badge() -> str:
    """La del badge "Versión" del README (img.shields.io)."""
    badge = re.search(r"!\[Versión\]\(https://img\.shields\.io/badge/versión-([^-)]+)-",
                      README.read_text(encoding="utf-8"))
    assert badge is not None, "el README no tiene el badge de la versión"
    return badge.group(1)


def test_la_version_es_la_misma_en_todos_lados(raiz):
    releases = raiz.findall("releases/release")
    fechas = [datetime.date.fromisoformat(r.get("date")) for r in releases]
    pyproject = _proyecto()["project"]["version"]
    versiones = {
        "pyproject.toml": pyproject,
        "wiibackup_manager/__init__.py": __version__,
        "metainfo.xml (la release más nueva)": releases[0].get("version"),
        "README.md (el badge)": _version_del_badge(),
    }

    assert versiones == dict.fromkeys(versiones, pyproject)
    assert fechas == sorted(fechas, reverse=True)


def test_las_capturas_existen(raiz):
    urls = [img.text for img in raiz.iter("image")]

    assert urls
    for url in urls:
        nombre = re.search(r"/docs/screenshots/([^/]+)$", url)
        assert nombre is not None, url
        assert (RAIZ / "docs" / "screenshots" / nombre.group(1)).is_file(), url


@pytest.mark.parametrize("etiqueta", ["name", "summary"])
def test_hay_texto_en_ingles_y_en_espanol(raiz, etiqueta):
    idiomas = {e.get(LANG) for e in raiz.findall(etiqueta)}

    assert None in idiomas, f"falta el {etiqueta} base, en inglés"
    assert "es" in idiomas or etiqueta == "name"


def test_la_descripcion_esta_en_los_dos_idiomas(raiz):
    descripcion = raiz.find("description")
    parrafos = [p for p in descripcion.iter() if p.tag in ("p", "li")]
    base = [p for p in parrafos if p.get(LANG) is None]
    es = [p for p in parrafos if p.get(LANG) == "es"]

    assert base and len(base) == len(es)


def test_se_instala_en_share_metainfo():
    data_files = _proyecto()["tool"]["setuptools"]["data-files"]

    assert data_files.get("share/metainfo") == [
        f"data/{config.APP_ID}.metainfo.xml"]


@pytest.mark.skipif(shutil.which("appstreamcli") is None,
                    reason="sin appstreamcli")
def test_appstreamcli_lo_valida():
    r = subprocess.run(["appstreamcli", "validate", "--no-net", "--explain", str(METAINFO)],
                       capture_output=True, text=True, timeout=60)

    assert r.returncode == 0, r.stdout + r.stderr
