#!/usr/bin/env bash
# Regenera la plantilla de traducción y actualiza los catálogos existentes.
#
# Se corre después de agregar o cambiar textos de la interfaz:
#
#     ./tools/update-translations.sh
#
# Qué hace, en orden:
#   1. Vuelve a extraer del código todos los textos marcados con _() y
#      ngettext() a data/locale/wiibackup-manager.pot.
#   2. Fusiona ese .pot con cada .po que ya exista (msgmerge), así las
#      traducciones ya hechas se conservan y solo aparecen como pendientes
#      las cadenas nuevas o las que cambiaron.
#   3. Compila cada .po a .mo, que es el formato binario que lee la app.
#
# Requiere gettext (paquete `gettext` en Fedora/Debian).
set -euo pipefail

cd "$(dirname "$0")/.."
DOMAIN="wiibackup-manager"
POT="data/locale/$DOMAIN.pot"

# El idioma del código fuente es el español: los msgid son las cadenas en
# español, así que no hay un catálogo es/ que mantener (ver i18n.py).
xgettext \
    --language=Python \
    --keyword=_ \
    --keyword=ngettext:1,2 \
    --from-code=UTF-8 \
    --package-name="WiiBackup Manager" \
    --msgid-bugs-address="https://github.com/KendryGod/wiibackup-manager-linux/issues" \
    --add-comments=TRANSLATORS \
    --sort-by-file \
    --output="$POT" \
    $(find wiibackup_manager -name '*.py' | sort)

# El .pot sale con "charset=CHARSET" hasta que se lo inicializa; los .po de
# verdad ya traen UTF-8, así que se corrige solo en la plantilla.
sed -i 's/charset=CHARSET/charset=UTF-8/' "$POT"

for po in data/locale/*/LC_MESSAGES/$DOMAIN.po; do
    [ -e "$po" ] || continue
    echo "→ $po"
    msgmerge --update --backup=none --quiet "$po" "$POT"
    msgfmt --check --output-file="${po%.po}.mo" "$po"
done

echo "Listo. Cadenas en la plantilla: $(grep -c '^msgid' "$POT")"
