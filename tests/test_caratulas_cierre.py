"""Copiar carátulas no deja temporales a medias al cerrar ni tras un corte.

Cada carátula se escribe a un temporal oculto `.<ID6>.png.parcial-XXXX` y
entra con un rename. Al cerrar la ventana, `transfer_view.shutdown()` solo
pedía cortar (se mira entre carátula y carátula) y la ventana se cerraba
en el acto: el hilo daemon podía morir con el temporal a medias. Y el
Recovery Manager no lo ve: no baja más de `MAX_DEPTH` (3) niveles, y
`apps/usbloader_gx/images/2D/` es el cuarto.

Ahora:
- al cerrar con una copia de carátulas en curso, se pide cortar y se
  espera a que termine la que está escribiendo (pocos KB), sin preguntar y
  con el mismo tope que el resto (`CLOSE_WAIT_SECONDS`);
- `copy_drive_covers` borra al empezar los temporales propios huérfanos de
  images/2D, que es lo que cubre un kill -9 o un corte de luz. Es una
  limpieza dirigida: subir `MAX_DEPTH` haría que el escaneo de arranque
  recorriera un nivel más en todas las unidades.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_window_close import _VentanaDeMentira
from wiibackup_manager import config, usbloader_covers
from wiibackup_manager.fsutil import PNG_MAGIC
from wiibackup_manager.operations import OperationKind, OperationManager

_IEND = b"\x00\x00\x00\x00IEND\xaeB`\x82"


# ------------------------------------------------------ Al cerrar --
@pytest.fixture
def ventana(monkeypatch):
    from wiibackup_manager.window import Adw
    dialogos = []
    monkeypatch.setattr(Adw.AlertDialog, "present",
                        lambda self, parent=None: dialogos.append(self))
    v = _VentanaDeMentira(OperationManager())
    v.dialogos = dialogos
    return v


def test_cerrar_con_una_copia_de_caratulas_espera_sin_preguntar(ventana):
    op = ventana.ops.start(OperationKind.COPYING_COVERS, resources=["/run/media/usb"])
    al_cerrar = []
    ventana.close = lambda: (ventana.eventos.append("close"),
                             al_cerrar.append(ventana.ops.active_operations()))
    temporizador = ventana._close_timer
    vueltas = []

    def _timer(ms, funcion):
        # La carátula en curso termina en la segunda vuelta del sondeo.
        def _vuelta():
            vueltas.append(1)
            if len(vueltas) == 2:
                ventana.ops.finish(op)
            return funcion()
        return temporizador(ms, _vuelta)

    ventana._close_timer = _timer

    frenado = ventana._on_close_request()

    assert frenado is True, "cerró sin esperar a la carátula en curso"
    assert ventana.dialogos == [], "preguntó por unos KB"
    assert ventana.eventos == ["shutdown:transfer", "shutdown:homebrew",
                               "shutdown:memory", "close"]
    assert al_cerrar == [[]], "cerró con la copia todavía en curso"


def test_si_no_termina_cierra_igual_al_vencer_el_plazo(ventana):
    ventana.ops.start(OperationKind.COPYING_COVERS, resources=["/run/media/usb"])

    assert ventana._on_close_request() is True
    assert ventana.eventos[-1] == "close"
    assert ventana.reloj >= 3.0 - 1e-9


def test_sin_copia_de_caratulas_cierra_como_siempre(ventana):
    assert ventana._on_close_request() is False
    assert ventana.eventos == ["shutdown:transfer", "shutdown:homebrew",
                               "shutdown:memory"]


# --------------------------------------------- Huérfanos de un corte --
@pytest.fixture
def cache(tmp_path, monkeypatch):
    carpeta = tmp_path / "covers"
    carpeta.mkdir()
    monkeypatch.setattr(config, "COVERS_DIR", carpeta)
    (carpeta / "RMCE01.EN.png").write_bytes(PNG_MAGIC + b"mario" + _IEND)
    return carpeta


def _unidad(tmp_path: Path) -> Path:
    raiz = tmp_path / "usb"
    (raiz / "wbfs" / "RMCE01").mkdir(parents=True)
    (raiz / "wbfs" / "RMCE01" / "RMCE01.wbfs").write_bytes(b"juego")
    (raiz / "apps" / "usbloader_gx" / "images" / "2D").mkdir(parents=True)
    return raiz


def test_un_parcial_huerfano_se_limpia_sin_tocar_lo_del_cliente(tmp_path, cache):
    raiz = _unidad(tmp_path)
    carpeta = raiz / "apps" / "usbloader_gx" / "images" / "2D"
    huerfanos = [carpeta / ".RMCE01.png.parcial-k2j4h1", carpeta / ".SMNE01.png.parcial-x9"]
    for h in huerfanos:
        h.write_bytes(b"\x89PNG a med")
    del_cliente = {
        "SMNE01.png": b"png real",
        ".oculto-del-cliente": b"no es nuestro",
        "notas.txt": b"del cliente",
        ".RMCE01.png.respaldo": b"otro nombre: no es nuestro",
        ".rmce01.png.parcial-otra": b"minusculas: no es un ID6 nuestro",
    }
    for nombre, datos in del_cliente.items():
        (carpeta / nombre).write_bytes(datos)
    (carpeta / ".SMNE02.png.parcial-carpeta").mkdir()
    (carpeta / ".SMNE03.png.parcial-enlace").symlink_to(carpeta / "notas.txt")

    r = usbloader_covers.copy_drive_covers(raiz, "EN")

    assert not any(h.exists() for h in huerfanos)
    for nombre, datos in del_cliente.items():
        assert (carpeta / nombre).read_bytes() == datos, nombre
    assert (carpeta / ".SMNE02.png.parcial-carpeta").is_dir()
    assert (carpeta / ".SMNE03.png.parcial-enlace").is_symlink()
    assert r.copied == ["RMCE01"]
    assert (carpeta / "RMCE01.png").read_bytes() == PNG_MAGIC + b"mario" + _IEND


def test_sin_usb_loader_gx_no_limpia_nada(tmp_path, cache):
    raiz = tmp_path / "usb"
    otra = raiz / "otra" / "images" / "2D"
    otra.mkdir(parents=True)
    (otra / ".RMCE01.png.parcial-k2").write_bytes(b"x")

    usbloader_covers.copy_drive_covers(raiz, "EN")

    assert (otra / ".RMCE01.png.parcial-k2").exists()
