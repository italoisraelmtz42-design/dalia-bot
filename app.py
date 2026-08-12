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

# 🔧 CORREGIDO: load_dotenv() debe ejecutarse ANTES de importar crm
# (que a su vez importa database.py). database.py lee SQLITE_DB_PATH con
# os.getenv() en cuanto se importa el módulo -- si el .env todavía no se
# había cargado en ese momento, SIEMPRE se quedaba con el valor por
# defecto ("dalia_bot.db", local) sin importar lo que dijera el .env.
# En Render esto significaba que el disco persistente (/var/data) nunca
# se usaba de verdad, aunque SQLITE_DB_PATH estuviera bien configurado.
load_dotenv()

import crm
import pedido_manager
import audio_handler
from constantes import ModoAtencion

# ===========================
# CONFIGURACIÓN
# ===========================

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

# Número personal de Dalia (con lada, sin signos: ej. "5218114905653"),
# al que se le manda una notificación cada vez que se confirma un
# anticipo. Si no está configurado, simplemente no se manda la
# notificación (no rompe nada del resto del bot).
DALIA_WHATSAPP_NUMERO = os.getenv("DALIA_WHATSAPP_NUMERO", "")

# Números autorizados para usar el código de reactivación/reset (🧸☠️🧸).
# Lista separada por comas en .env, ej: "5218112345678,5218187654321".
# Por seguridad, Dalia (DALIA_WHATSAPP_NUMERO) siempre queda autorizada
# aunque no se agregue explícitamente. Si esta lista queda vacía y
# DALIA_WHATSAPP_NUMERO tampoco está configurado, el reset queda
# deshabilitado por completo (nadie puede activarlo) en vez de quedar
# abierto a cualquiera, que era el comportamiento inseguro anterior.
_RESET_NUMEROS_ENV = os.getenv("RESET_NUMEROS_AUTORIZADOS", "")
NUMEROS_AUTORIZADOS_RESET = {
    n.strip() for n in _RESET_NUMEROS_ENV.split(",") if n.strip()
}
if DALIA_WHATSAPP_NUMERO:
    NUMEROS_AUTORIZADOS_RESET.add(DALIA_WHATSAPP_NUMERO)

# 🔧 Datos bancarios reales: antes vivían escritos directo en el código
# fuente (visibles para quien tenga acceso al repo, aunque sea privado).
# Ahora se leen del .env, igual que las API keys y tokens.
DATOS_BANCARIOS_TARJETA = os.getenv("DATOS_BANCARIOS_TARJETA", "")
DATOS_BANCARIOS_CLABE = os.getenv("DATOS_BANCARIOS_CLABE", "")
DATOS_BANCARIOS_BANCO = os.getenv("DATOS_BANCARIOS_BANCO", "")
if not (DATOS_BANCARIOS_TARJETA and DATOS_BANCARIOS_CLABE):
    print("⚠️ DATOS_BANCARIOS_TARJETA / DATOS_BANCARIOS_CLABE no configurados en .env "
          "— la detección de 'ya se enviaron los datos de pago' y el filtro de "
          "envío prematuro no van a funcionar correctamente hasta llenarlos.")

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


def _clave_sin_acentos(s: str) -> str:
    """Helper local (definido antes que _sin_acentos más abajo en el
    archivo, porque cargar_catalogo_imagenes() se ejecuta al importar el
    módulo, antes de llegar a esa definición). Quita acentos para que las
    claves de imagen sean consistentes con normalizar_producto_clave --
    antes una imagen con tilde en el nombre generaba una clave con acento
    (ej. 'oración_con_velita') que era fácil de no emparejar bien."""
    if not s:
        return ""
    rep = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
    return s.translate(rep)


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
            clave = _clave_sin_acentos(archivo.stem.strip().lower().replace(" ", "_"))
            nombre_mostrar = archivo.stem.replace("_", " ").replace("-", " ").strip().capitalize()
            if "#u00" in clave or "#U00" in archivo.stem:
                print(f"🚨 Nombre de archivo con codificación rota, revisa el nombre real: {archivo.name}")
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


# 🔧 Imágenes que se mandan de inmediato por palabra clave del CLIENTE,
# sin depender de que el modelo se acuerde de llamar mostrar_foto_producto
# (bug real detectado en pruebas: el bot no mandaba la foto de colores
# aunque el cliente preguntara por colores). "colores_disponibles" es la
# más importante -- evita dudas sobre qué colores existen. El resto son
# productos de UNA sola variante (no necesitan preguntar nada antes, a
# diferencia de los ositos, que sí tienen varias presentaciones y se
# quedan con el flujo normal de mostrar_foto_producto para no adelantar
# la foto equivocada).
PALABRAS_CLAVE_IMAGEN_AUTOMATICA = {
    "colores_disponibles": ("color", "colores"),
    "velas_de_toalla_cyg": ("velita", "velitas", "vela de toalla", "velas de toalla", "vela grande", "vela chica"),
    "elefante_de_toalla": ("elefante", "elefantito", "elefantes"),
    "jirafa_de_toalla": ("jirafa", "jirafas"),
    "buho_con_virrete_de_toalla": ("birrete", "virrete"),
    "buho_de_toalla": ("buho", "buhos"),
    "caballito_de_toalla": ("caballo", "caballito", "caballos"),
    "conejito_de_toalla": ("conejo", "conejito", "conejos"),
    "leoncito_de_toalla": ("leon", "leoncito", "leones"),
    "mariposa_de_toalla": ("mariposa", "mariposas"),
    "perrito_de_toalla": ("perro", "perrito", "perros"),
    "unicornio_de_toalla": ("unicornio", "unicornios"),
}


def detectar_imagenes_automaticas(texto_cliente: str) -> list:
    """Palabras clave del mensaje del CLIENTE -> claves de imagen a
    mandar de inmediato. No reemplaza mostrar_foto_producto (el modelo
    lo sigue usando para productos con variantes, como los ositos);
    esto es un respaldo determinístico solo para colores y productos de
    una sola presentación, donde no hay nada que preguntar antes."""
    if not texto_cliente:
        return []
    texto_norm = normalizar_producto_clave(texto_cliente)
    claves = []
    for clave_imagen, palabras in PALABRAS_CLAVE_IMAGEN_AUTOMATICA.items():
        if clave_imagen not in CATALOGO_IMAGENES:
            continue
        if any(normalizar_producto_clave(p) in texto_norm for p in palabras):
            claves.append(clave_imagen)
    # Caso especial: "buho con birrete" no debe mandar TAMBIÉN la foto
    # del buho sencillo -- si se detectó la variante con birrete, se
    # quita la genérica de la lista.
    if "buho_con_virrete_de_toalla" in claves and "buho_de_toalla" in claves:
        claves.remove("buho_de_toalla")
    return claves


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

