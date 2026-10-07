"""Formato del nombre del cliente y del servicio en el Ticket de Entrega.

En el mostrador se escribe rápido ("kendry", "hackeo e instalacion
gaming") y el ticket lo imprimía tal cual. El formato se aplica SOLO al
imprimir: lo que se escribió en el diálogo (y lo que viaja en
`TicketData`) no cambia. El número de serie y la versión del sistema no se
tocan nunca. Todos los datos son de prueba.
"""
from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from wiibackup_manager import pdf_export, ticket_service
from wiibackup_manager.pdf_export import format_client_name, format_service
from wiibackup_manager.ticket_service import ConsoleInfo


@pytest.mark.parametrize("escrito,impreso", [
    ("kendry", "Kendry"),
    ("cliente ficticio", "Cliente Ficticio"),
    ("CLIENTE FICTICIO", "Cliente Ficticio"),
    ("juan de la cruz", "Juan de la Cruz"),
    ("de la cruz", "De la Cruz"),
    ("ana pérez y gómez", "Ana Pérez y Gómez"),
    ("maría del carmen núñez", "María del Carmen Núñez"),
    ("ludwig van beethoven", "Ludwig van Beethoven"),
    ("ronald mcdonald", "Ronald McDonald"),
    ("MCDONALD", "McDonald"),
    ("o'brien", "O'Brien"),
    ("garcía-lópez", "García-López"),
    ("ñandú", "Ñandú"),
    ("  ana   del   valle ", "Ana del Valle"),
    ("", ""),
])
def test_el_nombre_del_cliente_se_imprime_con_formato_de_titulo(escrito, impreso):
    assert format_client_name(escrito) == impreso


@pytest.mark.parametrize("escrito", ["DeLuca", "McDonald", "LeBlanc",
                                     "Juan DeLuca"])
def test_un_nombre_con_mayusculas_ya_puestas_se_respeta(escrito):
    """Quien escribió "DeLuca" así sabía lo que quería."""
    assert format_client_name(escrito) == escrito


@pytest.mark.parametrize("escrito,impreso", [
    ("hackeo e instalacion gaming", "Hackeo e instalacion gaming"),
    ("instalación de juegos por USB", "Instalación de juegos por USB"),
    ("Limpieza", "Limpieza"),
    ("3 juegos", "3 juegos"),
    ("  cambio de lente ", "Cambio de lente"),
    ("", ""),
])
def test_el_servicio_lleva_mayuscula_inicial_y_nada_mas(escrito, impreso):
    assert format_service(escrito) == impreso


# ================================================================ PDF --
def _datos(**kw):
    base = dict(
        client_name="cliente ficticio de la prueba", notes="",
        generated_at=datetime(2026, 1, 2, 10, 30),
        drive_label="USB", drive_path=Path("/run/media/x/USB"),
        total_bytes=0, used_bytes=0, free_bytes=0, filesystem="FAT32",
        contents=ticket_service.DriveContents(0, 0, 0),
        console=ConsoleInfo(model="wii rvl-001", serial="lu12ab345",
                            system_version="4.3u",
                            service="hackeo e instalacion gaming"),
    )
    base.update(kw)
    return ticket_service.TicketData(**base)


@pytest.mark.skipif(shutil.which("pdftotext") is None,
                    reason="poppler-utils no está instalado")
def test_el_pdf_imprime_con_formato_y_no_toca_serie_ni_version(tmp_path):
    datos = _datos()
    pdf = pdf_export.render_ticket(datos, tmp_path / "t.pdf",
                                   pdf_export.ShopProfile(name="taller ficticio"))
    texto = subprocess.run(["pdftotext", str(pdf), "-"], capture_output=True,
                           text=True, check=True).stdout
    assert "Cliente Ficticio de la Prueba" in texto
    assert "Hackeo e instalacion gaming" in texto
    # Tal cual se escribieron.
    assert "lu12ab345" in texto
    assert "4.3u" in texto
    assert "wii rvl-001" in texto
    # Lo escrito no cambió: el formato es solo de la hoja.
    assert datos.client_name == "cliente ficticio de la prueba"
    assert datos.console.service == "hackeo e instalacion gaming"


@pytest.mark.skipif(shutil.which("pdftotext") is None,
                    reason="poppler-utils no está instalado")
def test_las_hojas_siguientes_tambien_llevan_el_nombre_con_formato(tmp_path):
    juegos = tuple(ticket_service.GameEntry("wii", f"W{i:05d}", f"Juego {i:03d}")
                   for i in range(90))
    datos = _datos(games=juegos,
                   contents=ticket_service.DriveContents(90, 0, 0))
    pdf = pdf_export.render_ticket(datos, tmp_path / "t.pdf")
    pagina2 = subprocess.run(["pdftotext", "-f", "2", "-l", "2", str(pdf), "-"],
                             capture_output=True, text=True, check=True).stdout
    assert "Cliente Ficticio de la Prueba" in pagina2


def test_el_nombre_del_archivo_sigue_siendo_el_que_se_escribio():
    """El formato es de lo impreso: el nombre de archivo propuesto no
    cambia."""
    nombre = ticket_service.suggested_filename("kendry", datetime(2026, 1, 2))
    assert nombre == "Ticket kendry 2026-01-02.pdf"
