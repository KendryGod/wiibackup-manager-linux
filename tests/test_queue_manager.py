"""Pruebas de `queue_manager.TransferQueue`.

La cola corre en un hilo de fondo (ver el docstring del módulo), así que
acá no se llama a `_copy` ni a ningún método privado directo: se encola de
verdad con `add_jobs` y se espera a que la tarea llegue a un estado final,
igual que hace `tools/manual_queue_e2e.py`. El único ajuste para probar
sin GTK es `dispatch=sync_dispatch`, que reemplaza `GLib.idle_add` por una
llamada directa -documentado como parámetro inyectable en
`TransferQueue.__init__`.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from wiibackup_manager import (atomicfs, library_ops, oplog, queue_manager,
                               transfer_plan, wit_wrapper)
from wiibackup_manager.operations import OperationManager
from wiibackup_manager.queue_manager import JobStatus, QueueSummary, TransferQueue


def sync_dispatch(func, *args) -> None:
    func(*args)


def hacer_cola() -> TransferQueue:
    return TransferQueue(OperationManager(), dispatch=sync_dispatch)


def esperar_final(job, timeout=10.0):
    t0 = time.monotonic()
    while not job.is_final and time.monotonic() - t0 < timeout:
        time.sleep(0.01)
    return job


# ------------------------------------------------------------ GameCube --
def test_copy_marca_error_sin_espacio_gc(make_game, tmp_path, monkeypatch):
    """El chequeo de espacio libre de `_copy` corre igual para GameCube que
    para Wii: si no entra, la tarea termina en Error y no llega a escribir
    nada en el destino."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 100)

    juego = make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                      console="gc", contenido=b"x" * 4096)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    cola = hacer_cola()
    job = cola.add_jobs([juego], dest_root)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.ERROR, job.error_msg
    assert not transfer_plan.gc_dest_path(juego, dest_root).exists()


