import os
import json
import time
import random
import threading
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ZONA_HORARIA_NEGOCIO = ZoneInfo("America/Monterrey")

import requests
from flask import Flask, request, jsonify, send_from_directory
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
CARPETA_IMAGENES = BASE / "imagenes"

MODELO = "gpt-4.1-mini"
MAX_TURNOS_HISTORIAL = 20  # mensajes (usuario+asistente) que se guardan por cliente

# URL pública de tu servicio en Render (para que WhatsApp pueda descargar las
# imágenes). Si algún día cambia el dominio, solo actualiza la variable de
# entorno PUBLIC_BASE_URL en Render, sin tocar código.
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://dalia-bot.onrender.com")

# ===========================
# CATÁLOGO DE FOTOS DE PRODUCTO
# Se arma AUTOMÁTICAMENTE leyendo lo que haya en la carpeta imagenes/.
# Para agregar un producto nuevo solo tienes que:
#   1. Poner tu foto (ya con la info escrita encima) dentro de imagenes/
#      Ejemplo: imagenes/osito_toalla_jabon.jpg
#   2. Subir el cambio a GitHub. Render redespliega solo y el bot ya
#      puede mandar esa foto. No hace falta tocar este archivo.
#
# La "clave" del producto (con la que el modelo identifica la foto) sale
# del nombre del archivo sin extensión, ej: "osito_toalla_jabon.jpg" ->
# clave "osito_toalla_jabon". Usa nombres de archivo cortos, sin espacios
# ni acentos, con guiones bajos.
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
# Para actualizar el catálogo completo (el PDF con todos los productos):
#   1. Sube el PDF nuevo a la carpeta catalogo/ en tu repo (reemplaza el
#      anterior o bórralo primero si le cambias de nombre).
#   2. Commit + push. Render redespliega solo.
# El bot comparte el LINK del PDF por texto cuando el cliente pide ver el
# catálogo completo (no manda el archivo en sí, para no ser pesado).
# ===========================

CARPETA_CATALOGO = BASE / "catalogo"


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

# IDs de mensajes de WhatsApp ya procesados, para ignorar reintentos que
# Meta manda si el webhook no responde 200 OK lo bastante rápido.
mensajes_procesados = set()
mensajes_procesados_lock = threading.Lock()
MAX_MENSAJES_PROCESADOS = 2000


def ya_fue_procesado(mensaje_id):
    """True si este message_id ya se procesó antes; si no, lo marca como procesado."""
    if not mensaje_id:
        return False  # sin id no podemos deduplicar, dejamos pasar
    with mensajes_procesados_lock:
        if mensaje_id in mensajes_procesados:
            return True
        mensajes_procesados.add(mensaje_id)
        if len(mensajes_procesados) > MAX_MENSAJES_PROCESADOS:
            mensajes_procesados.pop()
        return False


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


def info_enviada_vacia():
    """Rastrea qué bloques de información 'fija' ya se le mandaron a este
    cliente, para no repetirlos en cada respuesta (datos de pago, colores,
    ubicación del local, link del catálogo)."""
    return {
        "datos_pago": False,
        "colores_disponibles": False,
        "ubicacion_local": False,
        "catalogo_pdf": False,
    }


def obtener_sesion(numero):
    """Devuelve (y crea si no existe) la sesión de un cliente por su número."""
    with sesiones_lock:
        if numero not in sesiones:
            sesiones[numero] = {
                "messages": [],
                "pedido": pedido_vacio(),
                "info_enviada": info_enviada_vacia(),
                "imagenes_enviadas": set(),  # claves de CATALOGO_IMAGENES ya mandadas
                "lock": threading.Lock(),  # serializa mensajes del MISMO cliente
            }
        return sesiones[numero]


def resumen_pedido(pedido):
    datos = [f"{k}: {v}" for k, v in pedido.items() if v]
    return "\n".join(datos) if datos else "Sin datos confirmados."


def resumen_info_enviada(info_enviada):
    ya_enviados = [k for k in info_enviada if info_enviada[k]]
    if not ya_enviados:
        return "Nada de esto se ha enviado todavía."
    etiquetas = {
        "datos_pago": "Datos bancarios para el anticipo",
        "colores_disponibles": "Lista de colores disponibles",
        "ubicacion_local": "Ubicación del local (link de Maps)",
        "catalogo_pdf": "Link del catálogo completo en PDF",
    }
    return "\n".join(f"- {etiquetas[k]}: YA SE ENVIÓ, no lo repitas" for k in ya_enviados)


