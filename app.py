import os
import json
import time
import random
import re
import hmac
import hashlib
import base64
import logging
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
import audio_handler
from constantes import ModoAtencion

# ===========================
# CONFIGURACIÓN
# ===========================

load_dotenv()

# 🔥 CAMBIO CLAVE: sin esto, logger_crm.info(...) y logger_pedidos.info(...)
# (folio creado, borrador sincronizado, etc.) nunca se imprimen en los logs
# de Render, porque el logging de Python por defecto solo muestra WARNING+.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    force=True,  # por si algún import ya tocó el root logger antes
)

app = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Crea las tablas de SQLite si no existen
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
CARPETA_NOTAS.mkdir(exist_ok=True)  # Asegura que la carpeta exista

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
# CARGAR BASE DE CONOCIMIENTO (RECURSIVO CON OS.WALK Y DIAGNÓSTICO)
# ===========================

def cargar_conocimiento():
    knowledge = ""
    
    print("\n" + "=" * 60)
    print("CARGANDO BASE DE CONOCIMIENTO...")
    print("=" * 60)

    # 1. Ruta absoluta y diagnóstico de la carpeta
    ruta_absoluta = CARPETA.resolve()
    print(f"Ruta absoluta de conocimiento: {ruta_absoluta}")
    print(f"¿Existe?: {ruta_absoluta.exists()}")

    # 2. Variables para conteo y listado
    encontrados_txt = []
    carpetas_encontradas = set()
    archivos_encontrados = []

    # 3. Recorrer recursivamente la carpeta con os.walk() para obtener estructura
    if ruta_absoluta.exists():
        for root, dirs, files in os.walk(str(ruta_absoluta)):
            root_path = Path(root)
            rel_root = root_path.relative_to(ruta_absoluta) if root_path != ruta_absoluta else Path('.')
            # Guardamos la ruta de la carpeta relativa para listarla
            if str(rel_root) != '.':
                carpetas_encontradas.add(str(rel_root))
            
            for file in files:
                if file.lower().endswith('.txt'):
                    full_path = root_path / file
                    encontrados_txt.append(full_path)
                    archivos_encontrados.append(str(full_path.relative_to(ruta_absoluta)))

        # 4. Imprimir listado de carpetas
        print("\nListado de carpetas encontradas:")
        if not carpetas_encontradas:
            print("  (Solo la raíz)")
        for carpeta in sorted(carpetas_encontradas):
            print(f"  📁 {carpeta}")

        # 5. Imprimir listado de archivos
        print("\nListado de archivos encontrados:")
        for archivo in sorted(archivos_encontrados):
            print(f"  📄 {archivo}")

        print(f"\nTOTAL TXT ENCONTRADOS: {len(encontrados_txt)}")

        # 6. Si no encuentra nada, imprime la estructura completa del árbol y aborta
        if len(encontrados_txt) == 0:
            print("\n🟡 ADVERTENCIA: NO SE ENCONTRARON ARCHIVOS .TXT. ESTRUCTURA COMPLETA DEL DIRECTORIO:")
            for root, dirs, files in os.walk(str(ruta_absoluta)):
                nivel = root.replace(str(ruta_absoluta), '').count(os.sep)
                indent = ' ' * (nivel * 2)
                print(f"{indent}📁 {Path(root).name}/")
                for file in files:
                    print(f"{indent}   📄 {file}")
            return ""  # No cargar nada si hay 0 archivos

    else:
        print(f"❌ ERROR: La carpeta '{ruta_absoluta}' NO EXISTE.")
        return ""

    # 7. Cargar recursivamente los archivos .txt encontrados (insensible a mayúsculas)
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
    print(f"TOTAL TXT CARGADOS: {len(CONOCIMIENTO_POR_ARCHIVO)}")
    print("TOTAL CARACTERES  :", len(knowledge))
    print("=" * 60 + "\n")

    return knowledge


CONOCIMIENTO_POR_ARCHIVO = {}
KNOWLEDGE = cargar_conocimiento()

ARCHIVOS_CONOCIMIENTO_SIEMPRE = {
    "04_REGLAS_GENERALES.txt",
    "033_Reglas_Conversacion.txt",
    "027_Pagos_y_Anticipos.txt",
    "028_Colores_Disponibles.txt",
    "029_Flujo_de_Venta.txt",
    "050_Saludos_Humanos.txt",
}


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
# SESIONES POR CLIENTE
# ===========================

