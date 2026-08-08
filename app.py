import os
import json
import time
import random
import re
import hmac
import hashlib
import base64
import threading
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZONA_HORARIA_NEGOCIO = ZoneInfo("America/Monterrey")

import requests
from flask import Flask, request, jsonify, send_from_directory
from dotenv import load_dotenv
from openai import OpenAI

import crm
import pedido_manager

# ===========================
# CONFIGURACIÓN
# ===========================
load_dotenv()

app = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

try:
    crm.inicializar_base_datos()
    print("✅ Base de datos (SQLite) lista")
except Exception as e:
    print("⚠️ No se pudo inicializar la base de datos:", repr(e))

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "cambia_este_token")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")

GRAPH_API_VERSION = "v20.0"
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_ID}/messages"

BASE = Path(__file__).resolve().parent
CARPETA = BASE / "conocimiento"
CARPETA_IMAGENES = BASE / "imagenes"
CARPETA_CATALOGO = BASE / "catalogo"
CARPETA_NOTAS = BASE / "notas"
CARPETA_NOTAS.mkdir(exist_ok=True)

MODELO = "gpt-4.1-mini"
MAX_TURNOS_HISTORIAL = 20

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://dalia-bot.onrender.com")

# ===========================
# CATÁLOGO DE FOTOS DE PRODUCTO
# ===========================
EXTENSIONES_IMAGEN_VALIDAS = {".jpg", ".jpeg", ".png", ".webp"}

def cargar_catalogo_imagenes():
    catalogo = {}
    if not CARPETA_IMAGENES.exists():
        print(f"⚠️ No existe la carpeta {CARPETA_IMAGENES}, no habrá fotos de producto")
        return catalogo

    archivos = sorted(CARPETA_IMAGENES.iterdir())
    print("\n" + "=" * 60)
    print("CARGANDO CATÁLOGO DE FOTOS DE PRODUCTO...")
    print("=" * 60)
    for archivo in archivos:
        if archivo.is_file() and archivo.suffix.lower() in EXTENSIONES_IMAGEN_VALIDAS:
            clave = archivo.stem.strip().lower().replace(" ", "_")
            nombre_mostrar = archivo.stem.replace("_", " ").replace("-", " ").strip().capitalize()
            catalogo[clave] = {
                "nombre_mostrar": nombre_mostrar,
                "archivo": archivo.name,
            }
            print(f"✅ {clave}  ->  {archivo.name}")
    print("TOTAL DE FOTOS DE PRODUCTO:", len(catalogo))
    print("=" * 60 + "\n")
    return catalogo

CATALOGO_IMAGENES = cargar_catalogo_imagenes()

# ===========================
# CATÁLOGO GENERAL EN PDF
# ===========================
def encontrar_catalogo_pdf():
    if not CARPETA_CATALOGO.exists():
        print(f"⚠️ No existe la carpeta {CARPETA_CATALOGO}, no habrá link de catálogo")
        return None
    pdfs = sorted(CARPETA_CATALOGO.glob("*.pdf"))
    if not pdfs:
        print(f"⚠️ La carpeta {CARPETA_CATALOGO} no tiene ningún PDF todavía")
        return None
    print(f"✅ Catálogo PDF encontrado: {pdfs[0].name}")
    return pdfs[0].name

NOMBRE_CATALOGO_PDF = encontrar_catalogo_pdf()
URL_CATALOGO_PDF = (
    f"{PUBLIC_BASE_URL}/catalogo/{NOMBRE_CATALOGO_PDF}" if NOMBRE_CATALOGO_PDF else None
)

def url_imagen_producto(clave_producto):
    info = CATALOGO_IMAGENES.get(clave_producto)
    if not info:
        return None
    return f"{PUBLIC_BASE_URL}/imagenes/{info['archivo']}"

# ===========================
# CARGAR BASE DE CONOCIMIENTO Y VARIABLES DE CONTEXTO
# ===========================
CONOCIMIENTO_POR_ARCHIVO = {}
ARCHIVOS_CONOCIMIENTO_SIEMPRE = {
    "04_REGLAS_GENERALES.txt",
    "033_Reglas_Conversacion.txt",
    "027_Pagos_y_Anticipos.txt",
    "028_Colores_Disponibles.txt",
    "029_Flujo_de_Venta.txt",
    "050_Saludos_Humanos.txt",
}

