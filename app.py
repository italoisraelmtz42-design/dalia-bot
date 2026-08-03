import os
import time
import random
import threading
from pathlib import Path
from datetime import datetime

import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from openai import OpenAI

# ===========================
# CONFIGURACIÓN
# ===========================

load_dotenv()

app = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
# El token de verificación ya NO va escrito directo en el código.
# Defínelo en tu .env como WHATSAPP_VERIFY_TOKEN=lo-que-tu-quieras
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "cambia_este_token")

GRAPH_API_VERSION = "v20.0"
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_ID}/messages"

BASE = Path(__file__).resolve().parent
CARPETA = BASE / "conocimiento"

MODELO = "gpt-4.1-mini"
MAX_TURNOS_HISTORIAL = 20  # mensajes (usuario+asistente) que se guardan por cliente


# ===========================
# CARGAR BASE DE CONOCIMIENTO
# (una sola vez, al iniciar el servidor)
# ===========================

def cargar_conocimiento():
    knowledge = ""
    archivos = sorted(CARPETA.glob("*.txt"))

    print("\n" + "=" * 60)
    print("CARGANDO BASE DE CONOCIMIENTO...")
    print("=" * 60)

    for i, archivo in enumerate(archivos, start=1):
        print(f"[{i:02}/{len(archivos)}] ✅ {archivo.name}")
        try:
            contenido = archivo.read_text(encoding="utf-8", errors="ignore")
            knowledge += f"""

==================================================
ARCHIVO: {archivo.name}
==================================================

{contenido}

==================================================
FIN DEL ARCHIVO
==================================================

"""
        except Exception as e:
            print(f"❌ Error leyendo {archivo.name}: {e}")

    print("\n" + "=" * 60)
    print("TOTAL DE ARCHIVOS :", len(archivos))
    print("TOTAL CARACTERES  :", len(knowledge))
    print("=" * 60 + "\n")

    return knowledge


KNOWLEDGE = cargar_conocimiento()


# ===========================
# SESIONES POR CLIENTE
# Cada número de WhatsApp tiene su propio historial
# y su propio "pedido" en construcción.
# ===========================

sesiones = {}
sesiones_lock = threading.Lock()


def pedido_vacio():
    return {
        "producto": None,
        "cantidad": None,
        "evento": None,
        "fecha_evento": None,
        "color_toalla": None,
        "color_mono": None,
        "color_velita": None,
        "datos_tarjeta": None,
        "tipo_entrega": None,
        "direccion": None,
    }


def obtener_sesion(numero):
    """Devuelve (y crea si no existe) la sesión de un cliente por su número."""
    with sesiones_lock:
        if numero not in sesiones:
            sesiones[numero] = {
                "messages": [],
                "pedido": pedido_vacio(),
            }
        return sesiones[numero]


def resumen_pedido(pedido):
    datos = [f"{k}: {v}" for k, v in pedido.items() if v]
    return "\n".join(datos) if datos else "Sin datos confirmados."


def construir_system_prompt(pedido):
    ahora = datetime.now()
    fecha = ahora.strftime("%d/%m/%Y")
    hora = ahora.strftime("%H:%M")

    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    dia_semana = dias[ahora.weekday()]

    return f"""
Eres DALIA, asesora de ventas de Recuerditos Dalia.

Hoy es {dia_semana} {fecha}.
La hora actual es {hora}.

Toda la información oficial está en la Base de Conocimiento.

REGLAS:
- Usa únicamente la Base de Conocimiento.
- Nunca inventes datos.
- Nunca inventes productos, precios o políticas.
- Si algo no existe en la Base de Conocimiento, indícalo.
- Responde como una asesora humana por WhatsApp.
- Sé amable, natural y orientada a cerrar ventas.

ESTADO ACTUAL DEL PEDIDO DE ESTE CLIENTE:

{resumen_pedido(pedido)}

No vuelvas a preguntar datos ya confirmados.
Pregunta únicamente los datos faltantes.

BASE DE CONOCIMIENTO:

{KNOWLEDGE}
"""


# ===========================
# LLAMADA A OPENAI
# ===========================

def preguntar_ia(numero, texto_cliente):
    sesion = obtener_sesion(numero)
    historial = sesion["messages"]

    historial.append({"role": "user", "content": texto_cliente})

    system_prompt = construir_system_prompt(sesion["pedido"])
    mensajes_completos = [{"role": "system", "content": system_prompt}] + historial

    # Recortar historial para no crecer sin límite (igual que en main.py)
    if len(mensajes_completos) > MAX_TURNOS_HISTORIAL + 1:
        mensajes_completos = [mensajes_completos[0]] + mensajes_completos[-MAX_TURNOS_HISTORIAL:]
        sesion["messages"] = mensajes_completos[1:]

    r = client.chat.completions.create(
        model=MODELO,
        messages=mensajes_completos,
        temperature=0.4,
        top_p=0.9,
        max_tokens=350,
    )

    texto = r.choices[0].message.content
    historial.append({"role": "assistant", "content": texto})
    return texto


# ===========================
# ENVIAR MENSAJE POR WHATSAPP
# ===========================

def enviar_whatsapp(numero, texto):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "text",
        "text": {"body": texto},
    }
    try:
        r = requests.post(GRAPH_URL, headers=headers, json=data, timeout=15)
        if r.status_code >= 400:
            print("⚠️ Error enviando WhatsApp:", r.status_code, r.text)
        return r
    except requests.RequestException as e:
        print("⚠️ Excepción enviando WhatsApp:", e)
        return None


# ===========================
# WEBHOOK: VERIFICACIÓN (Meta)
# ===========================

@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Error, verificación fallida", 403


# ===========================
# WEBHOOK: MENSAJES ENTRANTES
# ===========================

@app.route("/webhook", methods=["POST"])
def handle_message():
    data = request.get_json(silent=True) or {}

    try:
        entry = data["entry"][0]
        cambio = entry["changes"][0]
        valor = cambio["value"]
        mensajes = valor.get("messages")

        # Meta también manda notificaciones de "estado" (entregado, leído, etc.)
        # que no traen "messages". Las ignoramos sin error.
        if not mensajes:
            return jsonify({"status": "sin mensajes nuevos"}), 200

        mensaje = mensajes[0]
        numero = mensaje["from"]
        tipo = mensaje.get("type")

        if tipo != "text":
            enviar_whatsapp(
                numero,
                "Por ahora solo puedo leer mensajes de texto 🙂 ¿me lo escribes con palabras?",
            )
            return jsonify({"status": "tipo de mensaje no soportado"}), 200

        texto_cliente = mensaje["text"]["body"]

        respuesta = preguntar_ia(numero, texto_cliente)

        # Pequeña espera para que no se sienta instantáneo/robótico
        time.sleep(random.uniform(2, 4))

        enviar_whatsapp(numero, respuesta)

    except (KeyError, IndexError, TypeError) as e:
        # Payload inesperado (ej. notificación de estado) -> no truena el servidor
        print("Evento sin mensaje de texto reconocible:", e)

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    puerto = int(os.getenv("PORT", 5000))
    app.run(port=puerto, debug=debug_mode)
