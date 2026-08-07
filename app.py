import os
import json
import logging
import threading
import requests
from flask import Flask, request, jsonify
from datetime import datetime
from openai import OpenAI

# Importaciones de tus módulos existentes y el nuevo audio_handler
from database import init_db
import crm
import pedido_manager
import audio_handler  # Módulo nuevo para la transcripción

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# --- CONFIGURACIÓN DE API ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")

# 🔥 Fallback para que el sistema responda aunque Render falle con la variable de entorno
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID", "1256708880860678")

WHATSAPP_API_URL = f"https://graph.facebook.com/v18.0/{PHONE_NUMBER_ID}/messages"

# --- INICIALIZACIÓN DEL SISTEMA ---
try:
    init_db()
    logger.info("🚀 Bases de datos inicializadas correctamente.")
except Exception as e:
    logger.critical(f"❌ CRÍTICO: No se pudo inicializar la base de datos. {e}")

# ==============================================================================
# # FUNCIONES AUXILIARES
# ==============================================================================
def enviar_mensaje_whatsapp(telefono, texto, tipo="text"):
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
    try:
        response = requests.post(WHATSAPP_API_URL, headers=headers, json=payload)
        if response.status_code != 200:
            logger.error(f"Error enviando mensaje a WhatsApp: {response.text}")
        return response
    except Exception as e:
        logger.error(f"Excepción enviando mensaje: {e}")
        return None

# ==============================================================================
# # 💡 CEREBRO DE OPENAI (Esta es la función que conecta tu IA)
# ==============================================================================
def procesar_con_gpt(telefono, texto, historial=None):
    # Inicializa el cliente de OpenAI. NO pongas la clave aquí, la variable de entorno ya está en Render.
    client = OpenAI(api_key=OPENAI_API_KEY)

    # Construye el contexto o historial de la conversación
    messages = historial if historial else []
    messages.append({"role": "user", "content": texto})

    # Realiza la llamada a la IA
    response = client.chat.completions.create(
        model="gpt-4",  # Modelo OpenAI que estés usando
        messages=messages
    )

    # Retorna la respuesta generada por la IA
    return response.choices[0].message.content

# ==============================================================================
# # PROCESAMIENTO EN SEGUNDO PLANO (HILO PRINCIPAL)
# ==============================================================================
def procesar_mensaje_en_fondo(telefono, texto):
    try:
        logger.info(f"📥 Procesando mensaje de {telefono}: {texto}")

        cliente = crm.cargar_cliente(telefono)
        crm.guardar_mensaje_cliente(cliente, texto, "texto_recibido")

        if crm._detectar_intencion_pedido(texto):
            logger.info("🛒 Se detectó intención de pedido. Entrando al Motor de Pedidos.")
            respuesta = crm.manejar_intencion_pedido(cliente, texto)
        else:
            logger.info("💬 No es un pedido, usando flujo normal de conversación.")
            # Aquí es donde se usa el cerebro real
            respuesta = procesar_con_gpt(telefono, texto)

        enviar_mensaje_whatsapp(telefono, respuesta)

    except Exception as e:
        logger.error(f"❌ Error crítico en el procesamiento del mensaje de {telefono}: {e}")
        enviar_mensaje_whatsapp(telefono, "Lo siento, ocurrió un error interno procesando tu mensaje. Intenta de nuevo más tarde.")

# ==============================================================================
# # WEBHOOKS DE WHATSAPP
# ==============================================================================
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.json
        logger.info(f"Webhook recibido: {data}")

        if "messages" in data["entry"][0]["changes"][0]["value"]:
            messages = data["entry"][0]["changes"][0]["value"]["messages"]
            if messages:
                message = messages[0]
                phone_number = message["from"]

                # 1. FLUJO DE TEXTO
                if "text" in message:
                    text = message.get("text", {}).get("body", "")
                    if text:
                        thread = threading.Thread(target=procesar_mensaje_en_fondo, args=(phone_number, text))
                        thread.start()

                # 2. FLUJO DE AUDIO (YA FUNCIONA CORRECTAMENTE)
                elif "audio" in message:
                    try:
                        media_id = message["audio"]["id"]
                        texto_transcrito = audio_handler.procesar_audio(media_id, WHATSAPP_TOKEN)
                        
                        # 🔥 EL AUDIO SE INYECTA EXACTAMENTE EN EL MISMO HILO QUE EL TEXTO
                        thread = threading.Thread(target=procesar_mensaje_en_fondo, args=(phone_number, texto_transcrito))
                        thread.start()
                        
                    except Exception as e:
                        logger.error(f"❌ Error procesando audio: {e}")
                        enviar_mensaje_whatsapp(phone_number, "Lo siento, tuve problemas para entender tu audio. ¿Puedes escribirme el mensaje por favor?")

        return jsonify({"status": "success"}), 200
    except Exception as e:
        logger.error(f"Error en el endpoint POST /webhook: {e}")
        return jsonify({"status": "error"}), 500

@app.route("/webhook", methods=["GET"])
def verify_webhook():
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