sesiones = {}
sesiones_lock = threading.Lock()

mensajes_procesados = set()
orden_mensajes_procesados = []
mensajes_procesados_lock = threading.Lock()
MAX_MENSAJES_PROCESADOS = 2000


def ya_fue_procesado(mensaje_id):
    if not mensaje_id:
        return False
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
        "municipio": None,
        "tipo_jaboncito": None,
        "color_jaboncito": None,
        "nombre_bebe": None,
        "tarjetita": None,
        "notas": None,
        # 🔧 NUEVOS (corrigen el hallazgo crítico de la auditoría forense:
        # antes estos datos se perdían al confirmar el pedido oficial
        # porque el modelo nunca tenía forma de capturarlos).
        "precio_unitario": None,
        "monto_anticipo": None,
        "metodo_pago": None,
        "comprobante": None,
    }


def info_enviada_vacia():
    return {
        "datos_pago": False,
        "colores_disponibles": False,
        "ubicacion_local": False,
        "catalogo_pdf": False,
    }


# ==============================================================================
# 🔴 CAMBIO CLAVE 1: HIDRATACIÓN DE SESIÓN DESDE BORRADOR
# ==============================================================================
def obtener_sesion(numero):
    with sesiones_lock:
        if numero not in sesiones:
            mensajes_previos = []
            pedido_previo = None
            pedido_id = None
            try:
                cliente = crm.cargar_cliente(numero)
                mensajes_previos = crm.cargar_memoria(cliente, limite=MAX_TURNOS_HISTORIAL)
                pedido_db = crm.cargar_pedido(cliente)
                pedido_previo = crm.pedido_para_ram(pedido_db)
                if pedido_db:
                    pedido_id = pedido_db.id
                
                # Si no hay pedido oficial, intentamos cargar el BORRADOR persistente
                if not pedido_previo or not any(pedido_previo.values()):
                    borrador = pedido_manager.cargar_borrador_pedido(numero)
                    if borrador:
                        pedido_previo = borrador
                        print(f"♻️ Borrador persistente cargado desde SQLite para {numero}")
                
                if mensajes_previos or (pedido_previo and any(pedido_previo.values())):
                    print(f"♻️ Sesión de {numero} hidratada desde SQLite ({len(mensajes_previos)} mensajes previos, pedido ID {pedido_id})")
            except Exception as e:
                print(f"⚠️ No se pudo hidratar sesión de {numero} desde SQLite, arranca en blanco: {repr(e)}")

            sesiones[numero] = {
                "messages": mensajes_previos,
                "pedido": pedido_previo or pedido_vacio(),
                "pedido_id": pedido_id,
                "info_enviada": info_enviada_vacia(),
                "imagenes_enviadas": set(),
                "lock": threading.Lock(),
            }
        
        # 🔥 SIEMPRE recargamos el borrador desde SQLite al inicio de cada mensaje
        borrador = pedido_manager.cargar_borrador_pedido(numero)
        if borrador:
            sesiones[numero]["pedido"] = borrador
        return sesiones[numero]


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
        return ""

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
    fecha = fecha_inicio
    dias_sumados = 0
    while dias_sumados < dias_habiles:
        fecha += timedelta(days=1)
        if fecha.weekday() != 6:
            dias_sumados += 1
    return fecha


def seccion_catalogo_pdf():
    if not URL_CATALOGO_PDF:
        return ""

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


def construir_system_prompt(pedido, pedido_id, info_enviada, conocimiento=None):
    if conocimiento is None:
        conocimiento = KNOWLEDGE

    resumen = pedido_manager.generar_resumen(pedido_id=pedido_id, borrador=pedido)

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

ESTADO ACTUAL DEL PEDIDO DE ESTE CLIENTE (desde base de datos):

{resumen}

No vuelvas a preguntar datos ya confirmados.
Pregunta únicamente los datos faltantes.

Cada vez que el cliente confirme o mencione un dato nuevo del pedido
(producto, cantidad, evento, fecha, colores, tipo de entrega o dirección),
llama a la función actualizar_pedido con los campos correspondientes para
guardarlo. Puedes llamarla varias veces en la conversación conforme se vayan
confirmando más datos. No llames la función con datos que el cliente no ha
confirmado todavía.