def test_copy_gc_con_espacio_suficiente_copia(make_game, tmp_path, monkeypatch):
    """Con espacio de sobra, un juego de GameCube se copia entero a la
    estructura de Nintendont y la tarea llega a Completado."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)

    contenido = b"contenido de prueba gc" * 100
    juego = make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                      console="gc", contenido=contenido)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    cola = hacer_cola()
    job = cola.add_jobs([juego], dest_root)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg
    destino = transfer_plan.gc_dest_path(juego, dest_root)
    assert destino.read_bytes() == contenido


def test_send_to_wbfs_drive_gc_no_llama_needs_wbfs_split(make_game, tmp_path, monkeypatch):
    """Recorriendo la cola de punta a punta (no solo `send_to_wbfs_drive`
    directo), una tarea de GameCube tampoco debería disparar
    `drives.needs_wbfs_split`: ese chequeo es cosa de Wii/`wit`."""
    def _no_deberia_llamarse(*_a, **_k):
        raise AssertionError(
            "needs_wbfs_split no debería evaluarse para un juego de GameCube")
    monkeypatch.setattr(library_ops.drives, "needs_wbfs_split", _no_deberia_llamarse)

    juego = make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                      console="gc", contenido=b"contenido de prueba gc")
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    cola = hacer_cola()
    job = cola.add_jobs([juego], dest_root)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg


def test_send_to_wbfs_drive_gc_no_divide_aunque_el_destino_lo_pida(make_game, tmp_path,
                                                                    monkeypatch):
    """Aunque el destino "pida" dividir (FAT32 real o detectado como tal),
    una tarea de GameCube copia el archivo entero: a diferencia de Wii acá
    no hay `wit --split` de por medio, es una copia tal cual."""
    monkeypatch.setattr(library_ops.drives, "needs_wbfs_split", lambda path: True)

    contenido = b"contenido de prueba gc"
    juego = make_game(name="juego.iso", game_id="GZ2E01", title="Twilight Princess",
                      console="gc", contenido=contenido)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    cola = hacer_cola()
    job = cola.add_jobs([juego], dest_root)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg
    destino = transfer_plan.gc_dest_path(juego, dest_root)
    assert destino.read_bytes() == contenido
    assert not destino.with_suffix(".wbf1").exists()


# ------------------------------------------- RollbackFailedError hasta la UI --
def test_copy_con_rollback_fallido_distingue_los_dos_problemas_en_error_msg(
        make_game, tmp_path, monkeypatch):
    """El caso central que reportó la revisión de seguridad: `wit` falla
    Y ADEMÁS `DestinationGuard` no puede devolver el original a su lugar
    (acá, simulado para las dos partes de un WBFS dividido). El mensaje
    que llega a `job.error_msg` -lo que ve el usuario en la fila de la
    cola- tiene que nombrar los dos problemas, no solo "la conversión
    falló" como si el original se hubiera recuperado sin drama."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)
    monkeypatch.setattr(library_ops.wit_wrapper, "is_available", lambda _binary: True)

    def _fake_convert(src, dest, target_format, binary, **kwargs):
        # Simula lo que deja `wit` cuando falla a mitad de camino: los
        # nombres finales ya tienen contenido nuevo (corrupto/parcial).
        Path(dest).write_bytes(b"nuevo-corrupto")
        Path(dest).with_suffix(".wbf1").write_bytes(b"nuevo-corrupto-1")
        return subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="wit: fallo simulado")

    monkeypatch.setattr(library_ops.wit_wrapper, "convert", _fake_convert)

    real_replace = os.replace

    def _falla_solo_al_restaurar(origen, destino):
        if Path(origen).name.startswith("."):
            raise OSError("no se pudo restaurar (simulado)")
        return real_replace(origen, destino)

    monkeypatch.setattr(atomicfs.os, "replace", _falla_solo_al_restaurar)

    # `fmt="ISO"` fuerza el camino de `wit` (una copia WBFS directa no
    # pasa por DestinationGuard ni por wit_wrapper.convert).
    juego = make_game(name="juego.iso", game_id="RMCP01", title="Mario Kart Wii",
                      fmt="ISO", contenido=b"contenido de origen")
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    # El destino ya tiene un WBFS dividido de una transferencia anterior:
    # es lo que hace que `DestinationGuard` se active (`enabled=True`) y
    # tenga algo que apartar/restaurar.
    destino = transfer_plan.game_dest_path(juego, dest_root)
    destino.parent.mkdir(parents=True)
    destino.write_bytes(b"original-wbfs")
    destino.with_suffix(".wbf1").write_bytes(b"original-wbf1")

    cola = hacer_cola()
    job = cola.add_jobs([juego], dest_root, overwrite=True)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.ERROR
    # El motivo original (por qué falló la conversión)...
    assert "wit: fallo simulado" in job.error_msg
    # ...Y ADEMÁS que no se pudo restaurar -las dos cosas, no una sola.
    assert "restaurar" in job.error_msg.lower()

    # El original quedó SIN restaurar de verdad (no es solo el texto del
    # error): el respaldo sigue ahí, rescatable a mano.
    assert not destino.exists() or destino.read_bytes() != b"original-wbfs"
    respaldos = list(dest_root.rglob(".*.respaldo-*"))
    assert respaldos, "el respaldo temporal debería seguir existiendo"


# --------------------------------------------------- Tandas (batch_id) --
# El aviso de "cola terminada" sale del hilo de la cola y se ejecuta
# después, en el hilo de GTK. Entre una cosa y la otra el usuario puede
# encolar una tanda nueva, y el toast "Cola terminada" aparecía encima de
# una copia que recién arrancaba. El número de tanda viaja adentro del
# resumen para que la interfaz pueda descartar el aviso viejo.

def _cola_con_dispatch_diferido(resumenes: list):
    """Cola cuyo `dispatch` NO ejecuta el callback: lo apila, igual que
    `GLib.idle_add` deja el trabajo para el próximo giro del bucle. Es lo
    que permite reproducir la ventana entre el aviso y su ejecución."""
    pendientes: list = []
    cola = TransferQueue(OperationManager(),
                         on_queue_idle=resumenes.append,
                         dispatch=lambda func, *args: pendientes.append((func, args)))
    return cola, pendientes


def _esperar(condicion, timeout=10.0) -> bool:
    t0 = time.monotonic()
    while not condicion() and time.monotonic() - t0 < timeout:
        time.sleep(0.01)
    return condicion()


def test_la_primera_tanda_es_la_numero_uno(make_game, tmp_path):
    juego = make_game(name="juego.iso", game_id="GZ2E01", console="gc",
                      contenido=b"x" * 1024)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    cola = hacer_cola()
    assert cola.batch_id == 0, "sin nada encolado todavía no hubo ninguna tanda"
    job = cola.add_jobs([juego], dest_root)[0]
    assert cola.batch_id == 1
    esperar_final(job)
    cola.shutdown(wait=5)


