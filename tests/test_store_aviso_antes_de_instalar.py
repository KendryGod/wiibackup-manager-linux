"""La Store muestra dónde va a escribir antes de instalar, y lo anota.

Cuando un paquete escribe fuera de `apps/` y `controllers/`, omite algo o
choca con algo que el usuario ya tiene, `install_app` llama a
`confirmar(aviso)` DESPUÉS de bajar y validar el paquete y ANTES de tocar
la unidad:

- None: no se instala y la unidad queda como estaba;
- False: se instala conservando los archivos distintos del usuario (lo
  que el diálogo ofrece por defecto);
- True: se instala reemplazándolos (uno de 0 bytes, nunca).

Una app que solo escribe en `apps/` no pregunta nada, como siempre. Y el
historial dice qué se escribió fuera de `apps/`.
"""
from __future__ import annotations

import os
import threading
import types
import zipfile
from pathlib import Path

import pytest

from homebrew_falso import dol
from wiibackup_manager import drives, oplog, oscwii_installer
from wiibackup_manager.operations import OperationBusy, OperationKind, OperationManager
from wiibackup_manager.oscwii_client import HomebrewApp
from wiibackup_manager.oscwii_installer import AvisoDeInstalacion, InstallResult, InstallStatus

SMW = {
    "apps/smw-wiigii/boot.dol": dol(b"smw"),
    "smw/sfx/a.wav": b"RIFF a",
    "smw/sfx/b.wav": b"RIFF b",
    "smw/worlds/Contest.txt": b"mundo del paquete",
    "mame/mame.ini": b"ini del paquete",
    "wiicolem/wiicolem.conf": b"",
    "usr/local/lib/timidity/Drum_000/kick.pat": b"pat",
    "readme.txt": b"leeme",
}


def _app() -> HomebrewApp:
    return HomebrewApp(slug="smw-wiigii", name="Super Mario War",
                       zip_url="https://hbb1.oscwii.org/api/contents/smw-wiigii/x.zip")


@pytest.fixture
def paquete(monkeypatch):
    entradas = dict(SMW)

    def _falsa(app, destino, cancel_event, on_progress):
        with zipfile.ZipFile(destino, "w") as zf:
            for nombre, datos in entradas.items():
                zf.writestr(nombre, datos)
        return True, "", "", ""

    monkeypatch.setattr(oscwii_installer, "_download_zip", _falsa)
    return entradas


@pytest.fixture
def raiz(tmp_path):
    r = tmp_path / "usb"
    r.mkdir()
    return r


def _poner(raiz: Path, archivos: dict) -> None:
    for nombre, datos in archivos.items():
        (raiz / nombre).parent.mkdir(parents=True, exist_ok=True)
        (raiz / nombre).write_bytes(datos)


def _foto(raiz: Path) -> dict:
    return {str(p.relative_to(raiz)): (p.read_bytes() if p.is_file() else None)
            for p in sorted(raiz.rglob("*"))}


class _Confirmar:
    """Anota cada aviso y la foto de la unidad en ese momento, y responde
    `respuesta`."""

    def __init__(self, raiz, respuesta):
        self.raiz, self.respuesta = raiz, respuesta
        self.avisos, self.fotos = [], []

    def __call__(self, aviso):
        self.avisos.append(aviso)
        self.fotos.append(_foto(self.raiz))
        return self.respuesta


# ------------------------------------------------------- cuándo pregunta --
def test_una_app_que_solo_escribe_en_apps_no_pregunta(raiz, paquete):
    paquete.clear()
    paquete.update({"apps/WiiDonut/boot.dol": dol(b"x"),
                    "controllers/perfil.ini": b"[p]"})
    confirmar = _Confirmar(raiz, None)

    r = oscwii_installer.install_app(_app(), raiz, confirmar=confirmar)

    assert r.ok, r.error
    assert confirmar.avisos == []


