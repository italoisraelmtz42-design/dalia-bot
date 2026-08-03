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

BASE = Path(__file__).resolve().parent
CARPETA = BASE / "conocimiento"

DOTENV_PATH = BASE / ".env"
cargado = load_dotenv(dotenv_path=DOTENV_PATH)

app = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "cambia_este_token")

GRAPH_API_VERSION = "v20.0"
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_ID}/messages"


def _diagnostico_arranque():
    print("\n" + "=" * 60)
    print("DIAGNÓSTICO DE CONFIGURACIÓN (.env)")
    print("=" * 60)
    print(f"Ruta esperada del .env : {DOTENV_PATH}")
    print(f".env encontrado y leído: {'SÍ' if cargado else 'NO ⚠️'}")
    print(f"OPENAI_API_KEY cargada : {'SÍ' if os.getenv('OPENAI_API_KEY') else 'NO ⚠️'}")
    print(f"WHATSAPP_TOKEN cargado : {'SÍ' if WHATSAPP_TOKEN else 'NO ⚠️'}")
    print(f"WHATSAPP_PHONE_ID      : {WHATSAPP_PHONE_ID or 'NO ⚠️'}")
    if os.getenv("WHATSAPP_VERIFY_TOKEN"):
        print("WHATSAPP_VERIFY_TOKEN  : definido ✅")
    else:
        print("WHATSAPP_VERIFY_TOKEN  : NO definido -> usando valor por defecto ⚠️")
    print("=" * 60 + "\n")


_diagnostico_arranque()

MODELO = "gpt-4.1-mini"
MAX_TURNOS_HISTORIAL = 20


# ===========================
# CARGAR BASE DE CONOCIMIENTO
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

def enviar_whatsapp(numero, texto, phone_id=None):
    id_a_usar = phone_id or WHATSAPP_PHONE_ID
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{id_a_usar}/messages"

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
        r = requests.post(url, headers=headers, json=data, timeout=15)
        if r.status_code >= 400:
            print("⚠️ Error enviando WhatsApp:", r.status_code, r.text)
        return r
    except requests.RequestException as e:
        print("⚠️ Excepción enviando WhatsApp:", e)
        return None


# ===========================
# RUTA RAÍZ (health check)
# ===========================

@app.route("/", methods=["GET"])
def home():
    return "DALIA bot está corriendo ✅", 200


# ===========================
# POLÍTICA DE PRIVACIDAD
# ===========================

@app.route("/privacidad", methods=["GET"])
def privacidad():
    html = """
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>Política de Privacidad - Recuerditos Dalia</title>
        <style>
            body { font-family: Arial, sans-serif; max-width: 700px; margin: 40px auto; padding: 0 20px; line-height: 1.6; color: #222; }
            h1 { font-size: 24px; }
            h2 { font-size: 18px; margin-top: 30px; }
        </style>
    </head>
    <body>
        <h1>Política de Privacidad</h1>
        <p><strong>Última actualización:</strong> agosto de 2026</p>

        <p>Recuerditos Dalia ("nosotros") opera un asistente automatizado de ventas
        a través de WhatsApp ("DALIA"). Esta política explica qué información
        recopilamos cuando nos escribes y cómo la usamos.</p>

        <h2>Información que recopilamos</h2>
        <p>Cuando nos escribes por WhatsApp, podemos recopilar:</p>
        <ul>
            <li>Tu número de teléfono de WhatsApp.</li>
            <li>El contenido de los mensajes que nos envías (por ejemplo, tus
            preguntas, el producto que deseas, colores, fecha del evento y
            dirección de entrega si nos la proporcionas).</li>
        </ul>

        <h2>Cómo usamos tu información</h2>
        <p>Usamos esta información únicamente para:</p>
        <ul>
            <li>Responder tus preguntas sobre nuestros productos y precios.</li>
            <li>Procesar y dar seguimiento a tu pedido.</li>
            <li>Coordinar la entrega de tu compra.</li>
        </ul>
        <p>Para generar respuestas, tus mensajes se procesan mediante un
        servicio de inteligencia artificial de terceros (OpenAI), únicamente
        con el fin de generar una respuesta conversacional. No vendemos ni
        compartimos tu información con terceros para fines publicitarios.</p>

        <h2>Conservación de datos</h2>
        <p>Conservamos el historial de conversación mientras sea necesario
        para darte seguimiento a tu pedido. Puedes solicitar la eliminación
        de tus datos escribiéndonos directamente por WhatsApp.</p>

        <h2>Contacto</h2>
        <p>Si tienes dudas sobre esta política o quieres solicitar la
        eliminación de tus datos, contáctanos directamente por WhatsApp.</p>
    </body>
    </html>
    """
    return html, 200


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

    print("\n🔔 POST /webhook recibido")
    print("PAYLOAD CRUDO:", data)

    try:
        entry = data["entry"][0]
        cambio = entry["changes"][0]
        valor = cambio["value"]
        mensajes = valor.get("messages")

        if not mensajes:
            print("ℹ️ Es una notificación de estado (no trae 'messages'), se ignora.")
            return jsonify({"status": "sin mensajes nuevos"}), 200

        mensaje = mensajes[0]
        numero = mensaje["from"]
        tipo = mensaje.get("type")
        phone_id_destino = valor.get("metadata", {}).get("phone_number_id")

        print(f"📩 Mensaje de tipo '{tipo}' recibido del número: {numero}")
        print(f"📱 phone_number_id que recibió el mensaje: {phone_id_destino}")

        if tipo != "text":
            print("⚠️ No es texto, se manda respuesta genérica.")
            enviar_whatsapp(
                numero,
                "Por ahora solo puedo leer mensajes de texto 🙂 ¿me lo escribes con palabras?",
                phone_id=phone_id_destino,
            )
            return jsonify({"status": "tipo de mensaje no soportado"}), 200

        texto_cliente = mensaje["text"]["body"]
        print(f"💬 Texto del cliente: {texto_cliente}")

        print("🤖 Llamando a OpenAI...")
        respuesta = preguntar_ia(numero, texto_cliente)
        print(f"✅ OpenAI respondió: {respuesta[:200]}")

        time.sleep(random.uniform(2, 4))

        print(f"📤 Enviando respuesta por WhatsApp a {numero} usando phone_id={phone_id_destino}")
        resultado_envio = enviar_whatsapp(numero, respuesta, phone_id=phone_id_destino)
        if resultado_envio is not None:
            print(f"📬 Respuesta de la API de WhatsApp: {resultado_envio.status_code} - {resultado_envio.text}")

    except (KeyError, IndexError, TypeError) as e:
        print("❌ Evento sin mensaje de texto reconocible o payload inesperado:", e)
    except Exception as e:
        print("❌ ERROR INESPERADO procesando el mensaje:", repr(e))

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    puerto = int(os.getenv("PORT", 5000))
    app.run(port=puerto, debug=debug_mode)
