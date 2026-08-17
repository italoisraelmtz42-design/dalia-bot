# Tareas pendientes del bot

Lista de mejoras identificadas pero **no autorizadas todavía** — sirve para no perder el hilo entre sesiones. Nada de esto se implementa hasta que Israel lo apruebe explícitamente.

---

## 1. Mensaje de seguimiento automático a clientes silenciosos — ✅ IMPLEMENTADO (17 ago 2026, extendido a Messenger el mismo día)

**Estado real de lo que quedó programado** (autorizado por Israel, mensaje exacto proporcionado por él):
- Mensaje (mismo texto para ambos canales): *"Hola! Qué tal! gustas que continuemos con tu pedido? Cualquier duda estoy a la orden!"*
- Hilo en background dentro de `app.py` (`iniciar_hilo_seguimientos`), revisa cada 15 min, uno por uno los dos canales.
- Ventana de seguridad POR CANAL (cada uno se revisa aparte):
  - WhatsApp: dispara si el silencio lleva entre 23.0 y 23.5 horas.
  - Messenger: dispara si el silencio lleva entre 22.0 y 22.5 horas -- colchón más amplio (2h) a petición explícita de Israel el 17 ago 2026 ("para que no haya falla y no nos penalice Meta"), porque ahora mismo la mayoría de los clientes entran por Messenger.
  - Investigación previa a implementar Messenger (ago 2026, developers.facebook.com): Messenger también tiene una ventana de 24h donde se permite mandar cualquier mensaje (incluyendo promocional) sin plantilla, igual que WhatsApp -- confirmado en la documentación oficial del Send API. Fuera de esa ventana, Meta eliminó en feb 2026 la mayoría de los "message tags" que antes permitían re-enganchar (CONFIRMED_EVENT_UPDATE, ACCOUNT_UPDATE, POST_PURCHASE_UPDATE ya no sirven); lo único que queda (Utility Messages, Marketing Messages API con opt-in) NO aplica a un mensaje genérico como este. Por eso el mecanismo nunca manda fuera de la ventana de 24h, en ningún canal.
- Criterio final de "cliente silencioso" que sí aplica (igual en ambos canales): con un borrador de pedido en progreso (`producto` o `items` en `borradores_pedido`), sin que el bot esté en modo DALIA (cubre tanto silencios manuales como pedidos con anticipo ya confirmado), y sin bot pausado globalmente.
- Candado atómico en SQLite (tabla `seguimientos_23h`, con `UNIQUE(telefono, marca_ultimo_mensaje_cliente)`, columna `canal` incluida) para nunca mandarlo dos veces por el mismo silencio -- si el cliente vuelve a escribir y luego se queda callado otra vez, sí puede recibir un seguimiento nuevo para ese silencio distinto.
- Solo se manda una vez por silencio (no hay reintento posterior) -- si falla el envío (ej. error de red), se deja así, no se reintenta para ese mismo silencio.
- Messenger: se manda usando la única página de Facebook configurada hoy (confirmado en Render que no hay una segunda página). Si en el futuro se conecta una segunda página, este mecanismo necesitaría guardar de qué página vino cada conversación -- hoy esa información no se guarda en la base de datos, así que habría que agregarla antes.
- Probado localmente con una base de datos de prueba antes de desplegar (ventana de WhatsApp): la ventana de tiempo filtra correctamente, el candado bloquea el doble envío, y el hilo no truena el proceso si el envío falla. La versión de Messenger reutiliza exactamente la misma lógica, solo cambia el canal y la ventana de horas.

**Objetivo original:** si un cliente mostró interés (habló con el bot) pero dejó de responder, mandarle un mensaje amable de seguimiento antes de que se cierre la ventana de 24 h de WhatsApp, para intentar recuperar la venta.

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
