# WiiBackup Manager (Linux)

Gestor de respaldos de Wii para Linux, inspirado en **Wii Backup Manager**
de Windows. Hecho con GTK4 + libadwaita para verse nativo en Fedora/GNOME.

![Estado](https://img.shields.io/badge/estado-alpha-orange)
![Licencia](https://img.shields.io/badge/licencia-MIT-blue)

## Funciones

- Escaneo de una carpeta (biblioteca) buscando `.iso`, `.wbfs`, `.ciso`, `.wdf`
- Identificación de cada juego (Game ID de 6 caracteres + título)
- Descarga automática de carátulas desde GameTDB, con caché local
- Renombrado a la convención estándar `Título [ID].ext`
- Conversión ISO ↔ WBFS y verificación de integridad (vía `wit`)
- Agregar juegos por selector de archivos (se copian a la biblioteca)
- Eliminar juegos con confirmación
- Búsqueda por título o ID

## Por qué usa `wit` (Wiimms ISO Tools) por debajo

El formato WBFS (y variantes como CISO/WDF) tiene detalles binarios finos
(tamaño de sector, offset del primer disco, etc.) que son fáciles de leer
mal. En vez de reimplementar ese parseo — con el riesgo de mostrar datos
incorrectos sobre tus respaldos reales — esta app delega la lectura de
formatos envueltos, la conversión y la verificación en
[Wiimms ISO Tools](https://wit.wiimm.de/), que es el estándar de facto en
Linux para esto. Para archivos `.iso` planos, la app lee el header
directamente (sin dependencias) porque ese formato sí está fijo y
documentado.

Sin `wit` instalado, la app sigue funcionando para ISO planas, pero no
podrá identificar WBFS/CISO/WDF ni convertir/verificar.

## Instalación en Fedora

### 1. Dependencias del sistema

```bash
sudo dnf install python3-gobject gtk4 libadwaita python3-pip
```

### 2. Wiimms ISO Tools (`wit`)

Fedora no lo trae en los repos oficiales. Instalalo desde el binario
estático que publica el proyecto:

```bash
cd /tmp
curl -LO https://wit.wiimm.de/download/wit-vX.XX-x86_64.tar.gz   # revisá la última versión en wit.wiimm.de
tar xf wit-*.tar.gz
sudo install -m 755 wit-*/bin/wit /usr/local/bin/wit
wit --version
```

(Si preferís no instalar `wit`, la app funciona igual con ISO planas.)

### 3. La app

```bash
git clone https://github.com/TU_USUARIO/wiibackup-manager-linux.git
cd wiibackup-manager-linux
pip install --user .
wiibackup-manager
```

O para desarrollo, sin instalar el paquete:

```bash
pip install --user -r requirements.txt
python3 -m wiibackup_manager
```

### 4. Integración con el menú de aplicaciones e ícono (opcional)

Si instalaste con `pip install --user .` (sin `-e`), el `.desktop` y los
íconos ya se copiaron solos a `~/.local/share/applications` y
`~/.local/share/icons/hicolor/*/apps`. Solo falta refrescar la caché:

```bash
gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor
```

Si instalaste en modo editable (`pip install -e .`), `data_files` no se
copia (limitación de los installs editables), así que hacelo a mano:

```bash
mkdir -p ~/.local/share/applications
cp data/com.gamefixsps.WiiBackupManager.desktop ~/.local/share/applications/

APP_ID="com.gamefixsps.WiiBackupManager"
for size in 16 32 48 64 128 256 512; do
  mkdir -p ~/.local/share/icons/hicolor/${size}x${size}/apps
  cp "data/icons/hicolor/${size}x${size}/apps/${APP_ID}.png" \
     ~/.local/share/icons/hicolor/${size}x${size}/apps/
done

gtk-update-icon-cache -f -t ~/.local/share/icons/hicolor
```

## Uso

1. Abrí **Preferencias** y elegí la carpeta donde tenés (o querés tener)
   tus respaldos de Wii.
2. La app escanea automáticamente esa carpeta al abrir.
3. Usá el botón **+** para agregar ISOs/WBFS existentes (se copian a la
   biblioteca).
4. Cada juego tiene un menú (⋮) con: renombrar al estándar, convertir,
   verificar, eliminar.

## Estructura del proyecto

```
wiibackup_manager/
├── app.py                       # Adw.Application
├── window.py                    # Ventana principal: pestaña Biblioteca y lógica de UI
├── config.py                    # Preferencias persistidas (XDG)
├── library.py                   # Escaneo y modelo de datos de juegos
├── disc_header.py                # Parseo de header de ISO plana (sin dependencias)
├── wit_wrapper.py                # Llamadas a `wit` (conversión/identificación/verificación)
├── gametdb.py                    # Descarga y caché de carátulas
├── drives.py                     # Detección y expulsión de unidades USB/SD montadas
└── widgets/
    ├── game_row.py                # Fila de juego en la lista de la Biblioteca
    ├── game_detail_dialog.py       # Panel de detalle del juego (carátula + datos GameTDB)
    ├── preferences_dialog.py       # Diálogo de Preferencias
    ├── transfer_view.py            # Pestaña Transferir: elegir destino y copiar a WBFS
    └── gtk_helpers.py              # Utilidades chicas compartidas por diálogos de GTK
```

## Changelog

### 0.1.5

Se salta la 0.1.4. Esta versión junta 36 commits de correcciones de
integridad de datos, coordinación de operaciones y rendimiento de la
lista, más la metadata extendida de GameTDB.

#### Corregido

- **Path traversal**: el Game ID sale del header del archivo (contenido
  que la app no controla) y se usaba para armar rutas del filesystem sin
  validar. Ahora se valida antes de convertirlo en un componente de ruta.
- **Sobrescrituras silenciosas**: convertir ISO↔WBFS, enviar a una unidad
  WBFS e importar pisaban un archivo existente sin preguntar. Los tres
  confirman ahora con el mismo diálogo; al importar en lote, además, el
  archivo entra con un nombre alternativo ("Juego (2).wbfs") en vez de
  reemplazar a otro juego que tuviera el mismo nombre.
- **Cancelar no cancelaba**: era una bandera que se miraba entre juegos,
  así que un `wit` copiando 20 minutos seguía hasta el final. Ahora se
  mata el proceso (y su grupo) en el acto. La copia directa revisa la
  cancelación cada 1 MiB en vez de cada 4 MiB.
- **Timeout de `wit`**: era absoluto y cortaba copias lentas pero sanas.
  Ahora se lo da por colgado por inactividad (que el destino no crezca),
  con el límite absoluto solo como última red.
- **WBFS en FAT32**: los discos dual-layer de más de 4 GB no entran en
  FAT32 y quedaban truncados. Se divide con `--split-size` explícito
  cuando el destino lo necesita.
- **Operaciones simultáneas peligrosas**: un gestor central impide borrar
  o renombrar un juego que se está convirtiendo, dos escaneos que se
  pisan el resultado, y escanear mientras se escriben archivos en la
  biblioteca. El bloqueo es por conflicto real: verificar o borrar un
  juego suelto mientras se convierte OTRO sigue permitido, y cada botón
  se apaga solo si su propia acción no puede arrancar.
- **Carátulas**: la pestaña Transferir lanzaba un hilo por fila (300
  juegos = 300 descargas simultáneas). Ahora hay un único pool compartido
  con toda la app, y un rescan no vuelve a encolar lo que ya está en
  vuelo.
- **Escritura atómica** de `config.json` y del historial, con validación
  de tipos al leerlos: un corte a mitad de la escritura ya no deja el
  archivo corrupto.
- **Crash con carátulas tardías**: los callbacks de GameTDB tocaban la
  fila sin comprobar que siguiera existiendo. Reordenar la lista o cerrar
  el panel de detalle con una descarga en vuelo podía tirar la app
  (`Gtk-CRITICAL` y volcado de core). Además, una fila reusada que pasó a
  mostrar otro juego descarta los datos del anterior si llegan tarde.
- **Caché de `wiitdb.xml` corrupta**: si el volcado cacheado no parseaba,
  el índice quedaba vacío para siempre y no volvían a aparecer ni la
  sinopsis ni los controles. Ahora se descarta y se baja de nuevo, y un
  XML recién bajado se valida antes de quedar como caché buena.
- **Selección múltiple**: ya no se pierde al cambiar el orden ni al
  rescanear, así que se puede mandar la misma tanda de juegos a una
  unidad y después a la siguiente sin volver a tildarlos.
- **Rendimiento de la lista**: reordenar y reescanear reconstruían las
  filas una por una. Con 300 juegos, cambiar el orden pasó de ~900 ms a
  6 ms y un rescan sin cambios de ~700 ms a 10 ms.
- **Carpetas sin permiso**: se salteaban en silencio al escanear y
  faltaban juegos sin explicación. Ahora el escaneo sigue igual con el
  resto, pero avisa cuáles quedaron afuera y lo anota en el historial.
- **Parseo de contenedores WBFS multi-juego**: el título se mezclaba con
  las columnas de tamaño y región.

#### Agregado

- **Panel de detalle con datos de GameTDB**: sinopsis y accesorios
  compatibles (Wii Remote, Nunchuk, Balance Board…), además de género,
  jugadores, fecha, publisher y developer.
- **Título original de GameTDB** junto al título del disco, cuando aporta
  algo distinto de lo que ya se ve.
- **Barra de uso de disco** en el destino de Transferir, coloreada según
  el espacio que queda.
- **Pestaña Log**: historial persistente de todas las operaciones, con su
  resultado y el motivo de los errores.
- **Cancelar** ahora también en la conversión (individual y en lote) y en
  los lotes de verificar y eliminar, con el proceso en curso muerto de
  verdad y un resumen de cuántos se alcanzaron a procesar.
- **Ícono propio de la aplicación** instalado en el sistema (16 a 512 px)
  y referenciado desde el `.desktop`.
- **Progreso real por bytes** dentro de la conversión o copia de un solo
  juego grande, en vez de saltar de 0% a 100% al terminar.

### 0.1.3

- La pestaña Transferir valida sus destinos (unidades y carpetas agregadas
  a mano) al refrescar: si dejaron de existir o ser accesibles (unidad
  expulsada/desconectada), desaparecen solos de la lista en vez de quedar
  mostrados con un error genérico de espacio.
- Detección automática de unidades nuevas/desconectadas en Transferir por
  sondeo periódico (cada 3s, mismo patrón que la detección de la
  Biblioteca desconectada): ya no hace falta cambiar de pestaña ni
  refrescar a mano.
- El selector de "Agregar carpeta" (Biblioteca y Transferir) ya no muestra
  el error nativo "No se pudo encontrar «...»" cuando la última carpeta
  usada quedó en una unidad desconectada: cae en silencio a la carpeta
  home.
- La versión mostrada en "Acerca de" ahora se toma de `__version__`.

### 0.1.2

- Barra de estado en la Biblioteca con el total de juegos y tamaño.
- Botón para expulsar la unidad WBFS de forma segura antes de
  desconectarla.

### 0.1.1

- Pestaña Transferir: copiar juegos seleccionados a una unidad WBFS (USB
  Loader), con ETA, progreso y cancelación.
- Selección múltiple y acciones en lote (enviar, convertir, verificar,
  eliminar) en la Biblioteca.
- Panel de detalle del juego, orden de la lista y arrastrar y soltar
  (drag & drop) para agregar archivos.
- Mostrar espacio libre del destino y validarlo antes de transferir.
- Evitar un crash al abrir la app con `library_path` apuntando a un mount
  desconectado.

## Roadmap / ideas pendientes

- [ ] Vista de cuadrícula con carátulas grandes (alternativa a la lista)
- [ ] Soporte para múltiples carpetas de biblioteca
- [ ] Copiar directamente a una unidad WBFS (USB Loader) con barra de progreso
- [ ] Empaquetado como Flatpak
- [ ] Traducciones (i18n)

## Licencia

MIT — ver [LICENSE](LICENSE).
