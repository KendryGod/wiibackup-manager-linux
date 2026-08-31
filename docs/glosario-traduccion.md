# Glosario de traducción

Las decisiones de vocabulario del catálogo de inglés, para que la próxima
tanda de traducción no vuelva a elegirlas desde cero.

Acá **no** está la mecánica de gettext —cómo crear un catálogo nuevo, qué
hacen los marcadores `{}`, cómo funcionan los plurales—: eso está en el
README, en [Idiomas y traducciones](../README.md#idiomas-y-traducciones).
Esto es lo otro: qué palabra se usa para qué, y por qué.

## Por qué existe

Los textos se traducen por tandas, cada varias sesiones de trabajo. Entre
una tanda y la siguiente es fácil que la misma idea salga traducida de dos
formas distintas —"Verificar Memoria" como *Memory Check* en el sidebar y
como *Verify integrity* en el título de la página— y el usuario que ve las
dos pantallas no tiene forma de saber que son lo mismo.

Fijar el vocabulario una vez y anotarlo cuesta menos que descubrir la
inconsistencia cuando ya está en tres lugares.

## Lo que no se traduce

Nombres propios y términos del dominio. Un usuario que busca ayuda sobre
"WBFS" o "Nintendont" busca esas palabras, no una traducción de ellas:

> Wii · GameCube · WBFS · CISO · ISO · Homebrew · GameTDB · Nintendont ·
> USB Loader GX · Open Shop Channel · FAT32 · SD · USB ·
> `wit` · `f3` / `f3write` / `f3read` · GameFix SPS · WiiBackup Manager

También quedan igual los nombres de accesorios que ya son marcas (Wii
Remote, Nunchuk, Classic Controller, Wii Balance Board, Game Boy Advance,
Mii…). En el `.po` esas entradas tienen `msgstr` idéntico al `msgid`, y
eso es **intencional**: no son cadenas sin traducir.

## Nombres de las secciones

Una sola traducción por sección, en todos los lugares donde aparezca —el
sidebar, el título de la página, y cualquier texto que la mencione:

| Español | Inglés |
|---|---|
| Juegos | Games |
| Transferir | Transfer |
| Verificar Memoria | **Memory Check** |
| Modo Fábrica | **Factory Mode** |
| Homebrew Store | Homebrew Store |
| Ajustes | Settings |
| Ticket de Entrega | **Delivery Receipt** |

El Ticket es el único con dos formas, y sigue al original: `Delivery
Receipt` en el título del PDF (que es el nombre de un documento) y
`Delivery receipt` en el diálogo y el menú, donde el español también usa
minúscula.

## Vocabulario recurrente

| Español | Inglés |
|---|---|
| unidad | drive |
| memoria | memory |
| destino | destination |
| biblioteca | library |
| cola | queue |
| respaldo | backup |
| resto (de una operación) | leftover |
| papelera | trash |
| pendrive | USB stick |
| trucho | fake |
| configuración maestra | master configuration |
| ticket | receipt |
| expulsar | eject |

"Memoria" es el único con un matiz: suelto es *memory* ("The memory is
genuine"), pero cuando va contado necesita el sustantivo —*memory
device(s)*, como en "Look for connected memory devices again"—, porque
*memory* en inglés no se cuenta.

## La trampa: "verificar" son dos cosas

En esta app "verificar" significa dos cosas distintas, y mezclarlas hace
que el usuario crea que un botón hace lo que hace el otro:

- **La prueba de `f3` sobre una memoria** —escribir todo el espacio libre
  y volver a leerlo para ver si el pendrive es trucho— es **`check`**.
  *Memory Check*, *Check memory…*, *The check could not be completed*.
- **La verificación de integridad de un juego con `wit`** es **`verify`**.
  *Verify*, *Verify integrity*, *Verifying*.

Son operaciones sin nada que ver: una mira el hardware, la otra mira un
archivo. En español el verbo es el mismo por casualidad; en inglés no hay
por qué arrastrar esa casualidad.

Adentro del `verify` hay una segunda distinción, y esta no es de estilo:
**"no pasó la verificación" y "quedó sin verificar" son cosas distintas**.
Un archivo que `wit` miró y rechazó es *did not pass verification*; uno
que nadie llegó a mirar —timeout, `wit` que falta, verificación
cancelada— es *left unverified*. Traducir el primero como *unverified*
diría que no se comprobó, que es justo lo contrario de lo que pasó.

| Español | Inglés |
|---|---|
| Copiado, pero no verificó | **Copied, but did not pass verification** |
| quedó sin comprobar | **was left unverified** |

## Convenciones de forma

- **Ortografía**: `cancelled` / `cancelling` con doble L, que es lo que ya
  usaba el catálogo antes de esta ronda. Se mantiene por consistencia
  interna, no por preferencia.
- **Comillas**: `«{name}»` del español pasa a `“{name}”`. Las comillas
  simples del original (`'{name}'`) se dejan simples.
- **Se conservan tal cual**: los marcadores `{...}`, las llaves escapadas
  `{{word}}`, y los caracteres que la interfaz usa como separadores o
  íconos —`·`, `…`, `✓`— y los saltos `\n`.
- **El guion de inciso** del original (`-nunca un disco interno`) se
  mantiene con el mismo estilo en la traducción.

## Referencias cruzadas entre pantallas

Cuando una cadena le dice al usuario que use un botón, el texto tiene que
coincidir **exactamente** con la etiqueta real de ese botón. Si se traduce
el botón y no el mensaje que lo nombra, el usuario busca algo que no
existe.

Los casos que hay hoy:

| El mensaje dice… | …y tiene que coincidir con |
|---|---|
| `Use 'Clear finished' to send them again.` | el botón **Clear finished** |
| `that is what Factory Mode is for` | la sección **Factory Mode** |

Al agregar un mensaje que mencione otra parte de la interfaz, sumarlo acá.

## Cómo comprobar que un catálogo respeta esto

La suite ya cubre lo que se puede verificar sin criterio humano —que no
queden cadenas sin traducir, que los marcadores coincidan, que el `.mo`
esté al día con el `.po`:

```bash
pytest tests/test_i18n.py
```

Para lo léxico, que ninguna prueba puede decidir sola, sirve mirar dos
cosas a mano sobre `data/locale/en/LC_MESSAGES/wiibackup-manager.po`:

```bash
PO=data/locale/en/LC_MESSAGES/wiibackup-manager.po

# 1. Entradas con msgstr idéntico al msgid: casi todas tienen que ser
#    nombres propios de la lista de arriba. La excepción son las palabras
#    que en inglés se escriben igual -Error, General, Log, Publisher,
#    Developer- y las cadenas que son puro marcador ("{n} ok"). Cualquier
#    OTRA cosa es una cadena que se pasó por alto.
msgattrib --no-obsolete --translated --no-wrap "$PO" \
  | awk '/^msgid /{id=substr($0,7)}
         /^msgstr /{if (substr($0,8)==id && id!="\"\"") print id}'

# 2. Un término del glosario en contexto, para ver que se tradujo igual en
#    todos lados (cambiar "unidad" por el término que se esté revisando)
msgattrib --no-obsolete --translated --no-wrap "$PO" \
  | awk '/^msgid /{id=$0}
         /^msgstr /{if (tolower(id) ~ /unidad/) print id"\n  "$0"\n"}'
```

El `--no-wrap` no es decorativo: sin él, las cadenas largas quedan
partidas en varias líneas y `grep`/`awk` solo ven el primer pedazo.

## Para un idioma nuevo

Este glosario es del inglés, pero las dos decisiones de fondo valen para
cualquier idioma: **los nombres propios no se traducen**, y **cada sección
tiene un solo nombre en toda la app**. Al arrancar un catálogo nuevo,
copiar la estructura de este archivo y llenarla antes de traducir las más
de 400 cadenas de la plantilla sale más barato que corregirlas después.
