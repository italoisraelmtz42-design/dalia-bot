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

# ===========================
# CONFIGURACIÓN
# ===========================

load_dotenv()

app = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Crea las tablas de SQLite si no existen (y agrega columnas nuevas a las
# que ya existían). app.py nunca ejecuta SQL directamente: todo pasa por
# crm.py -> clientes.py / historial.py / pedidos.py / database.py.
try:
    crm.inicializar_base_datos()
    print("✅ Base de datos (SQLite) lista")
except Exception as e:
    # No tumbamos el bot si la base de datos falla al iniciar: el bot sigue
    # funcionando con la memoria en RAM (sesiones) como hasta ahora.
    print("⚠️ No se pudo inicializar la base de datos:", repr(e))

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_ID = os.getenv("WHATSAPP_PHONE_ID")
# El token de verificación ya NO va escrito directo en el código.
# Defínelo en tu .env como WHATSAPP_VERIFY_TOKEN=lo-que-tu-quieras
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "cambia_este_token")

# App Secret de tu app de Meta (developers.facebook.com -> tu app ->
# Configuración -> Básica -> "Clave secreta de la app"). Se usa para
# verificar que cada webhook que llega de verdad viene de Meta, y no de
# alguien que descubrió la URL y manda mensajes falsos. Si no lo defines
# todavía, el bot sigue funcionando igual que antes (sin esta protección),
# solo con una advertencia en los logs.
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET", "")

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
            bloque = f"""

==================================================
ARCHIVO: {archivo.name}
==================================================

{contenido}

==================================================
FIN DEL ARCHIVO
==================================================

"""
            knowledge += bloque
            CONOCIMIENTO_POR_ARCHIVO[archivo.name] = bloque
        except Exception as e:
            print(f"❌ Error leyendo {archivo.name}: {e}")

    print("\n" + "=" * 60)
    print("TOTAL DE ARCHIVOS :", len(archivos))
    print("TOTAL CARACTERES  :", len(knowledge))
    print("=" * 60 + "\n")

    return knowledge


# Etapa 2: se guarda también cada archivo por separado (mismo formato de
# bloque que ya se usaba) para poder seleccionar solo los relevantes al
# mensaje del cliente, en vez de mandar los ~48 archivos completos en
# cada llamada a OpenAI.
CONOCIMIENTO_POR_ARCHIVO = {}
KNOWLEDGE = cargar_conocimiento()

# Archivos que se incluyen SIEMPRE sin importar el mensaje del cliente,
# porque son reglas transversales (aplican casi a cualquier conversación:
# cómo hablar, cómo cobrar, qué colores hay, cómo se vende). Si cambias
# los nombres de estos archivos en /conocimiento, actualiza esta lista.
ARCHIVOS_CONOCIMIENTO_SIEMPRE = {
    "04_REGLAS_GENERALES.txt",
    "033_Reglas_Conversacion.txt",
    "027_Pagos_y_Anticipos.txt",
    "028_Colores_Disponibles.txt",
    "029_Flujo_de_Venta.txt",
    "050_Saludos_Humanos.txt",
}


def seleccionar_conocimiento_relevante(texto_cliente, historial_reciente=None, top_k=16):
    """Selecciona un subconjunto de archivos de conocimiento relevantes al
    mensaje del cliente (más los de ARCHIVOS_CONOCIMIENTO_SIEMPRE), en vez
    de mandar los ~48 archivos completos en cada llamada a OpenAI.

    Es un filtro simple por coincidencia de palabras, NO es RAG con
    embeddings (eso queda como mejora futura, ver roadmap). Si por
    cualquier motivo no hay archivos cargados individualmente, regresa el
    KNOWLEDGE completo como respaldo (mismo comportamiento que antes).
    """
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
# SESIONES POR CLIENTE
# Cada número de WhatsApp tiene su propio historial
# y su propio "pedido" en construcción.
# ===========================

