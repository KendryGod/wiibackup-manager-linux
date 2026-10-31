# WiiBackup Manager para el proyecto COPR (Fedora). Ver packaging/README.md.

%global appid com.gamefixsps.WiiBackupManager

Name:           wiibackup-manager
Version:        0.2.4
Release:        1%{?dist}
Summary:        Manage Wii and GameCube backups and prepare drives for them

# LICENSE del repo. Los dos archivos de assets/configs son de relleno,
# escritos para la app (golden_configs.py): misma licencia.
License:        MIT
URL:            https://github.com/KendryGod/wiibackup-manager-linux
Source0:        %{url}/archive/refs/tags/v%{version}.tar.gz#/wiibackup-manager-linux-%{version}.tar.gz

BuildArch:      noarch

BuildRequires:  python3-devel
# %%check: validar el .desktop y el metainfo instalados.
BuildRequires:  desktop-file-utils
BuildRequires:  appstream
# %%check: la suite. Casi todos los módulos cargan GTK 4 y libadwaita por
# introspección al importarse, pdf_export.py usa Pango, PangoCairo y
# GdkPixbuf, y las pruebas de traducción corren msgfmt.
BuildRequires:  python3-pytest
BuildRequires:  python3-gobject
BuildRequires:  gtk4
BuildRequires:  libadwaita
BuildRequires:  pango
BuildRequires:  gdk-pixbuf2
# Ver el Requires de abajo: sin esto no se importa ningún módulo con GTK.
BuildRequires:  gobject-introspection
BuildRequires:  gettext
# %%check: el smoke test, como en el CI (display virtual y bus de sesión).
BuildRequires:  xorg-x11-server-Xvfb
BuildRequires:  xorg-x11-xauth
BuildRequires:  dbus-daemon

# PyGObject y pycairo los agrega solos el generador de dependencias de
# Python, desde pyproject.toml: python3dist(pygobject) >= 3.42 y
# python3dist(pycairo) >= 1.18.
#
# python3dist(pygobject) lo da python3-gobject-base, pero el Ticket de
# Entrega le pasa un cairo.Context de pycairo a PangoCairo
# (pdf_export.py:342), y eso necesita gi/_gi_cairo, que está en
# python3-gobject.
Requires:       python3-gobject
# Gtk 4.0 y Gdk 4.0 (app.py:7, window.py:11-13, styles.py:17-18).
Requires:       gtk4
# El typelib de cairo, del que depende el de Gdk 4.0: sin él, cualquier
# `from gi.repository import Gtk` falla ("Typelib file for namespace
# 'cairo', version '1.0' not found", medido en mock). Ni gtk4 ni
# libadwaita ni python3-gobject lo arrastran.
Requires:       gobject-introspection
# Adw 1 (app.py:8). 1.5 es el mínimo real: Adw.AlertDialog, Adw.Dialog,
# Adw.AboutDialog y Adw.PreferencesDialog.
Requires:       libadwaita >= 1.5
# Pango y PangoCairo (pdf_export.py:65-66, homebrew_store_view.py:23).
Requires:       pango
# GdkPixbuf (pdf_export.py:67, gametdb.py:104).
Requires:       gdk-pixbuf2
# `gio mount -u` (drives.py:348). Los typelibs de Gio, GLib y GObject
# también son de glib2, pero esos ya llegan con python3-gobject-base
# (libgirepository-2.0 y libglib-2.0).
Requires:       /usr/bin/gio
# Convertir, verificar, comparar, listar y copiar a la unidad. La app está
# probada con 3.05a (wit_wrapper.py).
Requires:       wit >= 3.05a
# `udisksctl unmount`, `power-off` y `mount` (drives.py:329, 338, 1034).
Requires:       udisks2
# findmnt (drives.py:254, 277), umount (drives.py:1006) y lsblk
# (drives.py:1092), y wipefs y lsblk en el script de formateo
# (drives.py:1154-1190).
Requires:       util-linux
# udevadm (drives.py:586, y 1132, 1173, 1199-1200 en el script de formateo).
Requires:       systemd-udev
# pkexec: Modo Fábrica y el formateo después de Verificar Memoria
# (drives.py:1230).
Requires:       polkit
# parted, en el script de formateo (drives.py:1170-1171).
Requires:       parted
# mkfs.vfat, en el script de formateo (drives.py:1195).
Requires:       dosfstools
# awk, en el script de formateo (drives.py:1160). Sin él, el `for` sobre
# las particiones viejas queda vacío sin cortar el `set -e`: el script
# llega a `wipefs -a` y `parted` sobre el disco y recién corta después,
# con código 127 (medido con sh).
Requires:       gawk
# Dueño de los directorios /usr/share/icons/hicolor/*/apps de los íconos.
Requires:       hicolor-icon-theme