def test_sumar_a_una_tanda_en_curso_no_abre_una_nueva(make_game, tmp_path):
    """Encolar con la cola andando es justamente la gracia de tener cola:
    esas tareas son parte de la MISMA tanda, así que el resumen final
    sigue siendo el de esa tanda y no hay que descartarlo."""
    juegos = [make_game(name=f"juego{i}.iso", game_id="GZ2E01", console="gc",
                        contenido=b"x" * 1024) for i in range(2)]
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    visto: list = []
    sumado = threading.Event()

    def on_job_changed(job):
        # Al arrancar la primera tarea la cola está ACTIVA: sumar acá no
        # puede abrir una tanda nueva.
        if job.status is JobStatus.RUNNING and not sumado.is_set():
            sumado.set()
            cola.add_jobs([juegos[1]], dest_root)
            visto.append(cola.batch_id)

    cola = TransferQueue(OperationManager(), on_job_changed=on_job_changed,
                         dispatch=sync_dispatch)
    job = cola.add_jobs([juegos[0]], dest_root)[0]
    esperar_final(job)
    _esperar(lambda: bool(visto))
    cola.shutdown(wait=5)

    assert visto == [1], "la tanda en curso no tenía que cambiar de número"


def test_un_resumen_que_llega_tarde_no_coincide_con_la_tanda_nueva(make_game, tmp_path):
    """El caso del hallazgo, de punta a punta: el resumen queda pendiente
    en el bucle, el usuario encola otra cosa, y recién ahí se ejecuta el
    callback. El número que trae ya no es el de la cola."""
    juego = make_game(name="juego.iso", game_id="GZ2E01", console="gc",
                      contenido=b"x" * 1024)
    otro = make_game(name="otro.iso", game_id="GZ2E01", console="gc",
                     contenido=b"x" * 1024)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    resumenes: list = []
    cola, pendientes = _cola_con_dispatch_diferido(resumenes)
    job = cola.add_jobs([juego], dest_root)[0]
    esperar_final(job)
    assert _esperar(lambda: bool(pendientes)), "la cola nunca avisó que terminó"

    # El aviso todavía no se ejecutó (sigue "en el bucle de GTK") y el
    # usuario encola una tanda nueva.
    cola.add_jobs([otro], dest_root)
    assert cola.batch_id == 2

    # Ahora sí se ejecuta el aviso viejo.
    func, args = pendientes.pop(0)
    func(*args)
    cola.shutdown(wait=5)

    assert len(resumenes) == 1
    assert resumenes[0].batch_id == 1
    assert resumenes[0].batch_id != cola.batch_id, (
        "la interfaz no tendría forma de saber que este resumen quedó viejo")


def test_un_resumen_que_llega_a_tiempo_sigue_siendo_valido(make_game, tmp_path):
    """La otra mitad: sin tanda nueva en el medio, el número coincide y el
    aviso se muestra como siempre."""
    juego = make_game(name="juego.iso", game_id="GZ2E01", console="gc",
                      contenido=b"x" * 1024)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    resumenes: list = []
    cola, pendientes = _cola_con_dispatch_diferido(resumenes)
    job = cola.add_jobs([juego], dest_root)[0]
    esperar_final(job)
    assert _esperar(lambda: bool(pendientes))

    func, args = pendientes.pop(0)
    func(*args)
    cola.shutdown(wait=5)

    assert resumenes[0].batch_id == cola.batch_id == 1


# ------------------------------------------- El descarte en la interfaz --
# La otra mitad del arreglo vive en `TransferView._on_queue_idle`. Se lo
# llama con un `self` de mentira -lo mínimo que ese método toca- para
# ejercitar el código real sin necesitar un display: importar el módulo no
# abre ninguna ventana, y así este test corre en cualquier terminal y no
# solo bajo Xvfb.

class _VistaDeMentira:
    def __init__(self, cola):
        self.queue = cola
        self.toasts: list = []
        self.refrescos = 0

    def _show_toast(self, texto):
        self.toasts.append(texto)

    def _update_dest_space_label(self):
        self.refrescos += 1

    # Antes se llamaba `_update_eject_button`. Pasó a ser
    # `_update_dest_buttons` -que refresca el botón de expulsar Y el de
    # generar el ticket de entrega- cuando se sumó el segundo botón que
    # depende del destino elegido: los dos miran lo mismo y se actualizan
    # juntos.
    def _update_dest_buttons(self):
        self.refrescos += 1

    def _update_queue_header(self):
        self.refrescos += 1


