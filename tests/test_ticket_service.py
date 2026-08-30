"""Pruebas del Ticket de Entrega: el conteo del contenido de una unidad
(`ticket_service`) y el PDF que sale de eso (`pdf_export`).

Las unidades de prueba se arman de verdad en `tmp_path`, con la misma
estructura de carpetas que deja la app al copiar (`wbfs/<ID6>/<ID6>.wbfs`,
`games/<Título [ID6]>/game.iso`, `apps/<App>/boot.dol`). Es a propósito:
lo que se está probando es justamente la lectura de esa estructura, así
que un mock del filesystem probaría el mock y no el conteo.
"""
from __future__ import annotations

import re
import shutil
import unicodedata
from datetime import datetime
from pathlib import Path

import pytest

from wiibackup_manager import pdf_export, ticket_service


# ------------------------------------------------------------- Helpers --
def _archivo(path: Path, contenido: bytes = b"x") -> Path:
    """Crea `path` (y sus carpetas) con algo adentro."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contenido)
    return path


def _paginas(salida_de_pdfinfo: str) -> int:
    """Cuántas páginas dice `pdfinfo`. Se parsea con una expresión regular
    y no comparando la línea entera porque la salida alinea el valor con
    un relleno de espacios que no vale la pena fijar a mano."""
    m = re.search(r"^Pages:\s+(\d+)", salida_de_pdfinfo, re.MULTILINE)
    assert m is not None, salida_de_pdfinfo
    return int(m.group(1))


def _uso(total: int, libre: int):
    """Un `shutil.disk_usage` de mentira, para no depender del espacio
    real de la máquina que corre la suite."""
    return lambda _path: shutil._ntuple_diskusage(
        total=total, used=total - libre, free=libre)


@pytest.fixture
def unidad(tmp_path):
    """Una unidad preparada típica: 2 juegos de Wii, 1 de GameCube (de dos
    discos) y 2 apps de homebrew."""
    raiz = tmp_path / "USB_CLIENTE"
    _archivo(raiz / "wbfs" / "RMCP01" / "RMCP01.wbfs")
    _archivo(raiz / "wbfs" / "SOUE41" / "SOUE41.wbfs")
    _archivo(raiz / "games" / "Resident Evil [GBIE08]" / "game.iso")
    _archivo(raiz / "games" / "Resident Evil [GBIE08]" / "disc2.iso")
    _archivo(raiz / "apps" / "WiiFlow" / "boot.dol")
    _archivo(raiz / "apps" / "Nintendont" / "boot.dol")
    return raiz


# ================================================== Conteo: juegos Wii --
def test_cuenta_los_juegos_wii_de_la_estructura_de_carpetas(unidad):
    assert ticket_service.count_wii_games(unidad) == 2


def test_cuenta_igual_la_estructura_plana(tmp_path):
    """La app arma `wbfs/<ID6>/<ID6>.wbfs`, pero una unidad que pasó por
    otra herramienta puede tener los WBFS sueltos en `wbfs/`. Las dos
    disposiciones son juegos entregados y tienen que contar igual."""
    raiz = tmp_path / "usb"
    _archivo(raiz / "wbfs" / "RMCP01.wbfs")
    _archivo(raiz / "wbfs" / "RSBE01.wbfs")
    assert ticket_service.count_wii_games(raiz) == 2


def test_un_wbfs_dividido_cuenta_una_sola_vez(tmp_path):
    """`wit` parte los juegos grandes en `.wbfs` + `.wbf1` + `.wbf2` (ver
    `transfer_plan.wbfs_group`), y las tres piezas son UN juego. Contarlas por
    separado le diría al cliente que tiene el triple de juegos."""
    raiz = tmp_path / "usb"
    _archivo(raiz / "wbfs" / "SOUE41" / "SOUE41.wbfs")
    _archivo(raiz / "wbfs" / "SOUE41" / "SOUE41.wbf1")
    _archivo(raiz / "wbfs" / "SOUE41" / "SOUE41.wbf2")
    assert ticket_service.count_wii_games(raiz) == 1


def test_los_archivos_ocultos_no_cuentan_como_juegos(tmp_path):
    """Un respaldo de `DestinationGuard` que quedó huérfano, o la basura
    que dejan macOS y Windows en un pendrive, empiezan con punto. Son
    archivos que existen pero NO son juegos entregados, y no tienen que
    inflar el número que ve el cliente."""
    raiz = tmp_path / "usb"
    _archivo(raiz / "wbfs" / "RMCP01" / "RMCP01.wbfs")
    _archivo(raiz / "wbfs" / "RMCP01" / ".RMCP01.wbfs.respaldo-1234")
    _archivo(raiz / "wbfs" / "._RSBE01.wbfs")
    assert ticket_service.count_wii_games(raiz) == 1


def test_sin_carpeta_wbfs_no_hay_juegos_wii(tmp_path):
    assert ticket_service.count_wii_games(tmp_path) == 0


def test_los_archivos_que_no_son_juegos_no_cuentan(tmp_path):
    raiz = tmp_path / "usb"
    _archivo(raiz / "wbfs" / "RMCP01" / "RMCP01.wbfs")
    _archivo(raiz / "wbfs" / "leeme.txt")
    _archivo(raiz / "wbfs" / "RMCP01" / "cover.png")
    assert ticket_service.count_wii_games(raiz) == 1


# ============================================== Conteo: juegos GameCube --
def test_un_juego_gamecube_de_dos_discos_cuenta_una_vez(unidad):
    """Nintendont guarda los dos discos en LA MISMA carpeta (`game.iso` y
    `disc2.iso`, ver `transfer_plan.gc_dest_path`). Por eso acá se cuentan
    carpetas y no archivos: contando archivos, un juego de dos discos se
    entregaría como dos juegos."""
    assert ticket_service.count_gamecube_games(unidad) == 1


def test_una_carpeta_gamecube_vacia_no_cuenta(tmp_path):
    """Resto de un juego borrado a medias: la carpeta quedó, el juego no.
    Contarla sería prometerle al cliente algo que no está."""
    raiz = tmp_path / "usb"
    _archivo(raiz / "games" / "Metroid Prime [GM8E01]" / "game.iso")
    (raiz / "games" / "Vacia [XXXX01]").mkdir(parents=True)
    assert ticket_service.count_gamecube_games(raiz) == 1


def test_sin_carpeta_games_no_hay_juegos_gamecube(tmp_path):
    assert ticket_service.count_gamecube_games(tmp_path) == 0


# ============================================== Conteo: apps Homebrew --
def test_cuenta_las_apps_de_homebrew(unidad):
    assert ticket_service.count_homebrew_apps(unidad) == 2


def test_una_app_con_boot_elf_tambien_cuenta(tmp_path):
    """El Homebrew Channel arranca `boot.dol` o `boot.elf`: las dos son
    apps instaladas."""
    raiz = tmp_path / "usb"
    _archivo(raiz / "apps" / "ConDol" / "boot.dol")
    _archivo(raiz / "apps" / "ConElf" / "boot.elf")
    assert ticket_service.count_homebrew_apps(raiz) == 2


def test_el_ejecutable_en_mayusculas_tambien_cuenta(tmp_path):
    """En FAT/exFAT -el formato de estas unidades- "BOOT.DOL" y "boot.dol"
    son el mismo archivo, y hay ZIPs de homebrew que lo traen en
    mayúsculas."""
    raiz = tmp_path / "usb"
    _archivo(raiz / "apps" / "Nintendont" / "BOOT.DOL")
    assert ticket_service.count_homebrew_apps(raiz) == 1


def test_una_carpeta_sin_ejecutable_no_es_una_app(tmp_path):
    """`apps/` puede tener carpetas que no son apps: datos de
    configuración, restos de una desinstalación. Sin ejecutable, el
    Homebrew Channel no las muestra, así que el ticket tampoco."""
    raiz = tmp_path / "usb"
    _archivo(raiz / "apps" / "WiiFlow" / "boot.dol")
    _archivo(raiz / "apps" / "solo_datos" / "config.ini")
    assert ticket_service.count_homebrew_apps(raiz) == 1


def test_sin_carpeta_apps_no_hay_homebrew(tmp_path):
    assert ticket_service.count_homebrew_apps(tmp_path) == 0


# ========================================================== Capacidad --
def test_calcula_capacidad_usado_y_libre(tmp_path):
    data = ticket_service.collect_ticket_data(
        tmp_path,
        usage=_uso(total=64 * 1024 ** 3, libre=24 * 1024 ** 3),
        filesystem=lambda _p: "exfat",
    )
    assert data.total_bytes == 64 * 1024 ** 3
    assert data.free_bytes == 24 * 1024 ** 3
    assert data.used_bytes == 40 * 1024 ** 3


def test_el_usado_se_calcula_como_total_menos_libre(tmp_path):
    """Y no con el `used` que informa el sistema. En ext4 y familia hay
    bloques reservados para root que están en `total` pero no en `used` ni
    en `free`: lo que le importa a quien lee el ticket es cuánto del
    pendrive NO puede usar. Mismo criterio que
    `transfer_view._update_dest_space`."""
    def uso_con_reservados(_path):
        # 100 total, 50 libres, pero `used` dice 40: los 10 que faltan son
        # los bloques reservados.
        return shutil._ntuple_diskusage(total=100, used=40, free=50)

    data = ticket_service.collect_ticket_data(
        tmp_path, usage=uso_con_reservados, filesystem=lambda _p: "ext4")
    assert data.used_bytes == 50
    assert data.used_ratio == pytest.approx(0.5)


def test_una_unidad_que_no_se_puede_medir_no_rompe_el_ticket(tmp_path):
    """Si la unidad se desconectó entre que el usuario apretó el botón y
    esto corrió, el resto del ticket -que es lo que le importa al
    cliente- sigue siendo válido."""
    def falla(_path):
        raise OSError("unidad desconectada")

    data = ticket_service.collect_ticket_data(
        tmp_path, usage=falla, filesystem=lambda _p: None)
    assert (data.total_bytes, data.used_bytes, data.free_bytes) == (0, 0, 0)
    assert data.used_ratio is None


# ========================================================= Filesystem --
@pytest.mark.parametrize("fstype,esperado", [
    ("vfat", "FAT32"),
    ("msdos", "FAT32"),
    ("exfat", "exFAT"),
    ("ntfs", "NTFS"),
    ("ext4", "ext4"),
])
def test_el_filesystem_se_muestra_con_el_nombre_de_la_gente(fstype, esperado):
    """El ticket lo lee un cliente, no un técnico: "vfat" no le dice nada
    y "FAT32" sí."""
    assert ticket_service.filesystem_label(fstype) == esperado


def test_un_filesystem_desconocido_se_muestra_tal_cual():
    """Mejor mostrar el nombre raro que esconderlo detrás de un "otro":
    para quien tenga que diagnosticar algo después, el dato sirve."""
    assert ticket_service.filesystem_label("btrfs") == "BTRFS"


def test_un_filesystem_que_no_se_pudo_determinar_se_dice(tmp_path):
    """`drives.filesystem_of` devuelve None cuando no lo pudo determinar
    con confianza. Afirmar un formato que no se verificó sería peor que
    admitirlo."""
    assert ticket_service.filesystem_label(None) == "Desconocido"


# ======================================================== TicketData --
def test_reune_todo_el_contenido_de_la_unidad(unidad):
    data = ticket_service.collect_ticket_data(
        unidad, client_name="Juan Pérez", notes="Incluye 2 controles",
        now=datetime(2026, 8, 29, 15, 30),
        usage=_uso(total=64 * 1024 ** 3, libre=24 * 1024 ** 3),
        filesystem=lambda _p: "exfat",
    )
    assert data.client_name == "Juan Pérez"
    assert data.notes == "Incluye 2 controles"
    assert data.generated_at == datetime(2026, 8, 29, 15, 30)
    assert data.filesystem == "exFAT"
    assert data.drive_label == "USB_CLIENTE"
    assert data.contents.wii_games == 2
    assert data.contents.gamecube_games == 1
    assert data.contents.homebrew_apps == 2
    assert data.contents.total_games == 3


def test_le_saca_los_espacios_al_nombre_y_a_las_notas(unidad):
    """Se normaliza en el servicio y no en la interfaz, para que se
    comporte igual lo llame quien lo llame."""
    data = ticket_service.collect_ticket_data(
        unidad, client_name="  Juan Pérez \n", notes="  con notas  ",
        usage=_uso(100, 50), filesystem=lambda _p: "vfat")
    assert data.client_name == "Juan Pérez"
    assert data.notes == "con notas"


def test_nombre_y_notas_vacios_no_rompen_nada(unidad):
    """El caso del mostrador: entregar rápido, sin cargar nada. El ticket
    tiene que salir igual."""
    data = ticket_service.collect_ticket_data(
        unidad, usage=_uso(100, 50), filesystem=lambda _p: "vfat")
    assert data.client_name == ""
    assert data.notes == ""
    assert data.contents.wii_games == 2


def test_una_unidad_vacia_da_un_ticket_de_ceros(tmp_path):
    """Una unidad recién formateada: todo en cero, sin excepciones."""
    data = ticket_service.collect_ticket_data(
        tmp_path, usage=_uso(100, 100), filesystem=lambda _p: "exfat")
    assert data.contents == ticket_service.DriveContents(0, 0, 0)


def test_una_unidad_que_ya_no_existe_no_rompe(tmp_path):
    """Nada de esto puede levantar una excepción: el ticket es de solo
    lectura y tiene que degradar a ceros, no explotar."""
    data = ticket_service.collect_ticket_data(
        tmp_path / "no-existe", usage=_uso(0, 0), filesystem=lambda _p: None)
    assert data.contents.wii_games == 0
    assert data.filesystem == "Desconocido"


# ================================================== Nombre de archivo --
def test_el_nombre_propuesto_lleva_cliente_y_fecha():
    """Se compara en NFC porque `game_model.sanitize_filename` -el mismo que
    la app usa para los nombres de juego- normaliza a NFKD, y ahí la "é"
    de "Pérez" queda como "e" + tilde combinante: se ve igual en pantalla
    pero no es la misma cadena. Lo que importa acá es que el nombre y la
    fecha estén, no en qué forma Unicode."""
    nombre = ticket_service.suggested_filename(
        "Juan Pérez", datetime(2026, 8, 29))
    assert (unicodedata.normalize("NFC", nombre)
            == "Ticket Juan Pérez 2026-08-29.pdf")


def test_sin_cliente_el_nombre_propuesto_sigue_siendo_valido():
    nombre = ticket_service.suggested_filename("", datetime(2026, 8, 29))
    assert nombre == "Ticket 2026-08-29.pdf"


def test_un_nombre_de_cliente_con_barras_no_arma_una_ruta():
    """El nombre lo escribe el usuario y termina siendo un nombre de
    archivo: una barra ahí adentro convertiría el "guardar como" en una
    subcarpeta. Lo limpia `game_model.sanitize_filename`, el mismo que usa la
    app para los nombres de juego."""
    nombre = ticket_service.suggested_filename(
        "../../etc/passwd", datetime(2026, 8, 29))
    assert "/" not in nombre
    assert nombre.endswith(".pdf")


# =============================================================== PDF --
def _datos_de_ejemplo(**kw):
    base = dict(
        client_name="Juan Pérez", notes="Incluye 2 controles",
        generated_at=datetime(2026, 8, 29, 15, 30),
        drive_label="USB_CLIENTE", drive_path=Path("/run/media/x/USB"),
        total_bytes=64 * 1024 ** 3, used_bytes=40 * 1024 ** 3,
        free_bytes=24 * 1024 ** 3, filesystem="exFAT",
        contents=ticket_service.DriveContents(12, 3, 5),
    )
    base.update(kw)
    return ticket_service.TicketData(**base)


def test_el_pdf_generado_es_un_pdf_valido(tmp_path):
    """Estructuralmente: la firma de la primera línea y el marcador de fin
    que todo lector de PDF busca para encontrar la tabla de objetos."""
    destino = pdf_export.render_ticket(_datos_de_ejemplo(),
                                        tmp_path / "ticket.pdf")
    contenido = destino.read_bytes()
    assert contenido.startswith(b"%PDF-")
    assert contenido.rstrip().endswith(b"%%EOF")
    assert len(contenido) > 1000


def test_el_pdf_se_puede_abrir_con_un_lector_de_verdad(tmp_path):
    """Que empiece con %PDF- no alcanza para saber que un lector lo va a
    poder abrir. Poppler -que es el que usa el visor de GNOME, o sea el
    que de verdad va a abrir este archivo- se usa acá si está instalado,
    y si no la prueba se saltea: no vale sumar una dependencia para
    esto."""
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo is None:
        pytest.skip("poppler-utils no está instalado")
    import subprocess

    destino = pdf_export.render_ticket(_datos_de_ejemplo(),
                                        tmp_path / "ticket.pdf")
    result = subprocess.run([pdfinfo, str(destino)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert _paginas(result.stdout) == 1


def test_el_ticket_entra_en_una_sola_pagina_aunque_las_notas_sean_largas(tmp_path):
    """El ticket se manda por WhatsApp: dos páginas es peor que una nota
    recortada. Con poppler se verifica de verdad; sin poppler, al menos
    que no falle."""
    notas = ("Se revisó el lector y se cambió la lente. " * 40)
    destino = pdf_export.render_ticket(_datos_de_ejemplo(notes=notas),
                                        tmp_path / "largo.pdf")
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo is None:
        assert destino.read_bytes().startswith(b"%PDF-")
        return
    import subprocess

    result = subprocess.run([pdfinfo, str(destino)],
                            capture_output=True, text=True, timeout=30)
    assert _paginas(result.stdout) == 1


def test_el_pdf_sin_cliente_ni_notas_se_genera_igual(tmp_path):
    """Los dos campos son opcionales, y el PDF los saltea sin dejar
    huecos. Lo que no puede pasar es que falte el ticket."""
    destino = pdf_export.render_ticket(
        _datos_de_ejemplo(client_name="", notes=""),
        tmp_path / "sin_datos.pdf")
    assert destino.read_bytes().startswith(b"%PDF-")
    assert destino.stat().st_size > 1000


def test_una_unidad_sin_capacidad_medida_no_rompe_el_pdf(tmp_path):
    """`used_ratio` es None y la barra de uso no se dibuja; el resto de la
    hoja sale igual."""
    datos = _datos_de_ejemplo(total_bytes=0, used_bytes=0, free_bytes=0)
    assert datos.used_ratio is None
    destino = pdf_export.render_ticket(datos, tmp_path / "sin_medida.pdf")
    assert destino.read_bytes().startswith(b"%PDF-")


def test_el_contenido_del_ticket_llega_al_pdf(tmp_path):
    """Prueba diferencial: dos tickets que solo se diferencian en los
    números tienen que producir PDFs distintos. Es la forma de verificar
    que los datos se están dibujando de verdad sin depender de una
    librería que extraiga el texto (cairo incrusta las fuentes en
    subconjuntos, así que los bytes no se pueden leer como texto)."""
    a = pdf_export.render_ticket(
        _datos_de_ejemplo(contents=ticket_service.DriveContents(12, 3, 5)),
        tmp_path / "a.pdf")
    b = pdf_export.render_ticket(
        _datos_de_ejemplo(contents=ticket_service.DriveContents(99, 88, 77)),
        tmp_path / "b.pdf")
    assert a.read_bytes() != b.read_bytes()


def test_el_pdf_crea_la_carpeta_del_destino_si_no_existe(tmp_path):
    """El usuario puede elegir guardar en una carpeta que todavía no
    está."""
    destino = pdf_export.render_ticket(
        _datos_de_ejemplo(), tmp_path / "nueva" / "sub" / "ticket.pdf")
    assert destino.is_file()


def test_un_fallo_al_dibujar_no_deja_un_pdf_cortado(tmp_path, monkeypatch):
    """Se escribe con `atomicfs.atomic_write_target`, así que un error a
    mitad del dibujo no puede dejar media hoja con el nombre del archivo
    definitivo: el cliente no puede recibir un ticket truncado."""
    def boom(_ctx, _data):
        raise RuntimeError("simulado: falló el dibujo")

    monkeypatch.setattr(pdf_export, "_dibujar", boom)

    destino = tmp_path / "ticket.pdf"
    with pytest.raises(RuntimeError, match="falló el dibujo"):
        pdf_export.render_ticket(_datos_de_ejemplo(), destino)

    assert not destino.exists()
    assert list(tmp_path.iterdir()) == []


def test_un_ticket_de_punta_a_punta_desde_una_unidad_real(tmp_path, unidad):
    """El camino completo, que es el que corre en producción: leer una
    unidad de verdad, armar los datos y generar el PDF."""
    datos = ticket_service.collect_ticket_data(
        unidad, client_name="Juan Pérez", notes="Entrega completa",
        usage=_uso(total=64 * 1024 ** 3, libre=24 * 1024 ** 3),
        filesystem=lambda _p: "exfat")
    destino = pdf_export.render_ticket(datos, tmp_path / "final.pdf")

    assert destino.read_bytes().startswith(b"%PDF-")
    assert datos.contents.wii_games == 2
    assert datos.contents.gamecube_games == 1
    assert datos.contents.homebrew_apps == 2


# ================================================ Lógica de la vista --
# El botón vive en la pestaña Transferir. Construir la vista entera
# necesitaría un display, así que se prueba el método REAL contra un
# `self` de mentira con lo mínimo que ese método toca -el mismo truco que
# usa `test_queue_manager` para `_on_queue_idle`-. Así se ejercita el
# código que de verdad corre, y el test anda en cualquier terminal.
class _BotonDeMentira:
    def __init__(self):
        self.sensible = None
        self.tooltip = None

    def set_sensitive(self, valor):
        self.sensible = valor

    def set_tooltip_text(self, texto):
        self.tooltip = texto


class _VistaDeMentira:
    def __init__(self, dest_path):
        self._dest_path = dest_path
        self.ticket_button = _BotonDeMentira()


def _actualizar_boton(dest_path):
    from wiibackup_manager.widgets import transfer_view

    vista = _VistaDeMentira(dest_path)
    transfer_view.TransferView._update_ticket_button(vista)
    return vista.ticket_button


def test_sin_destino_elegido_el_boton_del_ticket_esta_apagado():
    boton = _actualizar_boton(None)
    assert boton.sensible is False
    assert "Elegí primero un destino" in boton.tooltip


def test_con_un_destino_elegido_el_boton_del_ticket_se_habilita(unidad):
    boton = _actualizar_boton(unidad)
    assert boton.sensible is True
    assert "PDF" in boton.tooltip


def test_un_destino_que_ya_no_esta_apaga_el_boton(tmp_path):
    """La SD que se sacó mientras la ventana seguía abierta: la ruta sigue
    elegida pero ya no existe, y no hay nada que contar."""
    boton = _actualizar_boton(tmp_path / "se-desconecto")
    assert boton.sensible is False
