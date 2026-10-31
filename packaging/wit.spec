# Wiimms ISO Tools (wit) para el proyecto COPR de WiiBackup Manager.
#
# La app usa solo el binario `wit` (wit_wrapper.find_wit: shutil.which
# sobre "wit", o la ruta que se elija en Preferencias) y está probada
# contra wit v3.05a r8638. Se instalan también las otras herramientas de
# línea de comandos de la suite (wwt, wdf y sus alias wdf-cat y
# wdf-dump), que no suman dependencias. Ver packaging/README.md.

# Revisión SVN de la release 3.05a: la que llevan los binarios oficiales
# (wit-v3.05a-r8638-*.tar.gz en https://wit.wiimm.de/download.html). El
# tarball de fuentes no la trae y, sin ella, `wit --version` dice "r0".
%global svnrev 8638

# Lo que se le pasa a cada make: sin strip (el Makefile despoja cada
# binario apenas lo enlaza, y así el -debuginfo quedaba vacío) y con el
# prefijo real, que va a version.h como SHARE_PATH (lo usa
# `wit GETTITLES`) y a la documentación generada.
%global wit_make_args STRIP=: INSTALL_PATH=%{_prefix}

Name:           wit
Version:        3.05a
Release:        1%{?dist}
Summary:        Command line tools to manipulate Wii and GameCube disc images

# El código de wit (src/, dclib/ y lo que gen-ui genera en src/ui/) es
# GPL-2.0-or-later: sus 113 cabeceras con licencia GPL dicen "either
# version 2 of the License, or (at your option) any later version" y
# ninguna limita a la versión 2. Además quedan compilados en los binarios:
#   src/lzma/              LZMA SDK (Igor Pavlov): dominio público, con el
#                          texto ya revisado por Fedora Legal
#                          (public-domain-text.txt de fedora-license-data).
#   src/libwbfs/rijndael.c AES de Mike Scott (1999): "Permission for free
#                          direct or derivative use is granted subject to
#                          compliance with any conditions that the
#                          originators of the algorithm place on its
#                          exploitation." No tiene identificador SPDX ni
#                          está en fedora-license-data: va como LicenseRef.
# No quedan en los binarios: src/libbz2 (se borra en %%prep y se usa el
# bzip2 del sistema), el SHA-1 de src/crypto (con openssl-devel se usa el
# de OpenSSL, -DUSE_SHA_LIB) y dclib-punycode (DCLIB_NETWORK=0).
License:        GPL-2.0-or-later AND LicenseRef-Fedora-Public-Domain AND LicenseRef-Rijndael-Mike-Scott
URL:            https://wit.wiimm.de/
# El enlace "Source code / Download" de https://wit.wiimm.de/.
Source0:        https://download.wiimm.de/source/wiimms-iso-tools/wiimms-iso-tools.source-%{version}.txz
# Los motivos de cada parche están en su cabecera.
Patch0:         wit-3.05a-fedora-cflags.patch
Patch1:         wit-3.05a-system-bzip2.patch
Patch2:         wit-3.05a-share-path.patch
Patch3:         wit-3.05a-cet.patch

BuildRequires:  gcc
BuildRequires:  make
# -ltinfo y -lncurses del Makefile: sin esto no enlaza.
BuildRequires:  ncurses-devel
# Imágenes GCZ (lib-gcz.c): setup.sh las activa si encuentra zlib.h. El
# wit con que se probó la app las tiene.
BuildRequires:  zlib-devel
# SHA-1 de OpenSSL en vez de la copia de src/crypto (setup.sh: HAVE_SHA).
BuildRequires:  openssl-devel
# Patch1.
BuildRequires:  bzip2-devel

# Copias de terceros que siguen compiladas adentro (ver License).
Provides:       bundled(lzma-sdk)
Provides:       bundled(libwbfs)

%description
Wiimms ISO Tools is a set of command line tools to extract, modify and
create Wii and GameCube ISO images and WBFS containers. This package
contains wit (Wiimms ISO Tool), wwt (Wiimms WBFS Tool) and wdf (Wiimms
WDF Tool, also available as wdf-cat and wdf-dump), plus the title
database they use to name the games.