# 🔧 CORREGIDO: esta lista tenía nombres de archivo VIEJOS (sin
# subcarpeta, con la numeración de antes de reorganizar /conocimiento en
# carpetas como "Politicas generales/", "Ventas/", etc.). Como
# CONOCIMIENTO_POR_ARCHIVO guarda las claves con la ruta relativa
# completa (ver cargar_conocimiento: CONOCIMIENTO_POR_ARCHIVO[str(rel_path)]),
# NINGUNO de esos nombres viejos hacía match — esta lista de "siempre
# incluidos" llevaba tiempo sin incluir NADA, en silencio. Eso significaba
# que archivos críticos como las reglas del anticipo solo se mandaban al
# modelo si el mensaje del cliente coincidía por palabras clave, y si no,
# el modelo podía inventar cifras (como pasó: dijo que el anticipo era de
# $200, un número que no existe en ningún archivo — la Base de
# Conocimiento solo dice "desde $50 pesos").
ARCHIVOS_CONOCIMIENTO_SIEMPRE = {
    "Politicas generales/00_PRIORIDAD_MAXIMA.txt",
    "Productos/00_INDICE_PRECIOS.txt",
    "Politicas generales/Anticipos.txt",
    "Politicas generales/Colores disponibles.txt",
    "Politicas generales/Datos bancarios  para pagos, transferencias y anticipos.txt",
    "Politicas generales/Entregas y envíos.txt",
    "Politicas generales/Pedidos urgentes.txt",
    "Politicas generales/Precios de mayoreo.txt",
    "Politicas generales/REGLAS IRROMPIBLES DEL NEGOCIO.txt",
    "Politicas generales/Resumen del pedido.txt",
    "Preguntas y respuestas/033_Reglas_Conversacion.txt",
    "Preguntas y respuestas/045_Guia_Tono_y_Personalidad.txt",
    "Preguntas y respuestas/050_Saludos_Humanos.txt",
    "Trato al cliente/035_Variantes_De_Producto.txt",
    "Ventas/ERRORES_PROHIBIDOS.txt",
    "Ventas/MEMORIA_CONVERSACIONAL.txt",
    "Ventas/Proceso de venta.txt",
}

# 🆕 Validación al arrancar: si algún nombre de esta lista no existe de
# verdad en la carpeta /conocimiento, se avisa FUERTE en los logs, en vez
# de fallar en silencio como pasó esta vez. Si renombras o mueves algún
# archivo de conocimiento en el futuro, esto te va a avisar de inmediato.
_faltantes_siempre = sorted(ARCHIVOS_CONOCIMIENTO_SIEMPRE - set(CONOCIMIENTO_POR_ARCHIVO.keys()))
if _faltantes_siempre:
    print("\n" + "🚨" * 20)
    print("ADVERTENCIA: estos archivos de ARCHIVOS_CONOCIMIENTO_SIEMPRE")
    print("NO se encontraron en /conocimiento (revisa el nombre exacto):")
    for _f in _faltantes_siempre:
        print(f"   ❌ {_f}")
    print("🚨" * 20 + "\n")
else:
    print(f"✅ Los {len(ARCHIVOS_CONOCIMIENTO_SIEMPRE)} archivos 'siempre incluidos' existen correctamente\n")


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

    # Prioridad 2: TODO el catálogo de productos siempre disponible
    # (evita precios inventados/mezclados cuando el modelo no ve el archivo)
    for nombre in CONOCIMIENTO_POR_ARCHIVO:
        if nombre.startswith("Productos/") or nombre.startswith("Productos\\"):
            seleccionados.add(nombre)

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



# --- Validación de dominio (anti-alucinación estructural) ---
COLORES_VALIDOS = {
    "turquesa", "azul rey", "azulrey", "celeste", "blanco", "hueso",
    "fiusha", "fucsia", "rosa palo", "rosapalo", "rosa pastel", "rosapastel",
    "café claro", "cafe claro", "caféclaro", "cafeclaro", "amarillo",
    # excepción moño/listón
    "rojo", "dorado",
}
# Colores extra solo para productos especiales (afelpada / peluche)
COLORES_ESPECIALES = {
    "morado", "vino tinto", "vinotinto", "beige", "rojo",
}

def _normalizar_color(valor: str) -> str:
    if not valor:
        return ""
    v = " ".join(str(valor).lower().strip().split())
    v = v.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    return v

def color_es_valido(valor: str, producto: str = "") -> bool:
    if not valor:
        return True
    v = _normalizar_color(valor)
    if v in {_normalizar_color(c) for c in COLORES_VALIDOS}:
        return True
    prod = (producto or "").lower()
    if any(x in prod for x in ("afelpada", "peluche")):
        if v in {_normalizar_color(c) for c in COLORES_ESPECIALES}:
            return True
    return False



# --- Catálogo de precios oficiales (fuente de verdad en código) ---
# GPT NO escribe precio_unitario; Python lo resuelve con esta tabla.
PRECIOS_CATALOGO = {
    # clave normalizada (sin acentos, lower) -> precio base
    "osito con jaboncito": 12.0,
    "osito jaboncito": 12.0,
    "osito clasico": 12.0,
    "osito clasico con jabon": 12.0,
    "osito sencillo": 12.0,
    "osito sin jabon": 12.0,
    "osito sin jaboncito": 12.0,
    "osito doble pie": 14.0,
    "osito doble piecito": 14.0,
    "osito inicial chica": 15.0,
    "osito con inicial chica": 15.0,
    "osito doble inicial": 19.0,
    "osito doble inicial chica": 19.0,
    "osito inicial grande": 22.0,
    "osito con inicial grande": 22.0,
    "osito peluche": 18.0,
    "osito de peluche": 18.0,
    "osito afelpada": 18.0,
    "osito toalla afelpada": 18.0,
    "osito afelpado": 18.0,
    "kit osito oracion": 21.0,
    "kit osito oracion velita": 21.0,
    "elefante": 14.0,
    "elefante de toalla": 14.0,
    "elefantito": 14.0,
    "jirafa": 16.0,
    "jirafa de toalla": 16.0,
    "leon": 14.0,
    "leon de toalla": 14.0,
    "leoncito": 14.0,
    "caballo": 15.0,
    "caballito": 15.0,
    "conejo": 13.5,
    "conejito": 13.5,
    "perrito": 13.0,
    "perro de toalla": 13.0,
    "buho": 14.0,
    "buho de toalla": 14.0,
    "buho birrete": 14.0,
    "buho con birrete": 14.0,
    "unicornio": 14.0,
    "mariposa": 14.5,
    "vela chica": 12.0,
    "vela de toalla chica": 12.0,
    "velita chica": 12.0,
    "vela grande": 16.5,
    "vela de toalla grande": 16.5,
    "velita grande": 16.5,
    "abanico": 23.0,
    "abanico de mano": 23.0,
    "abanico de madera": 23.0,
    "domino": 35.0,
    "dominó": 35.0,
    "encendedor": 10.0,
    "espejo redondo": 14.0,
    "espejito": 14.0,
    "destapador": 15.5,
    "oracion con decenario": 15.0,
    "oracion con velita": 10.0,
}

def _sin_acentos(s: str) -> str:
    if not s:
        return ""
    rep = str.maketrans("áéíóúüñÁÉÍÓÚÜÑ", "aeiouunAEIOUUN")
    return s.translate(rep)

def normalizar_producto_clave(nombre: str) -> str:
    n = _sin_acentos(nombre or "").lower()
    n = " ".join(n.split())
    return n

# Palabras sin valor para distinguir un producto de otro -- se ignoran al
# comparar (permite que "osito DE TOALLA con jaboncito" empareje con la
# clave de catálogo "osito con jaboncito" aunque el orden/las palabras de
# relleno cambien).
_STOPWORDS_PRODUCTO = {"de", "con", "sin", "para", "toalla", "un", "una", "el", "la", "los", "las"}


def _palabras_clave(texto: str) -> set:
    return {w for w in normalizar_producto_clave(texto).split() if w not in _STOPWORDS_PRODUCTO}