IMPORTANTE — PRECIO: en el momento en que le informes al cliente el precio
por pieza o el total del pedido (usando el precio de la Base de Conocimiento),
llama a actualizar_pedido incluyendo precio_unitario con ese valor numérico.
Si no guardas el precio aquí, el pedido oficial queda registrado con precio
$0, aunque se lo hayas dicho al cliente.

{seccion_fotos_producto(catalogo_imagenes=CATALOGO_IMAGENES)}

{seccion_catalogo_pdf()}

RECEPCIÓN DE IMÁGENES DEL CLIENTE (Vision):
Cuando el cliente te mande una imagen, clasifícala primero en una de estas
categorías y actúa según corresponda:

1. COMPROBANTE DE PAGO (pantalla de banco, ticket, captura de transferencia
   o depósito) CON MONTO LEGIBLE: confirma amablemente que lo recibiste,
   menciona el monto, agradece, y llama a actualizar_pedido con
   anticipo_confirmado=true, monto_anticipo (el monto que leas en la
   imagen), metodo_pago (ej. "transferencia" o "depósito", según lo que
   veas), y comprobante (una descripción breve de lo que se ve, ej. banco
   y referencia si se alcanzan a leer).

   IMPORTANTE: este es tu ÚLTIMO mensaje en esta conversación — en cuanto
   confirmas el anticipo, Dalia (la dueña, una persona real) toma el
   control para coordinar entrega, costos adicionales y seguimiento. Así
   que en este mensaje debes: agradecer, confirmar el monto recibido,
   avisar que su pedido ya se empezó a elaborar, incluir el emoji ⏳, y
   decirle que Dalia sigue la conversación desde aquí. No hagas preguntas
   de seguimiento ni ofrezcas más ayuda en este mensaje — ciérralo.

   Si el monto NO se alcanza a leer bien, es un caso distinto: dile que no
   se ve claro y pide que lo reenvíe o confirme el monto por texto — no
   inventes un monto que no puedas leer con seguridad, y en ese caso NO
   llenes monto_anticipo ni anticipo_confirmado (la conversación sigue
   normal, tú sigues respondiendo).
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

===========================================================
🚨 REGLA DE SEGURIDAD Y CIERRE AUTOMÁTICO (LEE ESTO CON ATENCIÓN):
===========================================================
Cuando el cliente CONFIRME explícitamente su pedido (diciendo "sí", "confirmo", "está bien", etc.) 
o cuando ENVÍE UN COMPROBANTE DE PAGO (imagen de transferencia o depósito),
DEBES llamar INMEDIATAMENTE a la función actualizar_pedido con todos los datos confirmados 
(anticipo_confirmado=true, producto, cantidad, colores, fecha, entrega, etc.) para guardar 
el pedido en la base de datos. No dejes el pedido en la RAM. Si el cliente ya confirmó todo,
la llamada a actualizar_pedido debe ejecutarse SIN que el cliente tenga que pedírtelo de nuevo.