%prep
# Upstream no firma sus tarballs: se fija el sha256 del oficial.
echo "e5a8a488f26465ff4ec03f10990ee28e104eb4542990812bbdbea03af12ed8bd  %{SOURCE0}" | sha256sum -c -
%autosetup -n wiimms-iso-tools.source-%{version} -p1
# Instalador de Windows precompilado (PE32): no se usa ni se distribuye.
rm setup/windows-install.exe
# bzip2 1.0.6 incluido: se usa el del sistema (Patch1).
rm -r src/libbz2
# Código que gen-ui genera desde src/ui/tab-*.inc: se regenera al compilar.
rm src/ui/ui.def src/ui/ui-*.c src/ui/ui-*.h
echo "revision=%{svnrev}" > revision.sh

%build
%set_build_flags
# Sin wfuse (ver %%install), aunque el buildroot tenga un fuse.h.
export NO_FUSE=1
# El Makefile no declara que wit-mix.o necesita src/ui/ui-wit.h, que lo
# genera gen-ui: en paralelo, wit-mix.c se compilaba antes y fallaba
# siempre. Por eso primero gen-ui y lo que genera (en serie y una sola
# vez: su regla tiene varios objetivos) y después el resto.
%make_build %{wit_make_args} gen-ui
%{__make} %{wit_make_args} ui
%make_build %{wit_make_args} wit wwt wdf
# La documentación que trae la distribución oficial en doc/.
%{__make} %{wit_make_args} doc
# Su plantilla (templates/wit-source.txt) tiene fines de línea CRLF.
sed -i 's/\r$//' doc/wit-source.txt

%install
install -d %{buildroot}%{_bindir}
install -p -m 0755 wit wwt wdf %{buildroot}%{_bindir}/
# wdf reconoce estos alias por el nombre con que se lo llama (argv[0]).
ln -s wdf %{buildroot}%{_bindir}/wdf-cat
ln -s wdf %{buildroot}%{_bindir}/wdf-dump
# Base de títulos (de GameTDB), menú del sistema y patrones para file(1):
# wit los busca en <prefijo>/share/wit (Patch2).
install -d %{buildroot}%{_datadir}/wit
install -p -m 0644 share/*.txt %{buildroot}%{_datadir}/wit/
# Fuera a propósito:
# - wfuse: necesita FUSE 2 y la app no lo usa.
# - wtest y gen-ui: herramientas de prueba y de compilación.
# - load-titles.sh: baja la base de títulos por HTTP plano
#   (http://gametdb.com/titles.txt) y la escribe sobre estos mismos
#   archivos de /usr/share/wit, que son del paquete.

%check
# La versión con la que está probada la app (wit_wrapper.wit_version).
%{buildroot}%{_bindir}/wit --version
%{buildroot}%{_bindir}/wit --version | grep -F ' v%{version} r%{svnrev} '
for alias in wdf-cat wdf-dump; do
    %{buildroot}%{_bindir}/$alias --version | grep "^$alias v%{version} "
done
# wit encuentra la base de títulos instalada (Patch2). Desde un directorio
# vacío, para que no la tome de ./titles.txt.
mkdir check-vacio
pushd check-vacio
test "$(%{buildroot}%{_bindir}/wit TITLES | wc -l)" -eq "$(grep -c . ../share/titles.txt)"
popd

%files
%license gpl-2.0.txt
%doc doc/DOCUMENTATION.txt doc/FAQ.txt doc/HISTORY.txt doc/WBFS.txt doc/WDF.txt
%doc doc/wit.txt doc/wwt.txt doc/wit-source.txt
%{_bindir}/wit
%{_bindir}/wwt
%{_bindir}/wdf
%{_bindir}/wdf-cat
%{_bindir}/wdf-dump
%{_datadir}/wit/

%changelog
* Fri Oct 30 2026 Kendry <kendryflores1234@gmail.com> - 3.05a-1
- Primera versión, para el proyecto COPR de WiiBackup Manager