def resolver_precio(nombre_producto: str, cantidad: int = 1) -> float | None:
    """Devuelve precio oficial o None si no hay match razonable.

    🔧 CORREGIDO (bug real detectado en pruebas): antes esto solo hacía un
    match de substring EXACTO ("osito con jaboncito" in "osito de toalla
    con jaboncito" -> False, por el orden/palabras de relleno), y si no
    encontraba nada, se quedaba en silencio sin precio -- el producto
    terminaba contando como $0 en el total sin que nadie lo notara.
    Ahora compara por CONJUNTO DE PALABRAS CLAVE (ignorando relleno como
    "de", "con", "toalla"), y si hay más de un candidato posible, elige el
    de MAYOR traslape para evitar falsos positivos entre productos
    parecidos (ej. "osito con jaboncito" vs "osito doble inicial").
    """
    if not nombre_producto:
        return None
    clave = normalizar_producto_clave(nombre_producto)
    if clave in PRECIOS_CATALOGO:
        precio = PRECIOS_CATALOGO[clave]
    else:
        precio = None
        palabras_pedido = _palabras_clave(nombre_producto)
        mejor_score = 0
        for k, v in PRECIOS_CATALOGO.items():
            palabras_catalogo = _palabras_clave(k)
            if not palabras_catalogo:
                continue
            # Todas las palabras clave del catálogo deben estar presentes
            # en lo que pidió el cliente (evita medios-matches raros).
            if palabras_catalogo.issubset(palabras_pedido):
                score = len(palabras_catalogo)
                if score > mejor_score:
                    mejor_score = score
                    precio = v
        if precio is None:
            # Último recurso: substring literal (comportamiento anterior,
            # cubre casos que el match por palabras no contempla).
            for k, v in PRECIOS_CATALOGO.items():
                if k in clave or clave in k:
                    precio = v
                    break
    if precio is None:
        print(f"🚨 PRECIO NO ENCONTRADO para producto '{nombre_producto}' -- "
              f"revisa si falta agregarlo a PRECIOS_CATALOGO en app.py.")
        return None
    # Mayoreos simples
    c = normalizar_producto_clave(nombre_producto)
    cant = int(cantidad or 1)
    if "abanico" in c and cant >= 100:
        return 21.0
    if "domino" in c and cant >= 50:
        return 30.0
    if "peluche" in c and cant >= 50:
        return 16.0
    if "encendedor" in c and "bolsa" in c:
        return 11.0
    if "destapador" in c and "bolsa" in c:
        return 16.5
    return float(precio)


def aplicar_precio_oficial(item: dict) -> dict:
    """Sobrescribe precio_unitario con el del catálogo. Nunca confía en GPT.

    🔧 CORREGIDO (bug real detectado en pruebas): antes, si resolver_precio
    no encontraba nada, esta función no hacía NADA -- el item se quedaba
    con precio_unitario=None y calcular_total lo trataba como $0 sin
    ninguna señal visible. Ahora, si no hay match, se marca explícitamente
    con "_precio_pendiente": True para que calcular_total y el prompt del
    modelo puedan detectarlo y avisar en vez de dar un total incompleto
    con confianza.
    """
    if not isinstance(item, dict):
        return item
    prod = item.get("producto") or ""
    cant = item.get("cantidad") or 1
    oficial = resolver_precio(prod, cant)
    if oficial is not None:
        item["precio_unitario"] = oficial
        item["_precio_pendiente"] = False
    else:
        item["_precio_pendiente"] = True
    return item


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
        "precio_unitario": None,
        "monto_anticipo": None,
        "metodo_pago": None,
        "comprobante": None,
        "costo_envio": None,
        # Soporte multi-producto (elefantes + velitas, etc.)
        "items": [],
        "es_urgente": None,
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
                # 🔧 CORREGIDO: antes se usaba pedido_previo directo, que
                # podía venir de un borrador guardado con un esquema viejo
                # (de antes de que existieran campos como
                # anticipo_confirmado, precio_unitario, monto_anticipo,
                # etc.). Si a ese diccionario le faltaban esas claves,
                # aplicar_actualizacion_pedido las descartaba en silencio
                # después (su chequeo "if campo in pedido" las trataba
                # como inexistentes). Ahora siempre se parte de un
                # pedido_vacio() con TODOS los campos actuales, y encima
                # se pisan con los valores que sí traiga pedido_previo.
                "pedido": {**pedido_vacio(), **(pedido_previo or {})},
                "pedido_id": pedido_id,
                "info_enviada": info_enviada_vacia(),
                "imagenes_enviadas": set(),
                "lock": threading.Lock(),
            }
        
        # 🔥 SIEMPRE recargamos el borrador desde SQLite al inicio de cada mensaje
        # 🔧 CORREGIDO: mismo problema que arriba pero en cada mensaje, no
        # solo al hidratar la sesión por primera vez -- este era el punto
        # que de verdad causaba la pérdida de datos en producción, porque
        # se ejecuta SIEMPRE, incluso en sesiones que ya llevaban rato
        # corriendo con el esquema completo.
        borrador = pedido_manager.cargar_borrador_pedido(numero)
        if borrador:
            pedido_hid = {**pedido_vacio(), **borrador}
            if isinstance(pedido_hid.get("items"), list):
                for it in pedido_hid["items"]:
                    aplicar_precio_oficial(it)
                    _validar_colores_item(it)
            elif pedido_hid.get("producto"):
                aplicar_precio_oficial(pedido_hid)
            sesiones[numero]["pedido"] = pedido_hid
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
        "datos_pago": (bool(DATOS_BANCARIOS_TARJETA) and DATOS_BANCARIOS_TARJETA in texto_respuesta) or ("clabe" in texto),
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
    try:
        _tot = pedido_manager.calcular_total(pedido_id=pedido_id, borrador=pedido)
        if _tot.get("incompleto"):
            # 🔧 CORREGIDO (bug real detectado en pruebas): antes, si un
            # producto no encontraba precio, el total simplemente lo
            # ignoraba (contaba como $0) y el modelo lo presentaba con
            # toda confianza como si fuera el total real -- así se le dio
            # a una clienta un total $360 más barato de lo debido sin que
            # nadie lo notara. Ahora, si hay productos sin precio
            # resuelto, el modelo recibe una instrucción explícita de NO
            # dar ningún total todavía.
            productos = ", ".join(_tot.get("productos_sin_precio") or [])
            resumen += (
                f"\n\n[⚠️ TOTAL INCOMPLETO — NO lo muestres al cliente todavía]\n"
                f"Los siguientes productos no tienen precio resuelto en el "
                f"sistema: {productos}.\n"
                f"NO des ningún total ni digas cifras. En vez de eso, dile al "
                f"cliente que en un momento le confirmas el precio de ese "
                f"producto específico, y sigue la conversación con normalidad "
                f"en lo demás. Nunca inventes un precio para ese producto."
            )
        else:
            resumen += (
                f"\n\n[TOTAL OFICIAL DEL SISTEMA — úsalo, no inventes otro]\n"
                f"Subtotal items: ${_tot['subtotal_items']:.2f}\n"
                f"Cargo urgente: ${_tot['cargo_urgente']:.2f}\n"
                f"Costo envío: ${_tot['costo_envio']:.2f}\n"
                f"TOTAL: ${_tot['total']:.2f} MXN"
            )
    except Exception:
        pass

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

