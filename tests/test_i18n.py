"""Traducciones: que el catálogo cargue y que no haya marcadores rotos.

El test que más vale acá es el último: un `{name}` mal escrito en una
traducción no se nota hasta que un usuario abre esa pantalla y la app
revienta con un KeyError en pleno `.format()`.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

DOMINIO = "wiibackup-manager"
LOCALE_DIR = Path(__file__).resolve().parent.parent / "data" / "locale"
CATALOGOS = sorted(LOCALE_DIR.glob("*/LC_MESSAGES/%s.po" % DOMINIO))


def test_hay_al_menos_un_catalogo():
    assert CATALOGOS, "no se encontró ningún .po en data/locale/"


def test_el_fallback_es_el_español(monkeypatch):
    """Sin catálogo para ese idioma, `_()` devuelve el msgid, que es el
    español del fuente. No hay un es.po que mantener."""
    import gettext
    vacio = gettext.NullTranslations()
    assert vacio.gettext("Sin juegos todavía") == "Sin juegos todavía"


def test_el_catalogo_ingles_traduce():
    import gettext
    tr = gettext.translation(DOMINIO, str(LOCALE_DIR), languages=["en"])
    assert tr.gettext("Sin juegos todavía") == "No games yet"
    assert tr.gettext("Mover a la papelera") == "Move to trash"


def test_el_catalogo_ingles_tiene_plurales():
    import gettext
    tr = gettext.translation(DOMINIO, str(LOCALE_DIR), languages=["en"])
    uno = tr.ngettext("{count} juego · {size}", "{count} juegos · {size}", 1)
    varios = tr.ngettext("{count} juego · {size}", "{count} juegos · {size}", 3)
    assert uno == "{count} game · {size}"
    assert varios == "{count} games · {size}"


@pytest.mark.parametrize("po", CATALOGOS, ids=lambda p: p.parent.parent.name)
def test_el_mo_esta_al_dia_con_el_po(po, tmp_path):
    """El .mo se versiona junto al .po (setuptools copia archivos, no
    compila). Si alguien edita el .po y se olvida de correr
    tools/update-translations.sh, la app instalada seguiría mostrando la
    traducción vieja y nada lo avisaría.

    Se compara el CONTENIDO y no las fechas: git no preserva mtimes, así
    que en un checkout limpio el orden de las fechas es arbitrario.
    msgfmt es determinista (no mete timestamps propios en el .mo), así
    que recompilar y comparar bytes es una prueba estable."""
    mo = po.with_suffix(".mo")
    assert mo.exists(), f"falta {mo.name}: correr tools/update-translations.sh"
    recien = tmp_path / "recien-compilado.mo"
    subprocess.run(["msgfmt", "--output-file", str(recien), str(po)], check=True)
    assert recien.read_bytes() == mo.read_bytes(), (
        f"{mo.name} no coincide con {po.name}: "
        "correr tools/update-translations.sh")


@pytest.mark.parametrize("po", CATALOGOS, ids=lambda p: p.parent.parent.name)
def test_el_po_es_valido(po):
    r = subprocess.run(["msgfmt", "--check", "--output-file=/dev/null", str(po)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def _marcadores(texto: str) -> list:
    return sorted(re.findall(r"\{([a-zA-Z_][a-zA-Z0-9_]*)[^}]*\}", texto))


def _entradas(po: Path):
    """(msgid, [msgstrs]) de cada entrada traducida del .po."""
    texto = po.read_text(encoding="utf-8")
    for bloque in texto.split("\n\n"):
        if "msgid " not in bloque:
            continue
        def junta(clave):
            m = re.search(r'^%s ((?:"(?:[^"\\]|\\.)*"\n?)+)' % clave, bloque, re.M)
            return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))) if m else None
        msgid = junta("msgid")
        if not msgid:
            continue
        traducciones = [junta("msgstr")] if "msgstr[" not in bloque else [
            "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', g))
            for g in re.findall(r'^msgstr\[\d\] ((?:"(?:[^"\\]|\\.)*"\n?)+)', bloque, re.M)
        ]
        yield msgid, [t for t in traducciones if t]


@pytest.mark.parametrize("po", CATALOGOS, ids=lambda p: p.parent.parent.name)
def test_los_marcadores_coinciden_con_el_original(po):
    """Cada {marcador} del original tiene que estar en la traducción: la
    app hace .format() con esos nombres, y uno renombrado u omitido es un
    KeyError en tiempo de ejecución."""
    problemas = []
    for msgid, traducciones in _entradas(po):
        esperados = _marcadores(msgid)
        for t in traducciones:
            if _marcadores(t) != esperados:
                problemas.append(f"{msgid[:50]!r}: {esperados} != {_marcadores(t)}")
    assert not problemas, "\n".join(problemas)


@pytest.mark.parametrize("po", CATALOGOS, ids=lambda p: p.parent.parent.name)
def test_no_quedan_cadenas_sin_traducir(po):
    sin_traducir = [m for m, t in _entradas(po) if not t]
    assert not sin_traducir, (
        f"{len(sin_traducir)} cadenas sin traducir en {po.parent.parent.name}: "
        + ", ".join(s[:40] for s in sin_traducir[:5]))


# --------------------------------- Cadenas que no se marcan con `_()` --
#
# Los valores de `OperationKind` y `JobStatus` se muestran al usuario pero
# se traducen lejos de donde se escriben, con `_(self.value)` en `.label`.
# `xgettext` no puede deducir eso -lo único que ve es una variable-, así
# que van envueltos en `N_()`, que no traduce nada y solo los hace
# visibles para la extracción. Estos dos tests cuidan ese arreglo: sin
# ellos, agregar un estado nuevo sin la marca vuelve a dejar media
# interfaz en español, y en silencio.


def _msgids(po: Path) -> set:
    return {msgid for msgid, _t in _entradas(po)}


@pytest.mark.parametrize("po", CATALOGOS, ids=lambda p: p.parent.parent.name)
def test_los_valores_de_los_enums_visibles_estan_en_el_catalogo(po):
    """Cada valor que termina en pantalla vía `.label` tiene su entrada.

    Es la prueba del caso real: `JobStatus.PENDING` valía "Pendiente" sin
    marcar, no estaba en ningún catálogo, y un usuario en inglés veía
    "Pendiente" en la fila de una cola por lo demás traducida."""
    from wiibackup_manager.operations import OperationKind
    from wiibackup_manager.queue_manager import JobStatus

    presentes = _msgids(po)
    faltan = [f"{enum.__name__}.{m.name} = {m.value!r}"
              for enum in (JobStatus, OperationKind)
              for m in enum if m.value not in presentes]
    assert not faltan, (
        "valores de enum que no llegaron al catálogo (¿les falta `N_()`, o "
        "hay que correr tools/update-translations.sh?):\n  "
        + "\n  ".join(faltan))


def test_toda_cadena_marcada_con_N_llega_a_la_plantilla():
    """El otro lado del mismo arreglo: que `--keyword=N_` siga en
    `tools/update-translations.sh`. Si alguien lo saca, las cadenas
    marcadas dejan de extraerse y esto lo dice, en vez de que se note
    recién en una captura de pantalla en inglés a medias."""
    raiz = Path(__file__).resolve().parent.parent
    # Por `_entradas` y no con un `in` sobre el texto: `xgettext` parte las
    # cadenas largas en varias líneas, y un `msgid "..."` literal no las
    # encontraría.
    plantilla = _msgids(LOCALE_DIR / f"{DOMINIO}.pot")
    marcadas = set()
    for py in sorted((raiz / "wiibackup_manager").rglob("*.py")):
        marcadas |= set(re.findall(r'\bN_\(\s*"((?:[^"\\]|\\.)*)"\s*\)',
                                   py.read_text(encoding="utf-8")))
    assert marcadas, "no se encontró ninguna cadena marcada con N_()"
    faltan = [m for m in sorted(marcadas) if m not in plantilla]
    assert not faltan, (
        "marcadas con N_() pero ausentes de la plantilla: " + ", ".join(faltan))


@pytest.mark.parametrize("po", CATALOGOS, ids=lambda p: p.parent.parent.name)
def test_no_quedan_entradas_fuzzy(po):
    """Una entrada `#, fuzzy` NO se muestra: `msgfmt` la deja afuera del
    .mo y la app cae al español, igual que si no estuviera traducida. Y no
    la ve `test_no_quedan_cadenas_sin_traducir`, porque msgstr no está
    vacío -tiene la adivinanza de `msgmerge`, que suele ser cualquier
    cosa: al agregar estos enums propuso "Customer" para "Pendiente"-."""
    r = subprocess.run(["msgattrib", "--fuzzy", "--no-obsolete", str(po)],
                       capture_output=True, text=True, check=True)
    difusas = [m for m in re.findall(r'^msgid "((?:[^"\\]|\\.)+)"', r.stdout, re.M)]
    assert not difusas, (
        f"{len(difusas)} entradas fuzzy en {po.parent.parent.name} "
        "(revisarlas y sacarles la marca): " + ", ".join(difusas[:5]))