def detectar_info_enviada(texto_respuesta):
    """Revisa el texto que el bot está a punto de mandar y marca qué bloques
    de información fija incluyó, para no repetirlos después."""
    texto = texto_respuesta.lower()
    detectado = {
        "datos_pago": ("5579 0701 5291 2153" in texto_respuesta) or ("clabe" in texto),
        "colores_disponibles": ("turquesa" in texto and "rosa palo" in texto),
        "ubicacion_local": "maps.app.goo.gl" in texto,
        "catalogo_pdf": bool(URL_CATALOGO_PDF) and (URL_CATALOGO_PDF.lower() in texto),
    }
    return detectado


def seccion_fotos_producto(catalogo_imagenes):
    if not catalogo_imagenes:
        return ""  # no hay fotos cargadas todavía, no mencionamos la herramienta

    lista = "\n".join(
        f"- \"{clave}\" -> {info['nombre_mostrar']}"
        for clave, info in catalogo_imagenes.items()
    )
    return f"""
Cuando el cliente muestre interés claro en ver cómo se ve un producto
específico (pregunta "cómo se ve", "tienes foto", muestra intención de
comprar ese producto, o es la primera vez que pregunta por ese producto en
la conversación), llama a la función mostrar_foto_producto con la clave del
producto correspondiente. No la llames en cada mensaje ni para productos que
el cliente no mencionó. Si ya le mandaste la foto de ese producto antes en
esta conversación, no la vuelvas a mandar salvo que el cliente la pida de
nuevo explícitamente.

FOTOS DE PRODUCTO DISPONIBLES (clave -> producto):
{lista}

Solo puedes mostrar fotos de estas claves. Si el cliente pregunta por un
producto que no está en esta lista, no llames la función; simplemente
indícale que por ahora no tienes foto de ese producto.
"""


def sumar_dias_habiles(fecha_inicio, dias_habiles):
    """Suma días hábiles a una fecha, saltando domingos (el local no abre domingos)."""
    fecha = fecha_inicio
    dias_sumados = 0
    while dias_sumados < dias_habiles:
        fecha += timedelta(days=1)
        if fecha.weekday() != 6:  # 6 = domingo
            dias_sumados += 1
    return fecha


def seccion_catalogo_pdf():
    if not URL_CATALOGO_PDF:
        return ""  # no hay catálogo PDF cargado todavía

    return f"""
Si el cliente pide ver el CATÁLOGO COMPLETO, todos los productos, o el
catálogo general (no un producto específico), comparte este link donde
puede verlo completo en PDF:

{URL_CATALOGO_PDF}

No mandes el catálogo completo si el cliente solo pregunta por UN producto
en específico (para eso usa mostrar_foto_producto). No repitas este link si
ya se lo compartiste antes en esta conversación, salvo que lo pida de nuevo
explícitamente.
"""