class _ColaDeMentira:
    def __init__(self, batch_id):
        self.batch_id = batch_id
        self.jobs: list = []


def _llamar_on_queue_idle(monkeypatch, batch_id_cola, batch_id_resumen):
    from wiibackup_manager.widgets import gtk_helpers, transfer_view
    monkeypatch.setattr(gtk_helpers, "widget_is_alive", lambda w: True)
    vista = _VistaDeMentira(_ColaDeMentira(batch_id_cola))
    resumen = QueueSummary(done=3, batch_id=batch_id_resumen)
    transfer_view.TransferView._on_queue_idle(vista, resumen)
    return vista


def test_la_vista_descarta_el_resumen_viejo(monkeypatch):
    vista = _llamar_on_queue_idle(monkeypatch, batch_id_cola=2, batch_id_resumen=1)
    assert vista.toasts == [], (
        "mostró 'cola terminada' con una tanda nueva ya en marcha")


def test_la_vista_muestra_el_resumen_de_la_tanda_actual(monkeypatch):
    vista = _llamar_on_queue_idle(monkeypatch, batch_id_cola=1, batch_id_resumen=1)
    assert len(vista.toasts) == 1
    assert "3" in vista.toasts[0]


# ------------------------------------ Verificación después de copiar --
#
# El switch de Ajustes (`Settings.verify_after_copy`, apagado por defecto)
# hace que la cola vuelva a leer con `wit VERIFY` lo que acaba de escribir.
# Acá `wit` se simula igual que en el resto del archivo: parcheando
# `wit_wrapper`, nunca corriendo el binario de verdad.


def _cola_con_log(tmp_path):
    """Una cola cuyo `OperationManager` sí escribe en un historial, para
    poder comprobar CÓMO queda anotada la verificación y no solo el estado
    de la fila."""
    log = oplog.OperationLog(tmp_path / "history.json")
    return TransferQueue(OperationManager(log), dispatch=sync_dispatch), log


def _juego_wbfs(make_game, contenido=b"contenido wbfs"):
    """Un juego de Wii ya en WBFS: `send_to_wbfs_drive` lo copia directo,
    sin pasar por `wit`, así que el único `wit` en juego es el de la
    verificación y no hay que simular también la conversión."""
    return make_game(name="juego.wbfs", game_id="RMCP01",
                     title="Mario Kart Wii", fmt="WBFS", contenido=contenido)


def _sin_wit_para_copiar(monkeypatch):
    """`send_to_wbfs_drive` le pregunta a `wit` si el WBFS de origen tiene
    más de un juego adentro; con `wit` no disponible saltea ese chequeo y
    copia directo, que es lo que queremos acá."""
    monkeypatch.setattr(library_ops.wit_wrapper, "is_available",
                        lambda _binary: False)