def test_pregunta_antes_de_tocar_la_unidad(raiz, paquete):
    _poner(raiz, {"smw/worlds/MiMundo.txt": b"mio"})
    antes = _foto(raiz)
    confirmar = _Confirmar(raiz, False)

    r = oscwii_installer.install_app(_app(), raiz, confirmar=confirmar)

    assert r.ok, r.error
    assert confirmar.fotos == [antes]


def test_el_aviso_dice_donde_escribe_y_que_omite(raiz, paquete):
    _poner(raiz, {"mame/mame.ini": b"mi configuracion",
                  "wiicolem/wiicolem.conf": b"mi conf",
                  "WiiFlow/x.ini": b"del usuario"})
    paquete["wiiflow/plugins/smw.ini"] = b"[smw]"
    confirmar = _Confirmar(raiz, False)

    oscwii_installer.install_app(_app(), raiz, confirmar=confirmar)

    [aviso] = confirmar.avisos
    assert aviso.reemplaza == ("apps/smw-wiigii/",)
    assert aviso.carpetas == (("smw/", 3), ("usr/local/lib/timidity/", 1),
                              ("WiiFlow/", 1))
    assert aviso.distintos == ("mame/mame.ini",)
    assert aviso.intocables == ("wiicolem/wiicolem.conf",)
    assert aviso.omitidos == ("readme.txt",)


def test_si_solo_omite_algo_tambien_pregunta(raiz, paquete):
    paquete.clear()
    paquete.update({"apps/gnuboy/boot.dol": dol(b"x"), "history.txt": b"h"})
    confirmar = _Confirmar(raiz, False)

    r = oscwii_installer.install_app(_app(), raiz, confirmar=confirmar)

    assert r.ok, r.error
    [aviso] = confirmar.avisos
    assert aviso.carpetas == () and aviso.omitidos == ("history.txt",)


def test_si_fuera_de_apps_ya_esta_todo_igual_no_pregunta(raiz, paquete):
    del paquete["readme.txt"]
    oscwii_installer.install_app(_app(), raiz)
    confirmar = _Confirmar(raiz, None)

    r = oscwii_installer.install_app(_app(), raiz, confirmar=confirmar)

    assert r.ok, r.error
    assert confirmar.avisos == []


# ---------------------------------------------------------- respuestas --
def test_cancelar_en_el_aviso_no_escribe_nada(raiz, paquete):
    _poner(raiz, {"mame/mame.ini": b"mi configuracion"})
    antes = _foto(raiz)

    r = oscwii_installer.install_app(_app(), raiz, confirmar=_Confirmar(raiz, None))

    assert r.status is InstallStatus.CANCELLED, r
    assert _foto(raiz) == antes


@pytest.mark.parametrize("respuesta, queda", [
    (False, b"mi configuracion"),
    (True, b"ini del paquete"),
])
def test_la_respuesta_decide_si_se_reemplaza(raiz, paquete, respuesta, queda):
    _poner(raiz, {"mame/mame.ini": b"mi configuracion",
                  "wiicolem/wiicolem.conf": b"mi conf"})

    r = oscwii_installer.install_app(_app(), raiz, confirmar=_Confirmar(raiz, respuesta))

    assert r.ok, r.error
    assert (raiz / "mame/mame.ini").read_bytes() == queda
    assert (raiz / "wiicolem/wiicolem.conf").read_bytes() == b"mi conf"


# --------------------------------------------------------- el texto --
def _aviso(**kw) -> AvisoDeInstalacion:
    base = dict(reemplaza=("apps/smw-wiigii/",), carpetas=(("smw/", 1298),),
                distintos=(), intocables=(), omitidos=())
    base.update(kw)
    return AvisoDeInstalacion(**base)


