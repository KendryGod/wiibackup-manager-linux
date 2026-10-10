"""`drives.py` no define dos veces la misma constante de módulo.

Las rutas del sistema (`_SYS_BLOCK`, `_PROC_MOUNTS`…) son constantes de
módulo justamente para que las pruebas las apunten a un /sys falso con
monkeypatch. Si una se define dos veces, la segunda pisa a la primera al
importar: hoy valen lo mismo, pero cambiar una sola no tendría efecto y
nadie se enteraría. `pyflakes` no lo marca (solo mira imports y
funciones redefinidas)."""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import wiibackup_manager.drives as drives


def test_ninguna_constante_de_modulo_se_define_dos_veces():
    arbol = ast.parse(Path(drives.__file__).read_text(encoding="utf-8"))
    nombres = Counter(
        objetivo.id
        for nodo in arbol.body if isinstance(nodo, (ast.Assign, ast.AnnAssign))
        for objetivo in (nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target])
        if isinstance(objetivo, ast.Name) and objetivo.id.lstrip("_").isupper())
    repetidas = sorted(n for n, veces in nombres.items() if veces > 1)
    assert repetidas == [], f"definidas más de una vez en drives.py: {repetidas}"