def cargar_conocimiento():
    global CONOCIMIENTO_POR_ARCHIVO
    knowledge = ""
    
    print("\n" + "=" * 60)
    print("CARGANDO BASE DE CONOCIMIENTO...")
    print("=" * 60)

    ruta_absoluta = CARPETA.resolve()
    print(f"Ruta absoluta de conocimiento: {ruta_absoluta}")
    print(f"¿Existe?: {ruta_absoluta.exists()}")

    if not ruta_absoluta.exists():
        print("❌ ERROR: La carpeta 'conocimiento' NO EXISTE.")
        return ""

    encontrados_txt = []
    for root, dirs, files in os.walk(str(ruta_absoluta)):
        for file in files:
            if file.lower().endswith('.txt'):
                full_path = Path(root) / file
                encontrados_txt.append(full_path)

    if not encontrados_txt:
        print("⚠️ No se encontraron archivos .txt.")
        return ""

    print("=" * 60)
    for full_path in sorted(encontrados_txt):
        rel_path = full_path.relative_to(ruta_absoluta)
        try:
            contenido = full_path.read_text(encoding="utf-8", errors="ignore")
            bloque = f"""
==================================================
ARCHIVO: {rel_path}
==================================================
{contenido}
==================================================
FIN DEL ARCHIVO
==================================================
"""
            knowledge += bloque
            CONOCIMIENTO_POR_ARCHIVO[str(rel_path)] = bloque
        except Exception as e:
            print(f"❌ Error leyendo {rel_path}: {e}")

    print("\n" + "=" * 60)
    print(f"TOTAL TXT ENCONTRADOS: {len(encontrados_txt)}")
    print("TOTAL CARACTERES  :", len(knowledge))
    print("=" * 60 + "\n")
    return knowledge

KNOWLEDGE = cargar_conocimiento()

def seleccionar_conocimiento_relevante(texto_cliente, historial_reciente=None, top_k=16):
    if not CONOCIMIENTO_POR_ARCHIVO:
        return KNOWLEDGE

    texto_relevancia = texto_cliente or ""
    if historial_reciente:
        texto_relevancia += " " + " ".join(
            m.get("content", "") for m in historial_reciente[-4:]
            if isinstance(m.get("content"), str)
        )

    palabras_clave = {
        p for p in re.findall(r"[a-záéíóúñ0-9]+", texto_relevancia.lower())
        if len(p) > 3
    }

    puntajes = []
    for nombre, bloque in CONOCIMIENTO_POR_ARCHIVO.items():
        bloque_lower = bloque.lower()
        puntaje = sum(1 for palabra in palabras_clave if palabra in bloque_lower)
        puntajes.append((puntaje, nombre))

    puntajes.sort(key=lambda x: x[0], reverse=True)
    seleccionados = {nombre for _, nombre in puntajes[:top_k]}
    seleccionados |= ARCHIVOS_CONOCIMIENTO_SIEMPRE

    return "".join(
        CONOCIMIENTO_POR_ARCHIVO[nombre]
        for nombre in sorted(seleccionados)
        if nombre in CONOCIMIENTO_POR_ARCHIVO
    )

# ===========================
# SESIONES POR CLIENTE (CACHÉ LIGERA - SQLite es la fuente de verdad)
# ===========================
sesiones = {}
sesiones_lock = threading.Lock()

def obtener_sesion(numero):
    with sesiones_lock:
        if numero not in sesiones:
            mensajes_previos = pedido_manager.chat_cargar_memoria(numero, limite=MAX_TURNOS_HISTORIAL)
            pedido_id = pedido_manager.obtener_pedido_activo(numero)
            pedido_previo = None
            if pedido_id:
                pedido_obj = pedido_manager.obtener_pedido(pedido_id)
                pedido_previo = pedido_obj
            else:
                borrador = pedido_manager.cargar_borrador_pedido(numero)
                if borrador:
                    pedido_previo = borrador
                    print(f"♻️ Borrador persistente cargado desde SQLite para {numero}")
            if mensajes_previos or pedido_previo:
                print(f"♻️ Sesión de {numero} hidratada desde SQLite ({len(mensajes_previos)} mensajes previos, pedido ID {pedido_id})")
            sesiones[numero] = {
                "messages": mensajes_previos,
                "pedido": pedido_previo,
                "pedido_id": pedido_id,
                "lock": threading.Lock(),
            }
        return sesiones[numero]

