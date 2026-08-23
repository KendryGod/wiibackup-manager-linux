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

### 4. Integración con el menú de aplicaciones (opcional)

```bash
mkdir -p ~/.local/share/applications
cp data/com.gamefixsps.WiiBackupManager.desktop ~/.local/share/applications/
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
