"""Pruebas de `fsutil.py`: ubicación de los datos instalados de la app.

La escritura atómica que vivía en este módulo (`atomic_target`) se mudó a
`atomicfs` junto con las demás primitivas de la misma familia, y sus
tests se mudaron con ella a `test_atomicfs.py`."""
from __future__ import annotations

from pathlib import Path

from wiibackup_manager import fsutil


# =================================================== installed_data_dirs --
def test_el_primer_candidato_es_el_repo_clonado():
    dirs = fsutil.installed_data_dirs("data/locale", "locale")
    paquete = Path(fsutil.__file__).resolve().parent
    assert dirs[0] == paquete.parent / "data" / "locale"


def test_no_devuelve_candidatos_repetidos():
    dirs = fsutil.installed_data_dirs("data/locale", "locale")
    assert len(dirs) == len(set(dirs))


def test_incluye_las_rutas_de_sistema():
    dirs = fsutil.installed_data_dirs("assets/configs", "wiibackup-manager/configs")
    assert Path("/usr/share/wiibackup-manager/configs") in dirs
    assert Path("/usr/local/share/wiibackup-manager/configs") in dirs
    assert Path.home() / ".local/share/wiibackup-manager/configs" in dirs


def test_la_cola_share_se_aplica_a_todos_los_prefijos():
    dirs = fsutil.installed_data_dirs("data/locale", "locale")
    # Todos menos el primero (el repo) cuelgan de algún .../share/locale
    for d in dirs[1:]:
        assert d.parent.name == "share"
        assert d.name == "locale"


def test_todas_las_rutas_son_absolutas_salvo_lo_que_dependa_del_repo():
    dirs = fsutil.installed_data_dirs("data/locale", "locale")
    assert all(d.is_absolute() for d in dirs)


def test_instalado_en_site_packages_deduce_el_prefijo(monkeypatch):
    """La parte sutil de esta función: con `pip install --user`,
    `sys.prefix` sigue siendo /usr, así que sin deducir el prefijo del
    propio paquete un catálogo viejo del sistema le ganaría al recién
    instalado. Corriendo desde el repo clonado esta rama no se ejecuta
    nunca, así que se simula moviendo el `__file__` del módulo."""
    monkeypatch.setattr(
        fsutil, "__file__",
        "/fake/prefijo/lib/python3.14/site-packages/wiibackup_manager/fsutil.py")

    dirs = fsutil.installed_data_dirs("data/locale", "locale")

    # Va inmediatamente después del repo y ANTES de sys.prefix y del resto.
    assert dirs[1] == Path("/fake/prefijo/share/locale")


def test_instalado_en_dist_packages_tambien_deduce_el_prefijo(monkeypatch):
    """Debian/Ubuntu usan "dist-packages" donde Fedora usa
    "site-packages"; las dos tienen que funcionar igual."""
    monkeypatch.setattr(
        fsutil, "__file__",
        "/fake/prefijo/lib/python3/dist-packages/wiibackup_manager/fsutil.py")

    dirs = fsutil.installed_data_dirs("assets/configs", "wiibackup-manager/configs")

    assert dirs[1] == Path("/fake/prefijo/share/wiibackup-manager/configs")
