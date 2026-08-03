# Qué cambié y por qué

## 1. `app.py` — ahora sí es un chatbot funcional
Antes: el webhook solo recibía el mensaje, lo imprimía en consola y respondía "EVENT_RECEIVED". Nunca llamaba a OpenAI ni contestaba al cliente.

Ahora:
- Al iniciar el servidor, carga toda tu base de conocimiento (igual que hacía `main.py`).
- Cuando llega un mensaje real de WhatsApp, arma el prompt con las reglas + la base de conocimiento + el estado del pedido de ESE cliente, lo manda a OpenAI, y **envía la respuesta de vuelta por WhatsApp automáticamente**.
- Cada número de teléfono tiene su propia sesión (`sesiones[numero]`), con su propio historial de mensajes y su propio pedido en construcción. Así dos clientes escribiendo al mismo tiempo no se mezclan.
- Ignora sin tronar las notificaciones que WhatsApp manda que no son mensajes de texto (confirmaciones de entrega, de lectura, etc.).
- El token de verificación del webhook (`hub.verify_token`) ya no está escrito en el código: se lee del `.env` (`WHATSAPP_VERIFY_TOKEN`).
- `debug=True` de Flask ya no está fijo; se controla con `FLASK_DEBUG` en `.env` y por default es `false` (más seguro para producción).

## 2. Seguridad — llaves fuera del código
- `.env.example` es la plantilla nueva, sin secretos reales. Cópiala como `.env` y llena tus datos.
- `prueba_whatsapp.py` ya no trae tu token pegado en el archivo; lo toma del `.env`.
- **Importante, esto sigue pendiente de tu lado:** el `.env` original que subiste tenía tu API key de OpenAI y tu token de WhatsApp reales. Si no lo has hecho ya, revócalos y genera unos nuevos antes de usar este proyecto en producción, y pon los nuevos en tu `.env` local (que nunca debe compartirse ni subirse a ningún lado).

## 3. `requirements.txt` actualizado
Antes solo tenía `openai` y `python-dotenv`, pero `app.py` también necesita `flask` y `requests`. Ya están agregados los cuatro.

## 4. `main.py` — lo dejé intacto
Sigue sirviendo como herramienta de prueba en consola, útil para probar cambios en la base de conocimiento sin necesidad de WhatsApp.

---

## Cómo probarlo
1. Copia `.env.example` a `.env` y llena tus datos reales (llaves nuevas, ya rotadas).
2. `pip install -r requirements.txt`
3. `python app.py`
4. Expón tu servidor a internet (por ejemplo con `ngrok http 5000`) y configura esa URL + tu `WHATSAPP_VERIFY_TOKEN` en el panel de Meta como webhook.
5. Escríbele al número de WhatsApp Business conectado y debería contestarte usando la base de conocimiento.

## Lo que NO alcancé a resolver (para que lo tengas en el radar)
- **El "pedido" no se llena solo todavía.** El diccionario `pedido` (producto, cantidad, colores, etc.) existía desde el `main.py` original, pero ningún código lo actualizaba automáticamente — nunca se llenaba, y por lo tanto el bot no llevaba un registro estructurado real del pedido. Para resolverlo de verdad se necesitaría usar "function calling" de OpenAI para que el modelo extraiga esos datos de la conversación y los guarde. Es la siguiente mejora lógica si quieres que te avise, por ejemplo, cuando el pedido esté completo y listo para pedir el anticipo.
- **Las sesiones viven en memoria.** Si reinicias el servidor, se pierden todas las conversaciones y pedidos en curso. Para un negocio en producción real conviene guardar esto en una base de datos ligera (SQLite es suficiente para empezar).
- No agregué reintentos ni logging estructurado para cuando falla el envío a WhatsApp o la llamada a OpenAI; por ahora solo se imprime el error en consola.