def construir_system_prompt(pedido, info_enviada):
    ahora = datetime.now(ZONA_HORARIA_NEGOCIO)
    fecha = ahora.strftime("%d/%m/%Y")
    hora = ahora.strftime("%H:%M")

    dias = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
    dia_semana = dias[ahora.weekday()]

    fecha_minima = sumar_dias_habiles(ahora.date(), 4)
    fecha_maxima = sumar_dias_habiles(ahora.date(), 6)
    dia_semana_minima = dias[fecha_minima.weekday()]

    return f"""
Eres DALIA, asesora de ventas de Recuerditos Dalia.

Hoy es {dia_semana} {fecha}.
La hora actual es {hora} (hora de Monterrey, México).

Toda la información oficial está en la Base de Conocimiento.

REGLAS:
- Usa únicamente la Base de Conocimiento.
- Nunca inventes datos.
- Nunca inventes productos, precios o políticas.
- Si algo no existe en la Base de Conocimiento, indícalo.
- Responde como una asesora humana por WhatsApp.
- Sé amable, natural y orientada a cerrar ventas.
- Responde PRIMERO y de forma directa a lo que el cliente pidió en su último
  mensaje. No antepongas información que el cliente no pidió (ej. no repitas
  colores si el cliente está hablando de forma de entrega).
- Si el cliente dice que ya le diste cierta información antes ("ya me la
  pasaste", "otra vez?"), discúlpate en una sola frase breve y NO la repitas.

REGLAS DE FECHAS Y PEDIDOS URGENTES (usa SIEMPRE la fecha de hoy de arriba,
{dia_semana} {fecha}, para todo cálculo; nunca calcules fechas por tu cuenta):

- El tiempo normal de elaboración de un pedido es de 4 a 6 días hábiles.
- La fecha de entrega MÁS PRÓXIMA posible para un pedido NORMAL (no urgente)
  es el {dia_semana_minima} {fecha_minima.strftime('%d/%m/%Y')}. Un pedido
  normal podría tardar hasta el {fecha_maxima.strftime('%d/%m/%Y')}.
- Si el cliente pide una fecha de entrega ANTES de {fecha_minima.strftime('%d/%m/%Y')},
  eso es un PEDIDO URGENTE. Para pedidos urgentes aplican estas restricciones:
  - Solo se puede entregar EN EL LOCAL (nunca a domicilio ni en puntos de entrega).
  - No se aceptan pedidos urgentes los días sábado.
  - No se aceptan pedidos urgentes para entregarse en domingo (no abrimos domingos).
  - Avisa al cliente de estas restricciones ANTES de confirmar el pedido, de
    forma amable, y no confirmes un pedido urgente con entrega a domicilio o
    en punto de entrega bajo ninguna circunstancia.
- Nunca confirmes una fecha de entrega sin haber verificado si es un pedido
  normal o urgente según las reglas de arriba.

ESTADO ACTUAL DEL PEDIDO DE ESTE CLIENTE:

{resumen_pedido(pedido)}

No vuelvas a preguntar datos ya confirmados.
Pregunta únicamente los datos faltantes.

Cada vez que el cliente confirme o mencione un dato nuevo del pedido
(producto, cantidad, evento, fecha, colores, tipo de entrega o dirección),
llama a la función actualizar_pedido con los campos correspondientes para
guardarlo. Puedes llamarla varias veces en la conversación conforme se vayan
confirmando más datos. No llames la función con datos que el cliente no ha
confirmado todavía.

{seccion_fotos_producto(catalogo_imagenes=CATALOGO_IMAGENES)}

{seccion_catalogo_pdf()}

INFORMACIÓN QUE YA SE LE ENVIÓ A ESTE CLIENTE EN MENSAJES ANTERIORES
(no la repitas salvo que el cliente la pida explícitamente de nuevo):

{resumen_info_enviada(info_enviada)}

BASE DE CONOCIMIENTO:

{KNOWLEDGE}
"""


# ===========================
# HERRAMIENTA (function calling) PARA LLENAR EL PEDIDO
# ===========================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "actualizar_pedido",
            "description": (
                "Guarda o actualiza los datos del pedido del cliente que ya "
                "quedaron confirmados en la conversación. Llama esta función "
                "cada vez que el cliente confirme un dato nuevo. Solo incluye "
                "los campos que el cliente confirmó en este mensaje o que "
                "cambiaron; no hace falta mandar todos los campos cada vez."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "producto": {"type": "string", "description": "Producto pedido, ej. 'ositos con jaboncito'"},
                    "cantidad": {"type": "integer", "description": "Cantidad de piezas pedidas"},
                    "evento": {"type": "string", "description": "Tipo de evento, ej. 'baby shower', 'XV años'"},
                    "fecha_evento": {"type": "string", "description": "Fecha o día de entrega acordado"},
                    "color_toalla": {"type": "string"},
                    "color_mono": {"type": "string"},
                    "color_velita": {"type": "string"},
                    "tipo_entrega": {
                        "type": "string",
                        "description": "Uno de: 'local', 'punto_de_entrega', 'domicilio'",
                    },
                    "direccion": {"type": "string", "description": "Dirección o municipio para envío a domicilio"},
                },
            },
        },
    },
]

