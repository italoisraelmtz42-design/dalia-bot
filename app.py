import os
import logging
import threading
from flask import Flask, request, jsonify
from datetime import datetime

# Importaciones de la base de datos y módulos propios
from database import init_db
import crm
# Importar el manager de pedidos (aunque no se use directamente aquí, se requiere para que las tablas estén listas)
import pedido_manager

# Configuración básica de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- INICIALIZACIÓN ---
# Ejecutar al arrancar la app para garantizar que existen las tablas de pedidos
try:
    init_db()
    logger.info("🚀 Sistema de base de datos inicializado correctamente.")
except Exception as e:
    logger.critical(f"❌ CRÍTICO: No se pudo inicializar la base de datos al arrancar. {e}")

# --- WEBHOOK DE WHATSAPP ---
@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Punto de entrada de los mensajes de WhatsApp.
    El flujo actual de la app se mantiene intacto.
    """
    try:
        data = request.json
        logger.info(f"Webhook recibido: {data}")

        if "messages" in data["entry"][0]["changes"][0]["value"]:
            messages = data["entry"][0]["changes"][0]["value"]["messages"]
            if messages:
                message = messages[0]
                phone_number = message["from"]
                text = message.get("text", {}).get("body", "")
                
                # Ejecutar en un hilo para no bloquear la respuesta de verificación a WhatsApp (Tal cual ya se hacía)
                thread = threading.Thread(target=procesar_mensaje_en_fondo, args=(phone_number, text))
                thread.start()

        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.error(f"Error en el webhook: {e}")
        return jsonify({"status": "error"}), 500

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """Verificación del webhook de Meta/WhatsApp."""
    # Lógica de verificación estándar
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "mi_token_secreto")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == verify_token:
            logger.info("✅ Webhook verificado correctamente.")
            return challenge, 200
        else:
            logger.warning("❌ Fallo en la verificación del webhook.")
            return "Verification failed", 403
    return "Invalid request", 400

# --- FUNCIÓN DE PROCESAMIENTO DE FONDO ---
def procesar_mensaje_en_fondo(telefono, texto):
    """
    Procesa un mensaje entrante en segundo plano.
    """
    try:
        logger.info(f"Procesando mensaje de {telefono}")
        
        # 1. Obtener o cargar el cliente (Usando CRM original)
        cliente = crm.cargar_cliente(telefono)
        
        # 2. Guardar el mensaje recibido en el CRM (Tal como funcionaba antes)
        crm.guardar_mensaje_cliente(cliente, texto, "texto_recibido")

        # 3. EVOLUCIÓN: ¿El usuario tiene intención de hacer un pedido?
        # Usamos la nueva lógica del CRM para detectar y gestionar el pedido.
        if crm._detectar_intencion_pedido(texto):
            respuesta = crm.manejar_intencion_pedido(cliente, texto)
        else:
            # Si no es un pedido, usar la respuesta normal del sistema actual (OpenAI, etc.)
            # Aquí llamarías a tu función actual de openai o flujo normal del bot
            respuesta = f"Bot respondiendo normalmente a: '{texto}' (Modo conversación)"
            logger.info(f"Respuesta normal del bot a {telefono}: {respuesta}")

        # 4. Enviar la respuesta al usuario a través de la API de WhatsApp
        # Aquí debes mantener tu función de envío existente.
        # enviar_whatsapp(telefono, respuesta)
        logger.info(f"📤 Mensaje enviado a {telefono}:\n{respuesta}")

    except Exception as e:
        logger.error(f"Error en procesar_mensaje_en_fondo para {telefono}: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
