import os
import json
import logging
import threading
import requests
from flask import Flask, request, jsonify
from datetime import datetime

# Importaciones de tus módulos existentes y el nuevo audio_handler
from database import init_db
import crm
import pedido_manager
import audio_handler  # Módulo nuevo para la transcripción

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
print(f"PHONE_NUMBER_ID = '{os.getenv('PHONE_NUMBER_ID')}'")
print(f"WHATSAPP_TOKEN = '{os.getenv('WHATSAPP_TOKEN')[:10]}...'")

# --- CONFIGURACIÓN DE API ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
WHATSAPP_API_URL = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"

# --- INICIALIZACIÓN DEL SISTEMA ---
# Esto mantiene las tablas actualizadas sin romper nada
try:
    init_db()
    logger.info("🚀 Bases de datos inicializadas correctamente.")
except Exception as e:
    logger.critical(f"❌ CRÍTICO: No se pudo inicializar la base de datos. {e}")

# ==============================================================================
# # FUNCIONES AUXILIARES
# ==============================================================================
def enviar_mensaje_whatsapp(telefono, texto, tipo="text"):
    """
    Envía un mensaje de vuelta al usuario a través de la API de WhatsApp.
    """
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": telefono,
        "type": tipo
    }

    if tipo == "text":
        payload["text"] = {"body": texto}
    elif tipo == "image":
        # Si tu bot actual envía imágenes, aquí va esa lógica (sin cambios)
        pass

    try:
        response = requests.post(WHATSAPP_API_URL, headers=headers, json=payload)
        if response.status_code != 200:
            logger.error(f"Error enviando mensaje a WhatsApp: {response.text}")
        return response
    except Exception as e:
        logger.error(f"Excepción enviando mensaje: {e}")
        return None

# ==============================================================================
# # FUNCIÓN DE PROCESAMIENTO CON GPT (FLUJO DE CONVERSACIÓN)
# ==============================================================================
def procesar_con_gpt(telefono, texto, historial=None):
    """
    Aquí va tu lógica original de OpenAI.
    Puedes usar este placeholder para poner tu código real de OpenAI.
    """
    # EJEMPLO DE LLAMADA A OPENAI (Descomentar y poner tu código real)
    # client = OpenAI(api_key=OPENAI_API_KEY)
    # response = client.chat.completions.create(...)
    
    return f"Respuesta generada por el sistema original de OpenAI a: '{texto}'"

# ==============================================================================
# # PROCESAMIENTO EN SEGUNDO PLANO (HILO PRINCIPAL)
# ==============================================================================
def procesar_mensaje_en_fondo(telefono, texto):
    """
    Procesa el mensaje entrante en un hilo separado.
    Este flujo NO cambia, recibe texto ya sea escrito o transcrito.
    """
    try:
        logger.info(f"📥 Procesando mensaje de {telefono}: {texto}")

        # 1. Obtener o cargar el cliente desde el CRM
        cliente = crm.cargar_cliente(telefono)
        
        # 2. Guardar el mensaje recibido en el CRM (Historial de chat original)
        crm.guardar_mensaje_cliente(cliente, texto, "texto_recibido")

        # 3. ¿El usuario tiene intención de hacer un pedido?
        if crm._detectar_intencion_pedido(texto):
            logger.info("🛒 Se detectó intención de pedido. Entrando al Motor de Pedidos.")
            respuesta = crm.manejar_intencion_pedido(cliente, texto)
        else:
            logger.info("💬 No es un pedido, usando flujo normal de conversación.")
            respuesta = procesar_con_gpt(telefono, texto)

        # 4. Enviar la respuesta final al usuario a través de WhatsApp
        enviar_mensaje_whatsapp(telefono, respuesta)

    except Exception as e:
        logger.error(f"❌ Error crítico en el procesamiento del mensaje de {telefono}: {e}")
        enviar_mensaje_whatsapp(telefono, "Lo siento, ocurrió un error interno procesando tu mensaje. Intenta de nuevo más tarde.")

# ==============================================================================
# # WEBHOOKS DE WHATSAPP
# ==============================================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Punto de entrada de los mensajes de WhatsApp.
    Aquí se añadió el flujo de AUDIO sin tocar el resto.
    """
    try:
        data = request.json
        logger.info(f"Webhook recibido: {data}")

        if "messages" in data["entry"][0]["changes"][0]["value"]:
            messages = data["entry"][0]["changes"][0]["value"]["messages"]
            if messages:
                message = messages[0]
                phone_number = message["from"]

                # 🔹 1. FLUJO DE TEXTO (El ya existente y congelado)
                if "text" in message:
                    text = message.get("text", {}).get("body", "")
                    if text:
                        # Lanzamos un hilo en segundo plano para procesar el mensaje
                        thread = threading.Thread(target=procesar_mensaje_en_fondo, args=(phone_number, text))
                        thread.start()

                # 🔹 2. FLUJO DE AUDIO (Nuevo en Sprint 1.8)
                elif "audio" in message:
                    try:
                        media_id = message["audio"]["id"]
                        
                        # Convertir el audio a texto (audio_handler se encarga de todo)
                        texto_transcrito = audio_handler.procesar_audio(media_id, WHATSAPP_TOKEN)
                        
                        # Inyectar el texto transcrito exactamente en el mismo flujo de procesamiento
                        thread = threading.Thread(target=procesar_mensaje_en_fondo, args=(phone_number, texto_transcrito))
                        thread.start()
                        
                    except Exception as e:
                        logger.error(f"❌ Error procesando audio: {e}")
                        # Respuesta amigable si falla la transcripción
                        enviar_mensaje_whatsapp(phone_number, "Lo siento, tuve problemas para entender tu audio. ¿Puedes escribirme el mensaje por favor?")

        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.error(f"Error en el endpoint POST /webhook: {e}")
        return jsonify({"status": "error"}), 500

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    """
    Verificación del webhook con Meta (Método GET).
    """
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "mi_token_secreto")
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == verify_token:
            logger.info("✅ Webhook verificado exitosamente con Meta.")
            return challenge, 200
        else:
            logger.warning("❌ Fallo en la verificación del webhook (Token incorrecto).")
            return "Verification failed", 403
    return "Invalid request", 400

# ==============================================================================
# # ARRANQUE DE LA APP
# ==============================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
