"""Un selector que devuelve una ubicación sin ruta local no rompe nada.

Un `Gio.File` de una ubicación sin ruta local (un teléfono por MTP, un
recurso SMB sin gvfs-fuse) devuelve None en `get_path()`, y `Path(None)`
levantaba TypeError adentro del callback del selector: no pasaba nada y
quedaba solo un traceback en la terminal (en Preferencias, además, la ruta
de la biblioteca quedaba en None). Ahora se avisa y no se cambia nada.
"""
from __future__ import annotations

import types

import pytest

from wiibackup_manager import config


class _SinRuta:
    def get_path(self):
        return None


_DIALOGO = types.SimpleNamespace(
    select_folder_finish=lambda _r: _SinRuta(),
    save_finish=lambda _r: _SinRuta(),
    open_multiple_finish=lambda _r: types.SimpleNamespace(
        get_n_items=lambda: 1, get_item=lambda _i: _SinRuta()))


def _vista(**kw):
    toasts = []
    base = dict(_show_toast=toasts.append, toasts=toasts)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.mark.parametrize("metodo", ["_on_library_picked", "_on_wbfs_picked"])
def test_preferencias(tmp_path, metodo):
    from wiibackup_manager.widgets.preferences_dialog import PreferencesDialog

    avisos = []
    ajustes = config.Settings(library_path=str(tmp_path), wbfs_drive_path="/x")
    fila = types.SimpleNamespace(set_subtitle=lambda t: avisos.append(("fila", t)))
    dialogo = types.SimpleNamespace(settings=ajustes, _library_row=fila, _wbfs_row=fila,
                                    add_toast=lambda t: avisos.append(t.get_title()))

    getattr(PreferencesDialog, metodo)(dialogo, _DIALOGO, None)

    assert ajustes.library_path == str(tmp_path) and ajustes.wbfs_drive_path == "/x"
    assert len(avisos) == 1 and "ruta local" in avisos[0]


def test_destino_de_la_tienda():
    from wiibackup_manager.widgets.homebrew_store_view import HomebrewStoreView

    vista = _vista(_manual_path=None, _refresh_dest_choices=lambda: None)
    HomebrewStoreView._on_folder_picked(vista, _DIALOGO, None)

    assert vista._manual_path is None and "ruta local" in vista.toasts[0]


def test_guardar_el_ticket(tmp_path):
    from wiibackup_manager.widgets.transfer_view import TransferView

    vista = _vista(_dest_path=tmp_path, _update_ticket_button=lambda: None)
    TransferView._on_ticket_file_chosen(vista, _DIALOGO, None, "", "", None)

    assert "ruta local" in vista.toasts[0]


def test_destino_manual_de_transferir():
    from wiibackup_manager.widgets.transfer_view import TransferView

    filas = []
    vista = _vista(dest_list=types.SimpleNamespace(append=filas.append,
                                                   select_row=lambda _f: None))
    TransferView._on_folder_picked(vista, _DIALOGO, None)

    assert filas == [] and "ruta local" in vista.toasts[0]


def test_envio_en_lote():
    from wiibackup_manager.window import WiiBackupWindow

    vista = _vista()
    WiiBackupWindow._on_batch_send_folder_chosen(vista, _DIALOGO, None, [])

    assert "ruta local" in vista.toasts[0]


def test_agregar_archivos_y_carpeta():
    from wiibackup_manager.window import WiiBackupWindow

    importados = []
    vista = _vista(_start_import=importados.append)
    WiiBackupWindow._on_files_chosen(vista, _DIALOGO, None)
    WiiBackupWindow._on_folder_chosen(vista, _DIALOGO, None)

    assert importados == []
    assert len(vista.toasts) == 2 and all("ruta local" in t for t in vista.toasts)
