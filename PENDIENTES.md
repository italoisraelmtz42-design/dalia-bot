# Tareas pendientes del bot

Lista de mejoras identificadas pero **no autorizadas todavía** — sirve para no perder el hilo entre sesiones. Nada de esto se implementa hasta que Israel lo apruebe explícitamente.

---

## 1. Mensaje de seguimiento automático a clientes silenciosos (~23 h)

**Objetivo:** si un cliente mostró interés (habló con el bot) pero dejó de responder, mandarle un mensaje amable de seguimiento antes de que se cierre la ventana de 24 h de WhatsApp, para intentar recuperar la venta.

**Restricción de Meta (clave para el diseño):** WhatsApp solo deja mandar texto libre dentro de las 24 h desde el último mensaje del cliente. Pasada esa ventana, solo se puede mandar una plantilla ("message template") pre-aprobada por Meta — con revisión previa y normalmente clasificada como plantilla de "Marketing" (tiene costo por conversación).

**Por qué mandarlo a las ~23 h (no a las 24 h) resuelve esto:** si el seguimiento se manda con margen (23 h desde el último mensaje del cliente, 1 h de colchón), todavía cae DENTRO de la ventana de 24 h. Eso significa que cuenta como un mensaje normal de la conversación: el bot lo puede redactar libre, sin plantilla aprobada por Meta, sin revisión previa y sin el costo extra de plantilla de Marketing. Este es el motivo por el que esta ruta es viable y la de "a las 24 h o después" no lo era.

**Diseño técnico propuesto — hilo en background dentro del mismo proceso (`app.py`):**
- Confirmé en Render que el servicio `dalia-bot` tiene un disco persistente adjunto (donde vive `dalia_bot.db`), y Render **no permite escalar a más de una instancia** un servicio con disco ("Scaling is not supported for servers with disks"). Esto garantiza que SIEMPRE va a correr una sola instancia del proceso — por lo tanto un hilo en background dentro de la misma app (con `threading`, igual patrón que ya se usa para procesar mensajes entrantes) es seguro: no hay riesgo de que dos instancias manden el mismo seguimiento duplicado.
- Se descartó usar un "Cron Job" de Render como servicio separado porque el disco persistente solo se puede adjuntar a UN servicio a la vez — un Cron Job aparte no podría leer `dalia_bot.db` directamente. (Sería posible si ese cron le pegara por HTTP a un endpoint del mismo servicio web, pero es más complejo que la opción de abajo sin ninguna ventaja real para este caso.)
- Propuesta: un hilo que se despierta cada 15-30 minutos y revisa `historial_chat` buscando conversaciones donde:
  - El último mensaje es del `usuario` (cliente) hace ~23 h, sin que haya vuelto a escribir desde entonces.
  - El pedido asociado (si existe) NO está en estado `ANTICIPO_CONFIRMADO` (no tiene caso darle seguimiento a quien ya pagó).
  - Todavía no se le mandó un seguimiento a esa "conversación silenciosa" (para esto hace falta agregar una marca nueva, ej. columna `seguimiento_enviado` o una tabla chica de control, para no mandarlo dos veces).
- Aplica solo a WhatsApp por ahora — para Messenger, fuera de la ventana estándar Meta solo permite re-enganche con "tags" para casos muy específicos, no para un seguimiento genérico tipo "¿sigues interesada?".

**Qué falta decidir antes de programarlo:**
- El texto exacto del mensaje de seguimiento (tono, qué tan directo/vendedor debe sonar).
- Confirmar el criterio de "cliente silencioso" (¿aplica a cualquiera que habló con el bot, o solo a quien llegó a ver precio/cotización?).
- Si se debe reintentar un segundo seguimiento más adelante o solo mandarlo una vez.

**Complejidad estimada:** media. No requiere nada de Meta (sin plantillas, sin revisión), solo cambios en `app.py` (el hilo de background) y un ajuste chico en la base de datos para marcar quién ya recibió su seguimiento.

---

## 2. Bot revela que es un sistema automatizado ("Base de Conocimiento")

Detectado en el análisis de conversaciones reales (15 ago): un cliente preguntó por el teléfono del negocio y el bot respondió *"...en la Base de Conocimiento no aparece un número directo..."* — usando terminología interna que un cliente no debería ver. Ya existe una regla en el prompt contra esto (`051_Frases_Que_Un_Humano_No_Dice.txt`), pero se le sigue escapando al modelo en casos como este. Reforzar la instrucción con más ejemplos explícitos de qué NO decir.

## 3. El bot "habla mucho" — mensajes muy densos

Patrón detectado en conversaciones reales: el bot mete demasiada información y varias preguntas en un solo mensaje (ej. dirección + horario + link de Maps + oferta de catálogo, todo junto cuando el cliente solo preguntó la dirección; o pedir los 5 campos de una dirección de un jalón). Se siente menos natural que una conversación real de WhatsApp. Ajustar el prompt para que sea más conciso por mensaje y solo dé información extra si el cliente la pide.

## 4. Precio de mayoreo no definido para todos los productos (pregunta de negocio, no bug)

`osito con jaboncito` y `kit osito + oración + velita` no tienen descuento por cantidad, a diferencia de peluche/dominó/abanico que sí. Se vio un pedido real de 200 kits cotizado sin ningún descuento. Puede ser intencional — confirmar con Israel si quiere agregarles un precio de mayoreo.

---

## Ya conocidos de antes (en pausa, no tocar sin pedirlo explícitamente)

- **Verificación de negocio de Meta** para poder publicar la app "chatbot ositos" — requiere documentación oficial que el negocio (informal, sin RFC) no tiene. Israel lo está evaluando.
- **Plantilla de WhatsApp para `notificar_a_dalia`** (error 131047 en producción) — instrucción histórica de "no seguirle" hasta nueva indicación explícita.