sesiones = {}
sesiones_lock = threading.Lock()

# IDs de mensajes de WhatsApp ya procesados, para ignorar reintentos que
# Meta manda si el webhook no responde 200 OK lo bastante rápido.
# Se guardan en un set (para checar existencia rápido) + una lista que
# mantiene el orden real de llegada, así al llenarse se descarta siempre
# el más viejo (antes se usaba set.pop(), que en Python no garantiza cuál
# elemento quita).
mensajes_procesados = set()
orden_mensajes_procesados = []
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
        orden_mensajes_procesados.append(mensaje_id)
        if len(orden_mensajes_procesados) > MAX_MENSAJES_PROCESADOS:
            mas_viejo = orden_mensajes_procesados.pop(0)
            mensajes_procesados.discard(mas_viejo)
        return False


def verificar_firma_webhook(payload_bytes, firma_header):
    """Verifica que el request al webhook realmente venga de Meta, usando
    el App Secret para comparar contra el header X-Hub-Signature-256.

    Si WHATSAPP_APP_SECRET todavía no está configurado, deja pasar todo
    (igual que antes de este cambio) pero avisa en logs, para no romper
    instalaciones existentes de un día para otro.
    """
    if not WHATSAPP_APP_SECRET:
        print("⚠️ WHATSAPP_APP_SECRET no configurado: el webhook NO está verificando su origen")
        return True

    if not firma_header or not firma_header.startswith("sha256="):
        return False

    firma_esperada = hmac.new(
        WHATSAPP_APP_SECRET.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()
    firma_recibida = firma_header.split("sha256=", 1)[1]

    return hmac.compare_digest(firma_esperada, firma_recibida)


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
        "anticipo_confirmado": None,
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
    """Devuelve (y crea si no existe) la sesión de un cliente por su número.

    Etapa 1 del roadmap: SQLite es la fuente de verdad. Si el número no
    tiene sesión en RAM todavía (primer mensaje desde que arrancó este
    proceso, por ejemplo después de que Render reinició el servicio), la
    sesión se "hidrata" leyendo el historial y el pedido desde SQLite, en
    vez de empezar en blanco. Así la memoria del cliente sobrevive un
    reinicio, no solo mientras el proceso siga vivo.

    Una vez hidratada, la sesión vive en RAM igual que antes (preguntar_ia
    no cambió) y se sigue sincronizando hacia SQLite después de cada
    respuesta (ver crm.sincronizar_pedido en procesar_mensaje_en_fondo).
    """
    with sesiones_lock:
        if numero not in sesiones:
            mensajes_previos = []
            pedido_previo = None
            try:
                cliente = crm.cargar_cliente(numero)
                mensajes_previos = crm.cargar_memoria(cliente, limite=MAX_TURNOS_HISTORIAL)
                pedido_previo = crm.cargar_pedido_ram(cliente)
                if mensajes_previos or (pedido_previo and any(pedido_previo.values())):
                    print(f"♻️ Sesión de {numero} hidratada desde SQLite ({len(mensajes_previos)} mensajes previos)")
            except Exception as e:
                # Si SQLite falla por lo que sea, el bot sigue funcionando
                # con una sesión en blanco (el comportamiento de antes),
                # nunca se cae por esto.
                print(f"⚠️ No se pudo hidratar sesión de {numero} desde SQLite, arranca en blanco: {repr(e)}")

            sesiones[numero] = {
                "messages": mensajes_previos,
                "pedido": pedido_previo or pedido_vacio(),
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


def construir_system_prompt(pedido, info_enviada, conocimiento=None):
    if conocimiento is None:
        conocimiento = KNOWLEDGE  # respaldo: comportamiento igual que antes

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

RECEPCIÓN DE IMÁGENES DEL CLIENTE (Vision):
Cuando el cliente te mande una imagen, clasifícala primero en una de estas
categorías y actúa según corresponda:

1. COMPROBANTE DE PAGO (pantalla de banco, ticket, captura de transferencia
   o depósito): confirma amablemente que lo recibiste, menciona el monto
   si lo puedes leer con claridad, agradece, y llama a actualizar_pedido
   con anticipo_confirmado=true. Avísale que en breve le confirman su
   pedido. Si el monto no se alcanza a leer bien, dile que no se ve claro
   y pide que lo reenvíe o confirme el monto por texto — no inventes un
   monto que no puedas leer con seguridad.
2. REFERENCIA DE COLOR (foto de un color/tela/objeto que el cliente manda
   para pedir "quiero este color"): compáralo con los colores disponibles
   en la Base de Conocimiento y dile cuál de los tuyos se parece más. No
   asumas un color exacto que no tengas.
3. EJEMPLO O REFERENCIA DE PRODUCTO (foto de un recuerdito de otro lado
   que el cliente manda como inspiración): coméntale con naturalidad qué
   tan parecido es a lo que ustedes manejan, sin prometer una réplica
   exacta si no la tienen.
4. OTRA IMAGEN (no encaja en ninguna de las anteriores): coméntalo con
   naturalidad y sigue la conversación normal, sin asumir que es un pago.

INFORMACIÓN QUE YA SE LE ENVIÓ A ESTE CLIENTE EN MENSAJES ANTERIORES
(no la repitas salvo que el cliente la pida explícitamente de nuevo):

{resumen_info_enviada(info_enviada)}

BASE DE CONOCIMIENTO:

{conocimiento}
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
                    "anticipo_confirmado": {
                        "type": "boolean",
                        "description": (
                            "Márcalo como true cuando el cliente mande una imagen que sea "
                            "claramente un comprobante de pago o transferencia del anticipo."
                        ),
                    },
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


def ejecutar_tool_call(tool_call, sesion, numero, pedido):
    """Ejecuta una sola llamada a herramienta que pidió el modelo y
    devuelve el string de resultado que se le manda de vuelta a OpenAI.
    Se extrajo de preguntar_ia() para que esa función no quedara tan larga
    (Etapa 4: limpieza de código, mismo comportamiento de antes)."""
    if tool_call.function.name == "actualizar_pedido":
        aplicar_actualizacion_pedido(pedido, tool_call.function.arguments)
        return "ok"

    if tool_call.function.name == "mostrar_foto_producto":
        try:
            args = json.loads(tool_call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        clave = args.get("producto")
        imagenes_enviadas = sesion["imagenes_enviadas"]

        if clave in imagenes_enviadas:
            return "ya se le mandó esta foto antes en la conversación, no la repitas"

        url_imagen = url_imagen_producto(clave)
        if not url_imagen:
            return f"no hay foto disponible para '{clave}', no ofrezcas una foto de esto"

        nombre_mostrar = CATALOGO_IMAGENES[clave]["nombre_mostrar"]
        enviar_whatsapp_imagen(numero, url_imagen, caption=nombre_mostrar)
        imagenes_enviadas.add(clave)
        return "imagen enviada correctamente"

    return "función desconocida"


def preguntar_ia(numero, texto_cliente, imagen_base64=None, imagen_mime=None):
    sesion = obtener_sesion(numero)
    historial = sesion["messages"]
    pedido = sesion["pedido"]
    info_enviada = sesion["info_enviada"]

    if imagen_base64:
        # Etapa 3 (Vision): mensaje multimodal, texto (si lo hay) + la
        # imagen que mandó el cliente.
        contenido_usuario = [
            {"type": "text", "text": texto_cliente or "(El cliente mandó una imagen sin texto)"},
            {
                "type": "image_url",
                "image_url": {"url": f"data:{imagen_mime};base64,{imagen_base64}"},
            },
        ]
    else:
        contenido_usuario = texto_cliente

    historial.append({"role": "user", "content": contenido_usuario})

    # Etapa 2: en vez de mandar SIEMPRE la base de conocimiento completa,
    # se selecciona solo lo relevante al mensaje (+ los archivos que
    # siempre aplican). Se calcula una vez por mensaje y se reutiliza en
    # las vueltas del loop de herramientas de abajo.
    conocimiento_relevante = seleccionar_conocimiento_relevante(texto_cliente, historial_reciente=historial)

    system_prompt = construir_system_prompt(pedido, info_enviada, conocimiento=conocimiento_relevante)
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
                resultado = ejecutar_tool_call(tool_call, sesion, numero, pedido)
                mensajes_completos.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": resultado,
                })

            # Como el pedido pudo cambiar, refrescamos el system prompt antes
            # de la siguiente vuelta (por si el resumen del pedido cambia).
            mensajes_completos[0]["content"] = construir_system_prompt(
                pedido, info_enviada, conocimiento=conocimiento_relevante
            )
            continue  # volvemos a llamar al modelo para que dé la respuesta en texto

        # No hubo (más) tool_calls: esta es la respuesta final para el cliente
        texto = mensaje.content or "Disculpa, ¿me repites tu mensaje? 🙂"
        historial.append({"role": "assistant", "content": texto})

        # Marca qué bloques de info fija se acaban de enviar, para no repetirlos
        detectado = detectar_info_enviada(texto)
        for clave, se_envio in detectado.items():
            if se_envio:
                info_enviada[clave] = True

        # Etapa 2: registrar cuántos tokens consumió esta llamada (para
        # poder ver costo aproximado más adelante en un dashboard). Si
        # esto falla por lo que sea, nunca debe tumbar la respuesta al
        # cliente -> va en su propio try/except.
        try:
            uso = getattr(r, "usage", None)
            if uso is not None:
                crm.registrar_uso_openai(
                    numero, MODELO,
                    getattr(uso, "prompt_tokens", None),
                    getattr(uso, "completion_tokens", None),
                )
        except Exception as e:
            print("⚠️ No se pudo registrar uso de OpenAI:", repr(e))

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

print("=" * 60)
print("STATUS:", r.status_code)
print("BODY:")
print(r.text)
print("=" * 60)

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


def descargar_imagen_whatsapp(media_id):
    """Descarga una imagen que el CLIENTE mandó por WhatsApp (Etapa 3:
    Vision), usando su media_id. Meta funciona en dos pasos: primero da la
    URL real del archivo (que expira rápido), luego hay que descargarla
    con el mismo token. Devuelve (bytes_de_la_imagen, mime_type) o
    (None, None) si falla."""
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    try:
        r = requests.get(f"https://graph.facebook.com/{GRAPH_API_VERSION}/{media_id}", headers=headers, timeout=15)
        if r.status_code >= 400:
            print("⚠️ Error obteniendo URL del medio:", r.status_code, r.text)
            return None, None

        info = r.json()
        url_medio = info.get("url")
        mime_type = info.get("mime_type", "image/jpeg")
        if not url_medio:
            print("⚠️ La respuesta de Meta no trajo URL del medio:", info)
            return None, None

        r2 = requests.get(url_medio, headers=headers, timeout=20)
        if r2.status_code >= 400:
            print("⚠️ Error descargando el archivo del medio:", r2.status_code)
            return None, None

        return r2.content, mime_type
    except requests.RequestException as e:
        print("⚠️ Excepción descargando imagen de WhatsApp:", e)
        return None, None


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

def registrar_entrada_cliente(numero, texto_para_guardar, tipo="texto"):
    """Crea/actualiza el cliente en la base de datos y guarda su mensaje.
    Se llama para CUALQUIER mensaje entrante, sea texto o no soportado."""
    cliente = crm.cargar_cliente(numero)
    crm.guardar_mensaje_cliente(cliente, texto_para_guardar, tipo=tipo)
    return cliente


def procesar_mensaje_no_soportado(numero, tipo):
    """Registra en el CRM un mensaje de un tipo que el bot todavía no
    puede leer (imagen, audio, etc.) y le avisa al cliente."""
    cliente = registrar_entrada_cliente(numero, f"[mensaje no soportado: {tipo}]", tipo=tipo)
    respuesta = "Por ahora solo puedo leer mensajes de texto 🙂 ¿me lo escribes con palabras?"
    crm.guardar_respuesta(cliente, respuesta)
    enviar_whatsapp(numero, respuesta)


def procesar_mensaje_en_fondo(numero, texto_cliente, media_id_imagen=None):
    """Corre en un hilo aparte para no bloquear la respuesta al webhook de Meta."""
    print("=" * 70)
    print(f"🚀 Procesando mensaje de {numero}")
    print(f"💬 Texto recibido: {texto_cliente}")

    imagen_base64 = None
    imagen_mime = None
    tipo_para_crm = "texto"
    if media_id_imagen:
        print("🖼️ El cliente mandó una imagen (Vision), descargándola...")
        contenido, mime = descargar_imagen_whatsapp(media_id_imagen)
        if contenido:
            imagen_base64 = base64.b64encode(contenido).decode("utf-8")
            imagen_mime = mime
            tipo_para_crm = "imagen"
            print(f"✅ Imagen descargada ({len(contenido)} bytes, {mime})")
        else:
            print("❌ No se pudo descargar la imagen del cliente, se sigue solo con el texto (si había)")

    # CRM: crea/actualiza el cliente y guarda su mensaje en SQLite. Esto
    # corre en paralelo a la memoria en RAM (sesiones), que sigue siendo la
    # que usa preguntar_ia sin ningún cambio.
    texto_para_guardar = texto_cliente or ("(imagen sin texto)" if media_id_imagen else "")
    cliente = registrar_entrada_cliente(numero, texto_para_guardar, tipo=tipo_para_crm)

    sesion = obtener_sesion(numero)
    # Serializa mensajes del MISMO cliente (si llegan muy pegados) sin
    # bloquear el procesamiento de otros clientes.
    with sesion["lock"]:
        try:
            print("🧠 Consultando OpenAI...")
            respuesta = preguntar_ia(numero, texto_cliente, imagen_base64=imagen_base64, imagen_mime=imagen_mime)
            print("✅ Respuesta generada")
            print(respuesta[:300])
        except Exception as e:
            print("❌ Error llamando a OpenAI:", repr(e))
            respuesta = "Disculpa, tuve un problema técnico. ¿Me puedes repetir tu mensaje? 🙂"

        # CRM: guarda la respuesta del bot y sincroniza el pedido (RAM ->
        # SQLite) sin modificar preguntar_ia ni la lógica de ventas.
        try:
            crm.guardar_respuesta(cliente, respuesta)
            crm.sincronizar_pedido(cliente, sesion["pedido"])
        except Exception as e:
            print("⚠️ Error guardando en CRM (el bot sigue funcionando con RAM):", repr(e))

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
    firma = request.headers.get("X-Hub-Signature-256", "")
    if not verificar_firma_webhook(request.get_data(), firma):
        print("🚫 Webhook rechazado: la firma no coincide (el request no parece venir de Meta)")
        return jsonify({"status": "firma inválida"}), 403

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

        if tipo == "image":
            # Etapa 3 (Vision): se procesa igual que un mensaje de texto,
            # pero con la imagen adjunta. El caption (si el cliente le
            # puso texto a la foto) se manda como el "texto" del mensaje.
            media_id = mensaje["image"]["id"]
            caption = mensaje["image"].get("caption", "")
            threading.Thread(
                target=procesar_mensaje_en_fondo,
                args=(numero, caption),
                kwargs={"media_id_imagen": media_id},
                daemon=True,
            ).start()
            return jsonify({"status": "ok"}), 200

        if tipo != "text":
            threading.Thread(
                target=procesar_mensaje_no_soportado,
                args=(numero, tipo),
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
