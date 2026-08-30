"""Smoke test: que la app levante de verdad.

Los otros tests son de lógica pura y no tocan GTK. Este arranca la
aplicación completa —CSS, ventana, las tres pestañas, el escaneo inicial—
y comprueba que llega a mostrarse sin errores fatales. Es lo que atrapa
una regresión que las pruebas unitarias no ven: un `_()` que shadowea un
parámetro, un widget que se construye con un argumento que ya no existe,
un import circular.

Al final están los dos widgets del Recovery Manager (el aviso de la
ventana y su diálogo), acá y no en `test_recovery_service.py` por el mismo
motivo: la lógica de qué es un resto se prueba sin GTK, pero que la fila
se arme con los métodos que libadwaita realmente tiene solo se ve
construyéndola.

Necesita un display. En CI se corre bajo Xvfb (ver .github/workflows/ci.yml);
si no hay ninguno disponible, el test se saltea en vez de fallar, para que
correr `pytest` en una terminal sin sesión gráfica siga sirviendo.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.gui


def _hay_display() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def _no_disponible(motivo: str):
    """Saltea el test, o lo hace fallar si el entorno prometía un display.

    En CI el workflow exporta WBM_REQUIRE_GUI=1: ahí un skip sería peor
    que un fallo, porque el check saldría verde sin que el smoke test
    haya corrido nunca -exactamente el escenario que este test existe
    para evitar-. En una terminal sin sesión gráfica, en cambio, saltear
    es lo correcto."""
    if os.environ.get("WBM_REQUIRE_GUI") == "1":
        pytest.fail(f"WBM_REQUIRE_GUI=1 pero {motivo}")
    pytest.skip(motivo)


@pytest.fixture(scope="module")
def gtk():
    if not _hay_display():
        _no_disponible("sin display: el smoke test necesita Xvfb o una sesión gráfica")
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Gtk
    if not Gtk.init_check():
        _no_disponible("GTK no pudo inicializarse con este display")
    return gi


def test_la_ventana_se_arma_y_se_muestra(gtk, tmp_path, monkeypatch):
    """Levanta la app entera y espera a que la ventana esté presentada."""
    from gi.repository import Adw, GLib

    from wiibackup_manager import config, oscwii_client
    from wiibackup_manager.styles import load_css
    from wiibackup_manager.window import WiiBackupWindow

    biblioteca = tmp_path / "biblioteca"
    biblioteca.mkdir()
    monkeypatch.setattr(config.Settings, "load",
                        classmethod(lambda cls: config.Settings(
                            library_path=str(biblioteca))))
    # La página Homebrew Store pide el catálogo de OSC apenas se construye
    # (ver HomebrewStoreView._load_apps). Sin este parche, cada corrida del
    # smoke test dispararía un pedido HTTP real contra oscwii.org -lento,
    # dependiente de red, y que puede quedar corriendo en el pool
    # compartido del módulo más allá de que este test ya haya terminado.
    monkeypatch.setattr(oscwii_client, "fetch_apps_async", lambda on_done=None: None)

    resultado = {}

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id="com.gamefixsps.WiiBackupManager.Test")

        def do_activate(self):
            load_css()
            win = WiiBackupWindow(self)
            win.present()
            GLib.timeout_add(2500, self._revisar, win)

        def _revisar(self, win):
            # `finally: self.quit()` es a propósito: una excepción acá
            # (por ejemplo un atributo que ya no existe) no puede dejar el
            # bucle de GLib corriendo hasta el timeout de guardia de 60s
            # -mejor que el test falle en el acto con su traceback real.
            try:
                resultado["titulo"] = win.get_title()
                resultado["visible"] = win.get_visible()
                # Las 5 páginas del sidebar (Juegos/Cola de Tareas/Modo
                # Fábrica/Homebrew Store/Ajustes) tienen que existir dentro
                # del stack de contenido, cada una con su propia fila en el
                # sidebar.
                resultado["paginas"] = {
                    pid for pid in ("juegos", "cola", "memoria", "fabrica",
                                    "tienda", "ajustes")
                    if win._content_stack.get_child_by_name(pid) is not None
                }
                resultado["filas_sidebar"] = [pid for pid, _icon, _lbl in win._sidebar_items]
            finally:
                self.quit()
            return False

    # Un timeout de guardia: si la app se cuelga, el test tiene que fallar
    # con un mensaje claro y no dejar la suite colgada para siempre.
    GLib.timeout_add_seconds(60, lambda: (resultado.setdefault("colgada", True),
                                          False)[1])

    App().run([])

    assert not resultado.get("colgada"), "la app no terminó de arrancar"
    assert resultado.get("visible") is True
    assert "WiiBackup Manager" in resultado.get("titulo", "")
    assert resultado.get("paginas") == {"juegos", "cola", "memoria", "fabrica",
                                        "tienda", "ajustes"}
    assert resultado.get("filas_sidebar") == ["juegos", "cola", "memoria",
                                              "fabrica", "tienda", "ajustes"]


def test_la_ventana_tambien_arranca_en_ingles(gtk, tmp_path, monkeypatch):
    """El mismo arranque con el catálogo inglés cargado: comprueba que
    ninguna cadena traducida rompe la construcción de la interfaz."""
    import gettext
    from pathlib import Path

    from gi.repository import Adw, GLib

    from wiibackup_manager import config, i18n, oscwii_client
    from wiibackup_manager.styles import load_css

    locale_dir = Path(__file__).resolve().parent.parent / "data" / "locale"
    en = gettext.translation("wiibackup-manager", str(locale_dir), languages=["en"])
    monkeypatch.setattr(i18n, "_", en.gettext)

    # window.py importó `_` por valor, así que se parchea también ahí y en
    # los widgets que lo usan al construir la interfaz.
    from wiibackup_manager import window as window_mod
    from wiibackup_manager.widgets import homebrew_store_view, log_view, transfer_view
    for mod in (window_mod, homebrew_store_view, log_view, transfer_view):
        monkeypatch.setattr(mod, "_", en.gettext)

    biblioteca = tmp_path / "biblioteca"
    biblioteca.mkdir()
    monkeypatch.setattr(config.Settings, "load",
                        classmethod(lambda cls: config.Settings(
                            library_path=str(biblioteca))))
    # Ver el comentario equivalente en el test anterior: sin esto, este
    # test también dispararía un pedido HTTP real contra oscwii.org.
    monkeypatch.setattr(oscwii_client, "fetch_apps_async", lambda on_done=None: None)

    resultado = {}

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id="com.gamefixsps.WiiBackupManager.TestEn")

        def do_activate(self):
            load_css()
            win = window_mod.WiiBackupWindow(self)
            win.present()
            GLib.timeout_add(2500, self._revisar, win)

        def _revisar(self, win):
            try:
                resultado["paginas"] = {
                    pid for pid in ("juegos", "cola", "memoria", "fabrica",
                                    "tienda", "ajustes")
                    if win._content_stack.get_child_by_name(pid) is not None
                }
                resultado["vacio"] = win.status_page.get_title()
            finally:
                self.quit()
            return False

    # Misma guardia que en el test anterior: sin esto, una excepción
    # dentro de `_revisar` deja el bucle de GLib corriendo para siempre
    # (no hay timeout de guardia acá arriba que lo corte).
    GLib.timeout_add_seconds(60, lambda: (resultado.setdefault("colgada", True), False)[1])

    App().run([])

    assert not resultado.get("colgada"), "la app no terminó de arrancar"
    assert resultado.get("paginas") == {"juegos", "cola", "memoria", "fabrica",
                                        "tienda", "ajustes"}
    assert resultado.get("vacio") == "No games yet"


# ------------------------------------------------------- Recovery Manager --
def _pid_muerto() -> int:
    """Un PID que con seguridad ya no corre (ver el fixture equivalente en
    `test_recovery_service.py`)."""
    import subprocess
    import sys
    p = subprocess.Popen([sys.executable, "-c", ""])
    p.wait()
    return p.pid


def test_el_aviso_de_restos_aparece_con_lo_que_encuentra_el_escaneo(
        gtk, tmp_path, monkeypatch):
    """La ventana entera, con un respaldo huérfano en la biblioteca: el
    escaneo de fondo tiene que terminar, revelar el banner y contar lo que
    encontró. Y con la biblioteca limpia, no revelarlo -el silencio total
    es la mitad del comportamiento pedido."""
    from gi.repository import Adw, GLib

    from wiibackup_manager import config, oscwii_client, recovery_service
    from wiibackup_manager.styles import load_css
    from wiibackup_manager.window import WiiBackupWindow

    biblioteca = tmp_path / "biblioteca"
    biblioteca.mkdir()
    (biblioteca / f".Juego.wbfs.respaldo-{_pid_muerto()}").write_bytes(b"\0" * 2048)
    monkeypatch.setattr(config.Settings, "load",
                        classmethod(lambda cls: config.Settings(
                            library_path=str(biblioteca))))
    monkeypatch.setattr(oscwii_client, "fetch_apps_async", lambda on_done=None: None)
    # Sin esto, el test recorrería los USB que tenga conectados quien lo
    # corra: lento, y el resultado dependería de la máquina.
    monkeypatch.setattr(recovery_service, "scan_roots",
                        lambda settings=None, **kw: [biblioteca])

    resultado = {}

    class App(Adw.Application):
        def __init__(self):
            super().__init__(application_id="com.gamefixsps.WiiBackupManager.TestRec")

        def do_activate(self):
            load_css()
            win = WiiBackupWindow(self)
            win.present()
            GLib.timeout_add(2500, self._revisar, win)

        def _revisar(self, win):
            try:
                resultado["revelado"] = win._recovery_banner.get_revealed()
                resultado["titulo"] = win._recovery_banner.get_title()
                resultado["restos"] = len(win._recovery_leftovers)
                # Resolver el único resto tiene que hacer desaparecer el
                # aviso, sin esperar a otro escaneo.
                win._on_recovery_resolved(win._recovery_leftovers[0])
                resultado["revelado_despues"] = win._recovery_banner.get_revealed()
            finally:
                self.quit()
            return False

    GLib.timeout_add_seconds(60, lambda: (resultado.setdefault("colgada", True),
                                          False)[1])
    App().run([])

    assert not resultado.get("colgada"), "la app no terminó de arrancar"
    assert resultado.get("restos") == 1
    assert resultado.get("revelado") is True
    assert "1 resto" in resultado.get("titulo", "")
    assert resultado.get("revelado_despues") is False


def test_el_dialogo_de_restos_se_arma_y_sus_acciones_funcionan(gtk, tmp_path):
    """El diálogo con un respaldo y una staging: se construye una fila por
    resto (con "Restaurar" solo en el que se puede), restaurar deja el
    archivo en su nombre real, e ignorar no toca el disco."""
    from wiibackup_manager import recovery_service
    from wiibackup_manager.widgets.recovery_dialog import RecoveryDialog

    pid = _pid_muerto()
    respaldo = tmp_path / f".Juego.wbfs.respaldo-{pid}"
    respaldo.write_bytes(b"el juego del cliente")
    staging = tmp_path / f".WiiDonut.wbm-staging-{pid}"
    staging.mkdir()
    (staging / "boot.dol").write_bytes(b"\0" * 16)

    restos = recovery_service.scan([tmp_path])
    assert len(restos) == 2
    por_tipo = {lo.kind: lo for lo in restos}

    toasts, resueltos = [], []
    dialog = RecoveryDialog(restos, ops=None, show_toast=toasts.append,
                            on_resolved=resueltos.append)

    # Restaurar: el nombre original está libre, así que no hay diálogo de
    # confirmación de por medio y el archivo vuelve en el acto.
    dialog._on_restore(por_tipo[recovery_service.LeftoverKind.BACKUP])
    assert (tmp_path / "Juego.wbfs").read_bytes() == b"el juego del cliente"
    assert not respaldo.exists()

    # Ignorar: sale de la lista sin tocar nada en el disco.
    dialog._on_ignore(por_tipo[recovery_service.LeftoverKind.HOMEBREW_STAGING])
    assert staging.exists()

    assert len(resueltos) == 2
    assert len(toasts) == 1  # solo el de restaurar; ignorar no avisa nada