def test_con_el_switch_apagado_no_se_verifica_nada(make_game, tmp_path, monkeypatch):
    """El default. `verify_result` no se llama NI UNA VEZ: el costo de
    releer todo lo copiado solo se paga si alguien lo pidió."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)
    _sin_wit_para_copiar(monkeypatch)

    def _no_deberia_llamarse(*_a, **_k):
        raise AssertionError("no se debe verificar con el switch apagado")
    monkeypatch.setattr(queue_manager.wit_wrapper, "verify_result",
                        _no_deberia_llamarse)

    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    cola = hacer_cola()
    # Sin pasar `verify_after_copy`: se comprueba el default de la firma.
    job = cola.add_jobs([_juego_wbfs(make_game)], dest_root)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg
    assert job.verify_note == ""


def test_verificacion_exitosa_deja_la_tarea_completada_y_lo_anota(
        make_game, tmp_path, monkeypatch):
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)
    _sin_wit_para_copiar(monkeypatch)

    vistos = []

    def _verify_ok(path, binary="wit", timeout=None, cancel=None):
        vistos.append(Path(path))
        return wit_wrapper.VerifyResult(ok=True, timed_out=False, output="")
    monkeypatch.setattr(queue_manager.wit_wrapper, "verify_result", _verify_ok)

    juego = _juego_wbfs(make_game)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    cola, log = _cola_con_log(tmp_path)
    job = cola.add_jobs([juego], dest_root, verify_after_copy=True)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg
    assert job.verify_note == "verificado"
    # Se verificó el archivo que quedó en el destino, no el de origen.
    assert vistos == [transfer_plan.wbfs_dest_path(juego, dest_root)]
    entrada = log.entries()[0]
    assert entrada.status == oplog.STATUS_OK
    assert "verificado" in entrada.detail


def test_una_verificacion_fallida_no_se_confunde_con_un_fallo_de_copia(
        make_game, tmp_path, monkeypatch):
    """El corazón de la feature: el archivo se copió ENTERO y sigue en la
    unidad, pero al releerlo `wit` dice que está mal. Eso no es un Error de
    copia -donde en el destino no hay nada utilizable- y tiene que verse
    distinto: estado propio, y contado aparte en el resumen de la tanda."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)
    _sin_wit_para_copiar(monkeypatch)
    monkeypatch.setattr(
        queue_manager.wit_wrapper, "verify_result",
        lambda path, binary="wit", timeout=None, cancel=None:
            wit_wrapper.VerifyResult(
                ok=False, timed_out=False,
                output="!! wit: ERROR #31 [WRONG SHA1] in VerifyPartition()"))

    contenido = b"contenido wbfs completo"
    juego = _juego_wbfs(make_game, contenido)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    resumenes = []
    log = oplog.OperationLog(tmp_path / "history.json")
    cola = TransferQueue(OperationManager(log), dispatch=sync_dispatch,
                         on_queue_idle=resumenes.append)
    job = cola.add_jobs([juego], dest_root, verify_after_copy=True)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.CORRUPT
    assert job.status is not JobStatus.ERROR
    # El archivo NO se borra: existe, ocupa lugar y por eso hay que avisar.
    destino = transfer_plan.wbfs_dest_path(juego, dest_root)
    assert destino.exists() and destino.read_bytes() == contenido
    # El mensaje distingue las dos mitades: se copió, no verificó.
    assert "no pasó la verificación" in job.error_msg
    assert "WRONG SHA1" in job.error_msg
    # En el resumen de la tanda va en su propio contador, no en `errors`.
    assert resumenes and resumenes[-1].corrupt == 1
    assert resumenes[-1].errors == 0
    assert resumenes[-1].done == 0
    # En el historial no queda como "ok".
    assert log.entries()[0].status == oplog.STATUS_PARTIAL


def test_un_wbfs_dividido_se_verifica_como_conjunto(make_game, tmp_path, monkeypatch):
    """Un WBFS partido en `.wbfs` + `.wbf1` + `.wbf2`.

    Se le pasa a `wit` la PRIMERA parte y no cada pieza suelta: `wit` sigue
    solo la cadena de continuación (y falla si le falta una, ver
    `wit_wrapper.verify_result`). Lo que se comprueba acá es que la cola no
    invente otra cosa -que no verifique una parte cualquiera ni las mande
    de a una- y que deje anotado cuántos archivos formaban el conjunto,
    que es lo único que después permite saber que no se miró solo la
    primera."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)
    _sin_wit_para_copiar(monkeypatch)

    juego = _juego_wbfs(make_game)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    destino = transfer_plan.wbfs_dest_path(juego, dest_root)

    real_send = library_ops.send_to_wbfs_drive

    def _send_partido(game, drive_root, *args, **kwargs):
        # Copia normal y, encima, las partes que dejaría `wit --split`.
        ruta = real_send(game, drive_root, *args, **kwargs)
        ruta.with_suffix(".wbf1").write_bytes(b"parte 1")
        ruta.with_suffix(".wbf2").write_bytes(b"parte 2")
        return ruta
    monkeypatch.setattr(library_ops, "send_to_wbfs_drive", _send_partido)

    vistos = []

    def _verify(path, binary="wit", timeout=None, cancel=None):
        vistos.append(Path(path))
        return wit_wrapper.VerifyResult(ok=True, timed_out=False, output="")
    monkeypatch.setattr(queue_manager.wit_wrapper, "verify_result", _verify)

    cola = hacer_cola()
    job = cola.add_jobs([juego], dest_root, verify_after_copy=True)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg
    # Una sola llamada, con la primera parte: las `.wbfN` sueltas no son
    # imágenes válidas y mandarlas por separado daría un error por cada una.
    assert vistos == [destino]
    # Y queda dicho que el conjunto eran 3 archivos.
    assert job.verify_note == "verificado (3 archivos)"


def test_un_wbfs_dividido_al_que_le_falla_una_parte_queda_marcado(
        make_game, tmp_path, monkeypatch):
    """El caso que el conjunto existe para atrapar: las tres partes están
    escritas, pero una está mal. `wit` lo reporta al recorrer la cadena y
    la tarea no puede terminar en Completado."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)
    _sin_wit_para_copiar(monkeypatch)

    juego = _juego_wbfs(make_game)
    dest_root = tmp_path / "dest"
    dest_root.mkdir()

    real_send = library_ops.send_to_wbfs_drive

    def _send_partido(game, drive_root, *args, **kwargs):
        ruta = real_send(game, drive_root, *args, **kwargs)
        ruta.with_suffix(".wbf1").write_bytes(b"parte 1 sana")
        ruta.with_suffix(".wbf2").write_bytes(b"parte 2 rota")
        return ruta
    monkeypatch.setattr(library_ops, "send_to_wbfs_drive", _send_partido)

    monkeypatch.setattr(
        queue_manager.wit_wrapper, "verify_result",
        lambda path, binary="wit", timeout=None, cancel=None:
            wit_wrapper.VerifyResult(
                ok=False, timed_out=False,
                output="!! wit: ERROR #84 [READ FILE FAILED] in ReadWBFS(): "
                       "juego.wbf2"))

    cola, log = _cola_con_log(tmp_path)
    job = cola.add_jobs([juego], dest_root, verify_after_copy=True)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.CORRUPT
    # El motivo nombra la parte que falló, que es por dónde hay que empezar
    # a mirar.
    assert "wbf2" in job.error_msg
    assert log.entries()[0].status == oplog.STATUS_PARTIAL