# Solo agregamos la herramienta de fotos si de verdad hay imágenes cargadas
# en la carpeta imagenes/ (un enum vacío haría fallar la llamada a OpenAI).
if CATALOGO_IMAGENES:
    TOOLS.append({
        "type": "function",
        "function": {
            "name": "mostrar_foto_producto",
            "description": (
                "Manda por WhatsApp la foto de un producto del catálogo. "
                "Úsala cuando el cliente muestre interés claro en ver un "
                "producto específico. No la llames repetidamente para el "
                "mismo producto en la misma conversación."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "producto": {
                        "type": "string",
                        "enum": list(CATALOGO_IMAGENES.keys()),
                        "description": "Clave del producto del que se debe mandar la foto",
                    },
                },
                "required": ["producto"],
            },
        },
    })


# ===========================
# LLAMADA A OPENAI
# ===========================

def aplicar_actualizacion_pedido(pedido, argumentos_json):
    """Aplica al dict `pedido` los campos que el modelo mandó vía function calling."""
    try:
        datos = json.loads(argumentos_json) if argumentos_json else {}
    except (json.JSONDecodeError, TypeError):
        print("⚠️ No se pudo parsear argumentos de actualizar_pedido:", argumentos_json)
        return
    for campo, valor in datos.items():
        if campo in pedido and valor not in (None, ""):
            pedido[campo] = valor
    print("📝 Pedido actualizado:", pedido)


def preguntar_ia(numero, texto_cliente):
    sesion = obtener_sesion(numero)
    historial = sesion["messages"]
    pedido = sesion["pedido"]
    info_enviada = sesion["info_enviada"]

    historial.append({"role": "user", "content": texto_cliente})

    system_prompt = construir_system_prompt(pedido, info_enviada)
    mensajes_completos = [{"role": "system", "content": system_prompt}] + historial

    # Recortar historial para no crecer sin límite (igual que en main.py)
    if len(mensajes_completos) > MAX_TURNOS_HISTORIAL + 1:
        mensajes_completos = [mensajes_completos[0]] + mensajes_completos[-MAX_TURNOS_HISTORIAL:]
        sesion["messages"] = mensajes_completos[1:]
        historial = sesion["messages"]

    # Loop de function calling: el modelo puede llamar actualizar_pedido
    # una o varias veces antes de dar la respuesta final en texto.
    MAX_ITERACIONES_HERRAMIENTAS = 4
    for _ in range(MAX_ITERACIONES_HERRAMIENTAS):
        r = client.chat.completions.create(
            model=MODELO,
            messages=mensajes_completos,
            tools=TOOLS,
            temperature=0.4,
            top_p=0.9,
            max_tokens=600,
        )

        choice = r.choices[0]
        mensaje = choice.message

        if choice.finish_reason == "length":
            print("⚠️ Respuesta cortada por max_tokens, considera subirlo más")

        if mensaje.tool_calls:
            # Guardamos el mensaje del asistente (con los tool_calls) en la conversación
            mensajes_completos.append(mensaje.model_dump(exclude_none=True))

            for tool_call in mensaje.tool_calls:
                if tool_call.function.name == "actualizar_pedido":
                    aplicar_actualizacion_pedido(pedido, tool_call.function.arguments)
                    resultado = "ok"

                elif tool_call.function.name == "mostrar_foto_producto":
                    try:
                        args = json.loads(tool_call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    clave = args.get("producto")
                    imagenes_enviadas = sesion["imagenes_enviadas"]

                    if clave in imagenes_enviadas:
                        resultado = "ya se le mandó esta foto antes en la conversación, no la repitas"
                    else:
                        url_imagen = url_imagen_producto(clave)
                        if url_imagen:
                            nombre_mostrar = CATALOGO_IMAGENES[clave]["nombre_mostrar"]
                            enviar_whatsapp_imagen(numero, url_imagen, caption=nombre_mostrar)
                            imagenes_enviadas.add(clave)
                            resultado = "imagen enviada correctamente"
                        else:
                            resultado = f"no hay foto disponible para '{clave}', no ofrezcas una foto de esto"

                else:
                    resultado = "función desconocida"

                mensajes_completos.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": resultado,
                })

            # Como el pedido pudo cambiar, refrescamos el system prompt antes
            # de la siguiente vuelta (por si el resumen del pedido cambia).
            mensajes_completos[0]["content"] = construir_system_prompt(pedido, info_enviada)
            continue  # volvemos a llamar al modelo para que dé la respuesta en texto

        # No hubo (más) tool_calls: esta es la respuesta final para el cliente
        texto = mensaje.content or "Disculpa, ¿me repites tu mensaje? 🙂"
        historial.append({"role": "assistant", "content": texto})

        # Marca qué bloques de info fija se acaban de enviar, para no repetirlos
        detectado = detectar_info_enviada(texto)
        for clave, se_envio in detectado.items():
            if se_envio:
                info_enviada[clave] = True

        return texto

    # Si se agotaron las iteraciones de herramientas sin respuesta de texto
    texto = "Disculpa, dame un segundo y te confirmo 🙂"
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


