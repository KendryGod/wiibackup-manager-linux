"""Lo que el paquete importa de terceros está declarado en pyproject.toml.

`pdf_export.py` hace `import cairo` (pycairo) al cargarse, y pycairo no
estaba en `dependencies`: un `pip install` en un entorno sin el paquete
del sistema dejaba la app sin poder generar el Ticket de Entrega, y un
empaquetador que lee `pyproject.toml` no tenía cómo enterarse.

Acá se recorren los imports de todo `wiibackup_manager` (con `ast`, sin
importar nada) y:

- lo que se importa siempre tiene que estar en `dependencies`, y lo que
  está en `dependencies` tiene que usarse;
- lo que se importa adentro de un `try/except ImportError` es opcional
  (el QR del ticket: `segno` o, si no, `qrcode`) y no se exige;
- `requirements.txt` declara lo mismo que `dependencies`;
- pycairo pide al menos 1.18: `PDFSurface.set_metadata` llegó en esa
  versión (con cairo 1.15.10 o más nuevo).
"""
from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
PAQUETE = RAIZ / "wiibackup_manager"

# Módulo que se importa -> distribución que lo instala. Uno nuevo que no
# esté acá hace fallar el test a propósito: hay que decidir si va en
# `dependencies`.
DISTRIBUCION = {"gi": "PyGObject", "cairo": "pycairo",
                "segno": "segno", "qrcode": "qrcode"}


def _atrapa_import_error(nodo: ast.Try) -> bool:
    for handler in nodo.handlers:
        tipos = handler.type
        nombres = ([tipos] if not isinstance(tipos, ast.Tuple) else tipos.elts) if tipos else []
        if any(isinstance(t, ast.Name) and t.id in ("ImportError", "ModuleNotFoundError")
               for t in nombres):
            return True
    return False


def _imports_de_terceros() -> dict:
    """{módulo de primer nivel: True si se importa siempre en algún lado}."""
    vistos: dict = {}

    def _visitar(nodo, opcional: bool):
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, ast.Try) and _atrapa_import_error(hijo):
                # Solo el cuerpo del `try` es opcional; lo de los `except`,
                # el `else` y el `finally` no.
                for sub in hijo.body:
                    _registrar(sub, True)
                    _visitar(sub, True)
                for sub in hijo.handlers + hijo.orelse + hijo.finalbody:
                    _registrar(sub, opcional)
                    _visitar(sub, opcional)
                continue
            _registrar(hijo, opcional)
            _visitar(hijo, opcional)

    def _registrar(nodo, opcional: bool):
        if isinstance(nodo, ast.Import):
            modulos = [a.name.split(".")[0] for a in nodo.names]
        elif isinstance(nodo, ast.ImportFrom) and nodo.level == 0 and nodo.module:
            modulos = [nodo.module.split(".")[0]]
        else:
            return
        for m in modulos:
            if m in sys.stdlib_module_names or m == "wiibackup_manager":
                continue
            vistos[m] = vistos.get(m, False) or not opcional

    for archivo in sorted(PAQUETE.rglob("*.py")):
        _visitar(ast.parse(archivo.read_text(encoding="utf-8")), False)
    return vistos


def _nombre(requisito: str) -> str:
    return re.split(r"[<>=!~;\[ ]", requisito, maxsplit=1)[0].strip()


def _proyecto() -> dict:
    return tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))["project"]


def test_cada_import_de_terceros_tiene_su_distribucion():
    sin_mapear = sorted(set(_imports_de_terceros()) - set(DISTRIBUCION))
    assert sin_mapear == [], f"imports de terceros sin decidir: {sin_mapear}"


def test_lo_que_se_importa_siempre_esta_en_dependencies_y_nada_mas():
    obligatorios = {DISTRIBUCION[m] for m, siempre in _imports_de_terceros().items() if siempre}
    declarados = {_nombre(r) for r in _proyecto()["dependencies"]}

    assert obligatorios == {"PyGObject", "pycairo"}
    assert declarados == obligatorios


def test_requirements_declara_lo_mismo():
    lineas = [l.strip() for l in (RAIZ / "requirements.txt").read_text().splitlines()
              if l.strip() and not l.lstrip().startswith("#")]

    assert sorted(lineas) == sorted(_proyecto()["dependencies"])


def test_pycairo_pide_al_menos_la_1_18():
    [pycairo] = [r for r in _proyecto()["dependencies"] if _nombre(r) == "pycairo"]
    minimo = re.search(r">=\s*(\d+)\.(\d+)", pycairo)

    assert minimo is not None, pycairo
    assert (int(minimo.group(1)), int(minimo.group(2))) >= (1, 18)


def test_el_qr_sigue_siendo_opcional():
    imports = _imports_de_terceros()

    assert imports.get("segno") is False and imports.get("qrcode") is False
