# Manual del taller — WiiBackup Manager

Guía rápida para preparar USBs/SD de clientes sin sorpresas. Basado en revisión de seguridad de ChatGPT (30 de agosto, 2026), actualizado para la 0.2.0.

## Flujo recomendado para preparar una USB de cliente

1. Conectar la unidad
2. Correr **Verificar Memoria** (f3)
3. Si pasa → **Modo Fábrica**
4. Transferir los juegos con `verify_after_copy` **activado** en Ajustes
5. Esperar a que termine toda la cola
6. No entregar la unidad si algún juego quedó marcado **CORRUPTO**
7. Expulsar la unidad desde la app (nunca desconectar en frío)
8. Recién ahí, desconectar físicamente

## Las 5 reglas de oro

1. **No cerrar la app mientras F3 o `wit` estén trabajando.** Desde la 0.2.0 la app te frena sola: si intentás cerrar con una operación en curso pregunta, con **Seguir esperando** o **Cancelar operación y cerrar**. Elegí Seguir esperando salvo que sepas qué estás cortando. La excepción es el formateo: es el único que no se puede cancelar -corre con permisos de administrador, fuera de la app-, así que si cerrás ahí, la ventana desaparece pero el formateo sigue hasta terminar.
2. **Activar `verify_after_copy` en Ajustes.** No está prendido por defecto. Con esto activado, cada juego copiado se relee y confirma antes de darlo por bueno.
3. **Si una verificación da CORRUPTO, no entregar esa USB.** La app no borra el archivo automáticamente — identificá el juego, reemplazalo, volvé a copiar y a verificar. Si fallan varios juegos seguidos, sospechá del dispositivo y corré f3 de nuevo.
4. **Después de un F3 fallido o cancelado, revisar a mano si quedaron archivos `.h2w`.** El cleanup automático es "mejor esfuerzo" y a veces no logra borrarlos. No confundirlos con juegos, no entregar la unidad sin limpiarlos.
5. **Después de un crash a mitad de una operación, mirar el aviso de Recovery antes de preparar/entregar la unidad**, y si tenés dudas, revisar a mano si quedaron archivos ocultos con patrones `.parcial-`, `.wbm-staging-`, `.wbm-respaldo-`, `.respaldo-`.

## Sobre GameCube

`wit VERIFY` no funciona para juegos de GameCube (compara particiones de Wii, que GameCube no tiene). Un juego de GameCube copiado queda marcado como **"sin verificar"**, no como corrupto — es un estado distinto y esperado, no un error.

## Lo que NO hace falta que te preocupe

- Modo Fábrica identifica el USB por su identidad física real (serial/by-id), no solo por `/dev/sdX` — protegido contra el caso de desconectar un USB y conectar otro igual por error.
- Transferencia, Modo Fábrica, Verificar Memoria y Homebrew se bloquean entre sí automáticamente si intentan usar el mismo disco físico a la vez.
- Recovery no borra ni restaura nada sin tu confirmación explícita.
- F3 nunca arranca solo — siempre pide confirmación antes de escribir nada, y si la memoria no pasa no te ofrece formatearla: el botón de formatear aparece solo con un resultado limpio. Modo Fábrica sí se puede abrir por su cuenta desde la barra lateral, pero lo protege la misma lista blanca de discos removibles que ese camino.