def test_el_texto_lista_carpetas_y_aclara_que_no_borra():
    from wiibackup_manager.widgets.homebrew_store_view import _texto_del_aviso

    texto = _texto_del_aviso(_aviso(omitidos=("readme.txt",)))

    assert "apps/smw-wiigii/" in texto
    assert "smw/: 1298 archivos" in texto
    assert "no se borra nada" in texto
    assert "readme.txt" in texto


def test_el_texto_lista_los_distintos_y_corta_las_listas_largas():
    from wiibackup_manager.widgets.homebrew_store_view import _texto_del_aviso

    distintos = tuple(f"mame/cfg/{i}.cfg" for i in range(20))
    texto = _texto_del_aviso(_aviso(distintos=distintos,
                                    intocables=("wiicolem/wiicolem.conf",)))

    assert "mame/cfg/0.cfg" in texto
    assert "mame/cfg/19.cfg" not in texto
    assert "y 12 más" in texto
    assert "wiicolem/wiicolem.conf" in texto


# ---------------------------------------------------------- historial --
def test_el_historial_dice_que_se_escribio_fuera_de_apps(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    historial = oplog.OperationLog(path=tmp_path / "history.json")
    ops = OperationManager(log=historial)
    op = ops.start(OperationKind.INSTALLING_HOMEBREW, write=[tmp_path / "apps" / "x"])
    vista = types.SimpleNamespace(ops=ops, op_log=historial, _show_toast=lambda _m: None,
                                  _install_states={}, _card_by_slug={})
    resultado = InstallResult(
        InstallStatus.OK, "smw-wiigii", installed_paths=("a",) * 5,
        datos_escritos=("b",) * 4, datos_conservados=("c",),
        carpetas_de_datos=(("smw/", 3), ("usr/local/lib/timidity/", 1)),
        omitidos=("readme.txt",))

    HomebrewStoreView._on_install_done(vista, _app(), resultado, op)

    [entrada] = historial.entries()
    assert "fuera de apps/" in entrada.detail
    assert "smw/ (3)" in entrada.detail
    assert "usr/local/lib/timidity/ (1)" in entrada.detail
    assert "1 archivo tuyo" in entrada.detail


def test_sin_nada_fuera_de_apps_el_historial_queda_como_antes(tmp_path):
    from wiibackup_manager.widgets.homebrew_store_view import _install_outcome

    salida = _install_outcome(_app(), InstallResult(InstallStatus.OK, "x",
                                                     installed_paths=("a", "b")))

    assert salida.detail == "2 archivo(s)"


def test_la_instalacion_real_informa_las_carpetas(raiz, paquete):
    r = oscwii_installer.install_app(_app(), raiz)

    assert r.ok, r.error
    assert r.carpetas_de_datos == (("smw/", 3), ("mame/", 1), ("wiicolem/", 1),
                                   ("usr/local/lib/timidity/", 1))


# ------------------------------------------------------------- bloqueo --
def test_otra_operacion_no_puede_escribir_en_una_carpeta_de_datos_mientras_tanto(tmp_path):
    """No hace falta declarar las carpetas de datos: la instalación ya ocupa
    el destino entero como recurso (`drives.resources_for_mount_point`), y
    nadie escribe adentro de un recurso ocupado."""
    raiz = tmp_path / "usb"
    raiz.mkdir()
    ops = OperationManager()
    ops.start(OperationKind.INSTALLING_HOMEBREW, write=[raiz / "apps" / "smw-wiigii"],
              resources=drives.resources_for_mount_point(raiz))

    with pytest.raises(OperationBusy):
        ops.check(OperationKind.DELETING, write=[raiz / "smw" / "worlds" / "x.txt"])


# --------------------------------------------------------------- GTK --
@pytest.mark.gui
def test_el_dialogo_ofrece_conservar_por_defecto():
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("sin display")
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    if not Gtk.init_check():
        pytest.skip("GTK no pudo inicializarse")
    Adw.init()
    from wiibackup_manager.widgets.homebrew_store_view import _dialogo_del_aviso

    con = _dialogo_del_aviso("Super Mario War", _aviso(distintos=("mame/mame.ini",)),
                             lambda _r: None)
    sin = _dialogo_del_aviso("Super Mario War", _aviso(), lambda _r: None)

    assert con.get_default_response() == "keep"
    assert con.get_close_response() == "cancel"
    assert con.has_response("replace")
    assert (con.get_response_appearance("replace")
            == Adw.ResponseAppearance.DESTRUCTIVE)
    assert sin.get_default_response() == "install"
    assert not sin.has_response("replace")


@pytest.mark.gui
@pytest.mark.parametrize("respuesta, esperado", [
    ("cancel", None), ("keep", False), ("replace", True), ("install", False)])
def test_cada_respuesta_del_dialogo(respuesta, esperado):
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        pytest.skip("sin display")
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gtk
    if not Gtk.init_check():
        pytest.skip("GTK no pudo inicializarse")
    Adw.init()
    from wiibackup_manager.widgets.homebrew_store_view import _dialogo_del_aviso

    recibido = []
    dialogo = _dialogo_del_aviso("Super Mario War", _aviso(distintos=("m",)),
                                 recibido.append)
    dialogo.emit("response", respuesta)

    assert recibido == [esperado]


def test_al_terminar_se_cierra_el_aviso_que_quedo_abierto(tmp_path):
    """Cancelar desde la tarjeta con el aviso abierto contesta "no instalar"
    (`confirmar` mira el `cancel_event`); el aviso no puede quedar colgado."""
    from wiibackup_manager.widgets.homebrew_store_view import (
        HomebrewStoreView, _CardState, _InstallState)

    cerrados = []
    ops = OperationManager()
    op = ops.start(OperationKind.INSTALLING_HOMEBREW, write=[tmp_path / "apps" / "x"])
    estado = _InstallState(kind=_CardState.DOWNLOADING,
                           dialogo=types.SimpleNamespace(force_close=lambda: cerrados.append(1)))
    vista = types.SimpleNamespace(ops=ops, op_log=None, _show_toast=lambda _m: None,
                                  _install_states={"smw-wiigii": estado}, _card_by_slug={})

    HomebrewStoreView._on_install_done(
        vista, _app(), InstallResult(InstallStatus.CANCELLED, "smw-wiigii"), op)

    assert cerrados == [1]


@pytest.mark.parametrize("respuesta", [None, False, True])
def test_el_hilo_recibe_lo_que_se_contesto(respuesta):
    from wiibackup_manager.widgets.homebrew_store_view import _preguntar_y_esperar

    assert _preguntar_y_esperar(lambda responder: responder(respuesta),
                                threading.Event(), intervalo=0.01) is respuesta


def test_cancelar_desde_la_tarjeta_contesta_no_instalar():
    from wiibackup_manager.widgets.homebrew_store_view import _preguntar_y_esperar

    cancelar = threading.Event()
    threading.Timer(0.05, cancelar.set).start()

    assert _preguntar_y_esperar(lambda _responder: None, cancelar, intervalo=0.01) is None


def test_si_solo_choca_con_un_archivo_que_se_conserva_siempre_tambien_pregunta(raiz, paquete):
    """Reinstalar con todo igual menos un `wiicolem.conf` que el usuario
    configuró: el paquete lo trae vacío y no lo va a tocar, pero hay que
    decirlo."""
    del paquete["readme.txt"]
    oscwii_installer.install_app(_app(), raiz)
    (raiz / "wiicolem/wiicolem.conf").write_bytes(b"mi conf")
    confirmar = _Confirmar(raiz, False)

    oscwii_installer.install_app(_app(), raiz, confirmar=confirmar)

    [aviso] = confirmar.avisos
    assert aviso.intocables == ("wiicolem/wiicolem.conf",)
    assert aviso.carpetas == () and aviso.distintos == ()