El cliente NO debe saber que estás llamando a una herramienta. Solo actúa con normalidad.
===========================================================
"""


# ===========================
# HERRAMIENTA (function calling)
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
                    "municipio": {"type": "string", "description": "Municipio para envío a domicilio"},
                    "tipo_jaboncito": {"type": "string", "description": "Tipo de jaboncito (ej. 'lavanda', 'vainilla')"},
                    "color_jaboncito": {"type": "string", "description": "Color del jaboncito"},
                    "nombre_bebe": {"type": "string", "description": "Nombre del bebé para personalizar"},
                    "tarjetita": {"type": "string", "description": "Texto o diseño de la tarjetita"},
                    "notas": {"type": "string", "description": "Notas adicionales del cliente"},
                    "precio_unitario": {
                        "type": "number",
                        "description": (
                            "Precio por pieza en MXN, según la Base de Conocimiento. "
                            "Llénalo en cuanto informes el precio o total al cliente, "
                            "para que quede registrado en el pedido oficial."
                        ),
                    },
                    "monto_anticipo": {
                        "type": "number",
                        "description": "Monto del anticipo que el cliente pagó/confirmó, en MXN.",
                    },
                    "metodo_pago": {
                        "type": "string",
                        "description": "Cómo pagó el anticipo, ej. 'transferencia', 'efectivo', 'depósito'.",
                    },
                    "comprobante": {
                        "type": "string",
                        "description": (
                            "Breve descripción de lo que se ve en el comprobante de pago "
                            "que mandó el cliente (banco, referencia, etc.), si aplica."
                        ),
                    },
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
    try:
        datos = json.loads(argumentos_json) if argumentos_json else {}
    except (json.JSONDecodeError, TypeError):
        print("⚠️ No se pudo parsear argumentos de actualizar_pedido:", argumentos_json)
        return []
    campos_modificados = []
    for campo, valor in datos.items():
        if campo in pedido and valor not in (None, ""):
            pedido[campo] = valor
            campos_modificados.append(campo)
    print("📝 Pedido actualizado en RAM:", pedido)
    return campos_modificados


def ejecutar_tool_call(tool_call, sesion, numero, pedido):
    if tool_call.function.name == "actualizar_pedido":
        # 🔧 CORREGIDO (Observación 7): antes esta función también hacía un
        # guardar_borrador_pedido() aquí mismo, y luego procesar_mensaje_en_fondo
        # y crm.sincronizar_pedido lo volvían a guardar cada uno por su lado
        # (triple escritura por mensaje). Ahora esta función solo actualiza
        # la memoria en RAM; el guardado a SQLite ocurre UNA sola vez, en
        # crm.sincronizar_pedido, después de que preguntar_ia() termina.
        campos_modificados = aplicar_actualizacion_pedido(pedido, tool_call.function.arguments)
        return "ok", campos_modificados

    if tool_call.function.name == "mostrar_foto_producto":
        try:
            args = json.loads(tool_call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        clave = args.get("producto")
        imagenes_enviadas = sesion["imagenes_enviadas"]

        if clave in imagenes_enviadas:
            return "ya se le mandó esta foto antes en la conversación, no la repitas", []

        url_imagen = url_imagen_producto(clave)
        if not url_imagen:
            return f"no hay foto disponible para '{clave}', no ofrezcas una foto de esto", []

        nombre_mostrar = CATALOGO_IMAGENES[clave]["nombre_mostrar"]
        enviar_whatsapp_imagen(numero, url_imagen, caption=nombre_mostrar)
        imagenes_enviadas.add(clave)
        return "imagen enviada correctamente", []

    return "función desconocida", []


def preguntar_ia(numero, texto_cliente, imagen_base64=None, imagen_mime=None):
    sesion = obtener_sesion(numero)
    historial = sesion["messages"]
    pedido = sesion["pedido"]
    info_enviada = sesion["info_enviada"]
    pedido_id = sesion.get("pedido_id")

    # ================================================================
    # 🔥 CAMBIO CLAVE: LOGS DE CONTEXTO ANTES DE OPENAI
    # ================================================================
    print("\n" + "=" * 70)
    print("===== CONTEXTO OPENAI =====")
    print(f"Mensajes recuperados: {len(historial)}")
    print(f"Pedido borrador: {json.dumps(pedido, ensure_ascii=False, default=str)}")
    print(f"Historial enviado (últimos 5): {json.dumps(historial[-5:], ensure_ascii=False, default=str)}")
    print(f"Mensaje nuevo: {texto_cliente}")
    print("=" * 70)

    if imagen_base64:
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

    conocimiento_relevante = seleccionar_conocimiento_relevante(texto_cliente, historial_reciente=historial)

    system_prompt = construir_system_prompt(pedido, pedido_id, info_enviada, conocimiento=conocimiento_relevante)
    mensajes_completos = [{"role": "system", "content": system_prompt}] + historial

    if len(mensajes_completos) > MAX_TURNOS_HISTORIAL + 1:
        mensajes_completos = [mensajes_completos[0]] + mensajes_completos[-MAX_TURNOS_HISTORIAL:]
        sesion["messages"] = mensajes_completos[1:]
        historial = sesion["messages"]

    MAX_ITERACIONES_HERRAMIENTAS = 4
    campos_modificados_total = []
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
            mensajes_completos.append(mensaje.model_dump(exclude_none=True))

            for tool_call in mensaje.tool_calls:
                resultado, campos_modificados = ejecutar_tool_call(tool_call, sesion, numero, pedido)
                campos_modificados_total.extend(campos_modificados)
                mensajes_completos.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": resultado,
                })

            mensajes_completos[0]["content"] = construir_system_prompt(
                pedido, pedido_id, info_enviada, conocimiento=conocimiento_relevante
            )
            continue

        texto = mensaje.content or "Disculpa, ¿me repites tu mensaje? 🙂"
        historial.append({"role": "assistant", "content": texto})

        # ================================================================
        # 🔧 CORREGIDO (Observación 4): antes este log revisaba
        # `mensaje.tool_calls` del mensaje FINAL (de texto), que por
        # definición del propio loop siempre está vacío en este punto —
        # por eso SIEMPRE decía "Ninguno" sin importar lo que hubiera
        # cambiado antes. Ahora se usa la lista acumulada de campos que
        # de verdad se modificaron en cualquier vuelta del loop. También
        # se quitó la referencia a `args`, que no existía en este scope.
        # ================================================================
        print("\n" + "=" * 70)
        print("===== DESPUÉS DEL TOOL =====")
        print(f"Campos modificados: {campos_modificados_total if campos_modificados_total else 'Ninguno'}")
        print(f"Borrador actualizado: {json.dumps(pedido, ensure_ascii=False, default=str)}")
        print(f"Persistencia SQLite: OK (guardado inmediato)")
        print("=" * 70)

        detectado = detectar_info_enviada(texto)
        for clave, se_envio in detectado.items():
            if se_envio:
                info_enviada[clave] = True

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


def enviar_whatsapp_documento(numero, url_documento, nombre_archivo, caption=""):
    """
    Envía un documento (PDF) por WhatsApp.
    Esta función se usa desde nota_generator.py para enviar la nota de pedido.
    """
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    data = {
        "messaging_product": "whatsapp",
        "to": numero,
        "type": "document",
        "document": {
            "link": url_documento,
            "caption": caption,
            "filename": nombre_archivo,
        },
    }
    try:
        r = requests.post(GRAPH_URL, headers=headers, json=data, timeout=15)
        if r.status_code >= 400:
            print("⚠️ Error enviando documento por WhatsApp:", r.status_code, r.text)
        else:
            print(f"📄 Documento enviado a {numero}: {nombre_archivo}")
        return r
    except requests.RequestException as e:
        print("⚠️ Excepción enviando documento:", e)
        return None


def descargar_imagen_whatsapp(media_id):
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
# SERVIR ARCHIVOS ESTÁTICOS
# ===========================

@app.route("/imagenes/<path:nombre_archivo>")
def servir_imagen_producto(nombre_archivo):
    return send_from_directory(CARPETA_IMAGENES, nombre_archivo)


@app.route("/catalogo/<path:nombre_archivo>")
def servir_catalogo_pdf(nombre_archivo):
    return send_from_directory(CARPETA_CATALOGO, nombre_archivo)


@app.route("/notas/<path:nombre_archivo>")
def servir_nota_pdf(nombre_archivo):
    """Sirve las notas de pedido generadas en PDF."""
    return send_from_directory(CARPETA_NOTAS, nombre_archivo)


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
    cliente = crm.cargar_cliente(numero)
    crm.guardar_mensaje_cliente(cliente, texto_para_guardar, tipo=tipo)
    return cliente


def procesar_mensaje_no_soportado(numero, tipo):
    cliente = registrar_entrada_cliente(numero, f"[mensaje no soportado: {tipo}]", tipo=tipo)
    respuesta = "Por ahora solo puedo leer mensajes de texto 🙂 ¿me lo escribes con palabras?"
    crm.guardar_respuesta(cliente, respuesta)
    enviar_whatsapp(numero, respuesta)


def procesar_mensaje_en_fondo(numero, texto_cliente, media_id_imagen=None, media_id_audio=None):
    print("=" * 70)
    print(f"🚀 Procesando mensaje de {numero}")
    print(f"💬 Texto recibido: {texto_cliente}")

    imagen_base64 = None
    imagen_mime = None
    tipo_para_crm = "texto"

    if media_id_audio:
        print("🎤 El cliente mandó un audio, descargándolo...")
        contenido_audio, mime_audio = descargar_imagen_whatsapp(media_id_audio)
        if not contenido_audio:
            print("❌ No se pudo descargar el audio del cliente")
            respuesta_fallo = "No pude descargar tu audio 😔 ¿me lo puedes mandar otra vez, o escribirlo?"
            cliente = registrar_entrada_cliente(numero, "(audio no descargable)", tipo="audio")
            crm.guardar_respuesta(cliente, respuesta_fallo)
            enviar_whatsapp(numero, respuesta_fallo)
            return

        print(f"✅ Audio descargado ({len(contenido_audio)} bytes, {mime_audio}), transcribiendo...")
        texto_transcrito = audio_handler.transcribir_audio(client, contenido_audio, mime_audio)
        if not texto_transcrito:
            print("❌ No se pudo transcribir el audio")
            respuesta_fallo = "No logré entender tu audio 😔 ¿me lo puedes escribir, por favor?"
            cliente = registrar_entrada_cliente(numero, "(audio no se pudo transcribir)", tipo="audio")
            crm.guardar_respuesta(cliente, respuesta_fallo)
            enviar_whatsapp(numero, respuesta_fallo)
            return

        print(f"📝 Audio transcrito: {texto_transcrito}")
        texto_cliente = texto_transcrito
        tipo_para_crm = "audio"

    elif media_id_imagen:
        print("🖼️ El cliente mandó una imagen (Vision), descargándola...")
        contenido, mime = descargar_imagen_whatsapp(media_id_imagen)
        if contenido:
            imagen_base64 = base64.b64encode(contenido).decode("utf-8")
            imagen_mime = mime
            tipo_para_crm = "imagen"
            print(f"✅ Imagen descargada ({len(contenido)} bytes, {mime})")
        else:
            print("❌ No se pudo descargar la imagen del cliente, se sigue solo con el texto (si había)")

    texto_para_guardar = texto_cliente or ("(imagen sin texto)" if media_id_imagen else "")
    cliente = registrar_entrada_cliente(numero, texto_para_guardar, tipo=tipo_para_crm)

    # 🆕 Meta 2: si Dalia (humana) ya tomó el control de esta conversación
    # (esto pasa automáticamente en cuanto se confirma el anticipo), el
    # bot NO debe responder nada más. El mensaje ya quedó guardado arriba
    # para que Dalia lo vea, pero no se gasta una llamada a OpenAI ni se
    # manda ninguna respuesta automática.
    modo_atencion = pedido_manager.obtener_modo_atencion(numero)
    if modo_atencion != ModoAtencion.BOT.value:
        print(f"🙅 Bot en silencio para {numero} (modo_atencion={modo_atencion}); mensaje guardado, sin respuesta automática.")
        return

    sesion = obtener_sesion(numero)
    with sesion["lock"]:
        try:
            print("🧠 Consultando OpenAI...")
            respuesta = preguntar_ia(numero, texto_cliente, imagen_base64=imagen_base64, imagen_mime=imagen_mime)
            print("✅ Respuesta generada")
            print(respuesta[:300])
        except Exception as e:
            print("❌ Error llamando a OpenAI:", repr(e))
            respuesta = "Disculpa, tuve un problema técnico. ¿Me puedes repetir tu mensaje? 🙂"

        try:
            crm.guardar_respuesta(cliente, respuesta)

            # 🔧 CORREGIDO (Observación 7): antes aquí había una llamada
            # extra a guardar_borrador_pedido() (comentada como "CAMBIO
            # CLAVE 2"), redundante con la que ya hace ejecutar_tool_call
            # y con la que hace crm.sincronizar_pedido justo abajo — hasta
            # 3 escrituras a SQLite por un solo mensaje. Ahora solo se
            # guarda UNA vez, dentro de crm.sincronizar_pedido.
            crm.sincronizar_pedido(cliente, sesion["pedido"])
            pedido_db = crm.cargar_pedido(cliente)
            sesion["pedido_id"] = pedido_db.id if pedido_db else None
        except Exception as e:
            print("⚠️ Error guardando en CRM (el bot sigue funcionando con RAM):", repr(e))

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
            threading.Thread(
                target=procesar_mensaje_en_fondo,
                args=(numero, caption),
                kwargs={"media_id_imagen": media_id},
                daemon=True,
            ).start()
            return jsonify({"status": "ok"}), 200

        if tipo == "audio":
            # Meta manda "audio" tanto para notas de voz como para audios
            # adjuntos normales; ambos llegan igual, con un media_id.
            media_id = mensaje["audio"]["id"]
            threading.Thread(
                target=procesar_mensaje_en_fondo,
                args=(numero, ""),
                kwargs={"media_id_audio": media_id},
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

        threading.Thread(
            target=procesar_mensaje_en_fondo,
            args=(numero, texto_cliente),
            daemon=True,
        ).start()

    except (KeyError, IndexError, TypeError) as e:
        print("Evento sin mensaje de texto reconocible:", e)

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    puerto = int(os.getenv("PORT", 5000))
    app.run(port=puerto, debug=debug_mode)