# ===========================
# SYSTEM PROMPT Y TOOLS
# ===========================
def construir_system_prompt(estado_resumen, conocimiento=None):
    if conocimiento is None:
        conocimiento = KNOWLEDGE
    ahora = datetime.now(ZONA_HORARIA_NEGOCIO)
    fecha = ahora.strftime("%d/%m/%Y")
    hora = ahora.strftime("%H:%M")
    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    dia_semana = dias[ahora.weekday()]
    return f"""
Eres DALIA, asesora de ventas de Recuerditos Dalia.

Hoy es {dia_semana} {fecha}.
La hora actual es {hora} (hora de Monterrey, México).

REGLAS:
- Usa únicamente la Base de Conocimiento.
- Nunca inventes datos.
- Si algo no existe en la Base de Conocimiento, indícalo.
- Responde de forma amable y natural.

ESTADO ACTUAL DEL PEDIDO (desde base de datos):
{estado_resumen}

BASE DE CONOCIMIENTO:
{conocimiento}
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "actualizar_pedido",
            "description": "Actualiza el borrador del pedido. Si se incluye anticipo_confirmado=true, crea el pedido oficial.",
            "parameters": {
                "type": "object",
                "properties": {
                    "producto": {"type": "string"},
                    "cantidad": {"type": "integer"},
                    "evento": {"type": "string"},
                    "fecha_evento": {"type": "string"},
                    "color_toalla": {"type": "string"},
                    "color_moño": {"type": "string"},
                    "tipo_entrega": {"type": "string", "enum": ["local", "domicilio"]},
                    "direccion": {"type": "string"},
                    "municipio": {"type": "string"},
                    "anticipo_confirmado": {"type": "boolean"},
                    "nombre_bebe": {"type": "string"},
                    "tarjetita": {"type": "string"},
                    "notas": {"type": "string"},
                },
            },
        },
    },
]
if CATALOGO_IMAGENES:
    TOOLS.append({
        "type": "function",
        "function": {
            "name": "mostrar_foto_producto",
            "description": "Envía una foto de producto.",
            "parameters": {
                "type": "object",
                "properties": {"producto": {"type": "string", "enum": list(CATALOGO_IMAGENES.keys())}},
                "required": ["producto"]
            }
        }
    })

# ===========================
# ENVIAR MENSAJES
# ===========================
def enviar_whatsapp(numero, texto):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": numero, "type": "text", "text": {"body": texto}}
    try:
        r = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"⚠️ Error enviando WhatsApp: {r.text}")
        return r
    except Exception as e:
        print(f"⚠️ Excepción enviando WhatsApp: {e}")
        return None

def enviar_whatsapp_imagen(numero, image_url, caption=""):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {"messaging_product": "whatsapp", "to": numero, "type": "image", "image": {"link": image_url, "caption": caption}}
    try:
        r = requests.post(GRAPH_URL, headers=headers, json=payload, timeout=15)
        if r.status_code >= 400:
            print(f"⚠️ Error enviando imagen: {r.text}")
        return r
    except Exception as e:
        print(f"⚠️ Excepción enviando imagen: {e}")
        return None

def descargar_imagen_whatsapp(media_id):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        r = requests.get(f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}", headers=headers, timeout=15)
        if r.status_code >= 400:
            print(f"⚠️ Error obteniendo URL del medio: {r.text}")
            return None, None
        info = r.json()
        url_medio = info.get("url")
        mime_type = info.get("mime_type", "image/jpeg")
        if not url_medio:
            print("⚠️ La respuesta de Meta no trajo URL del medio.")
            return None, None
        r2 = requests.get(url_medio, headers=headers, timeout=20)
        if r2.status_code >= 400:
            print(f"⚠️ Error descargando el archivo del medio: {r2.status_code}")
            return None, None
        return r2.content, mime_type
    except Exception as e:
        print(f"⚠️ Excepción descargando imagen: {e}")
        return None, None

# ===========================
# PREGUNTAR A OPENAI
# ===========================
def preguntar_ia(numero, texto_cliente, imagen_base64=None, imagen_mime=None):
    sesion = obtener_sesion(numero)
    historial = sesion["messages"]
    pedido_cache = sesion["pedido"]
    pedido_id = sesion["pedido_id"]

    # Construir estado del pedido
    estado_resumen = "Sin pedido activo."
    if pedido_id:
        pedido_db = pedido_manager.obtener_pedido(pedido_id)
        if pedido_db:
            estado_resumen = f"Pedido oficial: Folio {pedido_db.folio}, {pedido_db.estado}"
    elif pedido_cache:
        estado_resumen = f"Borrador actual: {json.dumps(pedido_cache, ensure_ascii=False)}"

    system_prompt = construir_system_prompt(estado_resumen)
    mensajes = [{"role": "system", "content": system_prompt}] + historial

    if imagen_base64:
        contenido_usuario = [
            {"type": "text", "text": texto_cliente or "(El cliente mandó una imagen)"},
            {"type": "image_url", "image_url": {"url": f"data:{imagen_mime};base64,{imagen_base64}"}}
        ]
    else:
        contenido_usuario = texto_cliente
    mensajes.append({"role": "user", "content": contenido_usuario})

    response = client.chat.completions.create(
        model=MODELO,
        messages=mensajes,
        tools=TOOLS,
        temperature=0.4,
        top_p=0.9,
        max_tokens=600
    )

    choice = response.choices[0]
    mensaje = choice.message

    # Procesar herramientas
    if mensaje.tool_calls:
        for tool_call in mensaje.tool_calls:
            args = json.loads(tool_call.function.arguments)
            if tool_call.function.name == "actualizar_pedido":
                # 🔥 Aplicar los cambios al caché de la sesión
                if not pedido_cache or isinstance(pedido_cache, dict):
                    if not pedido_cache:
                        pedido_cache = {}
                    pedido_cache.update({k: v for k, v in args.items() if v is not None})
                else:
                    # Si es PedidoData, lo convertimos a dict y actualizamos
                    pedido_cache = {k: v for k, v in vars(pedido_cache).items() if v is not None}
                    pedido_cache.update({k: v for k, v in args.items() if v is not None})
                sesion["pedido"] = pedido_cache
            elif tool_call.function.name == "mostrar_foto_producto":
                clave = args.get("producto")
                url = url_imagen_producto(clave)
                if url:
                    enviar_whatsapp_imagen(numero, url, caption=CATALOGO_IMAGENES[clave]["nombre_mostrar"])
        respuesta = mensaje.content or "He actualizado tu pedido."
    else:
        respuesta = mensaje.content or "Disculpa, ¿me repites?"

    # Guardar la respuesta del bot en SQLite
    pedido_manager.chat_guardar_mensaje(numero, respuesta, "bot")
    return respuesta

# ===========================
# PROCESAMIENTO DE MENSAJES
# ===========================
def procesar_mensaje_en_fondo(numero, texto_cliente, media_id_imagen=None):
    print("=" * 70)
    print(f"🚀 Procesando mensaje de {numero}")
    print(f"💬 Texto recibido: {texto_cliente}")

    imagen_base64 = None
    imagen_mime = None
    if media_id_imagen:
        print("🖼️ El cliente mandó una imagen (Vision), descargándola...")
        contenido, mime = descargar_imagen_whatsapp(media_id_imagen)
        if contenido:
            imagen_base64 = base64.b64encode(contenido).decode("utf-8")
            imagen_mime = mime
            print(f"✅ Imagen descargada ({len(contenido)} bytes, {mime})")
        else:
            print("❌ No se pudo descargar la imagen del cliente, se sigue solo con el texto")

    # Guardar mensaje del cliente en SQLite (historial)
    pedido_manager.chat_guardar_mensaje(numero, texto_cliente or "(imagen sin texto)", "usuario")

    # Consultar a OpenAI
    try:
        print("🧠 Consultando OpenAI...")
        respuesta = preguntar_ia(numero, texto_cliente, imagen_base64=imagen_base64, imagen_mime=imagen_mime)
        print("✅ Respuesta generada")
        print(respuesta[:300])
    except Exception as e:
        print(f"❌ Error llamando a OpenAI: {e}")
        respuesta = "Disculpa, tuve un problema técnico. ¿Me puedes repetir tu mensaje? 🙂"

    # Sincronizar con SQLite (el borrador o pedido se actualizará en crm.sincronizar_pedido)
    try:
        # Extraemos cliente y sesión
        cliente = crm.cargar_cliente(numero)
        datos_sesion = sesiones.get(numero, {}).get("pedido", {})
        crm.sincronizar_pedido(cliente, datos_sesion)
    except Exception as e:
        print(f"⚠️ Error guardando en CRM (el bot sigue funcionando con RAM): {e}")

    # Enviar respuesta
    print("📤 Enviando respuesta a WhatsApp...")
    enviar_whatsapp(numero, respuesta)
    print("🏁 Fin procesamiento")
    print("=" * 70)

# ===========================
# WEBHOOKS
# ===========================
@app.route("/webhook", methods=["GET"])
def verify_webhook():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return challenge, 200
    return "Error, verificación fallida", 403

@app.route("/webhook", methods=["POST"])
def handle_message():
    firma = request.headers.get("X-Hub-Signature-256", "")
    if not verificar_firma_webhook(request.get_data(), firma):
        print("🚫 Webhook rechazado: la firma no coincide")
        return jsonify({"status": "firma inválida"}), 403

    data = request.get_json(silent=True) or {}
    try:
        entry = data["entry"][0]
        cambio = entry["changes"][0]
        valor = cambio["value"]
        mensajes = valor.get("messages")
        if not mensajes:
            return jsonify({"status": "sin mensajes nuevos"}), 200
        
        mensaje = mensajes[0]
        numero = mensaje["from"]
        tipo = mensaje.get("type")
        mensaje_id = mensaje.get("id")
        if ya_fue_procesado(mensaje_id):
            print(f"🔁 Mensaje duplicado ignorado: {mensaje_id}")
            return jsonify({"status": "duplicado ignorado"}), 200

        if tipo == "image":
            media_id = mensaje["image"]["id"]
            caption = mensaje["image"].get("caption", "")
            threading.Thread(target=procesar_mensaje_en_fondo, args=(numero, caption), kwargs={"media_id_imagen": media_id}, daemon=True).start()
            return jsonify({"status": "ok"}), 200

        if tipo != "text":
            threading.Thread(target=procesar_mensaje_no_soportado, args=(numero, tipo), daemon=True).start()
            return jsonify({"status": "ok"}), 200

        texto_cliente = mensaje["text"]["body"]
        threading.Thread(target=procesar_mensaje_en_fondo, args=(numero, texto_cliente), daemon=True).start()
    except Exception as e:
        print("Evento sin mensaje de texto reconocible:", e)
    return jsonify({"status": "ok"}), 200

def verificar_firma_webhook(payload_bytes, firma_header):
    if not WHATSAPP_APP_SECRET:
        print("⚠️ WHATSAPP_APP_SECRET no configurado: el webhook NO está verificando su origen")
        return True
    if not firma_header or not firma_header.startswith("sha256="):
        return False
    firma_esperada = hmac.new(WHATSAPP_APP_SECRET.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    firma_recibida = firma_header.split("sha256=", 1)[1]
    return hmac.compare_digest(firma_esperada, firma_recibida)

def ya_fue_procesado(mensaje_id):
    # Deduplicación básica de mensajes
    return False

def procesar_mensaje_no_soportado(numero, tipo):
    respuesta = "Por ahora solo puedo leer mensajes de texto 🙂 ¿me lo escribes con palabras?"
    pedido_manager.chat_guardar_mensaje(numero, f"[mensaje no soportado: {tipo}]", "usuario")
    pedido_manager.chat_guardar_mensaje(numero, respuesta, "bot")
    enviar_whatsapp(numero, respuesta)

# ===========================
# ARRANQUE
# ===========================
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