def test_un_timeout_verificando_no_marca_el_archivo_como_corrupto(
        make_game, tmp_path, monkeypatch):
    """"No terminé de mirarlo" no es "está mal". Un `wit VERIFY` que se
    pasa del timeout deja la tarea en Completado -el archivo se copió y
    nadie demostró lo contrario- pero con la nota de que quedó sin
    comprobar, y en el historial como parcial y no como ok."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)
    _sin_wit_para_copiar(monkeypatch)
    monkeypatch.setattr(
        queue_manager.wit_wrapper, "verify_result",
        lambda path, binary="wit", timeout=None, cancel=None:
            wit_wrapper.VerifyResult(
                ok=False, timed_out=True,
                output="`wit` no respondió en 1800s"))

    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    cola, log = _cola_con_log(tmp_path)
    job = cola.add_jobs([_juego_wbfs(make_game)], dest_root,
                        verify_after_copy=True)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE
    assert job.status is not JobStatus.CORRUPT
    assert "no terminó a tiempo" in job.verify_note
    assert log.entries()[0].status == oplog.STATUS_PARTIAL


def test_gamecube_no_se_verifica_porque_wit_verify_no_lo_acepta(
        make_game, tmp_path, monkeypatch):
    """Comprobado contra `wit` v3.05a: VERIFY contesta `WRONG FILE TYPE ...
    Wii ISO image expected` ante una imagen de GameCube. Correrlo igual
    marcaría como corrupto todo juego de GameCube copiado, así que con el
    switch prendido la cola ni lo intenta y lo deja dicho."""
    monkeypatch.setattr(transfer_plan, "free_space", lambda path: 10 ** 12)

    def _no_deberia_llamarse(*_a, **_k):
        raise AssertionError("wit VERIFY no acepta imágenes de GameCube")
    monkeypatch.setattr(queue_manager.wit_wrapper, "verify_result",
                        _no_deberia_llamarse)

    juego = make_game(name="juego.iso", game_id="GZ2E01",
                      title="Twilight Princess", console="gc",
                      contenido=b"contenido gc")
    dest_root = tmp_path / "dest"
    dest_root.mkdir()
    cola, log = _cola_con_log(tmp_path)
    job = cola.add_jobs([juego], dest_root, verify_after_copy=True)[0]
    esperar_final(job)
    cola.shutdown(wait=5)

    assert job.status is JobStatus.DONE, job.error_msg
    assert "solo acepta" in job.verify_note
    assert log.entries()[0].status == oplog.STATUS_OK