def enviar_whatsapp_imagen(numero, image_url, caption=""):
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "image",
        "image": {"link": image_url, "caption": caption},
    }
    try:
        r = requests.post(GRAPH_URL, headers=headers, json=data, timeout=15)
        if r.status_code >= 400:
            print("⚠️ Error enviando imagen por WhatsApp:", r.status_code, r.text)
        else:
            print(f"📤 Imagen enviada a {numero}: {image_url}")
        return r
    except requests.RequestException as e:
        print("⚠️ Excepción enviando imagen por WhatsApp:", e)
        return None


# ===========================
# SERVIR LAS FOTOS DE PRODUCTO
# WhatsApp necesita descargar la imagen de una URL pública para poder
# mandarla; esta ruta expone lo que hay en la carpeta imagenes/.
# ===========================

@app.route("/imagenes/<path:nombre_archivo>")
def servir_imagen_producto(nombre_archivo):
    return send_from_directory(CARPETA_IMAGENES, nombre_archivo)


@app.route("/catalogo/<path:nombre_archivo>")
def servir_catalogo_pdf(nombre_archivo):
    return send_from_directory(CARPETA_CATALOGO, nombre_archivo)


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

def procesar_mensaje_en_fondo(numero, texto_cliente):
    """Corre en un hilo aparte para no bloquear la respuesta al webhook de Meta."""
    print("=" * 70)
    print(f"🚀 Procesando mensaje de {numero}")
    print(f"💬 Texto recibido: {texto_cliente}")

    sesion = obtener_sesion(numero)
    # Serializa mensajes del MISMO cliente (si llegan muy pegados) sin
    # bloquear el procesamiento de otros clientes.
    with sesion["lock"]:
        try:
            print("🧠 Consultando OpenAI...")
            respuesta = preguntar_ia(numero, texto_cliente)
            print("✅ Respuesta generada")
            print(respuesta[:300])
        except Exception as e:
            print("❌ Error llamando a OpenAI:", repr(e))
            respuesta = "Disculpa, tuve un problema técnico. ¿Me puedes repetir tu mensaje? 🙂"

        # Pequeña espera para que no se sienta instantáneo/robótico
        time.sleep(random.uniform(2, 4))
        print("📤 Enviando respuesta a WhatsApp...")
        r = enviar_whatsapp(numero, respuesta)
        if r is not None:
            print(f"📨 WhatsApp respondió: {r.status_code}")
        else:
            print("❌ enviar_whatsapp devolvió None")

    print("🏁 Fin procesamiento")
    print("=" * 70)


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
        mensaje_id = mensaje.get("id")

        # Si Meta reintentó el webhook (mismo message id), lo ignoramos.
        if ya_fue_procesado(mensaje_id):
            print(f"🔁 Mensaje duplicado ignorado: {mensaje_id}")
            return jsonify({"status": "duplicado ignorado"}), 200

        if tipo != "text":
            threading.Thread(
                target=enviar_whatsapp,
                args=(numero, "Por ahora solo puedo leer mensajes de texto 🙂 ¿me lo escribes con palabras?"),
                daemon=True,
            ).start()
            return jsonify({"status": "tipo de mensaje no soportado"}), 200

        texto_cliente = mensaje["text"]["body"]

        # Procesamos en background y respondemos 200 OK de inmediato a Meta,
        # para reducir el riesgo de que Meta reintente el webhook por timeout.
        threading.Thread(
            target=procesar_mensaje_en_fondo,
            args=(numero, texto_cliente),
            daemon=True,
        ).start()

    except (KeyError, IndexError, TypeError) as e:
        # Payload inesperado (ej. notificación de estado) -> no truena el servidor
        print("Evento sin mensaje de texto reconocible:", e)

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    puerto = int(os.getenv("PORT", 5000))
    app.run(port=puerto, debug=debug_mode)