# Verificar Memoria: f3write y f3read (f3_wrapper.py:77-78).
Recommends:     f3
# El código QR del Ticket de Entrega (pdf_export.py:321). La app prefiere
# segno, que no está en Fedora; sin ninguno el ticket sale con el número
# en texto.
Recommends:     python3-qrcode
# La letra del cuerpo del Ticket de Entrega (pdf_export.py:85-86). Sin
# ella Pango usa la siguiente de la lista (Adwaita Sans, Cantarell...).
Suggests:       rsms-inter-fonts

%description
WiiBackup Manager keeps a library of Wii and GameCube backups (ISO, WBFS,
CISO and WDF) in order and prepares USB drives and SD cards to play them
with USB Loader GX and Nintendont: it converts and verifies games with
wit, copies them to FAT32 drives, formats drives (Factory Mode), detects
fake-capacity drives with f3, installs homebrew from the Open Shop
Channel and prints a delivery receipt in PDF.

%prep
%autosetup -n wiibackup-manager-linux-%{version} -p1

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l wiibackup_manager
%find_lang wiibackup-manager

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{appid}.desktop
appstreamcli validate --no-net --explain %{buildroot}%{_metainfodir}/%{appid}.metainfo.xml
%pyproject_check_import
# Algunas pruebas importan otras como `tests.<módulo>`
# (test_caratulas_cierre.py:25), y eso necesita la raíz del repo en
# sys.path: el CI la tiene porque corre `python3 -m pytest` desde ahí. Va
# AL FINAL de PYTHONPATH para que `wiibackup_manager` se siga importando
# del buildroot, y no del árbol de fuentes; la línea de abajo lo comprueba.
export PYTHONPATH="%{buildroot}%{python3_sitelib}:$PWD"
%{python3} -P -c 'import sys, wiibackup_manager as m; print(m.__file__); sys.exit(not m.__file__.startswith("%{buildroot}/"))'
# Las pruebas de lógica: el mismo subconjunto que el CI.
%pytest -m "not gui"
# El smoke test y las demás pruebas con GUI, también como el CI.
# WBM_REQUIRE_GUI=1 hace que fallen en vez de saltearse si no hay display.
# GDK_DISABLE=gl: el buildroot no tiene OpenGL y, al abrir una ventana,
# GTK abortaba adentro de libepoxy (gdk_gl_context_make_current); sin GL
# dibuja con cairo.
export WBM_REQUIRE_GUI=1 GDK_BACKEND=x11 GSETTINGS_BACKEND=memory GDK_DISABLE=gl
%{py3_test_envvars} dbus-run-session -- \
    xvfb-run -a -s "-screen 0 1280x1024x24" %{__pytest} -m gui

%files -f %{pyproject_files} -f wiibackup-manager.lang
%doc README.md MANUAL_TALLER.md
%{_bindir}/wiibackup-manager
%{_datadir}/applications/%{appid}.desktop
%{_metainfodir}/%{appid}.metainfo.xml
%{_datadir}/icons/hicolor/*/apps/%{appid}.png
%{_datadir}/wiibackup-manager/

%changelog
* Fri Oct 30 2026 Kendry <kendryflores1234@gmail.com> - 0.2.4-1
- Primera versión, para el proyecto COPR