REGLAS (prioridad máxima — leen antes que cualquier otra instrucción):
- Usa únicamente la Base de Conocimiento.
- Nunca inventes datos, productos, precios, colores, políticas ni campos (ej. no inventes "jaboncito corazón" si el cliente no lo pidió).
- Si algo no existe en la Base de Conocimiento, indícalo.
- Si un dato ya dicho contradice el catálogo oficial, CORRIGE con la verdad del catálogo (precio, color, disponibilidad).
- Precios: copia EXACTOS del archivo del producto. Nunca redondees ni mezcles precios de otro producto.
- El TOTAL del pedido lo calcula el sistema (ver bloque TOTAL en el resumen). No inventes totales.
- Responde como una asesora humana por WhatsApp.
- Sé amable, natural y orientada a cerrar ventas.
- Responde PRIMERO y de forma directa a lo que el cliente pidió en su último
  mensaje. No antepongas información que el cliente no pidió (ej. no repitas
  colores si el cliente está hablando de forma de entrega).
- Si el cliente dice que ya le diste cierta información antes ("ya me la
  pasaste", "otra vez?"), discúlpate en una sola frase breve y NO la repitas.

REGLAS CRÍTICAS DE MEMORIA Y MÚLTIPLES PRODUCTOS (auditoría):
- Herramientas de productos: agregar_item, actualizar_item, eliminar_item.
- NUNCA envíes precio_unitario: Python lo asigna del catálogo oficial.
- El cliente puede pedir VARIOS productos distintos en el mismo pedido.
- NUNCA olvides ni borres un producto ya confirmado al agregar otro.
- Para quitar un producto usa eliminar_item (ej. si dice "olvida los elefantes").
- Cuando generes un resumen o digas el total, INCLUYE SIEMPRE TODOS los
  productos del pedido. El TOTAL oficial lo calcula el sistema (bloque TOTAL);
  no inventes cifras. Para productos usa agregar_item / actualizar_item /
  eliminar_item (nunca reescribas la lista completa a mano).
- Si el cliente pregunta por "el pedido anterior" o "el total de todo",
  responde con el total combinado. Nunca digas que no tienes registrado
  un producto que el cliente ya confirmó en esta conversación.
- NUNCA asumas tamaño (chica/grande) si el cliente no lo dijo.
- COLORES: el color GRIS NO existe. Si lo piden, dilo claro y ofrece solo
  los colores de la lista oficial. Nunca aceptes gris ni digas que está
  disponible. Si por error se aceptó antes, corrígelo de inmediato.
- Si el catálogo tiene el producto (ej. elefante de toalla), NUNCA digas
  que no lo manejan.

REGLA FIJA DEL ANTICIPO (esta regla NO depende de la Base de Conocimiento,
así que aplícala siempre, incluso si no ves el archivo de anticipos en este
mensaje): el anticipo para CUALQUIER pedido es "desde $50 MXN", nunca un
monto distinto, y nunca varía según el tamaño o total del pedido. Jamás
menciones una cifra distinta de $50 como el anticipo requerido.

REGLAS DE FECHAS Y PEDIDOS URGENTES (usa SIEMPRE la fecha de hoy de arriba,
{dia_semana} {fecha}, para todo cálculo; nunca calcules fechas por tu cuenta):

- El tiempo normal de elaboración de un pedido es de 4 a 6 días hábiles.
- La fecha de entrega MÁS PRÓXIMA posible para un pedido NORMAL (no urgente)
  es el {dia_semana_minima} {fecha_minima.strftime('%d/%m/%Y')}. Un pedido
  normal podría tardar hasta el {fecha_maxima.strftime('%d/%m/%Y')}.
- 🚨 {fecha_minima.strftime('%d/%m/%Y')} es SOLO el límite mínimo para
  validar si un pedido es urgente o no -- NUNCA la uses como la fecha de
  entrega del pedido a menos que el cliente la haya pedido explícitamente.
  SIEMPRE pregunta "¿para cuándo lo necesitas?" antes de llenar
  fecha_evento/fecha_entrega. Si todavía no lo has preguntado, NO llenes
  ese campo, aunque el resto del pedido ya esté completo.
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

IMPORTANTE — GUARDA TODO LO QUE LE DIGAS AL CLIENTE, NO SOLO LO QUE ÉL DIGA:
esto aplica a cualquier dato concreto del pedido que TÚ le informes al
cliente (no solo lo que el cliente confirma) — precio por pieza, costo de
envío, fecha de entrega, municipio o dirección, tipo de entrega. En el
MISMO turno en que le des cualquiera de estos datos, llama a
actualizar_pedido con ese valor. Si solo se lo dices en el mensaje pero no
llamas la función, el dato se pierde y el pedido oficial queda incompleto
o en $0, aunque el cliente sí haya visto la información correcta.

Ejemplos: si le cotizas el envío a domicilio ("el envío a Guadalupe cuesta
$90"), llama a actualizar_pedido con costo_envio=90 en ese mismo turno. Si
le confirmas la fecha de entrega ("te lo tengo listo el jueves
13/08/2026"), llama a actualizar_pedido con fecha_evento="13/08/2026" en
ese mismo turno. Lo mismo aplica para tipo_entrega, direccion, municipio y
precio_unitario.

🚫 NO COMPARTAS LOS DATOS BANCARIOS DEL ANTICIPO hasta que el pedido tenga
guardados (vía actualizar_pedido, no solo mencionados en el chat): producto,
cantidad, los colores/variantes que apliquen, fecha_evento, tipo_entrega, y
si la entrega es a domicilio, también direccion/municipio y costo_envio. Si
el cliente pide el anticipo antes de que tengas todo esto, dile que primero
necesitas confirmar esos datos y pregúntale lo que falte — no le mandes los
datos bancarios todavía.

IMPORTANTE — PRECIO: en el momento en que le informes al cliente el precio
por pieza o el total del pedido (usando el precio de la Base de Conocimiento),
el sistema asigna el precio solo; tú llama agregar_item o actualizar_item sin precio_unitario.
Si no guardas el precio aquí, el pedido oficial queda registrado con precio
$0, aunque se lo hayas dicho al cliente.

{seccion_fotos_producto(catalogo_imagenes=CATALOGO_IMAGENES)}

{seccion_catalogo_pdf()}

RECEPCIÓN DE IMÁGENES DEL CLIENTE (Vision):
Cuando el cliente te mande una imagen, clasifícala primero en una de estas
categorías y actúa según corresponda:

1. COMPROBANTE DE PAGO (pantalla de banco, ticket, captura de transferencia
   o depósito) CON MONTO LEGIBLE: llama a actualizar_pedido con
   anticipo_confirmado=true, monto_anticipo (el monto que leas en la
   imagen), metodo_pago (ej. "transferencia" o "depósito", según lo que
   veas), y comprobante (una descripción breve de lo que se ve, ej. banco
   y referencia si se alcanzan a leer).

   IMPORTANTE: NO redactes ningún mensaje de confirmación ni de
   despedida — el sistema le manda al cliente un mensaje fijo
   automáticamente en cuanto detecta anticipo_confirmado=true, tú no
   tienes que escribir nada más para este caso. Solo llama a la función
   con los datos correctos.

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
Hay una diferencia importante entre dos cosas que NO debes confundir:

- Que el cliente confirme que el RESUMEN DEL PEDIDO está correcto (dice
  "sí", "confirmo", "está bien" a un resumen que TÚ le mostraste antes) ->
  esto solo significa que los DATOS del pedido son correctos. NO llames a
  actualizar_pedido con anticipo_confirmado=true por esto — el cliente
  todavía no ha pagado nada, solo aprobó los datos.

- Que el cliente confirme que YA PAGÓ el anticipo -> esto es lo único que
  debe activar anticipo_confirmado=true, y solo debe pasar cuando:
    a) el cliente manda una imagen de comprobante de pago con monto
       legible (ver sección de Vision arriba), o
    b) el cliente te dice explícitamente por texto que ya pagó/transfirió,
       mencionando un monto específico (ej. "ya te transferí $150", "ya
       deposité 100 pesos") — en ese caso llama a actualizar_pedido con
       anticipo_confirmado=true, monto_anticipo (el monto que mencionó), y
       metodo_pago="confirmado por texto".

Si el cliente dice "sí" a cualquier otra cosa que NO sea confirmar que ya
pagó (el resumen del pedido, si quiere que le expliques el anticipo, etc.),
NO actives anticipo_confirmado — sigue la conversación con normalidad.

Cuando SÍ se confirme el pago (por imagen o por texto con monto), llama a
actualizar_pedido de inmediato, sin que el cliente tenga que pedírtelo de
nuevo. El cliente no debe saber que estás llamando a una herramienta.
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
                "Actualiza datos del PEDIDO a nivel general (entrega, fecha, urgente, anticipo, envío). "
                "NO uses esta función para agregar o quitar productos; usa agregar_item / actualizar_item / eliminar_item."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evento": {"type": "string"},
                    "fecha_evento": {"type": "string", "description": "Fecha de entrega acordada"},
                    "tipo_entrega": {
                        "type": "string",
                        "description": "Uno de: local, punto_de_entrega, domicilio",
                    },
                    "direccion": {"type": "string"},
                    "municipio": {"type": "string"},
                    "costo_envio": {"type": "number"},
                    "es_urgente": {"type": "boolean"},
                    "anticipo_confirmado": {
                        "type": "boolean",
                        "description": "True solo si el cliente ya pagó el anticipo (comprobante o texto con monto).",
                    },
                    "monto_anticipo": {"type": "number"},
                    "metodo_pago": {"type": "string"},
                    "comprobante": {"type": "string"},
                    "notas": {"type": "string"},
                    "datos_tarjeta": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agregar_item",
            "description": (
                "Agrega un producto al pedido o suma cantidad si ya existe el mismo producto. "
                "NO envíes precio_unitario: el sistema lo asigna solo. "
                "No borra otros productos del pedido."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "producto": {"type": "string", "description": "Nombre del producto, ej. 'elefante de toalla'"},
                    "cantidad": {"type": "integer"},
                    "color_toalla": {"type": "string"},
                    "color_mono": {"type": "string"},
                    "color_velita": {"type": "string"},
                    "tipo_jaboncito": {"type": "string"},
                    "color_jaboncito": {"type": "string"},
                    "nombre_bebe": {"type": "string"},
                    "tarjetita": {"type": "string"},
                },
                "required": ["producto", "cantidad"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "actualizar_item",
            "description": (
                "Modifica un producto ya existente en el pedido (cantidad, colores, etc.). "
                "Identifica por nombre de producto. No afecta a los demás items. "
                "NO envíes precio_unitario."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "producto": {"type": "string", "description": "Producto a modificar"},
                    "cantidad": {"type": "integer"},
                    "color_toalla": {"type": "string"},
                    "color_mono": {"type": "string"},
                    "color_velita": {"type": "string"},
                    "tipo_jaboncito": {"type": "string"},
                    "color_jaboncito": {"type": "string"},
                    "nombre_bebe": {"type": "string"},
                    "tarjetita": {"type": "string"},
                },
                "required": ["producto"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "eliminar_item",
            "description": (
                "Quita un producto del pedido cuando el cliente dice que ya no lo quiere "
                "(ej. 'olvida los elefantes'). No afecta a los demás productos."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "producto": {"type": "string", "description": "Nombre del producto a eliminar"},
                },
                "required": ["producto"],
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


def _normalizar_nombre_producto(nombre):
    if not nombre:
        return ""
    return normalizar_producto_clave(nombre)


def _buscar_item(pedido, nombre_producto):
    clave = _normalizar_nombre_producto(nombre_producto)
    items = pedido.get("items") if isinstance(pedido.get("items"), list) else []
    for it in items:
        if _normalizar_nombre_producto(it.get("producto")) == clave:
            return it
        # match parcial
        n = _normalizar_nombre_producto(it.get("producto"))
        if clave and (clave in n or n in clave):
            return it
    return None


def aplicar_actualizacion_pedido(pedido, argumentos_json):
    """Solo campos a nivel PEDIDO (entrega, fecha, anticipo...). No toca items."""
    try:
        datos = json.loads(argumentos_json) if argumentos_json else {}
    except (json.JSONDecodeError, TypeError):
        print("⚠️ No se pudo parsear argumentos de actualizar_pedido:", argumentos_json)
        return []

    if "items" not in pedido or not isinstance(pedido.get("items"), list):
        pedido["items"] = []

    campos_pedido = {
        "evento", "fecha_evento", "tipo_entrega", "direccion", "municipio",
        "anticipo_confirmado", "monto_anticipo", "metodo_pago", "comprobante",
        "costo_envio", "es_urgente", "urgente", "notas", "datos_tarjeta",
    }
    campos_modificados = []
    for campo in campos_pedido:
        if campo in datos and datos[campo] not in (None, ""):
            pedido[campo] = datos[campo]
            campos_modificados.append(campo)

    if pedido.get("urgente") and not pedido.get("es_urgente"):
        pedido["es_urgente"] = True
        if "es_urgente" not in campos_modificados:
            campos_modificados.append("es_urgente")

    print("📝 Pedido (meta) actualizado:", {k: pedido.get(k) for k in campos_modificados})
    return campos_modificados


def _validar_colores_item(item):
    campos_color = ("color_toalla", "color_mono", "color_velita", "color_jaboncito")
    pref = item.get("producto") or ""
    for campo in campos_color:
        if item.get(campo) and not color_es_valido(item[campo], pref):
            print(f"🚫 Color inválido rechazado: {campo}={item[campo]}")
            item[campo] = None
    return item


def agregar_item_pedido(pedido, argumentos_json):
    try:
        datos = json.loads(argumentos_json) if argumentos_json else {}
    except (json.JSONDecodeError, TypeError):
        return []
    producto = (datos.get("producto") or "").strip()
    if not producto:
        return []
    if "items" not in pedido or not isinstance(pedido.get("items"), list):
        pedido["items"] = []

    existing = _buscar_item(pedido, producto)
    if existing:
        # sumar cantidad si viene, actualizar colores
        if datos.get("cantidad"):
            existing["cantidad"] = int(datos["cantidad"])
        for k in ("color_toalla", "color_mono", "color_velita", "tipo_jaboncito",
                  "color_jaboncito", "nombre_bebe", "tarjetita"):
            if datos.get(k) not in (None, ""):
                existing[k] = datos[k]
        aplicar_precio_oficial(existing)
        _validar_colores_item(existing)
        pedido["producto"] = existing.get("producto")
        pedido["cantidad"] = existing.get("cantidad")
        pedido["precio_unitario"] = existing.get("precio_unitario")
        print("📝 Item actualizado (vía agregar):", existing)
        return ["items"]

    item = {
        "producto": producto,
        "cantidad": int(datos.get("cantidad") or 1),
    }
    for k in ("color_toalla", "color_mono", "color_velita", "tipo_jaboncito",
              "color_jaboncito", "nombre_bebe", "tarjetita"):
        if datos.get(k) not in (None, ""):
            item[k] = datos[k]
    # IGNORAR cualquier precio que mande el modelo
    aplicar_precio_oficial(item)
    _validar_colores_item(item)
    pedido["items"].append(item)
    pedido["producto"] = item["producto"]
    pedido["cantidad"] = item["cantidad"]
    pedido["precio_unitario"] = item.get("precio_unitario")
    print("📝 Item agregado:", item)
    return ["items"]


def actualizar_item_pedido(pedido, argumentos_json):
    try:
        datos = json.loads(argumentos_json) if argumentos_json else {}
    except (json.JSONDecodeError, TypeError):
        return []
    producto = (datos.get("producto") or "").strip()
    if not producto:
        return []
    if "items" not in pedido or not isinstance(pedido.get("items"), list):
        pedido["items"] = []
    existing = _buscar_item(pedido, producto)
    if not existing:
        # si no existe, comportarse como agregar
        return agregar_item_pedido(pedido, argumentos_json)
    if datos.get("cantidad") not in (None, ""):
        existing["cantidad"] = int(datos["cantidad"])
    for k in ("color_toalla", "color_mono", "color_velita", "tipo_jaboncito",
              "color_jaboncito", "nombre_bebe", "tarjetita", "producto"):
        if k == "producto":
            continue
        if datos.get(k) not in (None, ""):
            existing[k] = datos[k]
    aplicar_precio_oficial(existing)
    _validar_colores_item(existing)
    print("📝 Item modificado:", existing)
    return ["items"]


def eliminar_item_pedido(pedido, argumentos_json):
    try:
        datos = json.loads(argumentos_json) if argumentos_json else {}
    except (json.JSONDecodeError, TypeError):
        return []
    producto = (datos.get("producto") or "").strip()
    if not producto:
        return []
    if "items" not in pedido or not isinstance(pedido.get("items"), list):
        return []
    clave = _normalizar_nombre_producto(producto)
    antes = len(pedido["items"])
    nuevos = []
    for it in pedido["items"]:
        n = _normalizar_nombre_producto(it.get("producto"))
        if n == clave or (clave and (clave in n or n in clave)):
            print(f"🗑️ Item eliminado: {it.get('producto')}")
            continue
        nuevos.append(it)
    pedido["items"] = nuevos
    if pedido["items"]:
        pedido["producto"] = pedido["items"][0].get("producto")
        pedido["cantidad"] = pedido["items"][0].get("cantidad")
        pedido["precio_unitario"] = pedido["items"][0].get("precio_unitario")
    else:
        pedido["producto"] = None
        pedido["cantidad"] = None
        pedido["precio_unitario"] = None
    return ["items"] if len(nuevos) != antes else []


def ejecutar_tool_call(tool_call, sesion, numero, pedido):
    name = tool_call.function.name
    args = tool_call.function.arguments

    if name == "actualizar_pedido":
        ya_estaba_confirmado = pedido.get("anticipo_confirmado") is True
        campos_modificados = aplicar_actualizacion_pedido(pedido, args)
        anticipo_recien_confirmado = (
            "anticipo_confirmado" in campos_modificados
            and pedido.get("anticipo_confirmado") is True
            and not ya_estaba_confirmado
        )
        # 🔧 CAPA 3 DE SEGURIDAD (bug real detectado en pruebas): nunca
        # dejar que se confirme un anticipo mientras haya productos sin
        # precio resuelto en el pedido -- es el último punto de control
        # antes de que el pedido se dé por cobrado. Aunque el matching de
        # precios (capa 1) y la señal de "incompleto" en el prompt (capa
        # 2) fallaran por cualquier motivo, esto es lo que evita que un
        # pedido con un producto gratis por error llegue a confirmarse.
        if anticipo_recien_confirmado:
            _tot_check = pedido_manager.calcular_total(borrador=pedido)
            if _tot_check.get("incompleto"):
                pedido["anticipo_confirmado"] = False
                productos = ", ".join(_tot_check.get("productos_sin_precio") or [])
                print(f"🚨 Se bloqueó confirmación de anticipo: precio pendiente para {productos}")
                return (
                    f"BLOQUEADO: no se puede confirmar el anticipo todavía porque "
                    f"estos productos no tienen precio resuelto: {productos}. "
                    f"Dile al cliente que en un momento le confirmas el precio de "
                    f"ese producto antes de continuar con el pago.",
                    campos_modificados,
                    False,
                )
        return "ok", campos_modificados, anticipo_recien_confirmado

    if name == "agregar_item":
        campos = agregar_item_pedido(pedido, args)
        return "ok", campos, False

    if name == "actualizar_item":
        campos = actualizar_item_pedido(pedido, args)
        return "ok", campos, False

    if name == "eliminar_item":
        campos = eliminar_item_pedido(pedido, args)
        return "ok", campos, False

    if name == "mostrar_foto_producto":
        try:
            args_obj = json.loads(args or "{}")
        except json.JSONDecodeError:
            args_obj = {}
        clave = args_obj.get("producto")
        imagenes_enviadas = sesion["imagenes_enviadas"]


        if clave in imagenes_enviadas:
            return "ya se le mandó esta foto antes en la conversación, no la repitas", [], False

        url_imagen = url_imagen_producto(clave)
        if not url_imagen:
            return f"no hay foto disponible para '{clave}', no ofrezcas una foto de esto", [], False

        nombre_mostrar = CATALOGO_IMAGENES[clave]["nombre_mostrar"]
        enviar_whatsapp_imagen(numero, url_imagen, caption=nombre_mostrar)
        imagenes_enviadas.add(clave)
        return "imagen enviada correctamente", [], False

    return "función desconocida", [], False


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
    for indice_iteracion in range(MAX_ITERACIONES_HERRAMIENTAS):
        # 🔧 CORREGIDO: se detectó en producción que, aunque el prompt le
        # pide al modelo llamar a actualizar_pedido al ver un comprobante
        # de pago, a veces el modelo simplemente responde en texto sin
        # llamar la función — le dice al cliente "gracias por tu anticipo"
        # pero nunca queda guardado en la base de datos, así que el bot
        # nunca se entera de que debía silenciarse ni avisarle a Dalia.
        # Una instrucción de texto no es 100% confiable; forzar la llamada
        # a la función sí lo es. Por eso, en la primera vuelta de este
        # loop, si llegó una imagen, se OBLIGA a llamar a
        # actualizar_pedido (con los campos que apliquen, aunque sea
        # vacío si la imagen no trae nada que guardar). De ahí en
        # adelante el modelo vuelve a responder con libertad normal.
        tool_choice_este_turno = "auto"
        if imagen_base64 and indice_iteracion == 0:
            tool_choice_este_turno = {"type": "function", "function": {"name": "actualizar_pedido"}}

        r = client.chat.completions.create(
            model=MODELO,
            messages=mensajes_completos,
            tools=TOOLS,
            tool_choice=tool_choice_este_turno,
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

            anticipo_recien_confirmado_este_turno = False
            for tool_call in mensaje.tool_calls:
                resultado, campos_modificados, anticipo_recien_confirmado = ejecutar_tool_call(
                    tool_call, sesion, numero, pedido
                )
                campos_modificados_total.extend(campos_modificados)
                if anticipo_recien_confirmado:
                    anticipo_recien_confirmado_este_turno = True
                mensajes_completos.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": resultado,
                })

            if anticipo_recien_confirmado_este_turno:
                # 🆕 No hace falta pedirle al modelo que redacte una
                # respuesta final: los 2 mensajes que le llegan al cliente
                # en este caso son siempre los mismos (ver
                # procesar_mensaje_en_fondo), así que ni se gasta la
                # llamada extra a OpenAI. Se marca la sesión para que
                # procesar_mensaje_en_fondo sepa que debe mandar los
                # mensajes fijos + notificar a Dalia.
                sesion["_anticipo_recien_confirmado"] = True
                print("⏳ Anticipo confirmado en este turno — se cortan las respuestas automáticas del modelo")
                return None

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

def pedido_tiene_total_valido(pedido) -> bool:
    """Gate: solo se permiten datos bancarios si hay al menos un item con
    cantidad y precio, o un total calculado > 0, Y el total no está
    incompleto (ningún producto con precio pendiente de resolver)."""
    try:
        tot = pedido_manager.calcular_total(borrador=pedido)
        if tot.get("incompleto"):
            return False
        if tot["total"] > 0:
            return True
    except Exception:
        pass
    items = pedido.get("items") if isinstance(pedido, dict) else None
    if items and isinstance(items, list):
        for it in items:
            if it.get("cantidad") and it.get("precio_unitario"):
                return True
    if pedido.get("cantidad") and pedido.get("precio_unitario"):
        return True
    return False


def filtrar_datos_bancarios_si_no_hay_total(texto: str, pedido) -> str:
    """Si el modelo intenta mandar CLABE/tarjeta sin total calculado,
    reemplaza ese tramo por una frase pidiendo confirmar el resumen primero."""
    if pedido_tiene_total_valido(pedido):
        return texto
    marcadores = [m for m in (DATOS_BANCARIOS_TARJETA, "clabe", DATOS_BANCARIOS_CLABE, DATOS_BANCARIOS_BANCO) if m]
    lower = texto.lower()
    if not any(m.lower() in lower for m in marcadores):
        return texto
    return (
        "Antes de pasarte los datos para el anticipo, confirma por favor el "
        "resumen completo de tu pedido (productos, cantidades, colores, fecha "
        "y total). Cuando esté todo correcto te comparto la información de pago."
    )


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


def notificar_a_dalia(pedido_db, pedido_ram):
    """Le manda a Dalia (a su WhatsApp personal, no al del bot) un aviso
    cada vez que se confirma un anticipo, con el RESUMEN COMPLETO del
    pedido — no solo lo mínimo. Como Dalia no puede ver la conversación
    que tuvo el bot con el cliente, este mensaje es la única forma en que
    se entera de qué se acordó, así que debe bastar por sí solo para que
    pueda contactar al cliente con seguridad, sin tener que preguntarle
    "oye, ¿qué habíamos quedado?".

    Usa como fuente principal el pedido YA GUARDADO en la base de datos
    (pedido_db, con sus items/entrega/pagos) porque es el registro más
    confiable -- pedido_ram (el borrador en RAM) se usa solo como
    respaldo si algo faltó en la BD. Si DALIA_WHATSAPP_NUMERO no está
    configurado, no hace nada (no rompe el resto del flujo).
    """
    if not DALIA_WHATSAPP_NUMERO:
        print("⚠️ DALIA_WHATSAPP_NUMERO no configurado, no se pudo notificar a Dalia")
        return

    folio = pedido_db.folio if pedido_db else "SIN FOLIO"
    telefono_cliente = pedido_db.telefono if pedido_db else "desconocido"

    items = (pedido_db.items if pedido_db and pedido_db.items else []) or []
    entrega = pedido_db.entrega if pedido_db else None
    pago = pedido_db.pagos[-1] if (pedido_db and pedido_db.pagos) else None

    def _valor(de_bd, campo_ram):
        return de_bd if de_bd not in (None, "") else pedido_ram.get(campo_ram)

    lineas = [
        f"🛒 Nuevo anticipo confirmado — Folio {folio}",
        f"Cliente: {telefono_cliente}",
        "",
    ]

    if items:
        for it in items:
            lineas.append(f"• {it.cantidad} x {it.producto} @ ${it.precio_unitario:.2f} = ${it.subtotal:.2f}")
            extras = []
            if it.color_toalla:
                extras.append(f"toalla {it.color_toalla}")
            if it.color_moño:
                extras.append(f"moño {it.color_moño}")
            if it.tipo_jaboncito:
                extras.append(f"jabón {it.tipo_jaboncito}" + (f" {it.color_jaboncito}" if it.color_jaboncito else ""))
            if it.nombre_bebe:
                extras.append(f"nombre: {it.nombre_bebe}")
            if extras:
                lineas.append("  (" + ", ".join(extras) + ")")
    else:
        # fallback campos planos del RAM
        prod = pedido_ram.get("producto") or "sin especificar"
        cant = pedido_ram.get("cantidad") or "?"
        lineas.append(f"• {cant} x {prod}")

    if entrega:
        lineas.append(f"Entrega: {entrega.tipo_entrega}")
        if entrega.fecha_entrega:
            lineas.append(f"Fecha entrega: {entrega.fecha_entrega}")
        if entrega.direccion:
            lineas.append(f"Dirección: {entrega.direccion}" + (f", {entrega.municipio}" if entrega.municipio else ""))
        if entrega.costo_envio:
            lineas.append(f"Costo envío: ${entrega.costo_envio:.2f}")
    else:
        te = pedido_ram.get("tipo_entrega")
        if te:
            lineas.append(f"Entrega: {te}")

    if pedido_db and pedido_db.es_urgente:
        lineas.append("⚠ PEDIDO URGENTE (+$50)")

    # Total determinístico
    try:
        tot = pedido_manager.calcular_total(pedido_id=pedido_db.id if pedido_db else None, borrador=pedido_ram)
        lineas.append(f"TOTAL DE LA VENTA: ${tot['total']:.2f} MXN")
    except Exception:
        subtotal = sum(float(it.subtotal or 0) for it in items)
        extra = 50 if (pedido_db and pedido_db.es_urgente) else 0
        envio = float(entrega.costo_envio) if (entrega and entrega.costo_envio) else 0
        lineas.append(f"TOTAL DE LA VENTA: ${subtotal + extra + envio:.2f} MXN")

    if pago:
        lineas.append(f"Anticipo recibido: ${pago.monto:.2f} ({pago.metodo or 'transferencia'})")
        if pago.comprobante:
            lineas.append(f"Comprobante: {pago.comprobante}")

    mensaje = "\n".join(lineas)
    enviar_whatsapp(DALIA_WHATSAPP_NUMERO, mensaje)


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

    # 🔧 Se checa ANTES de guardar el mensaje entrante -- si se checara
    # después, este mismo mensaje ya contaría como "1 mensaje previo" y
    # nunca detectaríamos al cliente como nuevo.
    es_primera_vez = pedido_manager.es_cliente_nuevo(numero)

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

    # 🔧 Código de reactivación / reset completo: si el mensaje trae la
    # secuencia 🧸☠️🧸, se borra TODO lo relacionado a este teléfono --
    # historial de chat, borrador, y pedidos oficiales -- y queda como
    # si el número nunca hubiera hablado con el bot.
    #
    # ⚠️ Es DESTRUCTIVO E IRREVERSIBLE.
    #
    # Se usa la secuencia 🧸☠️🧸 (no solo 🧸🧸) porque "osito" es el
    # nombre de un producto real del catálogo -- un cliente cotizando
    # "2 ositos 🧸🧸" no debe disparar un borrado accidental de su propio
    # pedido en construcción.
    #
    # Además, ahora se valida que el número que lo manda esté en
    # NUMEROS_AUTORIZADOS_RESET (Dalia + cualquier número de prueba que
    # se agregue en .env vía RESET_NUMEROS_AUTORIZADOS). Un cliente real
    # que por alguna razón mande la secuencia completa ya no puede borrar
    # historial de negocio real (pedidos ya entregados/cobrados).
    if texto_cliente and "🧸☠️🧸" in texto_cliente:
        if numero not in NUMEROS_AUTORIZADOS_RESET:
            print(f"🚫 {numero}: intentó usar el código de reset pero no está autorizado.")
            # No se le confirma ni se le desmiente nada al remitente para
            # no revelar que existe un código de reset; el mensaje se
            # guarda igual en el historial (arriba) y el bot sigue su
            # flujo normal a partir de aquí.
        elif pedido_manager.resetear_cliente_completo(numero):
            print(f"🧸☠️🧸 {numero}: reset completo (historial + pedidos eliminados)")

            # Se descarta la sesión en RAM por completo -- con la base de
            # datos ya vacía para este teléfono, la próxima vez que se
            # llame a obtener_sesion() se va a reconstruir desde cero.
            with sesiones_lock:
                sesiones.pop(numero, None)

            # 🔧 CORREGIDO (bug real detectado en pruebas): antes, justo
            # después de vaciar historial_chat, se volvían a insertar 2
            # filas ahí mismo (el "🧸☠️🧸" y la confirmación "✅ Listo,
            # empezamos de cero...") -- entonces historial_chat YA NO
            # quedaba realmente vacío, y es_cliente_nuevo() (que revisa
            # justo eso) dejaba de detectar al cliente como nuevo en su
            # siguiente mensaje. Resultado: después de un reset, el
            # saludo canónico + las 2 imágenes obligatorias nunca se
            # mandaban, porque el sistema ya no consideraba "nuevo" a ese
            # número. Ahora la confirmación del reset se manda sin
            # guardarse en historial_chat, para que el reset deje el
            # número genuinamente en cero.
            respuesta_reset = "✅ Listo, empezamos de cero. ¿En qué te puedo ayudar?"
            enviar_whatsapp(numero, respuesta_reset)
            return

    # 🆕 Meta 2: si Dalia (humana) ya tomó el control de esta conversación
    # (esto pasa automáticamente en cuanto se confirma el anticipo), el
    # bot NO debe responder nada más. El mensaje ya quedó guardado arriba
    # para que Dalia lo vea, pero no se gasta una llamada a OpenAI ni se
    # manda ninguna respuesta automática.
    modo_atencion = pedido_manager.obtener_modo_atencion(numero)
    if modo_atencion != ModoAtencion.BOT.value:
        print(f"🙅 Bot en silencio para {numero} (modo_atencion={modo_atencion}); mensaje guardado, sin respuesta automática.")
        return

    # 🔧 CORREGIDO (regla real detectada en pruebas): la Base de
    # Conocimiento (00_PRIORIDAD_MAXIMA.txt, regla 9) exige que TODO
    # cliente nuevo reciba un saludo canónico exacto + las imágenes
    # A.jpeg y B.jpeg, sin importar qué haya escrito. Antes esto dependía
    # 100% de que el modelo se acordara de seguir esa instrucción -- en
    # pruebas reales, un cliente nuevo preguntó por las entregas y el bot
    # se saltó directo a contestar, sin el saludo ni las fotos. Ahora se
    # fuerza en Python, igual que ya se hace con los precios y los
    # mensajes fijos post-anticipo: no se le pregunta al modelo, se manda
    # directo. El mensaje real del cliente (su pregunta, lo que sea) se
    # queda guardado en el historial y el bot lo atiende normal en
    # cuanto el cliente vuelva a escribir algo.
    if es_primera_vez:
        saludo_canonico = (
            "Hola! Buen día. Disponibles!! Te muestro algunos de nuestros "
            "productos y te comparto información de las entregas que "
            "manejamos!! Buscas algún recuerdito en especial?"
        )
        enviar_whatsapp(numero, saludo_canonico)
        crm.guardar_respuesta(cliente, saludo_canonico)
        for clave_img in ("a", "b"):
            url_img = url_imagen_producto(clave_img)
            if url_img:
                enviar_whatsapp_imagen(numero, url_img)
            else:
                print(f"⚠️ No se encontró la imagen '{clave_img}' para el saludo de cliente nuevo")
        print(f"👋 {numero}: saludo canónico + 2 imágenes enviados (cliente nuevo)")
        return

    sesion = obtener_sesion(numero)

    # 🔧 Envío determinístico de imágenes clave (colores + productos de
    # una sola variante) -- ver detectar_imagenes_automaticas arriba.
    # Se manda ANTES de consultar al modelo para que llegue de inmediato,
    # no como una foto más entre varias respuestas de texto.
    imagenes_enviadas = sesion["imagenes_enviadas"]
    for clave_img in detectar_imagenes_automaticas(texto_cliente):
        if clave_img in imagenes_enviadas:
            continue
        url_img = url_imagen_producto(clave_img)
        if url_img:
            enviar_whatsapp_imagen(numero, url_img)
            imagenes_enviadas.add(clave_img)
            print(f"🖼️ Imagen automática enviada a {numero}: {clave_img}")

    with sesion["lock"]:
        try:
            print("🧠 Consultando OpenAI...")
            respuesta = preguntar_ia(numero, texto_cliente, imagen_base64=imagen_base64, imagen_mime=imagen_mime)
            if respuesta is not None:
                print("✅ Respuesta generada")
                print(respuesta[:300])
        except Exception as e:
            print("❌ Error llamando a OpenAI:", repr(e))
            respuesta = "Disculpa, tuve un problema técnico. ¿Me puedes repetir tu mensaje? 🙂"

        # 🆕 Caso especial: se acaba de confirmar el anticipo en este turno
        # (preguntar_ia regresa None y deja la bandera en la sesión). Los
        # mensajes al cliente son fijos, no los redacta el modelo, y
        # además se le avisa a Dalia por su WhatsApp personal.
        if respuesta is None and sesion.get("_anticipo_recien_confirmado"):
            sesion["_anticipo_recien_confirmado"] = False
            mensaje_1 = "¡Gracias por tu anticipo! En breve te contactaremos para enviarte la nota de tu pedido."
            mensaje_2 = "⌛"

            try:
                # sincronizar_pedido ya regresa el pedido oficial (recién
                # creado o actualizado) con su folio — no usar
                # crm.cargar_pedido aquí, porque esa función EXCLUYE a
                # propósito los pedidos en modo DALIA (ver
                # pedido_manager.obtener_pedido_activo) y ya acabamos de
                # poner este pedido en modo DALIA.
                pedido_db = crm.sincronizar_pedido(cliente, sesion["pedido"])
                sesion["pedido_id"] = pedido_db.id if pedido_db else None

                crm.guardar_respuesta(cliente, mensaje_1)
                crm.guardar_respuesta(cliente, mensaje_2)
            except Exception as e:
                print("⚠️ Error guardando en CRM (el bot sigue funcionando con RAM):", repr(e))
                pedido_db = None

            time.sleep(random.uniform(2, 4))
            print("📤 Enviando mensajes fijos de confirmación de anticipo...")
            enviar_whatsapp(numero, mensaje_1)
            time.sleep(1.5)
            enviar_whatsapp(numero, mensaje_2)

            print("📣 Notificando a Dalia...")
            notificar_a_dalia(pedido_db, sesion["pedido"])

            print("🏁 Fin procesamiento (anticipo confirmado)")
            print("=" * 70)
            return

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
        # Gate: no mandar datos bancarios sin total calculado
        respuesta = filtrar_datos_bancarios_si_no_hay_total(respuesta, sesion.get("pedido") or {})
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
