#!/usr/bin/env python3
"""Prueba manual de extremo a extremo de `queue_manager.TransferQueue`,
contra un juego REAL de la biblioteca y un destino simulado (una carpeta
del disco interno, en vez de un USB de verdad).

Por qué existe
--------------
No siempre hay a mano un pendrive o una SD para probar en campo. Este
script ejercita la cola completa -encolar, copiar con progreso propio por
tarea, cancelar a mitad de copia, y el bloqueo de "expulsar" que usa
`is_writing_to`- usando una carpeta común como si fuera el punto de
montaje de la unidad. Todo lo que NO depende de que el medio sea removible
de verdad (atomicidad del archivo destino, bloqueo por cola, limpieza al
cancelar) queda cubierto. Lo que sí depende de hardware removible real
-una desconexión abrupta a mitad de escritura, un filesystem FAT32 real
con su límite de 4GiB- queda listado al final como pendiente.

Uso
---
    python3 tools/manual_queue_e2e.py --game /ruta/a/un/juego.wbfs \\
        --dest ~/prueba-destino

Cuando consigas un USB real, se corre exactamente igual pasando el punto
de montaje real en --dest (ej. /run/media/tu_usuario/MI_USB); nada más
cambia.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from wiibackup_manager import library  # noqa: E402
from wiibackup_manager.disc_header import UNKNOWN_GAME_ID  # noqa: E402
from wiibackup_manager.operations import OperationManager  # noqa: E402
from wiibackup_manager.queue_manager import JobStatus, TransferQueue  # noqa: E402

PASA, FALLA = "[OK]  ", "[FALLO]"
resultados: list[tuple[str, bool, str]] = []


def marcar(nombre: str, ok: bool, detalle: str = "") -> None:
    resultados.append((nombre, ok, detalle))
    print(f"{PASA if ok else FALLA} {nombre}" + (f" — {detalle}" if detalle else ""))


def hidden_partials(root: Path) -> list[Path]:
    """Busca temporales ocultos (`.algo.parcial-PID`, `.algo.respaldo-PID`,
    los que deja `wit`, etc.) bajo `root`. Si `cancel_job` limpió bien,
    esto tiene que volver vacío."""
    if not root.is_dir():
        return []
    return [p for p in root.rglob(".*") if p.is_file()]


def sync_dispatch(func, *args) -> None:
    """Reemplaza `GLib.idle_add`: este script no tiene un bucle de GTK
    corriendo, así que los avisos de la cola se ejecutan directo en el
    hilo que los dispara. `TransferQueue` se probó pensando justo en esto
    (parámetro `dispatch`)."""
    func(*args)


def hacer_cola(dest_log: list) -> TransferQueue:
    def on_job_changed(job):
        dest_log.append((time.monotonic(), "job", job.id, job.status, job.progress))

    def on_queue_idle(summary):
        dest_log.append((time.monotonic(), "idle", summary))

    ops = OperationManager()
    return TransferQueue(ops, on_job_changed=on_job_changed,
                          on_queue_idle=on_queue_idle, dispatch=sync_dispatch)


# --------------------------------------------------------------- Fase 1 --
def fase_1_cola_secuencial(game, dest_root: Path) -> None:
    """1) La cola copia juego por juego, con su propia barra de progreso.

    Con un solo juego real disponible, se encola DOS VECES el mismo
    `Game` hacia el mismo destino: la primera tarea hace la copia real
    (progreso 0→99%→Completado) y la segunda, que la cola arranca sola
    apenas la primera termina, encuentra el archivo que la primera acaba
    de dejar y termina en 'Ya estaba en el destino' sin volver a escribir
    nada. Eso ya alcanza para confirmar tres cosas a la vez: que la cola
    procesa de a una, que cada tarea tiene su propio estado/progreso
    (una llega a DONE con datos de velocidad, la otra a SKIPPED casi
    instantánea) y que no se pisan entre sí."""
    print("\n=== Fase 1: cola con dos tareas hacia el mismo destino ===")
    dest_root.mkdir(parents=True, exist_ok=True)
    log: list = []
    cola = hacer_cola(log)

    jobs = cola.add_jobs([game, game], dest_root, wit_binary="wit")
    ids = [j.id for j in jobs]
    print(f"Encoladas {len(jobs)} tareas: {ids}")

    listo = threading.Event()
    orig_idle = cola._on_queue_idle
    def _on_idle(summary):
        orig_idle(summary)
        listo.set()
    cola._on_queue_idle = _on_idle

    if not listo.wait(timeout=300):
        marcar("Fase 1: la cola terminó la tanda", False, "timeout esperando on_queue_idle")
        cola.shutdown(wait=5)
        return

    finales = {j.id: j.status for j in cola.jobs}
    print("Estados finales:", {i: s.value for i, s in finales.items()})

    progresos_vistos = sorted({round(entrada[4], 2) for entrada in log
                                if entrada[1] == "job" and entrada[2] == ids[0]})
    marcar("Fase 1: la tarea 1 avanzó con varios pasos de progreso (no un salto 0→100)",
           len([p for p in progresos_vistos if 0 < p < 1]) >= 2,
           f"progresos vistos: {progresos_vistos}")
    marcar("Fase 1: la tarea 1 terminó Completado", finales.get(ids[0]) is JobStatus.DONE,
           str(finales.get(ids[0])))
    marcar("Fase 1: la tarea 2 (mismo destino) terminó 'Ya estaba en el destino'",
           finales.get(ids[1]) is JobStatus.SKIPPED, str(finales.get(ids[1])))

    dest_final = library.wbfs_dest_path(game, dest_root)
    marcar("Fase 1: el archivo copiado existe y pesa lo mismo que el original",
           dest_final.exists() and dest_final.stat().st_size == game.path.stat().st_size,
           f"{dest_final} ({dest_final.stat().st_size if dest_final.exists() else 0} bytes)")

    cola.clear_finished()
    cola.shutdown(wait=5)


# ----------------------------------------------------------- Fases 2-4 --
def fase_2_3_4_cancelar_y_bloqueo(game, dest_root: Path) -> None:
    """2) Cancelar a mitad de copia no deja un ISO/WBFS corrupto sin avisar.
    3) Mientras hay una tarea 'Copiando', is_writing_to() (lo que usa el
       botón "Expulsar") da True.
    4) Al vaciar la cola (o simplemente al quedar la tarea en su estado
       final), el bloqueo se libera."""
    print("\n=== Fases 2, 3 y 4: cancelar a mitad de copia + bloqueo de expulsión ===")
    if dest_root.exists():
        shutil.rmtree(dest_root)
    dest_root.mkdir(parents=True)

    log: list = []
    cola = hacer_cola(log)

    marcar("Fase 3 (antes de encolar): is_writing_to da False sin trabajo pendiente",
           cola.is_writing_to(dest_root) is False)

    jobs = cola.add_jobs([game], dest_root, wit_binary="wit")
    job = jobs[0]
    print(f"Tarea {job.id} encolada, esperando a que arranque a copiar...")

    t0 = time.monotonic()
    while job.status is JobStatus.PENDING and time.monotonic() - t0 < 30:
        time.sleep(0.02)

    # Dejamos que escriba una porción real (no cancelar en el instante
    # cero) para que el archivo temporal exista de verdad en disco.
    t0 = time.monotonic()
    while job.progress <= 0.0 and time.monotonic() - t0 < 30:
        time.sleep(0.02)
    time.sleep(1.5)

    en_copia = job.status is JobStatus.RUNNING
    marcar("Fase 2/3: la tarea llegó a 'Copiando' con progreso > 0 antes de cancelar",
           en_copia and job.progress > 0,
           f"status={job.status.value}, progreso={job.progress:.2%}")

    ocupada_durante = cola.is_writing_to(dest_root)
    marcar("Fase 3: is_writing_to(destino) da True mientras la tarea está 'Copiando'",
           ocupada_durante is True)
    print("  (así se ve el toast real: 'No se puede expulsar ahora: la cola "
          "todavía tiene transferencias hacia esa unidad.')" if ocupada_durante
          else "  (el botón Expulsar quedaría HABILITADO acá, que sería el bug)")

    parciales_durante = hidden_partials(dest_root)
    marcar("Fase 2: existe un temporal oculto en el destino mientras copia",
           len(parciales_durante) >= 1, str(parciales_durante))

    dest_final = library.wbfs_dest_path(game, dest_root)
    print(f"Cancelando tarea {job.id} (progreso al cancelar: {job.progress:.2%})...")
    cola.cancel_job(job.id)

    t0 = time.monotonic()
    while not job.is_final and time.monotonic() - t0 < 30:
        time.sleep(0.02)

    marcar("Fase 2: la tarea quedó en estado final 'Cancelado' (no 'Error', no 'Completado')",
           job.status is JobStatus.CANCELLED, str(job.status))

    marcar("Fase 2: NO quedó un archivo a medio copiar en el nombre final del destino",
           not dest_final.exists(), str(dest_final))

    parciales_despues = hidden_partials(dest_root)
    marcar("Fase 2: los temporales ocultos se borraron al cancelar (nada huérfano)",
           len(parciales_despues) == 0, str(parciales_despues))

    otros_archivos = [p for p in dest_root.rglob("*") if p.is_file()]
    marcar("Fase 2: el destino no tiene NINGÚN archivo de esta tarea (ni oculto ni visible)",
           len(otros_archivos) == 0, str(otros_archivos))

    ocupada_despues_de_cancelar = cola.is_writing_to(dest_root)
    marcar("Fase 4: is_writing_to(destino) vuelve a False apenas la tarea queda "
           "en estado final (no hace falta ni vaciar la cola)",
           ocupada_despues_de_cancelar is False)

    restantes = cola.clear_finished()
    marcar("Fase 4: 'vaciar la cola' (clear_finished) se llevó la tarea cancelada",
           len(restantes) == 1 and len(cola.jobs) == 0, f"jobs restantes: {cola.jobs}")

    marcar("Fase 4: is_writing_to(destino) sigue en False después de vaciar la cola",
           cola.is_writing_to(dest_root) is False)

    cola.shutdown(wait=5)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--game", required=True, type=Path,
                     help="Ruta a un juego real (.iso/.wbfs/.ciso/.wdf) de tu biblioteca.")
    ap.add_argument("--dest", required=True, type=Path,
                     help="Carpeta destino simulada (ej. ~/prueba-destino). "
                          "Cuando tengas un USB real, pasá acá su punto de montaje.")
    ap.add_argument("--wit-binary", default="wit")
    args = ap.parse_args()

    game_path = args.game.expanduser().resolve()
    if not game_path.is_file():
        print(f"No existe el archivo de juego: {game_path}", file=sys.stderr)
        return 2

    game = library.identify_file(game_path, args.wit_binary)
    if game is None or game.game_id == UNKNOWN_GAME_ID:
        print(f"No se pudo identificar {game_path} como un juego de Wii válido.",
              file=sys.stderr)
        return 2
    print(f"Juego real detectado: {game.title} ({game.game_id}, {game.fmt}, "
          f"{library.format_size(game.size_bytes)})")

    dest_root = args.dest.expanduser().resolve()
    dest_root.mkdir(parents=True, exist_ok=True)

    fase_1_cola_secuencial(game, dest_root / "fase1")
    fase_2_3_4_cancelar_y_bloqueo(game, dest_root / "fase2")

    print("\n=== Resumen ===")
    ok_total = True
    for nombre, ok, detalle in resultados:
        ok_total &= ok
        print(f"{PASA if ok else FALLA} {nombre}")
    print("\nTODO OK" if ok_total else "\nHubo fallos — revisar arriba.")
    return 0 if ok_total else 1


if __name__ == "__main__":
    raise SystemExit(main())
