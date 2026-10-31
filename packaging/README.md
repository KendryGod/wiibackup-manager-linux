# Empaquetado RPM para COPR (Fedora)

Acá están los dos paquetes RPM con los que WiiBackup Manager se va a
publicar en un proyecto de [COPR](https://copr.fedorainfracloud.org/):

| Archivo | Qué es |
|---|---|
| `wit.spec` | Wiimms ISO Tools 3.05a (`wit`, `wwt`, `wdf`), compilado desde la fuente oficial. Fedora no lo tiene. |
| `wit-3.05a-*.patch` | Los cuatro parches de wit. Cada uno explica en su cabecera qué arregla y cómo se midió. |
| `wiibackup-manager.spec` | La app (`noarch`), desde el tarball del tag `v<versión>` en GitHub. |
| `packit.yaml.borrador` | Borrador **inactivo y sin probar** de la configuración de Packit (ver más abajo). |

Hay que publicar los dos en el **mismo** proyecto COPR: la app tiene
`Requires: wit >= 3.05a`, y wit no está en los repos de Fedora (ni en 43,
ni en 44, ni en rawhide; comprobado con `dnf repoquery` el 2026-10-30).

## Estado

Probado el 2026-10-30/31. **Ojo:** el mock de estas pruebas corrió dentro
de un contenedor de podman (ver [Sin sudo](#sin-sudo-mock-dentro-de-podman)),
no instalado en el sistema.

- Los dos construyen en `mock` para `fedora-43-x86_64`, `fedora-44-x86_64`
  y `fedora-rawhide-x86_64` (Python 3.15, OpenSSL 4.0), con su `%check`
  completo. Una build de la app en f44 falló una vez por un choque de
  displays X entre builds paralelas, propio del contenedor (ver abajo);
  repetida sola, pasó.
- wit pasa `annocheck` (las protecciones de Fedora, CET incluido) para
  `wit`, `wwt` y `wdf` (medido en f44).
- En el `%check` de la app corren `desktop-file-validate`,
  `appstreamcli validate --no-net`, la importación de todos los módulos,
  las pruebas sin GUI (lo mismo que el CI: 1766 pasan, 40 se saltean) y
  las pruebas con GUI bajo Xvfb (93 pasan). Ninguna necesita internet
  (comprobado sin red y con `lo` arriba, como en COPR).
- Instalados juntos en un chroot limpio de f44, con las dependencias de
  los repos de Fedora: `wit --version`, la base de títulos, los datos de
  la app, los dos lanzadores bajo Xvfb y `rpm -V` andan, y al
  desinstalarlos no queda ningún archivo de los paquetes. En f43 y
  rawhide se probó la instalación y el arranque.
- No se subió nada a COPR.

## Construir en local

Los specs no necesitan nada del repo fuera de `packaging/`: las fuentes se
bajan de las URL de `Source0`. En los comandos, `~/empaquetado` es un
directorio de trabajo cualquiera, fuera del repo.

### Con mock instalado (lo normal)

Una sola vez, con sudo:

```sh
sudo dnf install mock rpm-build rpmdevtools rpmlint
sudo usermod -aG mock "$USER"
```

El grupo nuevo recién vale en una sesión nueva (cerrar sesión y volver a
entrar, o `newgrp mock` en esa terminal).

Después, desde la raíz del repo:

```sh
S=~/empaquetado/compilacion/SOURCES; R=~/empaquetado/compilacion/SRPMS
mkdir -p "$S" "$R"
cp packaging/wit-3.05a-*.patch "$S"/
spectool -g -C "$S" packaging/wit.spec
spectool -g -C "$S" packaging/wiibackup-manager.spec
for spec in wit wiibackup-manager; do
    rpmbuild -bs --define "_sourcedir $S" --define "_srcrpmdir $R" packaging/$spec.spec
done

for chroot in fedora-44-x86_64 fedora-43-x86_64 fedora-rawhide-x86_64; do
    mock -r $chroot --resultdir ~/empaquetado/resultados/$chroot/wit \
        --rebuild "$R"/wit-3.05a-1.*.src.rpm
    mock -r $chroot --resultdir ~/empaquetado/resultados/$chroot/wiibackup-manager \
        --rebuild "$R"/wiibackup-manager-0.2.4-1.*.src.rpm
done
```

`%prep` de wit comprueba el sha256 del tarball oficial (upstream no lo
firma). El de la app no lo fija: Packit reemplaza `Source0` por un archivo
que arma desde git (ver más abajo), y un sha256 fijo rompería esos builds.
El tarball de GitHub del tag `v0.2.4`, descomprimido, es idéntico byte a
byte a `git archive --prefix=wiibackup-manager-linux-0.2.4/ v0.2.4`.

Para revisar los resultados:

```sh
rpmlint packaging/*.spec ~/empaquetado/compilacion/SRPMS/*.src.rpm \
    ~/empaquetado/resultados/fedora-44-x86_64/*/*.rpm
```

Lo que rpmlint 2.8.0 (con la configuración de Fedora) marca hoy, igual en
43, 44 y rawhide, y por qué se deja:

- wit: `spelling-error` en Wiimms, wwt y wdf (nombres propios);
  `no-manual-page-for-binary` en las cinco herramientas (upstream no tiene
  páginas de manual); `invalid-license LicenseRef-Rijndael-Mike-Scott`
  (ver la licencia de wit más abajo); `incorrect-fsf-address` en
  `gpl-2.0.txt` (el texto de upstream trae la dirección vieja de la FSF, y
  un texto de licencia no se modifica).
- wiibackup-manager: `spelling-error` en homebrew;
  `python-leftover-require python3-gobject` (hace falta por
  `gi/_gi_cairo`); `no-manual-page-for-binary`; y
  `desktopfile-without-binary ... python3`, porque el `Exec` del
  `.desktop` es `python3 -m wiibackup_manager`.

Para probar la instalación en un chroot limpio, con las dependencias de
los repos reales:

```sh
c=fedora-44-x86_64; d=~/empaquetado/resultados/$c
mock -r $c --init
mock -r $c --install $d/wit/wit-3.05a-1.fc44.x86_64.rpm \
    $d/wiibackup-manager/wiibackup-manager-0.2.4-1.fc44.noarch.rpm
mock -r $c --chroot 'wit --version'
mock -r $c --unpriv --chroot "python3 -c 'import wiibackup_manager'"
```

### Sin sudo: mock dentro de podman

Es lo que se usó en esta fase, porque mock no estaba instalado. Corre el
mock de verdad, con las configuraciones de `mock-core-configs`, pero
**no reproduce exactamente su aislamiento**:

- El contenedor es rootless y `--privileged`; mock usa
  `--isolation=simple` (chroot, sin systemd-nspawn) y
  `--no-bootstrap-image`.
- mock no se deja lanzar desde root salvo vía sudo, consolehelper o
  pkexec. Lanzado como root dentro del contenedor, consolehelper le pasa
  `USERHELPER_UID=0` y el `rpmbuild` corre **como root**: así una prueba
  de la app que usa una carpeta `chmod 000` fallaba (root la lee igual).
  Hay que imitar a `sudo mock`: un usuario `builder` (uid 1000, grupo
  `mock`) y `SUDO_UID=1000 SUDO_GID=1000 /usr/libexec/mock/mock ...`, con
  el `--resultdir` escribible por ese usuario.
- Con `--isolation=simple`, el `rpmbuild` **no** queda en un espacio de
  red propio: comparte la red del contenedor (medido con
  `readlink /proc/self/ns/net`). Eso tiene dos efectos: el `%check` tiene
  internet (en COPR no), y dos builds en paralelo que corren pruebas con
  GUI se pisan el display de Xvfb (las dos reciben `:99` y comparten el
  socket abstracto `@/tmp/.X11-unix/X99`). Así una build de la app falló
  una vez con un segfault de GTK. En este montaje, las builds con pruebas
  de GUI van de a una.

La imagen es `registry.fedoraproject.org/fedora:44` más `rpm-build
rpmdevtools rpmlint mock annobin-annocheck desktop-file-utils appstream`.
Si se le da a podman un almacenamiento propio (`CONTAINERS_STORAGE_CONF`
apuntando a un `storage.conf` con otro `graphroot`), todo lo que baja queda
en un solo lugar; para borrarlo hace falta `podman unshare rm -rf <graphroot>`,
porque hay archivos de uids del espacio de nombres del contenedor.

## Publicar en COPR

Siempre **primero wit y después la app**, en el mismo proyecto. Chroots:
`fedora-43-x86_64`, `fedora-44-x86_64` y `fedora-rawhide-x86_64`, que son
los probados. Fedora 45 ya está ramificada (rawhide es fc46) aunque no
publicada al 2026-10-30, y COPR ya ofrece `fedora-45-x86_64` (comprobado
en `https://copr.fedorainfracloud.org/api_3/mock-chroots/list`): conviene
sumarlo después de probarlo con `mock -r fedora-45-x86_64`. Los chroots
`aarch64` existen pero no se probaron.

Todo lo de COPR y Packit de esta sección está comprobado contra su
documentación oficial (docs.copr.fedorainfracloud.org y packit.dev,
leídas el 2026-10-30), pero ninguna de estas vías se corrió todavía.

### Opción 1: subir los SRPM a mano

Lo más simple y lo primero que conviene hacer, con los SRPM de arriba:

```sh
copr-cli create wiibackup-manager --chroot fedora-43-x86_64 \
    --chroot fedora-44-x86_64 --chroot fedora-rawhide-x86_64
copr-cli build wiibackup-manager ~/empaquetado/compilacion/SRPMS/wit-3.05a-1.fc44.src.rpm
copr-cli build wiibackup-manager ~/empaquetado/compilacion/SRPMS/wiibackup-manager-0.2.4-1.fc44.src.rpm
```

COPR reconstruye cada SRPM en cada chroot (el `.fc44` del nombre no
importa).

### Opción 2: Packit, para la app

Packit es una app de GitHub que, por ejemplo, manda un build a COPR cada
vez que se publica una release. Lo que dice su documentación:

- Lee su configuración solo de `.packit.yaml`, `.packit.yml`,
  `packit.yaml` o `packit.yml` **en la raíz del repo**. Por eso
  `packit.yaml.borrador` no hace nada donde está: activarlo es copiarlo a
  `/.packit.yaml`, un archivo fuera de `packaging/` (decisión pendiente).
- `specfile_path` puede apuntar a `packaging/wiibackup-manager.spec`.
- El job `copr_build` acepta los disparadores `pull_request`, `commit` y
  `release`. Con un proyecto propio (`owner` y `project`) los chroots salen
  de la configuración del proyecto en COPR.
- Para armar el SRPM, Packit hace un archivo desde git y su acción
  `fix-spec-file` cambia en el spec el `Source0`, el `-n` de `%autosetup`,
  el `Version` y el `Release` (este último salvo con
  `update_release: false`, que el borrador pone).
- Con un proyecto propio hay que darle a Packit permiso de *builder*
  (`copr-cli edit-permissions --builder packit wiibackup-manager`) y anotar
  `github.com/KendryGod/wiibackup-manager-linux` en el campo *Packit
  allowed forge projects* de la configuración del proyecto.
- Para usar Packit hace falta una cuenta FAS con el campo *GitHub
  Username* cargado, instalar la app *Packit-as-a-Service* en el repo y
  comentar `/packit verify-fas <usuario-fas>` en el issue que Packit abre
  al instalarla.

Packit no sirve para wit: wit no sale de este repo. Para wit quedan las
opciones 1 y 3.

### Opción 3: método "Custom" de COPR (o SCM)

COPR también puede armar el SRPM solo, desde git. Lo que dice su
documentación y cómo encaja acá:

- **Custom (script)**: un script que se escribe en la página del paquete
  en COPR (no va en el repo), corre en un chroot de mock como el usuario
  `mockbuild`, con red, y tiene que dejar el spec, las fuentes y los
  parches en su directorio de resultados (por defecto, el directorio
  actual); las herramientas se piden como *builddeps*. Para wit, con
  *builddeps* `git rpmdevtools`, alcanzaría con algo así (sin probar):

  ```sh
  #! /bin/sh -xe
  git clone --depth 1 --branch v0.2.4 https://github.com/KendryGod/wiibackup-manager-linux.git repo
  cp repo/packaging/wit.spec repo/packaging/wit-3.05a-*.patch .
  spectool -g wit.spec
  ```

- **SCM** (*Clone URL*, *Committish*, *Subdirectory* = `packaging`, *Spec
  File*), con uno de cuatro métodos para el SRPM:
  - `rpkg`, el predeterminado: desde diciembre de 2021 COPR usa rpkg-util
    v3, que según la documentación espera specs con plantillas `{{{ }}}`.
    Estos specs no las usan; no está probado si así funcionan.
  - `make srpm`: necesita un `.copr/Makefile` **en la raíz del repo**
    (fuera de `packaging/`: decisión pendiente).
  - `tito` y `tito test`: necesitan el repo inicializado con `tito init`.

- **Webhooks**: en *Settings → Integrations* del proyecto está la URL del
  webhook de GitHub. Para los disparos por tag, COPR busca el nombre del
  paquete en el tag (`PAQUETE-VERSIÓN`); como los tags de este repo son
  `v0.2.4`, habría que agregar el nombre del paquete COPR al final de esa
  URL.

## Pasos pendientes

| # | Paso | Quién |
|---|---|---|
| 1 | Crear la cuenta de Fedora (FAS) en https://accounts.fedoraproject.org/ | vos |
| 2 | Entrar a https://copr.fedorainfracloud.org/ con esa cuenta | vos |
| 3 | Sacar el token de la API en https://copr.fedorainfracloud.org/api/ y guardarlo en `~/.config/copr` (es una credencial: no va al repo ni a ningún chat). Instalar `copr-cli` con `sudo dnf install copr-cli` | vos |
| 4 | Crear el proyecto `wiibackup-manager` con los chroots de arriba (`copr-cli create ...` o la web) | vos |
| 5 | Construir wit y después la app (opción 1, 2 o 3) | vos lanzás; los errores de build los puedo revisar yo |
| 6 | Si se elige Packit: cargar *GitHub Username* en FAS, instalar *Packit-as-a-Service* en el repo, `/packit verify-fas <usuario-fas>`, permiso de *builder* para `packit` y la entrada en *Packit allowed forge projects* | vos |
| 7 | Si se elige Packit: decidir si `packit.yaml.borrador` pasa a `/.packit.yaml` y completar `owner` | vos decidís; el archivo lo puedo preparar yo |
| 8 | Probar la instalación desde COPR en una Fedora real (`sudo dnf copr enable <usuario>/wiibackup-manager`, `sudo dnf install wiibackup-manager`) | vos |

## Decisiones de empaquetado

### wit

- **Fuente:** `https://download.wiimm.de/source/wiimms-iso-tools/wiimms-iso-tools.source-3.05a.txz`,
  el enlace "Source code / Download" de https://wit.wiimm.de/. sha256
  `e5a8a488f26465ff4ec03f10990ee28e104eb4542990812bbdbea03af12ed8bd`.
- **Licencia:** `GPL-2.0-or-later` para el código de wit (ninguna cabecera
  limita a la versión 2), más lo que queda compilado adentro: el LZMA SDK
  (dominio público, `LicenseRef-Fedora-Public-Domain`) y el AES de Mike
  Scott de `src/libwbfs/rijndael.c`, cuyo texto no tiene identificador
  SPDX ni está en fedora-license-data: va como
  `LicenseRef-Rijndael-Mike-Scott` y habría que pedirle una revisión a
  Fedora Legal si alguna vez el paquete va a Fedora. La licencia de los
  `titles*.txt` (vienen de GameTDB) no está declarada en el tarball.
- **Se borra en `%prep`:** `setup/windows-install.exe` (binario PE32),
  `src/libbz2/` (bzip2 1.0.6: se usa el del sistema) y lo que `gen-ui`
  genera en `src/ui/` (se regenera al compilar).
- **Sigue incluido:** el LZMA SDK (`src/lzma`, archivos de 2009-2010; el
  `lzma-sdk-devel` de Fedora es 25.x) y `libwbfs`; van como
  `Provides: bundled(...)`.
- **Parches:** los flags de Fedora (`-O3` y `-fomit-frame-pointer` los
  pisaban), bzip2 del sistema, la búsqueda de `<prefijo>/share/wit` (con
  wit en `/usr/bin` no encontraba nunca la base de títulos, que la app usa
  para los nombres de GameCube) y CET (un objeto de ensamblador vacío le
  sacaba IBT/SHSTK a los tres binarios).
- **Se instala:** `wit`, `wwt`, `wdf` y los alias `wdf-cat` y `wdf-dump`;
  `/usr/share/wit/` (base de títulos, menú del sistema, `magic.txt`); la
  documentación que genera `make doc`. **No se instalan:** `wfuse`
  (necesita FUSE 2 y la app no lo usa), `wtest` y `gen-ui` (de prueba y de
  compilación) y `load-titles.sh` (baja la base de títulos por HTTP plano
  y la escribe sobre los archivos del paquete).

### wiibackup-manager

Las dependencias están justificadas una por una, con archivo y línea, en
el propio spec. Dos que no salen a simple vista:

- `python3-gobject` aparte de `python3dist(pygobject)` (que da
  `python3-gobject-base`): el Ticket de Entrega le pasa un `cairo.Context`
  a PangoCairo y eso necesita `gi/_gi_cairo`, que está en
  `python3-gobject`.
- `gobject-introspection`: tiene el typelib de cairo, del que depende el de
  Gdk 4.0. Sin él no se puede importar GTK ("Typelib file for namespace
  'cairo', version '1.0' not found"), y ni gtk4, ni libadwaita, ni
  python3-gobject lo arrastran.

## Dónde se piden privilegios

Inventario para decidir más adelante una política de polkit. No se cambió
nada. Las reglas por defecto son las de los paquetes de Fedora 44 (polkit
127, udisks2 2.11.2).

| Dónde | Comando | Qué hace | Autorización |
|---|---|---|---|
| `drives.py:1223-1230` (`_privileged_format_command`), lanzado en `drives.py:1348-1350` desde `format_fat32` | `pkexec /bin/sh -c <_FORMAT_SCRIPT> wbm-format <disco> /sys/block <sectores> <identidad> <etiqueta> <sectores-por-clúster>`; sin `pkexec` si la app ya corre como root | El script de `drives.py:1120-1202`, como root: repite los blindajes (removible, tamaño, identidad con `udevadm info`, nada montado con `lsblk`), `wipefs -a` sobre las particiones y el disco, `parted -s mklabel msdos` y `mkpart primary fat32 1MiB 100%`, espera la partición, `wipefs -a` sobre ella, `mkfs.vfat -F 32 [-s] [-n]`, `udevadm trigger --action=change` y `udevadm settle` | `org.freedesktop.policykit.exec`: `auth_admin` siempre (contraseña de administrador). El diálogo es el genérico de pkexec para `/bin/sh`. 126/127 = cancelado o no autorizado (`drives.py:1218-1220`, `1359`) |
| ↳ quién llama a `format_fat32` | | Modo Fábrica: `window.py:682` (`_start_factory_format` → `format_as_wii_usb`); formatear después de Verificar Memoria: `memory_check_view.py:638` (`_start_format`) | |
| `drives.py:1006` (`_unmount_all`, desde `format_fat32`, `drives.py:1335`) | `umount <dispositivo>` | Desmonta lo que tenga montado el disco antes de pedir la contraseña | `/usr/bin/umount` es setuid root; para montajes de udisks delega en `umount.udisks2` |
| `drives.py:1034` (`_mount_as_user`, desde `format_fat32`, `drives.py:1375`) | `udisksctl mount -b <partición>` | Monta la partición recién formateada como el usuario | udisks2 `filesystem-mount`: `yes` en sesión local activa; `auth_admin_keep` si es un disco del sistema |
| `drives.py:329` (`eject_mount_point`, desde el botón de expulsar, `transfer_view.py:1206`) | `udisksctl unmount -b <dispositivo>` | Desmonta para expulsar | Sin acción especial si lo montó el mismo usuario; `filesystem-unmount-others` (`auth_admin_keep`) si lo montó otro |
| `drives.py:338` (ídem) | `udisksctl power-off -b <dispositivo>` | Apaga la unidad (mejor esfuerzo) | udisks2 `power-off-drive`: `yes` en sesión local activa |
| `drives.py:348` (ídem, si `udisksctl` falló) | `gio mount -u <punto>` | Desmonta vía GVfs | Pasa por udisks2, con las mismas acciones |

Fuera de la app instalada: `tools/manual_factory_mode_e2e.py` exige root
(`sudo python3 tools/...`, comprobado en su línea 404) y usa `losetup`,
`mount`, `umount`, `mkfs.vfat`, etc.; `pyproject.toml` no lo instala. Los
textos "sudo dnf install f3" de `f3_wrapper.py:517` y
`memory_check_view.py:213` son solo consejos en pantalla.

Para la decisión de polkit: el mismo udisks2 de Fedora deja
`modify-device` en `yes` para la sesión local activa (`auth_admin_keep`
para `modify-device-system`). Antes de diseñar el formateo sobre UDisks2
hay que confirmar en el código de udisks qué acción pide cada llamada
(`Block.Format`, `PartitionTable.CreatePartition`); eso no se verificó.
