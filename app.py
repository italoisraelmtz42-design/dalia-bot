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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import quote as url_quote

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
import database
from constantes import ModoAtencion, campos_faltantes_pedido

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
# 🔧 CORREGIDO (fuga de memoria real detectada 19 ago 2026): el SDK de
# OpenAI, si no se le da un timeout explícito, usa 10 minutos (600s) por
# default -- una sola llamada lenta/colgada podía dejar un hilo de fondo
# vivo (y toda la memoria que tenía referenciada: historial, sesión,
# imágenes, etc.) hasta 10 minutos. Con muchos mensajes al día, algunos
# de esos hilos colgados se iban acumulando durante horas sin nunca
# soltarse, hasta llenar la memoria del servidor y tumbar el bot. 60s es
# más que suficiente para una respuesta normal del modelo.
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=60.0, max_retries=2)

# 🔧 CORREGIDO (mismo bug de fuga de memoria, causa raíz): antes, CADA
# mensaje entrante creaba un threading.Thread nuevo de sistema operativo
# (sin límite alguno) para procesarlo en segundo plano. Si llegaban
# muchos mensajes seguidos, o si algunas llamadas se colgaban (ver fix
# de timeout arriba), esos hilos se podían ir acumulando sin control --
# cada uno reservando su propia pila de memoria y manteniendo viva toda
# la sesión/historial que estaba usando, durante horas. Ahora todo el
# procesamiento en segundo plano pasa por un pool ACOTADO de hilos
# (máximo 20 a la vez): si llegan más mensajes de los que se pueden
# procesar en paralelo, simplemente se hacen fila en vez de crear más
# hilos nuevos -- así la memoria usada por hilos de fondo tiene un techo
# fijo, sin importar cuántos mensajes lleguen.
EJECUTOR_MENSAJES = ThreadPoolExecutor(max_workers=20, thread_name_prefix="msg-bg")


def _lanzar_en_fondo(target, *args, **kwargs):
    """Encola una tarea de procesamiento (mensaje, comentario, etc.) en el
    pool acotado de hilos de background, en vez de crear un hilo de SO
    nuevo cada vez (ver comentario de EJECUTOR_MENSAJES arriba). También
    atrapa cualquier excepción no manejada para que quede en los logs en
    vez de perderse en silencio (los threading.Thread daemon de antes
    tampoco la mostraban de forma consistente en producción)."""
    def _envoltura():
        try:
            target(*args, **kwargs)
        except Exception as e:
            nombre = getattr(target, "__name__", repr(target))
            print(f"⚠️ Excepción no capturada en tarea de fondo ({nombre}): {repr(e)}")

    EJECUTOR_MENSAJES.submit(_envoltura)

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

# 🆘 CANDADO DE EMERGENCIA (capa 1 -- la definitiva). Si algo sale mal en
# producción y necesitas apagar el bot YA, sin depender de que ninguna
# otra parte del código esté funcionando bien: entra a Render →
# Environment → agrega (o cambia) BOT_PAUSADO=true y guarda. Se
# redespliega solo en un par de minutos y el bot deja de contestar a
# CUALQUIER cliente, sin excepción -- ni siquiera llega a intentar nada,
# es el primer chequeo de todo el flujo. Para reactivar, bórrala o
# ponla en "false".
BOT_PAUSADO_GLOBAL = os.getenv("BOT_PAUSADO", "false").strip().lower() == "true"

# Número personal de Dalia (con lada, sin signos: ej. "5218114905653"),
# al que se le manda una notificación cada vez que se confirma un
# anticipo. Si no está configurado, simplemente no se manda la
# notificación (no rompe nada del resto del bot).
DALIA_WHATSAPP_NUMERO = os.getenv("DALIA_WHATSAPP_NUMERO", "")

# 🔧 (19 ago 2026) Número personal de una vendedora, a quien también se
# le manda EXACTAMENTE el mismo aviso de anticipo confirmado que a Dalia
# (mismo mensaje, mismo mecanismo -- ver notificar_a_dalia). Si no está
# configurado, simplemente no se le manda nada a ella (no rompe nada del
# resto del bot).
VENDEDORA_WHATSAPP_NUMERO = os.getenv("VENDEDORA_WHATSAPP_NUMERO", "")

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

# 🔧 Mismo mecanismo, pero para el PSID de Dalia en Messenger -- el
# candado de emergencia (🛑🛑🛑) y el reset (🧸☠️🧸) comparan "numero"
# tal cual llega, y en Messenger ese valor es el PSID (no un teléfono),
# así que necesita su propia entrada en la lista de autorizados.
DALIA_MESSENGER_PSID = os.getenv("DALIA_MESSENGER_PSID", "")
if DALIA_MESSENGER_PSID:
    NUMEROS_AUTORIZADOS_RESET.add(DALIA_MESSENGER_PSID)

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

# 🔧 (20 ago 2026, a pedido explícito de Israel) Antes la dirección del
# local solo vivía en el prompt (conocimiento/Entregas y envíos.txt) y
# dependía de que el modelo decidiera mandarla -- en una conversación real
# ("Tacos Luis") el cliente hizo su pedido urgente (solo recolección en
# local) y pagó el anticipo, pero el bot NUNCA le dijo dónde está el
# local. Como TODO pedido urgente y todo pedido con tipo_entrega=local
# necesita esta información sí o sí, ahora se manda de forma determinística
# (ver mensaje_ubicacion en procesar_mensaje_en_fondo), igual que ya se
# hacen los 3 mensajes fijos tras el anticipo -- no se deja a criterio del
# modelo.
DIRECCION_LOCAL = "Cedro #200B, Col. Los Encinos, Apodaca (el local está dentro de la papelería ISA)"
LINK_MAPS_LOCAL = "https://maps.app.goo.gl/WtPRbPHhnpgaWmVT8"
HORARIO_LOCAL_ENTRE_SEMANA = "Lunes a Viernes: 3:30pm - 6:30pm"
HORARIO_LOCAL_SABADO = "Sábado: 11:30am - 2:00pm"

GRAPH_API_VERSION = "v20.0"

# --- MESSENGER (Facebook Page) ---
# Page Access Token de la página PRINCIPAL de Facebook (Recuerditos
# Dalia) -- se genera al conectar la página a la App de Meta (misma App
# que ya usas para WhatsApp Cloud API). Es DISTINTO del WHATSAPP_TOKEN.
MESSENGER_PAGE_ACCESS_TOKEN = os.getenv("MESSENGER_PAGE_ACCESS_TOKEN", "")

# 🔧 Soporte para MÁS de una página de Facebook con el mismo bot (mismo
# catálogo/precios/reglas, solo cambia por dónde entra el cliente). Para
# agregar páginas extra, en Render pon MESSENGER_PAGE_ID_2 /
# MESSENGER_PAGE_ACCESS_TOKEN_2, luego _3, _4... (numeración consecutiva,
# se detiene en cuanto falte un par). La página "principal"
# (MESSENGER_PAGE_ACCESS_TOKEN de arriba) no necesita su propio
# MESSENGER_PAGE_ID -- se usa como respaldo si el ID de la página
# entrante no coincide con ninguna de las extra.
MESSENGER_TOKENS_POR_PAGINA = {}
_i = 2
while True:
    _page_id = os.getenv(f"MESSENGER_PAGE_ID_{_i}", "").strip()
    _token = os.getenv(f"MESSENGER_PAGE_ACCESS_TOKEN_{_i}", "").strip()
    if not _page_id or not _token:
        break
    MESSENGER_TOKENS_POR_PAGINA[_page_id] = _token
    print(f"✅ Página de Messenger extra configurada: {_page_id}")
    _i += 1


def token_para_pagina(page_id: str) -> str:
    """Devuelve el Page Access Token correcto según qué página recibió
    el mensaje. Si no coincide con ninguna página extra configurada,
    usa la principal como respaldo (cubre el caso normal de una sola
    página, y evita romper nada si algún evento no trae el ID)."""
    if page_id and page_id in MESSENGER_TOKENS_POR_PAGINA:
        return MESSENGER_TOKENS_POR_PAGINA[page_id]
    return MESSENGER_PAGE_ACCESS_TOKEN


# Token de verificación dedicado para el webhook de Messenger/Feed
# (comentarios en la página). Si no se define MESSENGER_VERIFY_TOKEN en
# las variables de entorno, cae de vuelta al mismo valor de WhatsApp por
# compatibilidad con configuraciones previas. Debe coincidir con lo que
# pongas en el panel de Meta al configurar el webhook de Messenger/Page.
MESSENGER_VERIFY_TOKEN = os.getenv("MESSENGER_VERIFY_TOKEN", VERIFY_TOKEN)
MESSENGER_GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/me/messages"
if not MESSENGER_PAGE_ACCESS_TOKEN:
    print("⚠️ MESSENGER_PAGE_ACCESS_TOKEN no configurado -- el bot no podrá "
          "responder mensajes de Facebook Messenger hasta que se configure.")

# 🔧 Interruptor para apagar SOLO Messenger sin tocar nada de la
# configuración en Meta (webhook, token, suscripciones se quedan tal
# cual, listos para cuando se quiera reactivar). Por defecto está
# activo; para apagarlo, en Render → Environment agrega
# MESSENGER_ACTIVO=false. WhatsApp no se ve afectado por esto en
# absoluto -- son interruptores completamente independientes.
MESSENGER_ACTIVO = os.getenv("MESSENGER_ACTIVO", "true").strip().lower() == "true"
if not MESSENGER_ACTIVO:
    print("🔕 MESSENGER_ACTIVO=false -- el bot va a ignorar todos los mensajes de Messenger.")

GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{WHATSAPP_PHONE_ID}/messages"

# --- WHATSAPP vía YCloud (Coexistencia -- número de PRUEBA) ---
# Este es un segundo "transporte" para WhatsApp, aparte del de Meta de
# arriba: mismo producto (WhatsApp) para efectos de CRM/dashboard (sigue
# guardándose con canal="whatsapp"), pero los mensajes salen/entran por
# la API de YCloud (api.ycloud.com) en vez de la Graph API de Meta. Se
# usa solo para el número de prueba (coexistencia), NO para el número de
# producción (que sigue 100% en Meta vía WHATSAPP_TOKEN/WHATSAPP_PHONE_ID
# de arriba, sin tocarse).
#
# YCLOUD_WHATSAPP_NUMBER debe llevar el "+" (formato E.164), ej:
# "+528143046969" -- así lo pide la API de YCloud para el campo "from".
YCLOUD_API_KEY = os.getenv("YCLOUD_API_KEY", "")
YCLOUD_WHATSAPP_NUMBER = os.getenv("YCLOUD_WHATSAPP_NUMBER", "")
YCLOUD_WEBHOOK_SECRET = os.getenv("YCLOUD_WEBHOOK_SECRET", "")
YCLOUD_API_URL = "https://api.ycloud.com/v2/whatsapp/messages"
if not (YCLOUD_API_KEY and YCLOUD_WHATSAPP_NUMBER):
    print("⚠️ YCLOUD_API_KEY / YCLOUD_WHATSAPP_NUMBER no configurados -- el "
          "canal de WhatsApp vía YCloud (número de prueba) no va a poder "
          "mandar mensajes hasta que se configuren. El número de producción "
          "(Meta) no se ve afectado por esto.")

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

# 🔧 Claves de la carpeta /imagenes que NO son fotos de producto (ej. la
# tarjeta con los datos bancarios para el anticipo) y por lo tanto nunca
# deben ofrecerse como "foto de producto disponible" ni mandarse por
# mostrar_foto_producto -- bug real detectado: el bot mandaba esta imagen
# de forma proactiva (ej. cuando el cliente solo preguntaba la ubicación),
# revelando datos bancarios sin que el cliente los pidiera ni estuviera
# listo para pagar. Los datos bancarios en texto siguen mandándose por su
# propio mecanismo (con su propio candado, ver filtrar_datos_bancarios_si_no_hay_total).
CLAVES_IMAGEN_NO_OFRECER = {"pagos_y_anticipos"}
CATALOGO_IMAGENES_PRODUCTO = {
    clave: info for clave, info in CATALOGO_IMAGENES.items()
    if clave not in CLAVES_IMAGEN_NO_OFRECER
}

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
    f"{PUBLIC_BASE_URL}/catalogo/{url_quote(NOMBRE_CATALOGO_PDF)}" if NOMBRE_CATALOGO_PDF else None
)


def url_imagen_producto(clave_producto):
    info = CATALOGO_IMAGENES.get(clave_producto)
    if not info:
        return None
    # 🔧 CORREGIDO (bug real detectado con clienta real): antes se pegaba
    # el nombre del archivo tal cual a la URL, sin codificar espacios ni
    # acentos -- con nombres simples nunca se notó, pero con acentos (ej.
    # "oración con decenario.jpeg") produce una URL inválida que Meta no
    # puede descargar. El bot decía "aquí tienes la foto" pero la imagen
    # nunca llegaba, solo el texto. url_quote codifica correctamente
    # cualquier caracter especial en el nombre del archivo.
    return f"{PUBLIC_BASE_URL}/imagenes/{url_quote(info['archivo'])}"


# 🔧 Imágenes que se mandan de inmediato por palabra clave del CLIENTE,
# sin depender de que el modelo se acuerde de llamar mostrar_foto_producto
# (bug real detectado en pruebas: el bot no mandaba la foto de colores
# aunque el cliente preguntara por colores). "colores_disponibles" es la
# más importante -- evita dudas sobre qué colores existen. El resto son
# productos de UNA sola variante (no necesitan preguntar nada antes, a
# diferencia de los ositos genéricos, que sí tienen varias presentaciones;
# para esos ver PALABRAS_CLAVE_OSITO_ESPECIFICO más abajo, que solo
# dispara con frases que ya identifican el modelo exacto).
PALABRAS_CLAVE_IMAGEN_AUTOMATICA = {
    "colores_disponibles": ("color", "colores"),
    # 🔧 Se quitaron "velita"/"velitas" sueltas de aquí: esas palabras
    # también aparecen dentro de "kit osito + oración + velita" (un osito,
    # no una vela de toalla), y disparaban por error la foto de este
    # producto equivocado cuando el bot mandaba la lista de precios de
    # ositos. Ahora solo dispara con frases que sí identifican una vela de
    # toalla sin ambigüedad.
    "velas_de_toalla_cyg": ("vela de toalla", "velas de toalla", "vela grande", "vela chica"),
    "elefante_de_toalla": ("elefante", "elefantito", "elefantes"),
    "jirafa_de_toalla": ("jirafa", "jirafas"),
    "buho_con_virrete_de_toalla": ("birrete", "virrete"),
    "buho_de_toalla": ("buho", "buhos"),
    "caballito_de_toalla": ("caballo", "caballito", "caballos"),
    "conejito_de_toalla": ("conejo", "conejito", "conejos"),
    # 🔧 (19 ago 2026, bug real detectado por Israel) Se quitó "leon"
    # suelta de aquí: "leon" (sin acento, ya normalizado) es substring de
    # "Nuevo León" -- el nombre del estado, que aparece muy seguido
    # cuando el bot manda su propia dirección ("...Apodaca, Nuevo León,
    # ..."). Eso disparaba por error la foto del leoncito de toalla en
    # medio de una respuesta sobre la ubicación del local, sin que nadie
    # hubiera mencionado el producto. "leoncito" y "leones" no tienen ese
    # problema (nadie escribe "Nuevo Leoncito" ni "Nuevo Leones").
    "leoncito_de_toalla": ("leoncito", "leones"),
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


# 🔧 Palabras que indican que el texto SÍ está hablando de un producto de
# toalla (los que de verdad usan la paleta de colores de toallita/moño/
# jaboncito que muestra la foto "colores_disponibles"). Se usan para
# filtrar esa foto cuando se detecta en la RESPUESTA del bot -- bug real
# detectado: un cliente preguntó por abanicos, el bot cotizó el abanico
# mencionando "...moño a elegir color, blonda..." y la sola palabra
# "color" disparó, por error, la foto de colores de toallita/jaboncito
# (un producto que ni siquiera aplica al abanico, que no lleva jaboncito).
# No se usa para filtrar lo que pregunta el CLIENTE (si el cliente pregunta
# "qué colores tienen" a secas, sí queremos mandarla).
PALABRAS_CONTEXTO_PRODUCTO_DE_TOALLA = (
    "toallita", "toalla", "toallas", "jaboncito", "jabon", "osito", "ositos",
    "animalito", "animalitos", "oracion", "decenario", "velita",
)


def _respuesta_menciona_producto_de_toalla(texto: str) -> bool:
    if not texto:
        return False
    texto_norm = normalizar_producto_clave(texto)
    return any(normalizar_producto_clave(p) in texto_norm for p in PALABRAS_CONTEXTO_PRODUCTO_DE_TOALLA)


# 🆕 Frases que SÍ identifican una variante ESPECÍFICA de osito (a
# diferencia de "osito" o "ositos" sueltos, que son ambiguos entre 9
# modelos distintos). Cada archivo de producto en /conocimiento ya dice
# "mandar siempre la imagen... cuando se le recomiende el producto", pero
# en la práctica el modelo no lo hacía solo -- en vez de mandar la foto,
# terminaba preguntando "¿quieres que te muestre la foto?". Ahora se manda
# sola, determinísticamente, en cuanto el CLIENTE o el propio BOT nombran
# el modelo exacto (ver uso más abajo, tanto en el mensaje del cliente
# como en la respuesta que arma el bot).
PALABRAS_CLAVE_OSITO_ESPECIFICO = {
    "osito_con_jaboncito": ("con jaboncito", "con jabon"),
    "osito_sencillo_sin_jabon": ("sencillo sin jabon", "sin jaboncito", "sin jabon"),
    "osito_doble_piecito": ("doble pie", "doble piecito"),
    "osito_con_doble_inicial_chica": ("doble inicial",),
    "osito_con_inicial_chica": ("inicial chica",),
    "osito_con_inicial_grande": ("inicial grande",),
    "osito_de_peluche_llavero": ("peluche llavero", "osito de peluche", "osito peluche"),
    "osito_toalla_afelpada": ("toalla afelpada", "osito afelpada", "osito afelpado"),
    "kit_osito_oracion_velita": ("kit osito oracion", "kit de osito con oracion"),
}


def detectar_imagen_osito_especifico(texto: str) -> list:
    """Detecta cuando un texto (mensaje del cliente O respuesta del bot)
    nombra una variante ESPECÍFICA de osito, para mandar la foto exacta de
    ese modelo sin preguntar. A diferencia de detectar_imagenes_automaticas,
    exige frases de 2+ palabras que ya identifican el modelo -- nunca
    dispara con "osito"/"ositos" sueltos, para no adelantar la foto
    equivocada cuando la pregunta todavía es genérica."""
    if not texto:
        return []
    # 🔧 Se quita puntuación (ej. "kit osito + oración + velita", tal cual
    # aparece en el índice de precios) para que las frases de 2+ palabras
    # sigan siendo substring contiguo aunque haya símbolos entre medio.
    texto_norm = normalizar_producto_clave(texto)
    texto_norm = re.sub(r"[^a-z0-9 ]", " ", texto_norm)
    texto_norm = " ".join(texto_norm.split())
    claves = []
    for clave_imagen, frases in PALABRAS_CLAVE_OSITO_ESPECIFICO.items():
        if clave_imagen not in CATALOGO_IMAGENES:
            continue
        if any(normalizar_producto_clave(f) in texto_norm for f in frases):
            claves.append(clave_imagen)
    # "doble inicial chica" contiene la subcadena "inicial chica" -- si ya
    # se detectó la variante doble, no mandar también la genérica.
    if "osito_con_doble_inicial_chica" in claves and "osito_con_inicial_chica" in claves:
        claves.remove("osito_con_inicial_chica")
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
    # 🔧 Actualizado: ya renombraste el archivo correctamente en GitHub
    # ("Entregas y envíos.txt", con la tilde bien puesta) -- esta entrada
    # ya coincide con el nombre real en disco.
    "Politicas generales/Entregas y envíos.txt",
    "Politicas generales/Pagina_De_Facebook.txt",
    # 🔧 Agregado (bug real detectado en pruebas): un cliente preguntó
    # "tienes pag. de fb?" y el bot contestó que no tenía esa info -- el
    # selector de archivos por palabra clave descarta "fb" y "pag" por
    # tener 3 caracteres o menos, así que Pagina_De_Facebook.txt nunca se
    # le mandaba al modelo salvo que el cliente escribiera "facebook"
    # completo. Como es un archivo muy corto (~600 caracteres), se manda
    # siempre en vez de depender de coincidencia de palabras.
    "Politicas generales/Pedidos urgentes.txt",
    "Politicas generales/Precios de mayoreo.txt",
    "Politicas generales/REGLAS IRROMPIBLES DEL NEGOCIO.txt",
    "Politicas generales/Resumen del pedido.txt",
    "Preguntas y respuestas/033_Reglas_Conversacion.txt",
    "Preguntas y respuestas/045_Guia_Tono_y_Personalidad.txt",
    "Preguntas y respuestas/050_Saludos_Humanos.txt",
    # 🔧 Agregado (bug real detectado con clienta real): el bot dijo "en
    # la Base de Conocimiento no aparece..." -- revela que es un sistema
    # automatizado usando su propia terminología interna, justo lo que
    # esta regla existe para evitar. Antes dependía de coincidencia de
    # palabras clave para aparecer en la conversación; ahora se manda
    # siempre, sin importar de qué se hable (es un archivo chico, no
    # afecta el costo).
    "Preguntas y respuestas/051_Frases_Que_Un_Humano_No_Dice.txt",
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


def seleccionar_conocimiento_relevante(texto_cliente, historial_reciente=None, top_k=22):
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
    seleccionados_extra = {nombre for _, nombre in puntajes[:top_k]} - ARCHIVOS_CONOCIMIENTO_SIEMPRE

    # 🔧 CORREGIDO (ahorro real de costo): antes se forzaba TODO el
    # catálogo de Productos (28 archivos) en cada mensaje sin importar de
    # qué hablara el cliente -- eso mandaba el 80% de toda la base de
    # conocimiento en cada turno. Se hizo así originalmente para evitar
    # precios inventados/mezclados, pero esa protección ya no depende del
    # texto: el precio real siempre lo asigna Python (PRECIOS_CATALOGO),
    # sin importar qué diga o no vea el modelo. Ahora los productos
    # también se seleccionan por palabra clave como todo lo demás
    # (top_k subió de 16 a 22 como margen de seguridad extra).
    #
    # 🔧 CORREGIDO (aprovechar el caché de prompts de OpenAI): los
    # archivos "siempre incluidos" van PRIMERO y en el mismo orden en
    # TODOS los mensajes de TODOS los clientes -- ese bloque es idéntico
    # letra por letra en cada llamada a la API, así que OpenAI lo cachea
    # automáticamente (75% más barato en esa parte). Los archivos
    # elegidos por palabra clave (que sí cambian según el mensaje) van
    # después, para no romper ese prefijo estable.
    texto_siempre = "".join(
        CONOCIMIENTO_POR_ARCHIVO[nombre]
        for nombre in sorted(ARCHIVOS_CONOCIMIENTO_SIEMPRE)
        if nombre in CONOCIMIENTO_POR_ARCHIVO
    )
    texto_extra = "".join(
        CONOCIMIENTO_POR_ARCHIVO[nombre]
        for nombre in sorted(seleccionados_extra)
        if nombre in CONOCIMIENTO_POR_ARCHIVO
    )
    return texto_siempre + texto_extra


# ===========================
# SESIONES POR CLIENTE
# ===========================

sesiones = {}
sesiones_lock = threading.Lock()

# 🔧 (21 ago 2026, a pedido explícito de Israel) Cuánto tiempo sin
# actividad tiene que pasar para que una sesión se quite de RAM (ver
# _limpiar_sesiones_inactivas). 2 horas es tiempo de sobra para que un
# cliente que sigue escribiendo activamente no pierda nada, pero corto
# para que la memoria no se quede acumulando clientes que ya se fueron.
TIEMPO_INACTIVIDAD_SESION_SEGUNDOS = 2 * 60 * 60


def ya_fue_procesado(mensaje_id):
    # 🔧 (19 ago 2026) Antes esto era un set() en memoria -- funcionaba
    # bien con un solo proceso de gunicorn, pero deja de ser confiable
    # con 2+ procesos (Procfile), porque cada proceso tendría su propio
    # set() separado y un mismo mensaje reintentado por Meta/YCloud
    # podría caer en otro proceso y procesarse (y contestarse) dos
    # veces. Ahora el dedupe vive en la base de datos (tabla
    # mensajes_webhook_procesados en database.py), compartida entre
    # todos los procesos.
    return database.reclamar_mensaje_procesado(mensaje_id)


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


def verificar_firma_ycloud(payload_bytes, header_value):
    """Verifica la firma HMAC-SHA256 que manda YCloud en el header
    'YCloud-Signature: t=<timestamp>,s=<firma>'. Mismo criterio que
    verificar_firma_webhook (Meta) arriba: si no hay secreto configurado,
    se deja pasar con una advertencia en vez de tumbar el webhook -- pero
    para producción real siempre se debe configurar YCLOUD_WEBHOOK_SECRET."""
    if not YCLOUD_WEBHOOK_SECRET:
        print("⚠️ YCLOUD_WEBHOOK_SECRET no configurado: el webhook de YCloud NO está verificando su origen")
        return True

    if not header_value:
        return False

    try:
        partes = dict(p.split("=", 1) for p in header_value.split(",") if "=" in p)
        timestamp = partes.get("t")
        firma_recibida = partes.get("s")
        if not timestamp or not firma_recibida:
            return False

        payload_firmado = f"{timestamp}.{payload_bytes.decode('utf-8')}"
        firma_esperada = hmac.new(
            YCLOUD_WEBHOOK_SECRET.encode("utf-8"),
            payload_firmado.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(firma_esperada, firma_recibida)
    except Exception as e:
        print("⚠️ Error verificando firma de YCloud:", repr(e))
        return False


# 🔧 Contexto por hilo: cada mensaje entrante se procesa en su propio
# threading.Thread daemon (uno por request, nunca se reutilizan), así que
# threading.local() aquí queda perfectamente aislado por conversación sin
# riesgo de mezclar el proveedor de un mensaje con el de otro. Se usa
# SOLO para que enviar_mensaje_canal/enviar_imagen_canal sepan si deben
# mandar por YCloud en vez de por Meta -- enviar_whatsapp() (Meta) sigue
# siendo la función que usa notificar_a_dalia() directamente, así que los
# avisos al WhatsApp personal de Dalia SIEMPRE salen por el número de
# producción, sin importar por qué canal haya entrado el mensaje del
# cliente que originó el aviso.
_contexto_hilo = threading.local()


def _usar_ycloud_en_este_hilo():
    return getattr(_contexto_hilo, "proveedor_whatsapp", "meta") == "ycloud"


# --- Validación de dominio (anti-alucinación estructural) ---
COLORES_VALIDOS = {
    "turquesa", "azul rey", "azulrey", "celeste", "blanco", "hueso",
    "fiusha", "fucsia", "rosa palo", "rosapalo", "rosa pastel", "rosapastel",
    "café claro", "cafe claro", "caféclaro", "cafeclaro", "amarillo",
    # excepción moño/listón
    "rojo", "dorado",
}

# 🔧 CORREGIDO (gap real detectado en auditoría): antes había un solo set
# "COLORES_ESPECIALES" compartido entre osito de peluche Y osito toalla
# afelpada -- pero según sus archivos de producto, cada uno tiene colores
# especiales DISTINTOS, no intercambiables. Con el set compartido, Python
# aceptaba por error "beige" o "vino tinto" en un peluche (solo válido en
# afelpada), y viceversa.
#
# Osito de peluche: según OSITO DE PELUCHE.txt, el único color EXTRA
# (fuera de la lista general) es "morado".
COLORES_ESPECIALES_PELUCHE = {"morado"}

# Osito toalla afelpada: según Osito_Toalla_Afelpada.txt, no es una lista
# de colores sueltos -- son 6 PAREJAS FIJAS de (toalla, moño). No se
# pueden combinar colores de parejas distintas.
PAREJAS_VALIDAS_AFELPADA = {
    ("celeste", "azul rey"),
    ("morado", "morado"),
    ("rosa pastel", "rosa pastel"),
    ("hueso", "hueso"),
    ("rojo", "vino tinto"),
    ("café claro", "beige"),
}

def _normalizar_color(valor: str) -> str:
    if not valor:
        return ""
    v = " ".join(str(valor).lower().strip().split())
    v = v.replace("á", "a").replace("é", "e").replace("í", "i").replace("ó", "o").replace("ú", "u")
    return v

def color_es_valido(valor: str, producto: str = "") -> bool:
    """Válido para cualquier campo de color EXCEPTO la pareja
    toalla+moño de osito toalla afelpada, que se valida aparte con
    pareja_afelpada_es_valida() porque depende de los DOS campos juntos,
    no de cada uno por separado."""
    if not valor:
        return True
    v = _normalizar_color(valor)
    if v in {_normalizar_color(c) for c in COLORES_VALIDOS}:
        return True
    prod = (producto or "").lower()
    if "peluche" in prod:
        if v in {_normalizar_color(c) for c in COLORES_ESPECIALES_PELUCHE}:
            return True
    if "afelpada" in prod or "afelpado" in prod:
        # Los colores de afelpada solo son válidos como PAREJA completa;
        # aquí solo se permite que pase el filtro por campo individual
        # (para no rechazar de más antes de tiempo mientras el cliente
        # todavía no ha dado el segundo color) -- la validación real de
        # la pareja ocurre en pareja_afelpada_es_valida().
        colores_afelpada_sueltos = {c for par in PAREJAS_VALIDAS_AFELPADA for c in par}
        if v in {_normalizar_color(c) for c in colores_afelpada_sueltos}:
            return True
    return False


def pareja_afelpada_es_valida(color_toalla: str, color_mono: str) -> bool:
    """Valida que la combinación toalla+moño de un osito toalla afelpada
    sea una de las 6 parejas oficiales -- no basta con que cada color
    individual sea válido por separado."""
    if not color_toalla or not color_mono:
        return True  # todavía falta un dato, no hay nada que rechazar aún
    par = (_normalizar_color(color_toalla), _normalizar_color(color_mono))
    parejas_normalizadas = {
        (_normalizar_color(a), _normalizar_color(b)) for a, b in PAREJAS_VALIDAS_AFELPADA
    }
    return par in parejas_normalizadas



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
    # 🔧 Agregado (gap real detectado en pruebas): "rosa" tenía imagen
    # (ROSA.jpeg) pero no existía en ningún catálogo real -- el modelo
    # inventaba el precio por su cuenta ($16 la primera vez, $15 la
    # imagen decía). Confirmado contigo: $16.00 c/u es el precio oficial.
    "rosa": 16.0,
    "rosa de toalla": 16.0,
}

# 🔧 Costo de envío a domicilio por municipio -- antes esto vivía SOLO en
# texto (00_INDICE_PRECIOS.txt) y el modelo tenía que acordarse del monto
# exacto para cada municipio y llamar actualizar_pedido con costo_envio=X
# a mano. Mismo riesgo que ya vimos con los precios de producto: si el
# modelo se equivoca o se le olvida, el total queda mal sin que nadie lo
# note. Ahora es una tabla determinística en Python, igual que
# PRECIOS_CATALOGO -- el municipio SIEMPRE gana sobre lo que diga el
# modelo.
COSTOS_ENVIO_MUNICIPIO = {
    "monterrey": 90.0,
    "apodaca": 90.0,
    "san nicolas": 90.0,
    "san nicolas de los garza": 90.0,
    "escobedo": 90.0,
    "general escobedo": 90.0,
    "guadalupe": 90.0,
    "santa catarina": 100.0,
    "san pedro": 120.0,
    "san pedro garza garcia": 120.0,
    "juarez": 120.0,
    "pesqueria": 150.0,
}
COSTO_ENVIO_FUERA_DE_ZONA = 300.0  # DHL, requiere pedido 100% liquidado


def resolver_costo_envio(municipio: str) -> float | None:
    """Devuelve el costo oficial de envío para un municipio, o None si el
    municipio no viene en la lista (fuera de zona -- requiere el precio
    especial de DHL, no un monto normal de domicilio)."""
    if not municipio:
        return None
    clave = normalizar_producto_clave(municipio)
    if clave in COSTOS_ENVIO_MUNICIPIO:
        return COSTOS_ENVIO_MUNICIPIO[clave]
    for k, v in COSTOS_ENVIO_MUNICIPIO.items():
        if k in clave or clave in k:
            return v
    print(f"⚠️ Municipio '{municipio}' no está en COSTOS_ENVIO_MUNICIPIO -- "
          f"tratado como fuera de zona (${COSTO_ENVIO_FUERA_DE_ZONA} DHL).")
    return None

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
        # 🔧 Cargo por cambio de moño (osito peluche / osito toalla
        # afelpada): antes esto solo vivía como texto en la Base de
        # Conocimiento ("cambio de moño +$2.00 c/u") y dependía 100% de
        # que el modelo se acordara de sumarlo aparte -- mismo riesgo que
        # ya vimos con los precios base. Ahora, si el modelo marcó
        # mono_personalizado=true en el item, Python agrega el cargo
        # directo al precio unitario, sin depender de que se acuerde de
        # nada más.
        clave_prod = normalizar_producto_clave(prod)
        es_peluche_o_afelpada = (
            "peluche" in clave_prod or "afelpad" in clave_prod
        )
        if es_peluche_o_afelpada and item.get("mono_personalizado"):
            oficial += 2.0

        # 🔧 Cargo por bolsa de celofán (encendedores / destapadores):
        # antes esto dependía de que el modelo escribiera literalmente
        # "con bolsa" dentro del texto del producto para que
        # resolver_precio lo detectara -- frágil, mismo riesgo que ya
        # vimos con otros cargos basados en texto libre. Ahora es un
        # campo estructurado (con_bolsa: true/false) que el cliente
        # responde explícitamente y Python aplica el cargo directo.
        es_encendedor_o_destapador = (
            "encendedor" in clave_prod or "destapador" in clave_prod
        )
        if es_encendedor_o_destapador and item.get("con_bolsa"):
            oficial += 1.0

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
        # 🔧 (20 ago 2026, a pedido explícito de Israel) En una conversación
        # real el bot mencionó el cargo urgente de +$50 unas 7-8 veces en
        # toda la plática -- Israel dice que así se puede molestar al
        # cliente. Reusa el mismo mecanismo de "ya se lo dije, no lo
        # repitas" que ya existe para datos_pago/colores/etc., pero con una
        # excepción: SÍ debe seguir apareciendo como línea del resumen
        # final del pedido (justo antes de pedir el anticipo) aunque ya se
        # haya mencionado antes -- ver resumen_info_enviada().
        "cargo_urgente_mencionado": False,
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

        # 🔧 (21 ago 2026, a pedido explícito de Israel -- fuga de memoria
        # confirmada en Render, instancia caída por "out of memory") Antes
        # las sesiones en RAM (este diccionario `sesiones`) solo se
        # borraban con un reset manual explícito -- cualquier cliente que
        # le escribiera al bot se quedaba ocupando memoria PARA SIEMPRE,
        # sin importar si nunca volvía a escribir. Con el negocio
        # recibiendo clientes nuevos todo el día, eso crecía sin límite
        # hasta tumbar el proceso. Ahora se guarda cuándo fue la última
        # vez que se usó cada sesión, para que _limpiar_sesiones_inactivas
        # (que corre cada rato en el hilo de seguimientos ya existente)
        # pueda quitar de RAM a los clientes inactivos con seguridad --
        # es seguro porque obtener_sesion ya sabe reconstruir todo desde
        # SQLite en cuanto ese cliente vuelva a escribir.
        sesiones[numero]["_ultima_actividad"] = time.time()

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


def _limpiar_sesiones_inactivas():
    """🔧 (21 ago 2026, a pedido explícito de Israel) Quita de RAM las
    sesiones de clientes que no han escrito en más de
    TIEMPO_INACTIVIDAD_SESION_SEGUNDOS -- es la corrección principal de
    la fuga de memoria que tumbó la instancia de Render por "out of
    memory" (el diccionario `sesiones` crecía sin límite porque antes
    solo se limpiaba con un reset manual). Es seguro: en cuanto ese
    cliente vuelva a escribir, obtener_sesion() reconstruye su sesión
    completa desde SQLite (mensajes, pedido en borrador, etc.), igual que
    ya hace hoy para cualquier sesión nueva."""
    ahora = time.time()
    numeros_a_quitar = []
    with sesiones_lock:
        for numero, datos_sesion in sesiones.items():
            ultima_actividad = datos_sesion.get("_ultima_actividad", 0)
            if ahora - ultima_actividad > TIEMPO_INACTIVIDAD_SESION_SEGUNDOS:
                numeros_a_quitar.append(numero)
        for numero in numeros_a_quitar:
            sesiones.pop(numero, None)
    if numeros_a_quitar:
        print(f"🧹 [Limpieza de memoria] {len(numeros_a_quitar)} sesión(es) inactiva(s) "
              f"quitadas de RAM (siguen intactas en SQLite): {numeros_a_quitar}")


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
    lineas = [f"- {etiquetas[k]}: YA SE ENVIÓ, no lo repitas" for k in ya_enviados if k in etiquetas]

    # 🔧 (20 ago 2026, a pedido explícito de Israel) Este caso es distinto
    # a los de arriba: el cargo urgente NO se calla para siempre, solo se
    # calla en mensajes normales -- tiene una excepción explícita para el
    # resumen final del pedido, así que necesita su propio texto en vez
    # del genérico "no lo repitas".
    if info_enviada.get("cargo_urgente_mencionado"):
        lineas.append(
            "- Cargo urgente (+$50): YA SE MENCIONÓ antes en esta conversación. "
            "NO lo vuelvas a mencionar en mensajes normales (ni para recordárselo "
            "ni para reconfirmarlo). ÚNICA excepción: si en este mensaje vas a "
            "mostrar el RESUMEN FINAL del pedido (antes de pedir el anticipo), ahí "
            "SÍ debe aparecer la línea 'Cargo urgente: $50' como dice la plantilla "
            "-- pero solo esa vez, dentro del resumen, no antes ni después."
        )

    return "\n".join(lineas) if lineas else "Nada de esto se ha enviado todavía."


def detectar_info_enviada(texto_respuesta):
    texto = texto_respuesta.lower()
    detectado = {
        "datos_pago": (bool(DATOS_BANCARIOS_TARJETA) and DATOS_BANCARIOS_TARJETA in texto_respuesta) or ("clabe" in texto),
        "colores_disponibles": ("turquesa" in texto and "rosa palo" in texto),
        "ubicacion_local": "maps.app.goo.gl" in texto,
        "catalogo_pdf": bool(URL_CATALOGO_PDF) and (URL_CATALOGO_PDF.lower() in texto),
        # 🔧 (20 ago 2026) Detecta cualquier mención del cargo urgente de
        # $50 (ya sea al cotizar, al recordarlo, o en el resumen) para
        # poder avisarle al modelo en el siguiente turno que ya lo dijo.
        "cargo_urgente_mencionado": "urgen" in texto and "$50" in texto_respuesta,
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
🔧 IMPORTANTE SOBRE FOTOS -- LÉELO ANTES DE RESPONDER: el sistema (no tú)
manda AUTOMÁTICAMENTE la foto de los colores disponibles y la foto de la
variante exacta de osito en cuanto tú o el cliente la mencionan por su
nombre (ej. "osito con jaboncito", "osito doble inicial", "osito toalla
afelpada"), y también la foto de productos de una sola presentación
(elefante, jirafa, búho, etc.) en cuanto se mencionan. Esto pasa solo,
sin que tengas que llamar ninguna función tú para esos casos.
- 🚫 NUNCA le preguntes al cliente "¿quieres que te muestre la foto?",
  "¿te gustaría ver una imagen?" ni nada parecido -- la foto ya se manda
  sola. Simplemente sigue respondiendo la conversación con normalidad,
  como si el cliente ya la hubiera visto (porque la va a ver).
- Si estás recomendando o cotizando un osito, sé específico con el nombre
  del modelo (ej. "el osito con jaboncito" en vez de solo "el osito") para
  que el sistema pueda identificar cuál foto mandar.
- Todavía puedes llamar a mostrar_foto_producto tú mismo como respaldo,
  SOLO para productos que no tengan una variante clara todavía, o si el
  cliente pide ver la foto de nuevo explícitamente aunque ya se le haya
  mandado antes. No la llames en cada mensaje ni para productos que el
  cliente no mencionó.

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


def parsear_fecha_pedido(texto_fecha):
    """Convierte el texto de fecha_evento/fecha_entrega (como lo haya
    escrito el modelo) a un date real de Python, o None si no se pudo
    interpretar. Prueba varios formatos, con DD/MM/YYYY primero (el que
    se usa en todo el resto del sistema)."""
    if not texto_fecha:
        return None
    texto_fecha = str(texto_fecha).strip()
    formatos = ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%y")
    for fmt in formatos:
        try:
            return datetime.strptime(texto_fecha, fmt).date()
        except ValueError:
            continue
    return None


def es_pedido_urgente(texto_fecha_entrega) -> bool | None:
    """Decide de forma determinística si una fecha de entrega es urgente,
    comparando contra la fecha de HOY real del servidor -- nunca contra
    lo que el modelo "crea" que es hoy.

    🔧 CORREGIDO (bug real detectado en pruebas, con una clienta real):
    antes, si el pedido era urgente o no lo decidía el modelo con
    actualizar_pedido(es_urgente=true/false) -- y el modelo se equivocó
    feo: le cobró $50 de cargo urgente a una clienta por una fecha
    (18 de septiembre) que en realidad estaba a más de un mes de
    distancia del día real (13 de agosto), muy lejos de necesitar cargo
    urgente. Parece que comparó los números de día sueltos ("18" vs
    "19") en vez de las fechas completas contra hoy. Ahora Python decide
    esto siempre, sin excepción, y sobreescribe cualquier es_urgente que
    haya puesto el modelo (aplicar_actualizacion_pedido en ejecutar_tool_call).

    Devuelve None si la fecha no se pudo interpretar (para no adivinar).
    """
    fecha = parsear_fecha_pedido(texto_fecha_entrega)
    if fecha is None:
        return None
    ahora = datetime.now(ZONA_HORARIA_NEGOCIO)
    fecha_minima = sumar_dias_habiles(ahora.date(), 4)
    return fecha < fecha_minima


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

    # 🔧 CORREGIDO (gap real detectado en pruebas): este cálculo
    # (campos_requeridos_para / campos_faltantes_pedido en constantes.py)
    # ya existía en el código pero nunca se conectaba a ningún lado --
    # el bot a veces cerraba pedidos sin haber preguntado datos
    # obligatorios (ej. color de moño) porque nadie se lo señalaba de
    # forma explícita. Ahora se calcula cada turno y, si falta algo, se
    # le avisa directo al modelo cuáles campos exactos faltan por
    # producto, en vez de depender de que se acuerde solo.
    try:
        if isinstance(pedido, dict):
            faltantes = campos_faltantes_pedido(pedido)
            if faltantes:
                resumen += (
                    f"\n\n[📋 DATOS QUE TODAVÍA FALTAN POR PREGUNTAR]\n"
                    f"{', '.join(faltantes)}\n"
                    f"Antes de armar el resumen final o pedir el anticipo, pregunta "
                    f"estos datos que faltan (uno o dos a la vez, no todos de golpe). "
                    f"No asumas ni inventes ninguno de estos valores."
                )
    except Exception as e:
        print(f"⚠️ Error calculando campos faltantes: {e}")

    if isinstance(pedido, dict) and pedido.get("_envio_fuera_de_zona"):
        resumen += (
            f"\n\n[⚠️ MUNICIPIO FUERA DE ZONA]\n"
            f"El municipio '{pedido.get('municipio')}' no está en la lista de "
            f"zonas normales de envío (no tiene costo fijo de $90-$150). Esto "
            f"significa envío especial por DHL: dile al cliente que ese envío "
            f"es por paquetería y que el pedido debe estar 100% liquidado "
            f"(no solo anticipo) antes de enviarse, y consulta el costo real "
            f"de DHL con Dalia en vez de inventar un monto. NO llames "
            f"actualizar_pedido con un costo_envio inventado para este caso.\n"
            f"🚫 Para este cliente, tipo_entrega SOLO puede ser 'domicilio' (vía "
            f"DHL) o 'local' (si él mismo puede recoger en persona). NUNCA le "
            f"ofrezcas ni le confirmes 'punto de entrega' -- esos son lugares y "
            f"horarios fijos solo dentro del área metropolitana de Monterrey, "
            f"inútiles para un cliente foráneo. Tampoco le ofrezcas pedido "
            f"urgente (ese cargo solo aplica con entrega en local, nunca con DHL)."
        )

    # 🔧 Bug real detectado (18 ago 2026): un cliente dijo "necesito 40
    # ositos en color rosa" desde su primer mensaje, pero el bot tardó
    # varios turnos en aclarar qué modelo de osito quería. Como solo se le
    # manda al modelo un resumen de los últimos 5 turnos, para cuando por
    # fin se aclaró el modelo, el mensaje original con "40" ya se había
    # salido de esa ventana -- y como la cantidad tampoco se había
    # guardado todavía (agregar_item necesita producto Y cantidad juntos),
    # el bot terminó preguntando la cantidad otra vez como si nunca se la
    # hubieran dicho. Este recordatorio hace que el número sobreviva entre
    # turnos aunque ya no aparezca en el historial reciente.
    if isinstance(pedido, dict) and pedido.get("cantidad_pendiente"):
        resumen += (
            f"\n\n[📌 CANTIDAD YA CONFIRMADA POR EL CLIENTE]\n"
            f"El cliente ya dijo explícitamente que quiere "
            f"{pedido.get('cantidad_pendiente')} piezas, aunque todavía no se "
            f"haya agregado el producto al pedido. NO le vuelvas a preguntar "
            f"cuántas piezas quiere -- en cuanto confirmes qué producto/modelo "
            f"es, llama a agregar_item usando cantidad={pedido.get('cantidad_pendiente')}."
        )

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

    # 🔧 (20 ago 2026, a pedido explícito de Israel) Antes el tiempo de
    # elaboración normal era un RANGO "4 a 6 días hábiles" (con fecha_maxima
    # como colchón extra de hasta 2 días). Israel pidió que ahora sea fijo:
    # 4 días hábiles y ya, sin margen. El "4" es el mismo número que SIEMPRE
    # se usó para decidir si un pedido es urgente o no (ver es_pedido_urgente
    # y el bloque de "Urgencia determinística" más abajo) -- eso no cambia,
    # solo se quita la fecha_maxima/"6" que únicamente se usaba como texto
    # informativo para el cliente.
    fecha_minima = sumar_dias_habiles(ahora.date(), 4)
    dia_semana_minima = dias[fecha_minima.weekday()]

    return f"""
Eres DALIA, asesora de ventas de Recuerditos Dalia.

Toda la información oficial está en la Base de Conocimiento.

REGLAS (prioridad máxima — leen antes que cualquier otra instrucción):
- Usa únicamente la Base de Conocimiento.
- Nunca inventes datos, productos, precios, colores, políticas ni campos (ej. no inventes "jaboncito corazón" si el cliente no lo pidió).
- Si algo no existe en la Base de Conocimiento, indícalo.
- Si un dato ya dicho contradice el catálogo oficial, CORRIGE con la verdad del catálogo (precio, color, disponibilidad).
- Precios: copia EXACTOS del archivo del producto. Nunca redondees ni mezcles precios de otro producto.
- El TOTAL del pedido lo calcula el sistema (ver bloque TOTAL en el resumen). No inventes totales.
- 🔧 Si es urgente o no lo calcula el sistema, no tú: cuando llames
  actualizar_pedido con fecha_evento, Python decide es_urgente comparando
  la fecha REAL de hoy contra la fecha que dio el cliente, y sobreescribe
  cualquier valor que hayas puesto. No intentes calcular tú si algo es
  urgente comparando los números de día sueltos (ej. "18 vs 19") -- eso
  ya causó un error real donde se le cobró un cargo urgente injustificado
  a una clienta por una fecha que en realidad estaba a más de un mes de
  distancia. Solo asegúrate de mandar fecha_evento en formato DD/MM/YYYY
  completo (con el año correcto que el cliente haya dicho o confirmado).
- Responde como una asesora humana por WhatsApp.
- Sé amable, natural y orientada a cerrar ventas.
- Responde PRIMERO y de forma directa a lo que el cliente pidió en su último
  mensaje. No antepongas información que el cliente no pidió (ej. no repitas
  colores si el cliente está hablando de forma de entrega).
- 🔧 Si el cliente vuelve a preguntar algo que ya le contestaste antes (ej.
  pide la dirección otra vez, pregunta de nuevo un precio o un dato que ya
  le diste), NUNCA le digas "ya te lo había dicho", "otra vez?" ni nada que
  suene a reclamo, sarcasmo o que fue su culpa por no acordarse. Es normal
  que un cliente se distraiga en WhatsApp y pierda el hilo. Simplemente
  contéstale de nuevo con la información que pide, de forma cálida y
  natural, como si fuera la primera vez que se la das.

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

🔧 El anticipo es OBLIGATORIO sin excepción, sin importar el tipo de
entrega (local, domicilio, punto de entrega) ni cómo el cliente prefiera
pagar el resto. Bug real detectado en pruebas: un cliente dijo "puedo
pagar al recibir" y el bot aceptó el pedido completo sin pedir el
anticipo -- eso está PROHIBIDO. Si el cliente sugiere pagar todo al
recibir, pagar contra entrega, o cualquier variante para evitar el
anticipo: acláralo con amabilidad -- el RESTO sí puede pagarse al
recibir, pero el ANTICIPO de $50 MXN es obligatorio desde antes para
poder agendar y fabricar. NUNCA digas frases como "tu pedido está listo
para elaboración", "queda agendado" o "confirmado" si el anticipo
todavía no se ha pedido y confirmado -- que el cliente diga "sí" al
resumen NO es lo mismo que el anticipo pagado, son dos pasos distintos y
los dos son obligatorios antes de dar por cerrado un pedido.

REGLAS DE FECHAS Y PEDIDOS URGENTES (usa SIEMPRE la fecha de hoy que se te
da más abajo, en la sección de estado del pedido, para todo cálculo;
nunca calcules fechas por tu cuenta):

- El tiempo normal de elaboración de un pedido es de 4 días hábiles.
- La fecha de entrega para un pedido NORMAL (no urgente) hecho hoy es el
  {dia_semana_minima} {fecha_minima.strftime('%d/%m/%Y')}.
- 🚨 {fecha_minima.strftime('%d/%m/%Y')} es SOLO el límite mínimo para
  validar si un pedido es urgente o no -- NUNCA la uses como la fecha de
  entrega del pedido a menos que el cliente la haya pedido explícitamente.
  SIEMPRE pregunta "¿para cuándo lo necesitas?" antes de llenar
  fecha_evento/fecha_entrega. Si todavía no lo has preguntado, NO llenes
  ese campo, aunque el resto del pedido ya esté completo.
- Si el cliente pide una fecha de entrega ANTES de {fecha_minima.strftime('%d/%m/%Y')},
  eso es un PEDIDO URGENTE. Para pedidos urgentes aplican estas restricciones:
  - Solo se puede entregar EN EL LOCAL (nunca a domicilio ni en puntos de entrega).
  - 🔧 Sí se puede entregar urgente EN SÁBADO, pero SOLO dentro del horario
    reducido de sábado: 11:30am a 2:00pm. Si el cliente pide urgente para
    sábado, confírmalo con ese horario específico (no el horario normal
    entre semana de 3:30pm-6:30pm).
  - No se aceptan pedidos urgentes para entregarse en domingo (no abrimos domingos).
  - 🚨 Error real ya cometido, nunca lo repitas: en una conversación real
    el bot le dijo a una clienta "los sábados no se aceptan pedidos
    urgentes" (falso, es lo contrario de esta regla) y luego, 2 mensajes
    después, se contradijo confirmando esa misma fecha de sábado que
    acababa de rechazar. Antes de decir que una fecha "no es posible",
    revisa 2 veces contra esta regla y contra lo que ya dijiste antes en
    la misma conversación -- el único día que de verdad nunca se trabaja
    es domingo.
  - Avisa al cliente de estas restricciones ANTES de confirmar el pedido, de
    forma amable, y no confirmes un pedido urgente con entrega a domicilio o
    en punto de entrega bajo ninguna circunstancia.
- Nunca confirmes una fecha de entrega sin haber verificado si es un pedido
  normal o urgente según las reglas de arriba.

REGLA GENERAL -- NO PIDAS PERMISO PARA COSAS OBVIAS, HAZLO DIRECTO:
🚫 Nunca termines un mensaje preguntando si quiere que le des algo que
CLARAMENTE ya quiere -- dáselo directo, en el mismo mensaje. Ejemplos
reales de esto pasando mal (no los repitas):
- "¿quieres que te mande la foto de los colores?" -> mándala/descríbela
  directo (esto además ya lo hace el sistema solo con la foto, tú solo
  sigue la conversación como si ya la hubiera visto).
- "¿quieres que te pase la ubicación?" -> si preguntó dónde están
  ubicados, dale la dirección completa, el horario y el link de Maps en
  ese mismo mensaje -- no preguntes si la quiere.
- "¿quieres que te recomiende un color?" -> si ya está eligiendo el
  producto y no ha dicho color, recomiéndale uno tú misma con una razón
  breve, no le preguntes si quiere que le recomiendes.
- "¿quieres que te diga los puntos de entrega/zonas de envío?" -> si
  preguntó por entrega o envío, dale directo las opciones (domicilio con
  costo, puntos de entrega si aplican, o recolectar en el local).
- Cualquier "¿quieres que te muestre/diga/explique ___?" donde la
  respuesta obvia sería "sí" -- no preguntes, dalo por hecho y respóndelo.
Sigue preguntando SOLO cuando de verdad dependa de una decisión o dato que
nada más el cliente puede dar (qué modelo de osito quiere, para cuándo lo
necesita, su dirección exacta, cuántas piezas, etc.) -- la diferencia es:
si tú ya sabes la respuesta o es obvio que la quiere, no preguntes, dásela
directo; si depende de él, ahí sí pregunta.

REGLA PARA PREGUNTAS VAGAS SOBRE "LOS OSITOS":
- 🔧 Si el cliente pregunta de forma genérica por "los ositos" (ej. "me
  interesan los ositos", "cuánto cuestan los ositos", "tienen ositos?"),
  SIN especificar cuál modelo o variante en específico, SIEMPRE respóndele
  primero con la lista completa de TODAS las variantes de osito y su
  precio (osito con jaboncito, osito sencillo, osito doble pie, osito
  inicial chica, osito doble inicial chica, osito inicial grande, osito
  peluche llavero, osito toalla afelpada, kit osito + oración + velita --
  usa los precios oficiales de la sección OSITOS del índice de precios).
  Esto es para que el cliente elija un modelo específico desde el
  principio y ya sepa el precio de cada uno, en vez de dejarlo ambiguo.
- No preguntes solamente "¿cuál te gustaría?" sin haberle mandado antes
  esa lista de opciones con precios -- primero la lista completa, y con
  eso ya lo estás guiando a que te diga cuál modelo quiere.
- Esta regla aplica cada vez que el cliente hable de "ositos" en plural o
  de forma general sin nombrar el modelo, aunque ya se le haya mandado la
  lista antes en la conversación (para reforzar cuál eligió, a menos que
  ya haya confirmado un modelo específico en un mensaje anterior).

No vuelvas a preguntar datos ya confirmados.
Pregunta únicamente los datos faltantes.

🔧 REGLA -- NUNCA ASUMAS LA CANTIDAD DE PIEZAS (bug real detectado: un
cliente pidió información de "el osito con jaboncito" sin decir nunca
cuántas piezas quería, y el bot asumió 1 pieza por su cuenta, armó el
resumen completo y llegó hasta pedir el anticipo -- el cliente tuvo que
preguntar él mismo para que el bot se diera cuenta del error):
- La cantidad de piezas SIEMPRE tiene que salir de un número explícito que
  el cliente haya escrito (ej. "quiero 3", "nomás uno", "10 piezas"). Nunca
  la asumas, nunca pongas 1 por default, nunca la infieras del contexto.
- En cuanto sepas qué producto quiere el cliente, si todavía no te ha dicho
  cuántas piezas, pregúntaselo directo ("¿cuántas piezas te gustaría?")
  ANTES de llamar a agregar_item -- no llames la función con una cantidad
  inventada solo para no dejar el campo vacío.
- Nunca armes ni ofrezcas el resumen completo del pedido (con total) ni
  pidas el anticipo si la cantidad de algún producto no vino de una
  respuesta explícita del cliente.

🔧 REGLA -- NO SE TE OLVIDE UNA CANTIDAD YA DICHA MIENTRAS ACLARAS EL
MODELO (bug real detectado: un cliente escribió desde su primer mensaje
"necesito 40 ositos en color rosa", pero el bot tardó varios turnos en
aclarar qué modelo de osito quería exactamente -- con jaboncito, sencillo,
etc. Como agregar_item necesita producto y cantidad juntos, la cantidad
nunca se guardó mientras tanto, y como solo se te manda un resumen de los
últimos turnos de la conversación (no toda), para cuando por fin se aclaró
el modelo, el "40" original ya no aparecía ahí -- el bot terminó
preguntando la cantidad otra vez, como si el cliente nunca la hubiera
dicho, lo cual es muy notorio y molesto para el cliente):
- Si el cliente ya te dio un número EXPLÍCITO de piezas pero todavía no
  sabes el producto/modelo exacto (por eso no puedes llamar agregar_item
  todavía), llama de inmediato a actualizar_pedido con
  cantidad_pendiente=ese número, en ese mismo turno -- no esperes a
  aclarar el modelo para guardarlo.
- Revisa el bloque "[📌 CANTIDAD YA CONFIRMADA POR EL CLIENTE]" si aparece
  en tu contexto: significa que ya tienes ese número guardado -- NUNCA
  vuelvas a preguntar cuántas piezas quiere, usa ese mismo número en
  cuanto llames a agregar_item con el producto ya confirmado.

🔧 REGLA -- NUNCA INVENTES UNA OPCIÓN QUE NO OFRECISTE, SOBRE TODO CON
TIPO_ENTREGA (bug real detectado: a un cliente de Guadalajara -- fuera de
la zona de cobertura, solo puede recibir por DHL -- se le preguntó "¿quieres
que te arme el resumen con entrega por DHL o prefieres recoger en local?".
El cliente respondió solo "si", sin decir cuál de las dos. El bot respondió
"perfecto, entonces queda la entrega en punto de entrega" -- una TERCERA
opción que ni siquiera se había ofrecido, y que además es imposible para un
cliente foráneo, porque los puntos de entrega son lugares y horarios fijos
dentro del área metropolitana de Monterrey):
- Cuando le des a elegir al cliente entre 2 o más opciones (tipo de
  entrega, colores, variantes, fechas, lo que sea) y su respuesta es
  ambigua ("si", "ok", "está bien", "como sea") sin decir cuál de ellas,
  NUNCA asumas ni inventes cuál eligió -- pregúntale explícitamente cuál de
  las opciones que le diste prefiere.
- Nunca respondas con una opción que no esté entre las que tú mismo
  ofreciste en el mensaje anterior.
- Los PUNTOS DE ENTREGA (Soriana Fresnos, Sendero Escobedo, Merco Pueblo
  Nuevo, Estación Metro Mitras) son EXCLUSIVOS para clientes dentro del
  área metropolitana de Monterrey. Si el cliente ya dijo que vive fuera de
  esa zona (otra ciudad o estado, envío por DHL), NUNCA le ofrezcas ni le
  confirmes "punto de entrega" como su tipo_entrega -- para esos clientes
  solo hay dos opciones válidas: envío nacional por DHL, o que el cliente
  recoja él mismo en el local.
- Los PEDIDOS URGENTES tampoco aplican para envío fuera de zona (DHL),
  porque urgente solo se puede entregar en el local. Si un cliente foráneo
  pregunta por entrega urgente, dile claramente que no aplica para su caso
  a menos que él pueda recoger en persona en el local -- no le ofrezcas
  "cambiar a punto de entrega" como forma de hacerlo urgente, esa
  combinación no existe.
- Recuerda también la regla de arriba: en cuanto le digas algo concreto al
  cliente (incluyendo a qué tipo de entrega quedó su pedido), llama a
  actualizar_pedido en ese mismo turno -- nunca digas en el chat que "ya
  quedó" algo sin de verdad haberlo guardado.

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

{seccion_fotos_producto(catalogo_imagenes=CATALOGO_IMAGENES_PRODUCTO)}

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

BASE DE CONOCIMIENTO:

{conocimiento}

-----------------------------------------------------------
🔧 A PARTIR DE AQUÍ: información específica de ESTA conversación
(esto sí cambia en cada mensaje, todo lo de arriba es fijo).
-----------------------------------------------------------

Hoy es {dia_semana} {fecha}.
La hora actual es {hora} (hora de Monterrey, México).

INFORMACIÓN QUE YA SE LE ENVIÓ A ESTE CLIENTE EN MENSAJES ANTERIORES
(no la repitas salvo que el cliente la pida explícitamente de nuevo):

{resumen_info_enviada(info_enviada)}

ESTADO ACTUAL DEL PEDIDO DE ESTE CLIENTE (desde base de datos):

{resumen}
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
                    "fecha_evento": {"type": "string", "description": "Fecha de entrega acordada, SIEMPRE en formato DD/MM/YYYY (ej. 18/09/2026). Nunca otro formato."},
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
                    "cantidad_pendiente": {
                        "type": "integer",
                        "description": (
                            "Úsala en cuanto el cliente te diga un número EXPLÍCITO "
                            "de piezas pero TODAVÍA no sepas el producto/modelo exacto "
                            "(por eso no puedes llamar agregar_item todavía, que "
                            "necesita producto y cantidad juntos). Guarda aquí ese "
                            "número de inmediato para no olvidarlo mientras aclaras "
                            "el modelo/colores -- después, cuando llames a "
                            "agregar_item con el producto ya confirmado, usa este "
                            "mismo número como cantidad, sin volver a preguntarlo."
                        ),
                    },
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
                "No borra otros productos del pedido. "
                "⚠️ NO llames esta función hasta que el cliente te haya dicho un número "
                "EXPLÍCITO de piezas -- pregúntale primero '¿cuántas piezas te gustaría?' "
                "(o similar) en un mensaje normal y espera su respuesta. Nunca adivines "
                "ni pongas 1 (ni ningún otro número) por default solo porque no dijo la "
                "cantidad."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "producto": {"type": "string", "description": "Nombre del producto, ej. 'elefante de toalla'"},
                    "cantidad": {
                        "type": "integer",
                        "description": (
                            "Número de piezas que el cliente dijo EXPLÍCITAMENTE. "
                            "Nunca asumas 1 ni ningún otro valor -- si el cliente no ha "
                            "dicho un número, pregúntaselo primero y espera la respuesta "
                            "antes de llamar a esta función."
                        ),
                    },
                    "color_toalla": {"type": "string"},
                    "color_mono": {"type": "string"},
                    "color_velita": {"type": "string"},
                    "tipo_jaboncito": {"type": "string"},
                    "color_jaboncito": {"type": "string"},
                    "nombre_bebe": {"type": "string"},
                    "tarjetita": {"type": "string"},
                    "mono_personalizado": {
                        "type": "boolean",
                        "description": (
                            "SOLO para osito peluche llavero u osito toalla afelpada: "
                            "true si el cliente pidió un color de moño DISTINTO al que "
                            "ya viene incluido de fábrica en ese color de osito (tiene "
                            "cargo extra de $2.00 c/u). false o no enviar si no aplica."
                        ),
                    },
                    "con_bolsa": {
                        "type": "boolean",
                        "description": (
                            "SOLO para encendedores o destapadores: true si el cliente "
                            "quiere el producto CON bolsa de celofán (cuesta $1.00 extra "
                            "c/u), false si lo quiere SIN bolsa (precio base). Siempre "
                            "pregúntale al cliente cuál prefiere antes de cotizar estos "
                            "productos -- no asumas."
                        ),
                    },
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
                    "cantidad": {
                        "type": "integer",
                        "description": (
                            "Solo envía este campo si el cliente dio un número EXPLÍCITO "
                            "de piezas nuevo. No la incluyas si no la mencionó -- no "
                            "asumas ni repitas un número que no confirmó en este mensaje."
                        ),
                    },
                    "color_toalla": {"type": "string"},
                    "color_mono": {"type": "string"},
                    "color_velita": {"type": "string"},
                    "tipo_jaboncito": {"type": "string"},
                    "color_jaboncito": {"type": "string"},
                    "nombre_bebe": {"type": "string"},
                    "tarjetita": {"type": "string"},
                    "mono_personalizado": {
                        "type": "boolean",
                        "description": (
                            "SOLO para osito peluche llavero u osito toalla afelpada: "
                            "true si el cliente pidió un color de moño DISTINTO al que "
                            "ya viene incluido de fábrica en ese color de osito (tiene "
                            "cargo extra de $2.00 c/u). false o no enviar si no aplica."
                        ),
                    },
                    "con_bolsa": {
                        "type": "boolean",
                        "description": (
                            "SOLO para encendedores o destapadores: true si el cliente "
                            "quiere el producto CON bolsa de celofán (cuesta $1.00 extra "
                            "c/u), false si lo quiere SIN bolsa (precio base). Siempre "
                            "pregúntale al cliente cuál prefiere antes de cotizar estos "
                            "productos -- no asumas."
                        ),
                    },
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


if CATALOGO_IMAGENES_PRODUCTO:
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
                        "enum": list(CATALOGO_IMAGENES_PRODUCTO.keys()),
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


# 🆕 Bug real detectado (17 ago 2026, prueba real de Israel): un pedido
# de 1 SOLO osito terminó con tipo_entrega="punto_de_entrega" (que según
# la política del negocio requiere mínimo 25 piezas -- ver
# "Entregas y envíos.txt") -- el modelo lo confirmó, armó fecha, punto y
# hasta el total, y nadie lo cachó hasta que el cliente preguntó
# directamente "¿aunque sea 1 osito me lo llevan a ese punto de
# entrega?". La regla ya estaba en la Base de Conocimiento, pero nada en
# el código la hacía cumplir -- quedaba 100% a que el modelo se acordara.
# Estas dos funciones son el mismo patrón que ya se usa para
# es_pedido_urgente/resolver_costo_envio: Python decide la regla
# verificable, no el modelo.
def _cantidad_total_pedido(pedido):
    """Suma total de piezas del pedido -- usa 'items' si existe (varios
    productos), si no cae al campo plano 'cantidad' del esquema de un
    solo producto."""
    items = pedido.get("items")
    if items and isinstance(items, list):
        return sum(float(it.get("cantidad") or 0) for it in items)
    return float(pedido.get("cantidad") or 0)


MINIMO_PIEZAS_PUNTO_DE_ENTREGA = 25


def _es_tipo_entrega_punto_de_entrega(valor):
    """Compara de forma tolerante -- el modelo no tiene un enum fijo para
    tipo_entrega (a diferencia de mostrar_foto_producto), así que puede
    mandar 'punto_de_entrega', 'punto de entrega', 'Punto De Entrega',
    etc. Se normaliza quitando espacios/guiones/acentos antes de
    comparar."""
    if not valor:
        return False
    norm = re.sub(r"[^a-z0-9]", "", normalizar_producto_clave(str(valor)))
    return "puntodeentrega" in norm or norm == "punto"


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
        "cantidad_pendiente",
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

    # 🔧 Urgencia determinística -- ver es_pedido_urgente() arriba. En
    # cuanto se sepa fecha_evento, Python decide si es urgente o no
    # comparando contra la fecha real de HOY, sin importar qué haya
    # decidido el modelo. Esto sobreescribe cualquier es_urgente/urgente
    # que el modelo haya puesto (bug real: confundió una fecha a más de
    # un mes de distancia con una urgente).
    if "fecha_evento" in campos_modificados:
        urgente_real = es_pedido_urgente(pedido.get("fecha_evento"))
        if urgente_real is not None and pedido.get("es_urgente") != urgente_real:
            pedido["es_urgente"] = urgente_real
            pedido["urgente"] = urgente_real
            if "es_urgente" not in campos_modificados:
                campos_modificados.append("es_urgente")
            print(f"📅 Urgencia recalculada para fecha_evento={pedido.get('fecha_evento')!r}: es_urgente={urgente_real}")

    print("📝 Pedido (meta) actualizado:", {k: pedido.get(k) for k in campos_modificados})

    # 🔧 Costo de envío determinístico -- ver COSTOS_ENVIO_MUNICIPIO. En
    # cuanto se sepa el municipio, Python decide el costo real, sin
    # importar qué monto haya dicho el modelo (igual que ya se hace con
    # aplicar_precio_oficial para productos).
    if "municipio" in campos_modificados and pedido.get("tipo_entrega") != "local":
        costo_oficial = resolver_costo_envio(pedido.get("municipio"))
        if costo_oficial is not None:
            if pedido.get("costo_envio") != costo_oficial:
                pedido["costo_envio"] = costo_oficial
                if "costo_envio" not in campos_modificados:
                    campos_modificados.append("costo_envio")
            pedido["_envio_fuera_de_zona"] = False
        else:
            # Municipio no reconocido -- fuera de zona, requiere el precio
            # especial de DHL y pedido 100% liquidado antes de enviarse.
            # No se asume $300 en automático porque además cambia la
            # condición de pago (liquidado, no solo anticipo).
            pedido["_envio_fuera_de_zona"] = True

    return campos_modificados


def _validar_colores_item(item):
    campos_color = ("color_toalla", "color_mono", "color_velita", "color_jaboncito")
    pref = item.get("producto") or ""
    for campo in campos_color:
        if item.get(campo) and not color_es_valido(item[campo], pref):
            print(f"🚫 Color inválido rechazado: {campo}={item[campo]}")
            item[campo] = None
    # 🔧 Validación extra para osito toalla afelpada: la pareja
    # toalla+moño debe ser una de las 6 combinaciones oficiales, no
    # cualquier combinación de colores individualmente válidos.
    prod_norm = normalizar_producto_clave(pref)
    if "afelpad" in prod_norm:
        ct, cm = item.get("color_toalla"), item.get("color_mono")
        if ct and cm and not pareja_afelpada_es_valida(ct, cm):
            print(f"🚫 Pareja de colores inválida para afelpada: toalla={ct}, moño={cm}")
            item["color_toalla"] = None
            item["color_mono"] = None
            item["_pareja_afelpada_invalida"] = True
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

    # 🔧 Respaldo del bug de "cantidad olvidada" (ver cantidad_pendiente
    # en actualizar_pedido / construir_system_prompt): si el modelo no
    # manda cantidad aquí pero ya había una cantidad pendiente guardada de
    # un turno anterior, se usa esa en vez de caer directo al default de 1.
    cantidad_solicitada = datos.get("cantidad") or pedido.get("cantidad_pendiente")

    existing = _buscar_item(pedido, producto)
    if existing:
        # sumar cantidad si viene, actualizar colores
        if cantidad_solicitada:
            existing["cantidad"] = int(cantidad_solicitada)
        for k in ("color_toalla", "color_mono", "color_velita", "tipo_jaboncito",
                  "color_jaboncito", "nombre_bebe", "tarjetita", "mono_personalizado", "con_bolsa"):
            if datos.get(k) not in (None, ""):
                existing[k] = datos[k]
        aplicar_precio_oficial(existing)
        _validar_colores_item(existing)
        pedido["producto"] = existing.get("producto")
        pedido["cantidad"] = existing.get("cantidad")
        pedido["precio_unitario"] = existing.get("precio_unitario")
        pedido["cantidad_pendiente"] = None
        print("📝 Item actualizado (vía agregar):", existing)
        return ["items"]

    item = {
        "producto": producto,
        "cantidad": int(cantidad_solicitada or 1),
    }
    for k in ("color_toalla", "color_mono", "color_velita", "tipo_jaboncito",
              "color_jaboncito", "nombre_bebe", "tarjetita", "mono_personalizado", "con_bolsa"):
        if datos.get(k) not in (None, ""):
            item[k] = datos[k]
    # IGNORAR cualquier precio que mande el modelo
    aplicar_precio_oficial(item)
    _validar_colores_item(item)
    pedido["items"].append(item)
    pedido["producto"] = item["producto"]
    pedido["cantidad"] = item["cantidad"]
    pedido["precio_unitario"] = item.get("precio_unitario")
    pedido["cantidad_pendiente"] = None
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
        pedido["cantidad_pendiente"] = None
    for k in ("color_toalla", "color_mono", "color_velita", "tipo_jaboncito",
              "color_jaboncito", "nombre_bebe", "tarjetita", "mono_personalizado", "con_bolsa", "producto"):
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


# 🔧 (19 ago 2026, candado nuevo a pedido explícito de Israel) Caso real
# detectado: el bot preguntó "¿cuál es el monto que VAS A PAGAR de
# anticipo?" (a futuro), la clienta contestó solo "$50 MXN" (la cantidad
# que pensaba pagar, no que ya hubiera pagado), y el modelo de todos
# modos llamó a actualizar_pedido con anticipo_confirmado=true, dándole
# las gracias por un anticipo que nunca llegó a pagar (nunca se le
# mandaron ni los datos bancarios). La regla en el prompt (sección
# "REGLA DE SEGURIDAD Y CIERRE AUTOMÁTICO") ya decía que el texto debe
# confirmar explícitamente que YA pagó/transfirió -- pero esa regla vive
# solo en el prompt, y el modelo puede (y en este caso lo hizo) no
# seguirla. Este candado la hace cumplir también por código: para una
# confirmación por TEXTO (no imagen), el mensaje del cliente en este
# mismo turno debe contener una palabra de pago YA REALIZADO ("ya
# pagué", "ya transferí", "ya deposité", "ya mandé el comprobante",
# etc.) -- una cifra sola ("$50 MXN") ya no es suficiente.
_PATRON_PAGO_YA_REALIZADO = re.compile(
    r"\bya\b[^.\n]{0,25}\b("
    r"transfer[íi]|transferido|transferencia\s+hecha|"
    r"deposit[ée]|depositado|dep[óo]sito\s+hecho|"
    r"pagu[ée]|pagado|"
    r"mand[ée]|enviado|envi[ée]|"
    r"hecho\s+el\s+pago|hice\s+el\s+pago|hice\s+la\s+transferencia|"
    r"hice\s+el\s+dep[óo]sito|qued[óo]\s+(hecho|pagado)|"
    r"est[áa]\s+(hecho|pagado)"
    r")\b",
    re.IGNORECASE,
)


# 🔧 (19 ago 2026) Ver uso en preguntar_ia -- detecta un número EXPLÍCITO
# de piezas en el mensaje del cliente (ej. "50 piezas", "40 ositos",
# "50 pzs") para no perderlo aunque el modelo tarde varios turnos en
# aclarar el producto exacto. A propósito NO matchea un número suelto
# sin palabra de cantidad/producto al lado (para no confundirlo con un
# precio, un teléfono, una fecha, etc.).
# 🔧 (21 ago 2026, bug real reportado por Israel con log de una clienta
# real) "pzs?\.?" solo cubría "pz"/"pzs" -- pero "pza" y "pzas" (con "a")
# son de las abreviaturas MÁS comunes en México ("30 pzas") y no
# matcheaban nada, así que ese candado no se activaba. La clienta escribió
# "30 pzas", el modelo lo entendió por su cuenta en el texto de respuesta
# pero NUNCA llamó a la función para guardarlo -- el candado de respaldo
# debía haberlo agarrado y no pudo. Ahora "pz(?:as?|s)?\.?" cubre las 4
# formas: pz, pzs, pza, pzas (con o sin punto).
_PATRON_CANTIDAD_EXPLICITA = re.compile(
    r"\b(\d{1,4})\s*(?:x\s*)?(piezas?|pz(?:as?|s)?\.?|unidades?|ositos?|ositas?|osos?)\b",
    re.IGNORECASE,
)


# 🔧 (19 ago 2026, bug real reportado por Israel) Caso real: la clienta
# dijo "Oso color celeste" (sin mencionar jaboncito para nada) y el
# modelo agregó al pedido "osito CON jaboncito" por su cuenta -- en
# contra de la regla ya escrita en la Base de Conocimiento
# ("Trato al cliente/035_Variantes_De_Producto.txt": "No asumas... 'con
# jabón' ni ninguna variante por defecto"). Igual que con el anticipo,
# la regla ya existía en el prompt pero el modelo no la siguió esa vez
# -- este candado la hace cumplir también por código.
_PRODUCTOS_OSITO_CON_VARIANTE_JABONCITO = (
    "osito con jaboncito", "osito sencillo sin jabon", "osito sencillo sin jaboncito",
)
_PALABRAS_JABONCITO = ("jaboncito", "jabon", "sencillo")


def _requiere_confirmar_variante_jaboncito(producto):
    """True si 'producto' es una de las variantes de osito que dependen
    de que el cliente haya elegido con/sin jaboncito (no aplica a otros
    productos como el kit oración+velita, que no lleva jaboncito)."""
    clave = normalizar_producto_clave(producto or "")
    if not clave:
        return False
    return any(normalizar_producto_clave(p) in clave for p in _PRODUCTOS_OSITO_CON_VARIANTE_JABONCITO)


def _variante_jaboncito_fue_mencionada(texto_cliente, sesion):
    """True si en el mensaje de este turno o en los últimos turnos de la
    conversación (los del CLIENTE, no los del bot) se mencionó alguna
    palabra relacionada a la variante con/sin jaboncito. Se revisa
    también la conversación reciente (no solo el mensaje de este turno)
    para no bloquear el caso legítimo de "el cliente ya dijo 'con
    jaboncito' hace 1-2 turnos y ahora solo está dando el color/cantidad"."""
    textos = []
    if texto_cliente:
        textos.append(texto_cliente)
    mensajes = (sesion or {}).get("messages") or []
    for m in mensajes[-8:]:
        if m.get("role") != "user":
            continue
        contenido = m.get("content")
        if isinstance(contenido, str):
            textos.append(contenido)
        elif isinstance(contenido, list):
            for parte in contenido:
                if isinstance(parte, dict) and parte.get("type") == "text":
                    textos.append(parte.get("text") or "")
    texto_junto = normalizar_producto_clave(" ".join(t for t in textos if t))
    return any(p in texto_junto for p in _PALABRAS_JABONCITO)


def _cliente_confirmo_pago_ya_realizado_por_texto(texto_cliente):
    """True si el texto del cliente (este turno) dice explícitamente que
    YA pagó/transfirió, no solo que va a pagar o cuánto piensa pagar."""
    if not texto_cliente:
        return False
    return bool(_PATRON_PAGO_YA_REALIZADO.search(texto_cliente))


def ejecutar_tool_call(tool_call, sesion, numero, pedido, canal="whatsapp", pagina_id=None, texto_cliente=None):
    name = tool_call.function.name
    args = tool_call.function.arguments

    if name == "actualizar_pedido":
        ya_estaba_confirmado = pedido.get("anticipo_confirmado") is True
        tipo_entrega_previo = pedido.get("tipo_entrega")
        campos_modificados = aplicar_actualizacion_pedido(pedido, args)

        # 🔧 Mínimo de 25 piezas para punto de entrega (bug real, ver nota
        # junto a _es_tipo_entrega_punto_de_entrega arriba). Se revisa
        # justo aquí, apenas se intenta poner/cambiar tipo_entrega, para
        # que nunca avance ni un turno más con una entrega que no aplica.
        if "tipo_entrega" in campos_modificados and _es_tipo_entrega_punto_de_entrega(pedido.get("tipo_entrega")):
            cantidad_total = _cantidad_total_pedido(pedido)
            if cantidad_total < MINIMO_PIEZAS_PUNTO_DE_ENTREGA:
                pedido["tipo_entrega"] = tipo_entrega_previo
                campos_modificados = [c for c in campos_modificados if c != "tipo_entrega"]
                print(f"🚨 Se bloqueó tipo_entrega=punto_de_entrega: el pedido tiene "
                      f"{cantidad_total:.0f} piezas, se requieren {MINIMO_PIEZAS_PUNTO_DE_ENTREGA}+")
                return (
                    f"BLOQUEADO: no se puede entregar en punto de entrega -- el pedido "
                    f"tiene {cantidad_total:.0f} pieza(s) y se requieren mínimo "
                    f"{MINIMO_PIEZAS_PUNTO_DE_ENTREGA} para esa opción. Explícale esto al "
                    f"cliente con amabilidad y ofrécele entrega en local o a domicilio en su lugar "
                    f"-- NO confirmes ni menciones el punto de entrega como si ya "
                    f"quedara así.",
                    campos_modificados,
                    False,
                )

        # 🔧 Bug real detectado (18 ago 2026, cliente de Guadalajara): con
        # el municipio confirmado fuera de zona (_envio_fuera_de_zona,
        # requiere DHL), "punto de entrega" nunca es una opción válida --
        # son lugares y horarios fijos solo dentro del área metropolitana
        # de Monterrey. Mismo patrón que el bloqueo de arriba, esta vez
        # por zona en lugar de por cantidad de piezas.
        if "tipo_entrega" in campos_modificados and _es_tipo_entrega_punto_de_entrega(pedido.get("tipo_entrega")) and pedido.get("_envio_fuera_de_zona"):
            pedido["tipo_entrega"] = tipo_entrega_previo
            campos_modificados = [c for c in campos_modificados if c != "tipo_entrega"]
            print(f"🚨 Se bloqueó tipo_entrega=punto_de_entrega: municipio "
                  f"'{pedido.get('municipio')}' está fuera de zona (requiere DHL).")
            return (
                f"BLOQUEADO: no se puede entregar en punto de entrega -- el municipio "
                f"'{pedido.get('municipio')}' está fuera de la zona de cobertura (requiere "
                f"envío por DHL). Los puntos de entrega son solo para el área metropolitana "
                f"de Monterrey. Para este cliente solo hay dos opciones válidas: envío "
                f"nacional por DHL a domicilio, o que él mismo recoja en persona en el local "
                f"-- pregúntale cuál prefiere, no asumas ninguna.",
                campos_modificados,
                False,
            )

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
            # 🔧 CAPA EXTRA -- red de seguridad de respaldo para el mínimo de
            # 25 piezas en punto de entrega (ver bloqueo arriba, apenas se
            # intenta poner tipo_entrega). Esto cubre el caso de que
            # tipo_entrega ya haya quedado guardado como "punto de entrega"
            # de un turno anterior (ej. antes de que existiera este
            # bloqueo, o por cualquier otra vía) y el modelo intente
            # confirmar el anticipo sin volver a tocar tipo_entrega en este
            # mismo turno -- nunca debe dejarse pasar un anticipo confirmado
            # sobre una entrega que no aplica.
            if _es_tipo_entrega_punto_de_entrega(pedido.get("tipo_entrega")):
                cantidad_total_anticipo = _cantidad_total_pedido(pedido)
                if cantidad_total_anticipo < MINIMO_PIEZAS_PUNTO_DE_ENTREGA:
                    pedido["anticipo_confirmado"] = False
                    print(f"🚨 Se bloqueó confirmación de anticipo: tipo_entrega=punto_de_entrega con "
                          f"{cantidad_total_anticipo:.0f} piezas, se requieren {MINIMO_PIEZAS_PUNTO_DE_ENTREGA}+")
                    return (
                        f"BLOQUEADO: no se puede confirmar el anticipo -- el pedido tiene "
                        f"{cantidad_total_anticipo:.0f} pieza(s) y la entrega en punto de entrega "
                        f"requiere mínimo {MINIMO_PIEZAS_PUNTO_DE_ENTREGA}. Explícale esto al cliente "
                        f"y ofrécele entrega en local o a domicilio antes de continuar con el anticipo.",
                        campos_modificados,
                        False,
                    )
                # 🔧 Mismo respaldo que arriba, ahora por zona (ver bloqueo
                # de tipo_entrega=punto_de_entrega + _envio_fuera_de_zona):
                # cubre el caso de que "punto de entrega" haya quedado
                # guardado de un turno anterior para un cliente fuera de zona.
                if pedido.get("_envio_fuera_de_zona"):
                    pedido["anticipo_confirmado"] = False
                    print(f"🚨 Se bloqueó confirmación de anticipo: tipo_entrega=punto_de_entrega con "
                          f"municipio '{pedido.get('municipio')}' fuera de zona.")
                    return (
                        f"BLOQUEADO: no se puede confirmar el anticipo -- el municipio "
                        f"'{pedido.get('municipio')}' está fuera de la zona de cobertura y un "
                        f"punto de entrega no aplica para ese caso. Pregúntale al cliente si "
                        f"prefiere envío por DHL a domicilio o recoger él mismo en el local "
                        f"antes de continuar con el anticipo.",
                        campos_modificados,
                        False,
                    )
            _tot_check = pedido_manager.calcular_total(borrador=pedido)
            items_actuales = pedido.get("items") if isinstance(pedido.get("items"), list) else []
            # 🔧 CORREGIDO (bug real detectado en pruebas): el bloqueo de
            # arriba solo cachaba productos CON precio pendiente -- pero
            # si el modelo confirmaba el anticipo sin haber agregado
            # NINGÚN producto todavía (pedido vacío), el total daba $0.00
            # "limpio" (no "incompleto"), así que este bloqueo no se
            # activaba. Resultado real visto en pruebas: un anticipo de
            # $200 se confirmó para "1 x Producto sin nombre @ $0.00 =
            # $0.00" -- una venta fantasma sin ningún producto real, que
            # además silenció al bot con ese cliente sin motivo. Ahora
            # también se bloquea si no hay ningún item en el pedido, o si
            # el total da $0 o menos.
            if _tot_check.get("incompleto") or not items_actuales or _tot_check.get("total", 0) <= 0:
                pedido["anticipo_confirmado"] = False
                productos = ", ".join(_tot_check.get("productos_sin_precio") or [])
                motivo = productos if productos else "el pedido no tiene ningún producto agregado todavía"
                print(f"🚨 Se bloqueó confirmación de anticipo: {motivo} (total=${_tot_check.get('total', 0):.2f})")
                return (
                    f"BLOQUEADO: no se puede confirmar el anticipo todavía -- {motivo}. "
                    f"Primero agrega los productos del pedido con agregar_item antes de "
                    f"registrar cualquier pago. Dile al cliente que necesitas confirmar "
                    f"primero qué está pidiendo antes de procesar el comprobante.",
                    campos_modificados,
                    False,
                )
            # 🔧 CAPA EXTRA (bug real con clienta real): antes, con una
            # imagen de por medio, el modelo quedaba obligado a decidir
            # algo (tool_choice forzado) -- y llegó a confirmar un
            # anticipo real solo porque la clienta reaccionó con un
            # sticker de 👍, sin haber mandado ningún comprobante. La
            # causa raíz (Messenger clasifica el sticker de "Me gusta"
            # como si fuera una foto) ya se corrigió en el webhook, pero
            # esta es la red de seguridad de respaldo: nunca confirmar
            # el anticipo si no hay ni un monto ni una descripción de
            # comprobante registrados.
            if not pedido.get("monto_anticipo") and not (pedido.get("comprobante") or "").strip():
                pedido["anticipo_confirmado"] = False
                print("🚨 Se bloqueó confirmación de anticipo: no hay monto_anticipo ni comprobante registrados")
                return (
                    "BLOQUEADO: no se puede confirmar el anticipo sin un monto o una "
                    "descripción real del comprobante de pago. Si el cliente solo "
                    "reaccionó con un emoji/sticker o dijo que 'ya va a pagar', eso NO "
                    "es un comprobante -- espera a que mande el comprobante real "
                    "(captura de transferencia, foto del depósito, etc.) antes de "
                    "confirmar.",
                    campos_modificados,
                    False,
                )
            # 🔧 CAPA EXTRA (bug real detectado 19 ago 2026, clienta real):
            # el bot preguntó "¿cuál es el monto que VAS A PAGAR?" (a
            # futuro) y la clienta solo contestó "$50 MXN" -- diciendo
            # cuánto pensaba pagar, no que ya hubiera pagado. El modelo
            # confirmó el anticipo de todos modos, sin haberle mandado
            # nunca los datos bancarios. La regla del prompt ya pide un
            # texto explícito de pago YA hecho para el camino de "texto"
            # (metodo_pago="confirmado por texto") -- este candado la
            # hace cumplir también por código, sin depender de que el
            # modelo se acuerde. Solo aplica al camino de texto: si vino
            # de una imagen (comprobante leído por Vision), ese camino ya
            # tiene su propia verificación arriba y no se toca aquí.
            if (pedido.get("metodo_pago") or "").strip().lower() == "confirmado por texto":
                if not _cliente_confirmo_pago_ya_realizado_por_texto(texto_cliente):
                    pedido["anticipo_confirmado"] = False
                    print(f"🚨 Se bloqueó confirmación de anticipo: el cliente no confirmó explícitamente "
                          f"un pago YA realizado por texto (mensaje: {texto_cliente!r})")
                    return (
                        "BLOQUEADO: no se puede confirmar el anticipo -- el cliente mencionó "
                        "un monto pero no dijo explícitamente que YA pagó o transfirió (ej. "
                        "'ya te transferí $50', 'ya deposité 50 pesos'). Si el cliente solo "
                        "dijo cuánto va a pagar o confirmó el resumen del pedido, eso NO "
                        "es lo mismo que confirmar el pago -- si no le has mandado los datos "
                        "bancarios todavía, mándaselos ahora; si ya se los mandaste, pídele "
                        "que te confirme cuando YA haya hecho la transferencia/depósito, o "
                        "que te mande la captura del comprobante.",
                        campos_modificados,
                        False,
                    )
        # 🔧 CORREGIDO (bug real detectado 19 ago 2026): Python ya
        # recalculaba es_urgente de forma determinística en cuanto se
        # sabía fecha_evento (ver bloque arriba, "Urgencia determinística"),
        # pero el resultado de esta tool call SIEMPRE regresaba solo "ok" --
        # el modelo nunca se enteraba de ese cálculo. Caso real: una
        # clienta pidió fecha para el 2 de septiembre (con hoy siendo 19
        # de agosto, muy lejos del límite real de urgencia), Python
        # guardó correctamente es_urgente=False, pero el modelo de todos
        # modos le dijo a la clienta "es un pedido urgente" con cargo
        # extra de $50 -- porque nunca vio la corrección, solo su propia
        # cuenta (equivocada) de fechas. Ahora, cuando fecha_evento se
        # tocó en este turno, el resultado de la tool call SIEMPRE
        # incluye el estado real de es_urgente calculado por Python, para
        # que el modelo no tenga que (ni deba) hacer esa cuenta por su
        # cuenta.
        mensaje_resultado = "ok"
        if "fecha_evento" in campos_modificados:
            fecha_minima_real = sumar_dias_habiles(
                datetime.now(ZONA_HORARIA_NEGOCIO).date(), 4
            )
            if pedido.get("es_urgente"):
                mensaje_resultado = (
                    "ok. 📅 CÁLCULO REAL DE URGENCIA (hecho por el sistema, no lo "
                    "recalcules tú): esta fecha de entrega SÍ es un PEDIDO URGENTE "
                    f"(antes del {fecha_minima_real.strftime('%d/%m/%Y')}, que es el "
                    "límite mínimo para un pedido normal). Aplica cargo extra de $50 "
                    "MXN y SOLO se puede entregar en el local -- avísale esto al "
                    "cliente."
                )
            else:
                mensaje_resultado = (
                    "ok. 📅 CÁLCULO REAL DE URGENCIA (hecho por el sistema, no lo "
                    "recalcules tú): esta fecha de entrega es un PEDIDO NORMAL, NO "
                    f"es urgente (el límite mínimo para ser urgente es antes del "
                    f"{fecha_minima_real.strftime('%d/%m/%Y')}, y hay tiempo de sobra). "
                    "NO le digas al cliente que es urgente ni le cobres el cargo extra "
                    "de $50, aunque tú hayas pensado lo contrario."
                )
            # 🔧 CORREGIDO (bug real detectado 21 ago 2026, transcript de
            # María Guadalupe): el modelo tiene que escribirle al cliente
            # el nombre del día de la semana de la fecha de entrega ("te
            # lo tengo para el viernes 28/08/2026"), pero calcular a mano
            # qué día de la semana cae una fecha es justo el tipo de
            # cuenta que a un modelo de lenguaje se le da mal -- en ese
            # caso real le dijo "viernes" a un 22/08/2026 que en realidad
            # era sábado, y después "jueves" a un 28/08/2026 que en
            # realidad era viernes. Igual que con es_urgente, Python
            # calcula aquí el día real con datetime (nunca se equivoca) y
            # se lo manda siempre que se toque fecha_evento, para que el
            # modelo no tenga que (ni deba) adivinarlo.
            fecha_evento_real = parsear_fecha_pedido(pedido.get("fecha_evento"))
            if fecha_evento_real is not None:
                dias_semana_fecha_evento = [
                    "lunes", "martes", "miércoles", "jueves",
                    "viernes", "sábado", "domingo",
                ]
                dia_semana_real = dias_semana_fecha_evento[fecha_evento_real.weekday()]
                mensaje_resultado += (
                    f" 📆 DÍA DE LA SEMANA REAL (calculado por el sistema, no lo "
                    f"calcules tú): {fecha_evento_real.strftime('%d/%m/%Y')} es "
                    f"{dia_semana_real}. Si le mencionas el día de la semana al "
                    "cliente, usa siempre este dato, nunca lo calcules de memoria."
                )
        return mensaje_resultado, campos_modificados, anticipo_recien_confirmado

    if name == "agregar_item":
        try:
            _args_agregar = json.loads(args) if args else {}
        except (json.JSONDecodeError, TypeError):
            _args_agregar = {}
        _producto_nuevo = (_args_agregar.get("producto") or "").strip()
        if (
            _requiere_confirmar_variante_jaboncito(_producto_nuevo)
            and not _buscar_item(pedido, _producto_nuevo)
            and not _variante_jaboncito_fue_mencionada(texto_cliente, sesion)
        ):
            print(f"🚨 Se bloqueó agregar_item: variante con/sin jaboncito no confirmada "
                  f"explícitamente por el cliente para '{_producto_nuevo}'")
            return (
                "BLOQUEADO: no se puede agregar este producto todavía -- el cliente no ha "
                "confirmado explícitamente si lo quiere CON jaboncito o SIN jaboncito "
                "(sencillo). No asumas ninguna de las dos opciones por default. Pregúntaselo "
                "directo antes de agregar el producto al pedido.",
                [],
                False,
            )
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

        if clave in CLAVES_IMAGEN_NO_OFRECER:
            return f"'{clave}' no es una foto de producto, no la mandes por aquí", [], False

        if clave in imagenes_enviadas:
            return "ya se le mandó esta foto antes en la conversación, no la repitas", [], False

        url_imagen = url_imagen_producto(clave)
        if not url_imagen:
            return f"no hay foto disponible para '{clave}', no ofrezcas una foto de esto", [], False

        nombre_mostrar = CATALOGO_IMAGENES[clave]["nombre_mostrar"]
        enviar_imagen_canal(numero, url_imagen, canal, caption=nombre_mostrar, pagina_id=pagina_id)
        imagenes_enviadas.add(clave)
        return "imagen enviada correctamente", [], False

    return "función desconocida", [], False


def _liberar_imagen_del_historial(historial, imagen_base64, texto_cliente):
    """🔧 (21 ago 2026, a pedido explícito de Israel -- fuga de memoria
    confirmada en Render, instancia caída por "out of memory") Cuando el
    cliente manda una foto (captura del catálogo, comprobante de pago,
    etc.), esa imagen completa en base64 se guardaba en el historial en
    RAM de esa sesión y se quedaba ahí PARA SIEMPRE mientras el proceso
    siguiera corriendo -- ya no se necesitaba después de que el modelo la
    usó para responder este turno, pero nadie la quitaba. Con varios
    clientes mandando fotos durante el día, esto se iba acumulando sin
    límite. Ahora, justo después de que el modelo ya respondió a este
    turno (o sea, ya no hace falta la imagen para nada más), se
    reemplaza por un texto ligero -- se pierde la imagen del historial
    en memoria (no de la conversación real, que sigue intacta en
    WhatsApp/Messenger), pero se conserva el texto que la acompañaba
    para que la conversación se siga leyendo con sentido.
    Debe llamarse UNA sola vez por turno, después de que se agregó la
    respuesta del asistente a `historial` -- así el turno del usuario que
    se va a "aligerar" es siempre historial[-2]."""
    if not imagen_base64 or len(historial) < 2:
        return
    historial[-2] = {
        "role": "user",
        "content": texto_cliente or "(el cliente mandó una imagen)",
    }


def preguntar_ia(numero, texto_cliente, imagen_base64=None, imagen_mime=None, canal="whatsapp", pagina_id=None):
    sesion = obtener_sesion(numero)
    historial = sesion["messages"]
    pedido = sesion["pedido"]
    info_enviada = sesion["info_enviada"]
    pedido_id = sesion.get("pedido_id")

    # 🔧 (19 ago 2026, bug real reportado por Israel) Respaldo
    # determinístico para "cantidad ya dicha mientras se aclara el
    # modelo" (ver REGLA en construir_system_prompt / cantidad_pendiente).
    # Caso real: la clienta escribió "necesito 50 piezas" ANTES de decir
    # qué producto quería; el modelo debía guardar cantidad_pendiente=50
    # de inmediato (así lo pide el prompt), pero no lo hizo -- resultado:
    # unos turnos después, ya con el producto aclarado, el bot le volvió
    # a preguntar cuántas piezas quería, como si nunca lo hubiera dicho.
    # Este candado no depende de que el modelo se acuerde: si el mensaje
    # del cliente trae un número explícito seguido de una palabra de
    # cantidad/producto ("50 piezas", "40 ositos", "50 pzs") y todavía no
    # hay ninguna cantidad_pendiente guardada, se guarda aquí mismo,
    # ANTES de construir el prompt -- así el recordatorio "[📌 CANTIDAD
    # YA CONFIRMADA...]" ya aparece desde este mismo turno.
    if texto_cliente and not pedido.get("cantidad_pendiente"):
        _match_cantidad = _PATRON_CANTIDAD_EXPLICITA.search(texto_cliente)
        if _match_cantidad:
            pedido["cantidad_pendiente"] = int(_match_cantidad.group(1))
            print(f"📌 Cantidad detectada de forma determinística en el mensaje del cliente: "
                  f"{pedido['cantidad_pendiente']} (texto: {texto_cliente!r})")

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
                    tool_call, sesion, numero, pedido, canal, pagina_id, texto_cliente=texto_cliente
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
        _liberar_imagen_del_historial(historial, imagen_base64, texto_cliente)

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
                # 🔧 Ahora sí se captura y guarda cached_tokens (viene en
                # prompt_tokens_details) -- sirve para confirmar en el
                # dashboard que el reordenamiento del prompt de verdad
                # está aprovechando el caché de OpenAI, no solo en teoría.
                detalles_prompt = getattr(uso, "prompt_tokens_details", None)
                tokens_cache = getattr(detalles_prompt, "cached_tokens", 0) if detalles_prompt else 0
                crm.registrar_uso_openai(
                    numero, MODELO,
                    getattr(uso, "prompt_tokens", None),
                    getattr(uso, "completion_tokens", None),
                    tokens_cache=tokens_cache,
                )
        except Exception as e:
            print("⚠️ No se pudo registrar uso de OpenAI:", repr(e))

        return texto

    texto = "Disculpa, dame un segundo y te confirmo 🙂"
    historial.append({"role": "assistant", "content": texto})
    _liberar_imagen_del_historial(historial, imagen_base64, texto_cliente)
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

    🔧 (19 ago 2026) Si VENDEDORA_WHATSAPP_NUMERO está configurado,
    también se le manda a ella este mismo mensaje (idéntico, mismo
    momento). Cada número se evalúa por separado: si falta uno de los
    dos, el otro igual recibe su aviso con normalidad.

    Usa como fuente principal el pedido YA GUARDADO en la base de datos
    (pedido_db, con sus items/entrega/pagos) porque es el registro más
    confiable -- pedido_ram (el borrador en RAM) se usa solo como
    respaldo si algo faltó en la BD. Si ni DALIA_WHATSAPP_NUMERO ni
    VENDEDORA_WHATSAPP_NUMERO están configurados, no hace nada (no rompe
    el resto del flujo).
    """
    if not DALIA_WHATSAPP_NUMERO and not VENDEDORA_WHATSAPP_NUMERO:
        print("⚠️ Ni DALIA_WHATSAPP_NUMERO ni VENDEDORA_WHATSAPP_NUMERO están configurados, no se pudo notificar a nadie")
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
    if DALIA_WHATSAPP_NUMERO:
        enviar_whatsapp(DALIA_WHATSAPP_NUMERO, mensaje)
    if VENDEDORA_WHATSAPP_NUMERO:
        enviar_whatsapp(VENDEDORA_WHATSAPP_NUMERO, mensaje)


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
# WHATSAPP vía YCLOUD (número de prueba, coexistencia)
# ===========================

def enviar_whatsapp_ycloud(numero, texto):
    headers = {"X-API-Key": YCLOUD_API_KEY, "Content-Type": "application/json"}
    data = {
        "from": YCLOUD_WHATSAPP_NUMBER,
        "to": _a_e164(numero),
        "type": "text",
        "text": {"body": texto},
    }
    try:
        r = requests.post(YCLOUD_API_URL, headers=headers, json=data, timeout=15)
        print("=" * 60)
        print("YCLOUD STATUS:", r.status_code)
        print("YCLOUD BODY:", r.text)
        print("=" * 60)
        return r
    except requests.RequestException as e:
        print("⚠️ Excepción enviando WhatsApp vía YCloud:", e)
        return None


def enviar_whatsapp_ycloud_imagen(numero, image_url, caption=""):
    headers = {"X-API-Key": YCLOUD_API_KEY, "Content-Type": "application/json"}
    data = {
        "from": YCLOUD_WHATSAPP_NUMBER,
        "to": _a_e164(numero),
        "type": "image",
        "image": {"link": image_url, "caption": caption},
    }
    try:
        r = requests.post(YCLOUD_API_URL, headers=headers, json=data, timeout=15)
        if r.status_code >= 400:
            print("⚠️ Error enviando imagen por WhatsApp (YCloud):", r.status_code, r.text)
        else:
            print(f"📤 [YCloud] Imagen enviada a {numero}: {image_url}")
        return r
    except requests.RequestException as e:
        print("⚠️ Excepción enviando imagen por WhatsApp (YCloud):", e)
        return None


def enviar_whatsapp_ycloud_documento(numero, url_documento, nombre_archivo, caption=""):
    headers = {"X-API-Key": YCLOUD_API_KEY, "Content-Type": "application/json"}
    data = {
        "from": YCLOUD_WHATSAPP_NUMBER,
        "to": _a_e164(numero),
        "type": "document",
        "document": {"link": url_documento, "caption": caption, "filename": nombre_archivo},
    }
    try:
        r = requests.post(YCLOUD_API_URL, headers=headers, json=data, timeout=15)
        if r.status_code >= 400:
            print("⚠️ Error enviando documento por WhatsApp (YCloud):", r.status_code, r.text)
        else:
            print(f"📄 [YCloud] Documento enviado a {numero}: {nombre_archivo}")
        return r
    except requests.RequestException as e:
        print("⚠️ Excepción enviando documento (YCloud):", e)
        return None


def descargar_media_ycloud(url_medio):
    """Las imágenes/audios que manda un cliente por el canal de YCloud
    llegan con una URL directa (a diferencia de Meta, que da un media_id
    privado que hay que resolver aparte). Se manda la API key por si el
    link la requiere para autorizar la descarga."""
    headers = {"X-API-Key": YCLOUD_API_KEY} if YCLOUD_API_KEY else {}
    try:
        r = requests.get(url_medio, headers=headers, timeout=20)
        if r.status_code >= 400:
            print("⚠️ Error descargando medio de YCloud:", r.status_code, r.text[:200])
            return None, None
        mime = r.headers.get("Content-Type", "application/octet-stream")
        return r.content, mime
    except requests.RequestException as e:
        print("⚠️ Excepción descargando medio de YCloud:", e)
        return None, None


def _a_e164(numero):
    """YCloud pide los números con '+' (E.164) en 'to'; el resto del bot
    guarda/compara números SIN '+' (para no romper CRM, SILENCIAR/
    REACTIVAR, dedup, etc. -- todo eso sigue igual)."""
    numero = (numero or "").strip()
    return numero if numero.startswith("+") else f"+{numero}"


# ===========================
# ENVIAR MENSAJE POR MESSENGER (Facebook Page)
# ===========================

def enviar_messenger(psid, texto, pagina_id=None):
    data = {
        "recipient": {"id": psid},
        "message": {"text": texto},
        "messaging_type": "RESPONSE",
    }
    try:
        r = requests.post(
            MESSENGER_GRAPH_URL,
            params={"access_token": token_para_pagina(pagina_id)},
            json=data,
            timeout=15,
        )
        if r.status_code >= 400:
            print("⚠️ Error enviando mensaje por Messenger:", r.status_code, r.text)
        return r
    except requests.RequestException as e:
        print("⚠️ Excepción enviando Messenger:", e)
        return None


def enviar_messenger_imagen(psid, image_url, pagina_id=None):
    data = {
        "recipient": {"id": psid},
        "message": {
            "attachment": {
                "type": "image",
                "payload": {"url": image_url, "is_reusable": True},
            }
        },
        "messaging_type": "RESPONSE",
    }
    try:
        r = requests.post(
            MESSENGER_GRAPH_URL,
            params={"access_token": token_para_pagina(pagina_id)},
            json=data,
            timeout=15,
        )
        if r.status_code >= 400:
            print("⚠️ Error enviando imagen por Messenger:", r.status_code, r.text)
        else:
            print(f"📤 Imagen enviada por Messenger a {psid}: {image_url}")
        return r
    except requests.RequestException as e:
        print("⚠️ Excepción enviando imagen por Messenger:", e)
        return None


# ===========================
# COMENTARIOS EN PUBLICACIONES DE LA PÁGINA (Facebook Feed)
# ===========================
# 🔧 Cuando alguien comenta en una publicación de la página, el bot NO
# vende dentro del comentario público (expondría precios/datos a
# cualquiera). En su lugar: (1) responde el comentario con un mensaje
# corto y genérico invitando a platicar por privado, y (2) manda una
# "respuesta privada" (private reply) -- una función especial de Meta
# que abre un chat de Messenger con quien comentó, aunque nunca le haya
# escrito antes a la página. Esa respuesta privada es el ÚNICO mensaje
# que se manda por este camino especial; en cuanto la persona conteste,
# ese mensaje ya llega como un evento normal de "messaging" (mismo
# webhook de siempre) y de ahí en adelante lo atiende
# procesar_mensaje_en_fondo igual que cualquier conversación de
# Messenger.

MENSAJE_RESPUESTA_PUBLICA_COMENTARIO = "¡Hola! 😊 Te escribimos por privado para ayudarte."


def enviar_respuesta_publica_comentario(comment_id, texto, pagina_id=None):
    """Responde PÚBLICAMENTE a un comentario (queda visible debajo del
    comentario original, como cualquier respuesta de la página)."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{comment_id}/comments"
    try:
        r = requests.post(
            url,
            params={"access_token": token_para_pagina(pagina_id)},
            json={"message": texto},
            timeout=15,
        )
        if r.status_code >= 400:
            print("⚠️ Error respondiendo comentario en público:", r.status_code, r.text)
        else:
            print(f"📤 Respuesta pública enviada al comentario {comment_id}")
        return r
    except requests.RequestException as e:
        print("⚠️ Excepción respondiendo comentario en público:", e)
        return None


def enviar_respuesta_privada_comentario(comment_id, texto, pagina_id=None):
    """Manda una 'respuesta privada' (private reply) al comentario --
    endpoint especial de Meta que abre un chat de Messenger con quien
    comentó, sin necesitar que esa persona le haya escrito antes a la
    página. Solo se puede usar como PRIMER mensaje hacia esa persona a
    partir de su comentario (Meta da una ventana de 7 días desde el
    comentario para poder usarlo)."""
    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{comment_id}/private_replies"
    try:
        r = requests.post(
            url,
            params={"access_token": token_para_pagina(pagina_id)},
            json={"message": texto},
            timeout=15,
        )
        if r.status_code >= 400:
            print("⚠️ Error mandando respuesta privada del comentario:", r.status_code, r.text)
        else:
            print(f"📤 Respuesta privada enviada por el comentario {comment_id}")
        return r
    except requests.RequestException as e:
        print("⚠️ Excepción mandando respuesta privada del comentario:", e)
        return None


def procesar_comentario_pagina(pagina_id_actual, comment_id, from_id, from_name, texto_comentario):
    """Punto de entrada para un comentario nuevo en una publicación de
    la página: responde en público (genérico, sin vender ahí) y manda
    la respuesta privada que abre el chat de Messenger con esa persona."""
    if not comment_id or not from_id:
        return
    if from_id == pagina_id_actual:
        # Es la propia página comentando/respondiendo (ej. un admin
        # respondió manual desde Meta Business Suite) -- no reaccionar
        # a esto, o el bot terminaría respondiéndose a sí mismo.
        return
    if ya_fue_procesado(f"comentario:{comment_id}"):
        print(f"🔁 Comentario duplicado ignorado: {comment_id}")
        return

    enviar_respuesta_publica_comentario(comment_id, MENSAJE_RESPUESTA_PUBLICA_COMENTARIO, pagina_id=pagina_id_actual)

    nombre = (from_name or "").split(" ")[0].strip()
    saludo = f"¡Hola {nombre}!" if nombre else "¡Hola!"
    mensaje_privado = (
        f"{saludo} Vi tu comentario"
        + (f" ({texto_comentario.strip()[:80]!r})" if texto_comentario and texto_comentario.strip() else "")
        + ". Con gusto te ayudo por aquí 😊 ¿Qué te gustaría saber?"
    )
    enviar_respuesta_privada_comentario(comment_id, mensaje_privado, pagina_id=pagina_id_actual)


def descargar_imagen_messenger(url_imagen):
    """Las imágenes que manda un cliente por Messenger llegan como una URL
    pública directa (a diferencia de WhatsApp, que da un media_id privado
    que hay que resolver con el token) -- no necesita autenticación."""
    try:
        r = requests.get(url_imagen, timeout=20)
        if r.status_code >= 400:
            print("⚠️ Error descargando imagen de Messenger:", r.status_code)
            return None, None
        mime = r.headers.get("Content-Type", "image/jpeg")
        return r.content, mime
    except requests.RequestException as e:
        print("⚠️ Excepción descargando imagen de Messenger:", e)
        return None, None


# 🆕 Bug real detectado (17 ago 2026): un cliente mandó un audio por
# Messenger y el bot respondió "solo puedo leer texto" -- el webhook de
# Messenger nunca reconocía attachments tipo "audio" (solo "image"), así
# que caía directo a "adjunto no soportado" sin siquiera intentar
# descargarlo. La transcripción en sí YA funcionaba bien para WhatsApp
# (ver audio_handler.transcribir_audio) -- lo único que faltaba era esta
# función para bajar el audio de Messenger, igual que ya existe para sus
# imágenes.
def descargar_audio_messenger(url_audio):
    """Los audios que manda un cliente por Messenger llegan igual que las
    imágenes: una URL pública directa en el webhook, sin necesitar
    autenticación. Mismo patrón que descargar_imagen_messenger."""
    try:
        r = requests.get(url_audio, timeout=20)
        if r.status_code >= 400:
            print("⚠️ Error descargando audio de Messenger:", r.status_code)
            return None, None
        # 🔧 Default "audio/mp4" (no "audio/ogg" como en WhatsApp) porque
        # Messenger manda sus notas de voz casi siempre en ese formato --
        # ver audio_handler._extension_desde_mime, que ya lo reconoce.
        mime = r.headers.get("Content-Type", "audio/mp4")
        return r.content, mime
    except requests.RequestException as e:
        print("⚠️ Excepción descargando audio de Messenger:", e)
        return None, None


def enviar_mensaje_canal(destinatario, texto, canal="whatsapp", pagina_id=None):
    """Dispatcher: manda el mensaje por el canal correcto según de dónde
    vino la conversación. Así el resto del código (procesar_mensaje_en_
    fondo y todo lo que ya funcionaba para WhatsApp) no necesita saber ni
    importarle qué canal es -- solo llama a esta función. pagina_id solo
    aplica a Messenger, para usar el token de la página correcta cuando
    hay más de una conectada."""
    if canal == "messenger":
        return enviar_messenger(destinatario, texto, pagina_id=pagina_id)
    if _usar_ycloud_en_este_hilo():
        return enviar_whatsapp_ycloud(destinatario, texto)
    return enviar_whatsapp(destinatario, texto)


def enviar_imagen_canal(destinatario, image_url, canal="whatsapp", caption="", pagina_id=None):
    if canal == "messenger":
        r = enviar_messenger_imagen(destinatario, image_url, pagina_id=pagina_id)
        # Messenger no soporta caption inline en la imagen (a diferencia
        # de WhatsApp) -- si hay caption, se manda como mensaje de texto
        # aparte justo después.
        if caption:
            enviar_messenger(destinatario, caption, pagina_id=pagina_id)
        return r
    if _usar_ycloud_en_este_hilo():
        return enviar_whatsapp_ycloud_imagen(destinatario, image_url, caption=caption)
    return enviar_whatsapp_imagen(destinatario, image_url, caption=caption)



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
# POLÍTICA DE PRIVACIDAD (pública)
# ===========================
# Requerida por Meta para poder publicar la app "chatbot ositos" (WhatsApp
# Business Messaging + Messenger from Meta). Debe ser accesible sin login,
# por eso NO usa DASHBOARD_PASSWORD como las rutas de /dashboard.

CONTACTO_PRIVACIDAD = os.getenv("CONTACTO_PRIVACIDAD_EMAIL", "italoisraelmtz42@outlook.com")
FECHA_ACTUALIZACION_PRIVACIDAD = "17 de agosto de 2026"


@app.route("/privacidad")
def politica_privacidad():
    html = f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Política de Privacidad — Recuerditos Dalia</title>
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; background:#faf7f5; margin:0; padding:24px; color:#333; line-height:1.55; }}
  .contenedor {{ max-width:720px; margin:0 auto; background:white; border-radius:12px; padding:32px 36px; box-shadow:0 1px 4px rgba(0,0,0,0.08); }}
  h1 {{ color:#c2185b; margin-bottom:4px; }}
  .subtitulo {{ color:#888; margin-top:0; margin-bottom:28px; font-size:14px; }}
  h2 {{ font-size:17px; color:#c2185b; margin-top:28px; margin-bottom:8px; }}
  p, li {{ font-size:15px; color:#444; }}
  a {{ color:#c2185b; }}
</style>
</head>
<body>
  <div class="contenedor">
    <h1>🧸 Recuerditos Dalia</h1>
    <p class="subtitulo">Política de Privacidad — Última actualización: {FECHA_ACTUALIZACION_PRIVACIDAD}</p>

    <p>Recuerditos Dalia es un negocio dedicado a la venta de recuerdos y regalos
    personalizados (ositos de tela, abanicos de mano y detalles similares), que
    atiende a sus clientes principalmente a través de WhatsApp y de Messenger
    en nuestra página de Facebook. Esta política explica qué información
    recopilamos cuando nos escribes y cómo la usamos.</p>

    <h2>1. Qué información recopilamos</h2>
    <p>Cuando nos escribes por WhatsApp o Messenger, podemos recopilar:</p>
    <ul>
      <li>Tu número de teléfono (WhatsApp) o el identificador de tu conversación de Messenger.</li>
      <li>Tu nombre, si lo compartes con nosotros.</li>
      <li>Los mensajes que nos envías, incluyendo texto e imágenes (por ejemplo, fotos de referencia para tu pedido o comprobantes de pago).</li>
      <li>Los detalles de tu pedido: producto, cantidad, fecha en que lo necesitas y, si aplica, dirección de entrega.</li>
    </ul>

    <h2>2. Cómo usamos tu información</h2>
    <p>Usamos tu información únicamente para:</p>
    <ul>
      <li>Responder tus preguntas y darle seguimiento a tu pedido.</li>
      <li>Procesar y confirmar pagos o anticipos.</li>
      <li>Contactarte sobre el estado de tu pedido.</li>
    </ul>
    <p>Parte de nuestras respuestas automáticas se generan con ayuda de un
    proveedor externo de inteligencia artificial (OpenAI), al cual se le
    envía el texto de la conversación únicamente para generar una respuesta.
    Este proveedor no usa tu información para fines distintos a los del
    servicio que nos presta.</p>

    <h2>3. Con quién compartimos tu información</h2>
    <p>No vendemos ni compartimos tu información personal con terceros para
    fines de mercadotecnia. Solo la compartimos con los proveedores
    necesarios para operar el servicio: Meta (WhatsApp y Messenger) para el
    envío de mensajes, OpenAI para generar respuestas automáticas, y
    nuestro proveedor de hosting para almacenar los datos de forma segura.</p>

    <h2>4. Cuánto tiempo conservamos tu información</h2>
    <p>Conservamos el historial de conversación y de pedidos mientras sea
    necesario para darte seguimiento y por motivos administrativos del
    negocio. Puedes solicitar la eliminación de tus datos en cualquier
    momento (ver sección 6).</p>

    <h2>5. Seguridad</h2>
    <p>Tomamos medidas razonables para proteger tu información, aunque
    ningún sistema de almacenamiento o transmisión de datos es 100%
    seguro.</p>

    <h2>6. Tus derechos</h2>
    <p>Puedes solicitarnos en cualquier momento que te digamos qué
    información tenemos sobre ti, que la corrijamos o que la eliminemos por
    completo. Para ejercer este derecho, escríbenos directamente por
    WhatsApp o Messenger, o al correo
    <a href="mailto:{CONTACTO_PRIVACIDAD}">{CONTACTO_PRIVACIDAD}</a>.</p>

    <h2>7. Menores de edad</h2>
    <p>Nuestros servicios están dirigidos a personas mayores de edad que
    realizan compras. No recopilamos intencionalmente información de
    menores de edad.</p>

    <h2>8. Cambios a esta política</h2>
    <p>Podemos actualizar esta política ocasionalmente. La fecha de la
    última actualización aparece al inicio de esta página.</p>

    <h2>9. Contacto</h2>
    <p>Para dudas sobre esta política de privacidad, escríbenos a
    <a href="mailto:{CONTACTO_PRIVACIDAD}">{CONTACTO_PRIVACIDAD}</a> o por
    WhatsApp/Messenger a través de nuestra página de Facebook
    "Recuerditos Dalia".</p>
  </div>
</body>
</html>"""
    return html


# ===========================
# DASHBOARD DEL NEGOCIO
# ===========================

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
if not DASHBOARD_PASSWORD:
    print("⚠️ DASHBOARD_PASSWORD no configurado -- el dashboard no va a "
          "estar accesible hasta que se configure (por seguridad, sin "
          "contraseña queda bloqueado por completo, no abierto).")

# Tipo de cambio USD->MXN para mostrar el gasto de OpenAI en pesos (el
# costo real que factura OpenAI siempre es en dólares). Verificado a
# mediados de agosto 2026 en ~17.10 -- como el dólar se mueve, si quieres
# más precisión agrega DASHBOARD_USD_MXN en Render con el valor del día.
USD_MXN = float(os.getenv("DASHBOARD_USD_MXN", "17.10"))


def _utc_a_hora_local(timestamp_str):
    """SQLite guarda CURRENT_TIMESTAMP en UTC -- esto lo convierte a hora
    de Monterrey para mostrarlo en el dashboard (los datos NO se
    modifican en la base, solo se convierten al desplegarlos)."""
    if not timestamp_str:
        return timestamp_str
    try:
        dt_utc = datetime.strptime(str(timestamp_str)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
        dt_local = dt_utc.astimezone(ZONA_HORARIA_NEGOCIO)
        return dt_local.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return timestamp_str


def _rango_fechas(periodo: str):
    """Devuelve (fecha_inicio, etiqueta) según el período pedido, en la
    zona horaria del negocio."""
    ahora = datetime.now(ZONA_HORARIA_NEGOCIO)
    hoy = ahora.date()
    if periodo == "hoy":
        inicio = datetime.combine(hoy, datetime.min.time(), tzinfo=ZONA_HORARIA_NEGOCIO)
        etiqueta = f"Hoy ({hoy.strftime('%d/%m/%Y')})"
    elif periodo == "mes":
        inicio = datetime.combine(hoy.replace(day=1), datetime.min.time(), tzinfo=ZONA_HORARIA_NEGOCIO)
        etiqueta = f"Este mes ({hoy.strftime('%B %Y')})"
    else:  # semana (default)
        inicio_semana = hoy - timedelta(days=hoy.weekday())
        inicio = datetime.combine(inicio_semana, datetime.min.time(), tzinfo=ZONA_HORARIA_NEGOCIO)
        etiqueta = f"Esta semana (desde {inicio_semana.strftime('%d/%m/%Y')})"
    # 🔧 CORREGIDO (bug real detectado): "inicio" se calcula en hora de
    # Monterrey, pero las fechas guardadas en la base de datos son UTC
    # (CURRENT_TIMESTAMP de SQLite siempre es UTC). Sin esta conversión,
    # "Hoy" comparaba medianoche de Monterrey directo contra timestamps
    # UTC -- 6 horas de diferencia, filtrando mal justo en los bordes
    # del período (por eso los pedidos de la madrugada/tarde se veían
    # en el período equivocado).
    inicio_utc = inicio.astimezone(ZoneInfo("UTC"))
    return inicio_utc.strftime("%Y-%m-%d %H:%M:%S"), etiqueta


@app.route("/dashboard")
def dashboard():
    # 🔒 Protección simple con contraseña por URL (?clave=...) -- no es
    # un sistema de login completo, pero evita que cualquiera con el
    # link del bot vea datos del negocio. Si no hay contraseña
    # configurada en Render, el dashboard queda bloqueado por completo
    # (más seguro que dejarlo abierto por accidente).
    clave_recibida = request.args.get("clave", "")
    if not DASHBOARD_PASSWORD or clave_recibida != DASHBOARD_PASSWORD:
        return "🔒 Acceso no autorizado. Agrega ?clave=TU_CLAVE a la URL.", 401

    periodo = request.args.get("periodo", "semana")
    if periodo not in ("hoy", "semana", "mes"):
        periodo = "semana"
    fecha_inicio, etiqueta_periodo = _rango_fechas(periodo)

    try:
        with database.get_db_connection() as conn:
            # --- Ingresos (pagos confirmados) ---
            fila = conn.execute(
                "SELECT COALESCE(SUM(monto), 0) as total, COUNT(*) as n FROM pagos "
                "WHERE confirmado = 1 AND fecha >= ?",
                (fecha_inicio,),
            ).fetchone()
            ingresos_total, ingresos_n = fila["total"], fila["n"]

            # --- Pedidos: total, urgentes vs normales, por canal ---
            fila = conn.execute(
                "SELECT COUNT(*) as n FROM pedidos WHERE fecha_creacion >= ?",
                (fecha_inicio,),
            ).fetchone()
            pedidos_total = fila["n"]

            fila = conn.execute(
                "SELECT COUNT(*) as n FROM pedidos WHERE fecha_creacion >= ? AND es_urgente = 1",
                (fecha_inicio,),
            ).fetchone()
            pedidos_urgentes = fila["n"]

            filas_canal_pedidos = conn.execute(
                "SELECT COALESCE(canal, 'whatsapp') as canal, COUNT(*) as n FROM pedidos "
                "WHERE fecha_creacion >= ? GROUP BY canal",
                (fecha_inicio,),
            ).fetchall()

            # --- Mensajes por canal ---
            filas_canal_msj = conn.execute(
                "SELECT COALESCE(canal, 'whatsapp') as canal, "
                "SUM(CASE WHEN emisor = 'bot' THEN 1 ELSE 0 END) as respondidos, "
                "COUNT(*) as total "
                "FROM historial_chat WHERE timestamp >= ? GROUP BY canal",
                (fecha_inicio,),
            ).fetchall()

            # 🆕 Clientes/conversaciones distintas atendidas (no confundir
            # con "mensajes respondidos" -- una sola conversación puede
            # tener 100 mensajes contestados, pero sigue siendo 1
            # cliente). Cuenta teléfonos distintos a los que el bot les
            # contestó al menos una vez en el período.
            filas_canal_clientes = conn.execute(
                "SELECT COALESCE(canal, 'whatsapp') as canal, "
                "COUNT(DISTINCT telefono) as clientes "
                "FROM historial_chat WHERE timestamp >= ? AND emisor = 'bot' "
                "GROUP BY canal",
                (fecha_inicio,),
            ).fetchall()

            # --- Productos más vendidos ---
            filas_productos = conn.execute(
                "SELECT pi.producto, SUM(pi.cantidad) as cantidad, SUM(pi.subtotal) as ingresos "
                "FROM pedido_items pi JOIN pedidos p ON pi.pedido_id = p.id "
                "WHERE p.fecha_creacion >= ? "
                "GROUP BY pi.producto ORDER BY cantidad DESC LIMIT 10",
                (fecha_inicio,),
            ).fetchall()

            # --- Entregas por municipio (domicilio) ---
            filas_municipios = conn.execute(
                "SELECT COALESCE(e.municipio, '(local o punto de entrega)') as lugar, COUNT(*) as n "
                "FROM entregas e JOIN pedidos p ON e.pedido_id = p.id "
                "WHERE p.fecha_creacion >= ? "
                "GROUP BY lugar ORDER BY n DESC",
                (fecha_inicio,),
            ).fetchall()

            # --- Gasto de OpenAI ---
            fila = conn.execute(
                "SELECT COALESCE(SUM(costo_estimado_usd), 0) as costo, "
                "COALESCE(SUM(tokens_entrada), 0) as tok_in, "
                "COALESCE(SUM(tokens_cache), 0) as tok_cache, "
                "COUNT(*) as llamadas "
                "FROM uso_openai WHERE timestamp >= ?",
                (fecha_inicio,),
            ).fetchone()
            openai_costo = fila["costo"]
            openai_llamadas = fila["llamadas"]
            pct_cache = round(100 * fila["tok_cache"] / fila["tok_in"], 1) if fila["tok_in"] else 0

            # --- Pedidos recientes (lista) ---
            # 🆕 (20 ago 2026, pedido explícito de Israel) Se agrega el
            # total de cada pedido -- misma cuenta que ya usa
            # calcular_total() y la tabla de "Desglose de pedidos" de
            # arriba: subtotal de items + $50 si es urgente + costo de
            # envío. Aplica igual sea que estés viendo Hoy, Esta semana o
            # Este mes, porque esta lista ya respeta ese mismo filtro de
            # período.
            filas_recientes = conn.execute(
                "SELECT p.folio, p.telefono, p.estado, p.es_urgente, COALESCE(p.canal, 'whatsapp') as canal, "
                "p.fecha_creacion, "
                "COALESCE((SELECT SUM(pi.subtotal) FROM pedido_items pi WHERE pi.pedido_id = p.id), 0) "
                "+ (CASE WHEN p.es_urgente THEN 50.0 ELSE 0.0 END) "
                "+ COALESCE((SELECT e.costo_envio FROM entregas e WHERE e.pedido_id = p.id), 0) as total "
                "FROM pedidos p WHERE p.fecha_creacion >= ? "
                "ORDER BY p.fecha_creacion DESC LIMIT 15",
                (fecha_inicio,),
            ).fetchall()

            # 🆕 (20 ago 2026, pedido explícito de Israel) Nombres de
            # Facebook de los clientes de Messenger de esta lista, para
            # mostrar "Juan Pérez" en vez del PSID -- un solo query para
            # los PSIDs de esta página, no uno por fila.
            psids_messenger = [
                r["telefono"] for r in filas_recientes
                if (r["canal"] or "whatsapp") == "messenger"
            ]
            nombres_messenger_cache = {}
            if psids_messenger:
                marcadores = ",".join("?" for _ in psids_messenger)
                filas_nombres = conn.execute(
                    f"SELECT psid, nombre FROM nombres_messenger WHERE psid IN ({marcadores})",
                    psids_messenger,
                ).fetchall()
                nombres_messenger_cache = {f["psid"]: f["nombre"] for f in filas_nombres}

            # 🆕 Venta total del período (valor de TODOS los pedidos creados,
            # ya sea que el anticipo/pago esté confirmado o no) -- separado
            # a propósito de "Ingresos confirmados" (que es solo el dinero
            # que ya se confirmó recibido). Antes el dashboard solo mostraba
            # "Ingresos confirmados" arriba y el valor total quedaba
            # escondido nada más como la suma de la tabla de productos, lo
            # cual generaba confusión (parecía que no cuadraban los
            # números, cuando en realidad son dos cosas distintas).
            fila = conn.execute(
                "SELECT COALESCE(SUM(pi.subtotal), 0) as total FROM pedido_items pi "
                "JOIN pedidos p ON pi.pedido_id = p.id WHERE p.fecha_creacion >= ?",
                (fecha_inicio,),
            ).fetchone()
            venta_total_periodo = fila["total"]

            # 🆕 (20 ago 2026, pedido explícito de Israel) Desglose por
            # pedido del período: envío, cargo urgente, anticipo ya
            # confirmado, saldo por pagar y total -- para verlo de un
            # vistazo sin tener que abrir cada pedido. Mismas fuentes que
            # ya usa calcular_total() en pedido_manager.py (subtotal de
            # items + $50 si es urgente + costo_envio de la entrega), para
            # que el total de aquí SIEMPRE cuadre con el que ve el cliente
            # en su resumen. "Por pagar" = total - envío - anticipo (el
            # envío y el anticipo ya se consideran cubiertos aparte).
            filas_desglose_pedidos = conn.execute(
                "SELECT p.folio, p.es_urgente, "
                "COALESCE(e.costo_envio, 0) as envio, "
                "COALESCE((SELECT SUM(pi.subtotal) FROM pedido_items pi WHERE pi.pedido_id = p.id), 0) as subtotal_items, "
                "COALESCE((SELECT SUM(pg.monto) FROM pagos pg WHERE pg.pedido_id = p.id AND pg.confirmado = 1), 0) as anticipo "
                "FROM pedidos p LEFT JOIN entregas e ON e.pedido_id = p.id "
                "WHERE p.fecha_creacion >= ? "
                "ORDER BY p.fecha_creacion DESC",
                (fecha_inicio,),
            ).fetchall()

            # 🆕 Histórico diario/semanal -- para poder ver "¿cuánto se
            # vendió ayer?" o "¿cómo nos fue la semana pasada?" sin tener
            # que ir cambiando de período uno por uno. Se trae una ventana
            # amplia (60 días) y se agrupa en Python porque las fechas se
            # guardan en UTC y hay que convertirlas a hora de Monterrey
            # ANTES de agrupar por día (agrupar directo en SQL con la
            # fecha en UTC corta los días mal, mismo bug que ya se había
            # corregido para "Hoy" más arriba).
            inicio_historico = (datetime.now(ZONA_HORARIA_NEGOCIO) - timedelta(days=60)) \
                .astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")

            filas_pagos_hist = conn.execute(
                "SELECT monto, fecha FROM pagos WHERE confirmado = 1 AND fecha >= ?",
                (inicio_historico,),
            ).fetchall()

            filas_ventas_hist = conn.execute(
                "SELECT pi.subtotal, p.fecha_creacion, p.id as pedido_id FROM pedido_items pi "
                "JOIN pedidos p ON pi.pedido_id = p.id WHERE p.fecha_creacion >= ?",
                (inicio_historico,),
            ).fetchall()

    except Exception as e:
        return f"Error consultando la base de datos: {e}", 500

    def _fecha_local(timestamp_str):
        """Igual que _utc_a_hora_local pero solo devuelve la fecha
        (YYYY-MM-DD) en hora de Monterrey, para poder agrupar por día."""
        convertido = _utc_a_hora_local(timestamp_str)
        return convertido[:10] if convertido else None

    def _inicio_semana_de(fecha_str):
        d = datetime.strptime(fecha_str, "%Y-%m-%d").date()
        return (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")

    # --- Agrupar ingresos confirmados y venta total por día ---
    ingresos_por_dia, venta_por_dia, pedidos_por_dia = {}, {}, {}
    for f in filas_pagos_hist:
        d = _fecha_local(f["fecha"])
        if d:
            ingresos_por_dia[d] = ingresos_por_dia.get(d, 0) + f["monto"]
    for f in filas_ventas_hist:
        d = _fecha_local(f["fecha_creacion"])
        if d:
            venta_por_dia[d] = venta_por_dia.get(d, 0) + f["subtotal"]
            pedidos_por_dia.setdefault(d, set()).add(f["pedido_id"])

    # --- Agrupar lo mismo, pero por semana (lunes a domingo) ---
    ingresos_por_semana, venta_por_semana, pedidos_por_semana = {}, {}, {}
    for f in filas_pagos_hist:
        d = _fecha_local(f["fecha"])
        if d:
            s = _inicio_semana_de(d)
            ingresos_por_semana[s] = ingresos_por_semana.get(s, 0) + f["monto"]
    for f in filas_ventas_hist:
        d = _fecha_local(f["fecha_creacion"])
        if d:
            s = _inicio_semana_de(d)
            venta_por_semana[s] = venta_por_semana.get(s, 0) + f["subtotal"]
            pedidos_por_semana.setdefault(s, set()).add(f["pedido_id"])

    # --- Armar las filas de la tabla diaria (últimos 14 días, hoy primero) ---
    hoy_local = datetime.now(ZONA_HORARIA_NEGOCIO).date()
    dias_semana_es = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
    filas_dias_tabla = []
    for i in range(14):
        d = hoy_local - timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        etiqueta = f"{dias_semana_es[d.weekday()]} {d.strftime('%d/%m')}" + (" (hoy)" if i == 0 else " (ayer)" if i == 1 else "")
        filas_dias_tabla.append({
            "etiqueta": etiqueta,
            "ingresos": ingresos_por_dia.get(d_str, 0),
            "venta_total": venta_por_dia.get(d_str, 0),
            "pedidos": len(pedidos_por_dia.get(d_str, set())),
        })

    # --- Armar las filas de la tabla semanal (últimas 8 semanas) ---
    inicio_semana_actual = hoy_local - timedelta(days=hoy_local.weekday())
    filas_semanas_tabla = []
    for i in range(8):
        inicio = inicio_semana_actual - timedelta(weeks=i)
        inicio_str = inicio.strftime("%Y-%m-%d")
        fin = inicio + timedelta(days=6)
        filas_semanas_tabla.append({
            "etiqueta": f"{inicio.strftime('%d/%m')} – {fin.strftime('%d/%m')}" + (" (esta semana)" if i == 0 else " (semana pasada)" if i == 1 else ""),
            "ingresos": ingresos_por_semana.get(inicio_str, 0),
            "venta_total": venta_por_semana.get(inicio_str, 0),
            "pedidos": len(pedidos_por_semana.get(inicio_str, set())),
        })

    def _fila_canal_pedidos(canal_nombre):
        for f in filas_canal_pedidos:
            if f["canal"] == canal_nombre:
                return f["n"]
        return 0

    def _fila_canal_msj(canal_nombre):
        for f in filas_canal_msj:
            if f["canal"] == canal_nombre:
                return f["respondidos"], f["total"]
        return 0, 0

    def _fila_canal_clientes(canal_nombre):
        for f in filas_canal_clientes:
            if f["canal"] == canal_nombre:
                return f["clientes"]
        return 0

    respondidos_wa, total_wa = _fila_canal_msj("whatsapp")
    respondidos_msg, total_msg = _fila_canal_msj("messenger")
    clientes_wa = _fila_canal_clientes("whatsapp")
    clientes_msg = _fila_canal_clientes("messenger")

    def _dinero_o_raya(monto):
        """Formatea un monto en dinero, o una raya si es $0 -- para que la
        tabla de desglose sea más fácil de escanear (menos "$0.00" repetido)."""
        return f"${monto:,.2f}" if monto else "—"

    filas_desglose_calc = []
    tot_envio = tot_urgente = tot_anticipo = tot_x_pagar = tot_total_desglose = 0.0
    for f in filas_desglose_pedidos:
        cargo_urgente = 50.0 if f["es_urgente"] else 0.0
        total_pedido = f["subtotal_items"] + cargo_urgente + f["envio"]
        x_pagar = total_pedido - f["envio"] - f["anticipo"]
        filas_desglose_calc.append({
            "folio": f["folio"],
            "envio": f["envio"],
            "urgente": cargo_urgente,
            "anticipo": f["anticipo"],
            "x_pagar": x_pagar,
            "total": total_pedido,
        })
        tot_envio += f["envio"]
        tot_urgente += cargo_urgente
        tot_anticipo += f["anticipo"]
        tot_x_pagar += x_pagar
        tot_total_desglose += total_pedido

    filas_desglose_html = "".join(
        f"<tr><td>{d['folio']}</td><td>{_dinero_o_raya(d['envio'])}</td>"
        f"<td>{_dinero_o_raya(d['urgente'])}</td><td>{_dinero_o_raya(d['anticipo'])}</td>"
        f"<td>${d['x_pagar']:,.2f}</td><td>${d['total']:,.2f}</td></tr>"
        for d in filas_desglose_calc
    ) or "<tr><td colspan='6'>Sin pedidos en este período</td></tr>"

    fila_total_desglose_html = (
        f"<tr style='font-weight:700;background:#faf0f3;'><td>Total</td>"
        f"<td>${tot_envio:,.2f}</td><td>${tot_urgente:,.2f}</td><td>${tot_anticipo:,.2f}</td>"
        f"<td>${tot_x_pagar:,.2f}</td><td>${tot_total_desglose:,.2f}</td></tr>"
        if filas_desglose_calc else ""
    )

    filas_productos_html = "".join(
        f"<tr><td>{p['producto']}</td><td>{p['cantidad']}</td><td>${p['ingresos']:.2f}</td></tr>"
        for p in filas_productos
    ) or "<tr><td colspan='3'>Sin ventas en este período</td></tr>"

    filas_municipios_html = "".join(
        f"<tr><td>{m['lugar']}</td><td>{m['n']}</td></tr>" for m in filas_municipios
    ) or "<tr><td colspan='2'>Sin entregas a domicilio en este período</td></tr>"

    def _nombre_o_telefono(r):
        # 🆕 Para Messenger mostramos el nombre real de Facebook si ya se
        # resolvió (ver resolver_nombre_messenger); si no, mostramos el
        # PSID tal cual mientras se resuelve. WhatsApp no cambia -- ahí
        # el "teléfono" ya es un número real y útil de por sí.
        if r["canal"] == "messenger":
            return nombres_messenger_cache.get(r["telefono"], r["telefono"])
        return r["telefono"]

    filas_recientes_html = "".join(
        f"<tr><td>{r['folio']}</td><td>{_nombre_o_telefono(r)}</td><td>{r['estado']}</td>"
        f"<td>{'🚨 Urgente' if r['es_urgente'] else 'Normal'}</td>"
        f"<td>{'📱 Messenger' if r['canal']=='messenger' else '💬 WhatsApp'}</td>"
        f"<td>{_utc_a_hora_local(r['fecha_creacion'])}</td>"
        f"<td>${r['total']:,.2f}</td>"
        f"<td><a href='/dashboard/conversacion?clave={clave_recibida}&telefono={r['telefono']}&canal={r['canal']}' "
        f"style='color:#c2185b;font-weight:600;'>Ver conversación →</a></td></tr>"
        for r in filas_recientes
    ) or "<tr><td colspan='8'>Sin pedidos en este período</td></tr>"

    filas_dias_html = "".join(
        f"<tr><td>{d['etiqueta']}</td><td>${d['ingresos']:,.2f}</td>"
        f"<td>${d['venta_total']:,.2f}</td><td>{d['pedidos']}</td></tr>"
        for d in filas_dias_tabla
    )

    filas_semanas_html = "".join(
        f"<tr><td>{s['etiqueta']}</td><td>${s['ingresos']:,.2f}</td>"
        f"<td>${s['venta_total']:,.2f}</td><td>{s['pedidos']}</td></tr>"
        for s in filas_semanas_tabla
    )

    def _link_periodo(p, texto):
        activo = "background:#f2385a;color:white;" if p == periodo else "background:#eee;color:#333;"
        return f'<a href="/dashboard?clave={clave_recibida}&periodo={p}" style="padding:8px 16px;border-radius:20px;text-decoration:none;margin-right:8px;{activo}">{texto}</a>'

    html = f"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Dashboard — Recuerditos Dalia</title>
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; background:#faf7f5; margin:0; padding:24px; color:#333; }}
  h1 {{ color:#c2185b; margin-bottom:4px; }}
  .subtitulo {{ color:#888; margin-top:0; margin-bottom:20px; }}
  .nav {{ margin-bottom:24px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(220px,1fr)); gap:16px; margin-bottom:28px; }}
  .card {{ background:white; border-radius:12px; padding:18px 20px; box-shadow:0 1px 4px rgba(0,0,0,0.08); }}
  .card .valor {{ font-size:28px; font-weight:700; color:#c2185b; margin:4px 0; }}
  .card .etiqueta {{ font-size:13px; color:#888; text-transform:uppercase; letter-spacing:0.5px; }}
  .card .detalle {{ font-size:13px; color:#666; }}
  table {{ width:100%; border-collapse:collapse; background:white; border-radius:12px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,0.08); margin-bottom:28px; }}
  th {{ background:#c2185b; color:white; text-align:left; padding:10px 14px; font-size:13px; }}
  td {{ padding:9px 14px; border-bottom:1px solid #f0f0f0; font-size:14px; }}
  h2 {{ font-size:17px; color:#444; margin-top:32px; margin-bottom:10px; }}
</style>
</head>
<body>
  <h1>🧸 Recuerditos Dalia</h1>
  <p class="subtitulo">{etiqueta_periodo}</p>
  <div class="nav">
    {_link_periodo('hoy', 'Hoy')}
    {_link_periodo('semana', 'Esta semana')}
    {_link_periodo('mes', 'Este mes')}
  </div>

  <div class="grid">
    <div class="card">
      <div class="etiqueta">Ingresos confirmados</div>
      <div class="valor">${ingresos_total:,.2f}</div>
      <div class="detalle">{ingresos_n} pago(s)/anticipo(s) ya confirmados</div>
    </div>
    <div class="card">
      <div class="etiqueta">Venta total del período</div>
      <div class="valor">${venta_total_periodo:,.2f}</div>
      <div class="detalle">valor de todos los pedidos, se haya confirmado el pago o no</div>
    </div>
    <div class="card">
      <div class="etiqueta">Pedidos</div>
      <div class="valor">{pedidos_total}</div>
      <div class="detalle">{pedidos_urgentes} urgente(s)</div>
    </div>
    <div class="card">
      <div class="etiqueta">Pedidos por canal</div>
      <div class="valor" style="font-size:18px;">💬 {_fila_canal_pedidos('whatsapp')} &nbsp; 📱 {_fila_canal_pedidos('messenger')}</div>
      <div class="detalle">WhatsApp &nbsp;&nbsp;&nbsp;&nbsp;&nbsp; Messenger</div>
    </div>
    <div class="card">
      <div class="etiqueta">Mensajes respondidos</div>
      <div class="valor" style="font-size:18px;">💬 {respondidos_wa}/{total_wa} &nbsp; 📱 {respondidos_msg}/{total_msg}</div>
      <div class="detalle">contestados / recibidos, por canal</div>
    </div>
    <div class="card">
      <div class="etiqueta">Clientes/conversaciones atendidas</div>
      <div class="valor" style="font-size:18px;">💬 {clientes_wa} &nbsp; 📱 {clientes_msg}</div>
      <div class="detalle">personas distintas a las que se les respondió (no mensajes)</div>
    </div>
    <div class="card">
      <div class="etiqueta">Gasto de OpenAI</div>
      <div class="valor">${openai_costo * USD_MXN:.2f} MXN</div>
      <div class="detalle">{openai_llamadas} llamada(s) · {pct_cache}% de tokens desde caché · (${openai_costo:.4f} USD)</div>
    </div>
  </div>

  <h2>🧾 Desglose de pedidos del período ({etiqueta_periodo})</h2>
  <table>
    <tr><th>Pedido</th><th>Envíos</th><th>Urgentes</th><th>Anticipos</th><th>Por pagar</th><th>Total</th></tr>
    {filas_desglose_html}
    {fila_total_desglose_html}
  </table>

  <h2>📅 Ventas por día (últimos 14 días)</h2>
  <table>
    <tr><th>Día</th><th>Ingresos confirmados</th><th>Venta total</th><th>Pedidos</th></tr>
    {filas_dias_html}
  </table>

  <h2>🗓️ Ventas por semana (últimas 8 semanas)</h2>
  <table>
    <tr><th>Semana</th><th>Ingresos confirmados</th><th>Venta total</th><th>Pedidos</th></tr>
    {filas_semanas_html}
  </table>

  <h2>📦 Productos vendidos <span style="font-size:13px;color:#888;font-weight:normal;">({etiqueta_periodo})</span></h2>
  <table>
    <tr><th>Producto</th><th>Cantidad</th><th>Ingresos</th></tr>
    {filas_productos_html}
  </table>

  <h2>📍 Entregas a domicilio por municipio</h2>
  <table>
    <tr><th>Municipio</th><th>Pedidos</th></tr>
    {filas_municipios_html}
  </table>

  <h2>🧾 Pedidos recientes</h2>
  <table>
    <tr><th>Folio</th><th>Cliente</th><th>Estado</th><th>Tipo</th><th>Canal</th><th>Fecha</th><th>Total</th><th></th></tr>
    {filas_recientes_html}
  </table>

</body>
</html>
"""
    return html


@app.route("/dashboard/conversacion")
def dashboard_conversacion():
    """Visor de la conversación real de un cliente específico -- usa
    historial_chat, que ya guarda cada mensaje (cliente y bot) con su
    canal y hora. No depende de nada externo, así que siempre funciona,
    a diferencia de los links a WhatsApp/Messenger de abajo."""
    clave_recibida = request.args.get("clave", "")
    if not DASHBOARD_PASSWORD or clave_recibida != DASHBOARD_PASSWORD:
        return "🔒 Acceso no autorizado. Agrega ?clave=TU_CLAVE a la URL.", 401

    telefono = request.args.get("telefono", "")
    canal = request.args.get("canal", "whatsapp")
    if not telefono:
        return "Falta el parámetro 'telefono'.", 400

    try:
        with database.get_db_connection() as conn:
            mensajes = conn.execute(
                "SELECT mensaje, emisor, timestamp FROM historial_chat "
                "WHERE telefono = ? ORDER BY id ASC",
                (telefono,),
            ).fetchall()
    except Exception as e:
        return f"Error consultando la base de datos: {e}", 500

    # 🆕 (20 ago 2026, pedido explícito de Israel) Mostrar el nombre real
    # de Facebook en vez del PSID, si ya se resolvió.
    nombre_mostrar = telefono
    if canal == "messenger":
        nombre_cacheado = database.obtener_nombre_messenger_cache(telefono)
        if nombre_cacheado:
            nombre_mostrar = nombre_cacheado

    burbujas_html = "".join(
        f"""<div style="display:flex;justify-content:{'flex-end' if m['emisor']=='bot' else 'flex-start'};margin-bottom:10px;">
              <div style="max-width:70%;background:{'#c2185b' if m['emisor']=='bot' else 'white'};
                          color:{'white' if m['emisor']=='bot' else '#333'};
                          padding:10px 14px;border-radius:14px;box-shadow:0 1px 3px rgba(0,0,0,0.1);">
                <div style="font-size:14px;white-space:pre-wrap;">{m['mensaje']}</div>
                <div style="font-size:11px;opacity:0.7;margin-top:4px;">{_utc_a_hora_local(m['timestamp'])}</div>
              </div>
            </div>"""
        for m in mensajes
    ) or "<p>No hay mensajes guardados para este número.</p>"

    # 🔧 Links a la conversación real -- wa.me es confiable y documentado
    # por Meta. El de Messenger (messenger.com/t/) está pensado para IDs
    # de perfiles personales, no necesariamente para el identificador
    # (PSID) que usa una página de negocio con un cliente -- se ofrece
    # como "inténtalo", no como garantía, por eso el aviso junto al botón.
    if canal == "messenger":
        link_externo = f"https://www.messenger.com/t/{telefono}"
        texto_boton = "📱 Intentar abrir en Messenger"
        aviso = "⚠️ Este link no siempre funciona para conversaciones de página de negocio -- si no abre, busca al cliente directo en el buzón de Meta Business Suite."
    else:
        link_externo = f"https://wa.me/{telefono}"
        texto_boton = "💬 Abrir conversación en WhatsApp"
        aviso = "Esto abre un chat nuevo con este número desde tu WhatsApp personal (no es el mismo hilo del bot, pero llega al mismo cliente)."

    html = f"""
<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<title>Conversación con {nombre_mostrar} — Recuerditos Dalia</title>
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; background:#e9dfda; margin:0; padding:24px; }}
  .header {{ background:white; border-radius:12px; padding:16px 20px; margin-bottom:16px; box-shadow:0 1px 4px rgba(0,0,0,0.08); }}
  .header a {{ color:#c2185b; text-decoration:none; font-size:14px; }}
  .chat {{ background:#e9dfda; border-radius:12px; padding:20px; max-width:700px; }}
  .boton {{ display:inline-block; background:#c2185b; color:white; padding:10px 18px; border-radius:8px; text-decoration:none; font-weight:600; margin-top:10px; }}
  .aviso {{ font-size:12px; color:#888; margin-top:6px; }}
</style>
</head>
<body>
  <div class="header">
    <a href="/dashboard?clave={clave_recibida}">← Volver al dashboard</a>
    <h2 style="margin:8px 0 4px 0;">Conversación con {nombre_mostrar}</h2>
    <p style="margin:0;color:#888;">{'📱 Messenger' if canal == 'messenger' else '💬 WhatsApp'} · {len(mensajes)} mensaje(s)</p>
    <a class="boton" href="{link_externo}" target="_blank">{texto_boton}</a>
    <div class="aviso">{aviso}</div>
  </div>
  <div class="chat">
    {burbujas_html}
  </div>
</body>
</html>
"""
    return html


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

def resolver_nombre_messenger(psid, pagina_id=None):
    """(20 ago 2026, pedido explícito de Israel) Para que el dashboard
    muestre el nombre real de Facebook del cliente en vez del PSID (un
    número que no le dice nada a nadie). Primero revisa la caché en
    SQLite (lectura local, rapidísima); si no está, dispara un hilo de
    background para pedírselo al Graph API de Meta y guardarlo -- así el
    mensaje del cliente se sigue procesando y respondiendo de inmediato,
    sin esperar a esta llamada de red. La primera vez que se vea a ese
    PSID el dashboard puede seguir mostrando el PSID por unos segundos,
    hasta que el hilo de background termine de guardar el nombre."""
    if not psid:
        return None
    nombre_cacheado = database.obtener_nombre_messenger_cache(psid)
    if nombre_cacheado:
        return nombre_cacheado

    # 🔧 Usa el mismo pool acotado (EJECUTOR_MENSAJES / _lanzar_en_fondo)
    # que ya se usa para todo lo demás en background, en vez de crear un
    # hilo suelto sin límite -- esto es justo lo que se corrigió antes en
    # todo el resto del código (ver test_executor_fix.py) para evitar
    # crear hilos sin límite bajo carga.
    def _pedir_nombre_en_segundo_plano():
        try:
            r = requests.get(
                f"https://graph.facebook.com/{GRAPH_API_VERSION}/{psid}",
                params={"fields": "first_name,last_name", "access_token": token_para_pagina(pagina_id)},
                timeout=10,
            )
            if r.status_code == 200:
                datos = r.json()
                nombre = " ".join(p for p in [datos.get("first_name"), datos.get("last_name")] if p).strip()
                if nombre:
                    database.guardar_nombre_messenger_cache(psid, nombre)
                    print(f"👤 Nombre de Messenger resuelto para {psid}: {nombre}")
            else:
                print(f"⚠️ No se pudo obtener el nombre de Messenger para {psid}: {r.status_code} {r.text}")
        except requests.RequestException as e:
            print(f"⚠️ Excepción obteniendo nombre de Messenger para {psid}: {e}")

    _lanzar_en_fondo(_pedir_nombre_en_segundo_plano)
    return None


def registrar_entrada_cliente(numero, texto_para_guardar, tipo="texto", canal="whatsapp", pagina_id=None):
    cliente = crm.cargar_cliente(numero)
    crm.guardar_mensaje_cliente(cliente, texto_para_guardar, tipo=tipo, canal=canal)
    if canal == "messenger":
        resolver_nombre_messenger(numero, pagina_id=pagina_id)
    return cliente


def procesar_mensaje_no_soportado(numero, tipo, canal="whatsapp", pagina_id=None, proveedor_whatsapp="meta"):
    _contexto_hilo.proveedor_whatsapp = proveedor_whatsapp
    cliente = registrar_entrada_cliente(numero, f"[mensaje no soportado: {tipo}]", tipo=tipo, canal=canal, pagina_id=pagina_id)
    respuesta = "Por ahora solo puedo leer mensajes de texto 🙂 ¿me lo escribes con palabras?"
    crm.guardar_respuesta(cliente, respuesta, canal=canal)
    enviar_mensaje_canal(numero, respuesta, canal, pagina_id=pagina_id)


def procesar_mensaje_en_fondo(numero, texto_cliente, media_id_imagen=None, media_id_audio=None, canal="whatsapp", media_url_imagen_messenger=None, pagina_id=None, proveedor_whatsapp="meta", media_url_imagen_ycloud=None, media_url_audio_ycloud=None, media_url_audio_messenger=None):
    # 🔧 Marca, para ESTE hilo únicamente, si los envíos de este mensaje
    # deben salir por YCloud (número de prueba) o por Meta (producción,
    # comportamiento de siempre). Ver _usar_ycloud_en_este_hilo() arriba.
    _contexto_hilo.proveedor_whatsapp = proveedor_whatsapp
    print("=" * 70)
    print(f"🚀 Procesando mensaje de {numero}")
    print(f"💬 Texto recibido: {texto_cliente}")

    # 🆘 CANDADO DE EMERGENCIA -- capa 1, la definitiva. Si
    # BOT_PAUSADO=true está puesto en Render, el bot no hace absolutamente
    # nada más: ni guarda el mensaje distinto de un log, ni llama a
    # OpenAI, ni responde. Esto va primero que cualquier otra lógica a
    # propósito, para que sea inmune a cualquier bug que pudiera existir
    # más abajo en el código.
    if BOT_PAUSADO_GLOBAL:
        print(f"🆘 BOT_PAUSADO=true -- ignorando mensaje de {numero} por completo.")
        return

    # 🔧 Se checa ANTES de guardar el mensaje entrante -- si se checara
    # después, este mismo mensaje ya contaría como "1 mensaje previo" y
    # nunca detectaríamos al cliente como nuevo.
    es_primera_vez = pedido_manager.es_cliente_nuevo(numero)

    imagen_base64 = None
    imagen_mime = None
    tipo_para_crm = "texto"

    if media_id_audio or media_url_audio_ycloud or media_url_audio_messenger:
        print("🎤 El cliente mandó un audio, descargándolo...")
        if media_url_audio_messenger:
            contenido_audio, mime_audio = descargar_audio_messenger(media_url_audio_messenger)
        elif media_url_audio_ycloud:
            contenido_audio, mime_audio = descargar_media_ycloud(media_url_audio_ycloud)
        else:
            contenido_audio, mime_audio = descargar_imagen_whatsapp(media_id_audio)
        if not contenido_audio:
            print("❌ No se pudo descargar el audio del cliente")
            respuesta_fallo = "No pude descargar tu audio 😔 ¿me lo puedes mandar otra vez, o escribirlo?"
            cliente = registrar_entrada_cliente(numero, "(audio no descargable)", tipo="audio", canal=canal)
            crm.guardar_respuesta(cliente, respuesta_fallo, canal=canal)
            enviar_mensaje_canal(numero, respuesta_fallo, canal, pagina_id=pagina_id)
            return

        print(f"✅ Audio descargado ({len(contenido_audio)} bytes, {mime_audio}), transcribiendo...")
        texto_transcrito = audio_handler.transcribir_audio(client, contenido_audio, mime_audio)
        if not texto_transcrito:
            print("❌ No se pudo transcribir el audio")
            respuesta_fallo = "No logré entender tu audio 😔 ¿me lo puedes escribir, por favor?"
            cliente = registrar_entrada_cliente(numero, "(audio no se pudo transcribir)", tipo="audio", canal=canal)
            crm.guardar_respuesta(cliente, respuesta_fallo, canal=canal)
            enviar_mensaje_canal(numero, respuesta_fallo, canal, pagina_id=pagina_id)
            return

        print(f"📝 Audio transcrito: {texto_transcrito}")
        texto_cliente = texto_transcrito
        tipo_para_crm = "audio"

    elif media_id_imagen or media_url_imagen_messenger or media_url_imagen_ycloud:
        print("🖼️ El cliente mandó una imagen (Vision), descargándola...")
        if canal == "messenger" and media_url_imagen_messenger:
            contenido, mime = descargar_imagen_messenger(media_url_imagen_messenger)
        elif media_url_imagen_ycloud:
            contenido, mime = descargar_media_ycloud(media_url_imagen_ycloud)
        else:
            contenido, mime = descargar_imagen_whatsapp(media_id_imagen)
        if contenido:
            imagen_base64 = base64.b64encode(contenido).decode("utf-8")
            imagen_mime = mime
            tipo_para_crm = "imagen"
            print(f"✅ Imagen descargada ({len(contenido)} bytes, {mime})")
        else:
            print("❌ No se pudo descargar la imagen del cliente, se sigue solo con el texto (si había)")

    texto_para_guardar = texto_cliente or ("(imagen sin texto)" if (media_id_imagen or media_url_imagen_ycloud) else "")
    cliente = registrar_entrada_cliente(numero, texto_para_guardar, tipo=tipo_para_crm, canal=canal, pagina_id=pagina_id)

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
            enviar_mensaje_canal(numero, respuesta_reset, canal, pagina_id=pagina_id)
            return

    # 🆘 CANDADO DE EMERGENCIA -- capa 2, comando rápido por WhatsApp o
    # Messenger. Solo números autorizados (los mismos que pueden usar
    # 🧸☠️🧸) pueden pausar/reanudar el bot para TODOS los clientes de
    # AMBOS canales, sin tocar Render. Pensado para el lanzamiento: si
    # algo se ve mal en los primeros mensajes reales, Dalia puede
    # apagarlo desde su celular en segundos. Es un interruptor (mandar
    # el mismo código lo prende o apaga según el estado actual).
    if texto_cliente and "🛑🛑🛑" in texto_cliente:
        if numero not in NUMEROS_AUTORIZADOS_RESET:
            print(f"🚫 {numero}: intentó usar el candado de emergencia pero no está autorizado.")
        else:
            pausado_actual = pedido_manager.bot_pausado_globalmente()
            pedido_manager.set_bot_pausado(not pausado_actual)
            if not pausado_actual:
                print(f"🆘 {numero}: BOT PAUSADO globalmente vía comando de {canal}.")
                enviar_mensaje_canal(
                    numero,
                    "🛑 Bot pausado. No le va a responder a NINGÚN cliente hasta "
                    "que mandes este mismo código otra vez para reactivarlo.",
                    canal,
                    pagina_id=pagina_id,
                )
            else:
                print(f"✅ {numero}: BOT reactivado globalmente vía comando de {canal}.")
                enviar_mensaje_canal(numero, "✅ Bot reactivado. Ya vuelve a responder normal a todos los clientes.", canal, pagina_id=pagina_id)
        return

    # 🆕 Silenciar/reactivar UNA conversación específica, sin borrar nada
    # y sin apagar el bot para nadie más. Dalia manda "SILENCIAR
    # <numero_o_psid>" desde CUALQUIER canal (no importa dónde esté
    # hablando ella), y el bot deja de contestarle a ese cliente puntual
    # -- pensado para cuando el bot cometió un error con un cliente real
    # y Dalia necesita contactarlo personalmente sin que el bot le
    # conteste encima. "REACTIVAR <numero_o_psid>" hace lo contrario.
    if texto_cliente and numero in NUMEROS_AUTORIZADOS_RESET:
        texto_normalizado = texto_cliente.strip()
        partes = texto_normalizado.split(None, 1)
        if len(partes) == 2 and partes[0].upper() == "SILENCIAR":
            objetivo = partes[1].strip()
            pedido_manager.silenciar_conversacion(objetivo)
            print(f"🙅 {numero}: silenció manualmente la conversación con {objetivo}")
            enviar_mensaje_canal(numero, f"🛑 Listo, el bot ya no le va a responder a {objetivo}. El resto de clientes sigue normal.", canal, pagina_id=pagina_id)
            return
        if len(partes) == 2 and partes[0].upper() == "REACTIVAR":
            objetivo = partes[1].strip()
            pedido_manager.reactivar_conversacion(objetivo)
            with sesiones_lock:
                sesiones.pop(objetivo, None)
            print(f"✅ {numero}: reactivó manualmente la conversación con {objetivo}")
            enviar_mensaje_canal(numero, f"✅ Listo, el bot vuelve a responderle a {objetivo} normal.", canal, pagina_id=pagina_id)
            return

    # 🆘 Si el candado de emergencia (capa 2, por WhatsApp) está activo,
    # el bot se queda callado con CUALQUIER cliente -- el mensaje ya
    # quedó guardado en el historial arriba, pero no se gasta una llamada
    # a OpenAI ni se manda respuesta. Los comandos de arriba (🧸☠️🧸 y
    # 🛑🛑🛑) siguen funcionando aunque esté pausado, para que se pueda
    # reactivar sin necesidad de tocar Render.
    if pedido_manager.bot_pausado_globalmente():
        print(f"🛑 Bot pausado globalmente -- mensaje de {numero} guardado, sin respuesta.")
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

    # Se adelanta obtener_sesion() (antes se llamaba varias líneas más
    # abajo) porque el saludo canónico de abajo ya necesita
    # sesion["imagenes_enviadas"] para marcar la foto del osito con
    # jaboncito como ya mandada. obtener_sesion() es idempotente (solo
    # hidrata una vez por número), así que adelantarla no repite trabajo.
    sesion = obtener_sesion(numero)

    # 🔧 CAMBIO DE DECISIÓN DE NEGOCIO (segunda vuelta): ahora sí se
    # fuerza otra vez un saludo garantizado en el primer mensaje de cada
    # cliente nuevo -- pero a diferencia de la primera versión (que
    # cortaba la respuesta y obligaba al cliente a escribir de nuevo
    # para que le contestaran su pregunta), esta vez el saludo se manda
    # como mensaje aparte y LUEGO el flujo sigue normal: el modelo
    # contesta la pregunta real del cliente en el mismo turno, sin que
    # tenga que repetirla.
    #
    # 🔧 (20 ago 2026) Ampliado a 3 partes por pedido explícito de Israel:
    # 1) catálogo (como ya estaba), 2) texto invitando a ver los ositos
    # con jaboncito + pregunta abierta de qué busca, 3) la foto del osito
    # con jaboncito ($12). Se manda en ese orden, cada uno como mensaje
    # aparte, ANTES de que el modelo conteste la pregunta real del
    # cliente en el mismo turno.
    if es_primera_vez:
        saludo_catalogo = (
            "Buen día, te comparto nuestro catálogo con información de "
            "nuestros recuerditos."
        )
        if URL_CATALOGO_PDF:
            saludo_catalogo += f"\n{URL_CATALOGO_PDF}"
        enviar_mensaje_canal(numero, saludo_catalogo, canal, pagina_id=pagina_id)
        crm.guardar_respuesta(cliente, saludo_catalogo, canal=canal)

        saludo_osito = (
            "Te mando también información de nuestros ositos con jaboncito. "
            "o buscas algún recuerdito en específico?"
        )
        enviar_mensaje_canal(numero, saludo_osito, canal, pagina_id=pagina_id)
        crm.guardar_respuesta(cliente, saludo_osito, canal=canal)

        url_osito_jaboncito = url_imagen_producto("osito_con_jaboncito")
        if url_osito_jaboncito:
            enviar_imagen_canal(numero, url_osito_jaboncito, canal, pagina_id=pagina_id)
            sesion["imagenes_enviadas"].add("osito_con_jaboncito")

        print(f"👋 {numero}: saludo canónico completo enviado (catálogo + texto + foto osito con jaboncito; cliente nuevo, canal={canal})")
        # Sin return -- el flujo sigue abajo y el modelo contesta la
        # pregunta real del cliente como un mensaje aparte.

    # 🔧 Envío determinístico de imágenes clave (colores + productos de
    # una sola variante + variante específica de osito si el CLIENTE ya la
    # nombró) -- ver detectar_imagenes_automaticas / detectar_imagen_osito_
    # especifico arriba. Se manda ANTES de consultar al modelo para que
    # llegue de inmediato, no como una foto más entre varias respuestas de
    # texto.
    imagenes_enviadas = sesion["imagenes_enviadas"]
    claves_imagen_cliente = list(dict.fromkeys(
        detectar_imagenes_automaticas(texto_cliente)
        + detectar_imagen_osito_especifico(texto_cliente)
    ))
    # 🔧 Bug real detectado: si el cliente vuelve a preguntar explícitamente
    # por algo que ya se le mandó antes en la misma conversación (ej. "qué
    # colores tienes?" una segunda vez), el candado de "no repetir foto"
    # bloqueaba el reenvío en silencio y el cliente se quedaba sin
    # respuesta a su pregunta. Aquí SÍ se manda aunque ya esté en
    # imagenes_enviadas -- si el cliente lo pide de nuevo, es porque de
    # verdad lo quiere ver de nuevo.
    if len(claves_imagen_cliente) >= 3:
        print(f"🖼️ Se omitió el envío automático de {len(claves_imagen_cliente)} imágenes "
              f"pedidas de golpe por el cliente ({', '.join(claves_imagen_cliente)}) -- revisar manualmente.")
    else:
        for clave_img in claves_imagen_cliente:
            url_img = url_imagen_producto(clave_img)
            if url_img:
                enviar_imagen_canal(numero, url_img, canal, pagina_id=pagina_id)
                imagenes_enviadas.add(clave_img)
                print(f"🖼️ Imagen automática (mensaje del cliente) enviada a {numero}: {clave_img}")

    with sesion["lock"]:
        try:
            print("🧠 Consultando OpenAI...")
            respuesta = preguntar_ia(numero, texto_cliente, imagen_base64=imagen_base64, imagen_mime=imagen_mime, canal=canal, pagina_id=pagina_id)
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
            # 🔧 (19 ago 2026, a pedido explícito de Israel) Ahora se piden
            # 3 mensajes separados en vez de 2: el de gracias, la petición
            # del número de WhatsApp de seguimiento, y al final el reloj
            # de arena SOLO -- Israel pidió explícitamente que el reloj de
            # arena sea siempre el último mensaje que se vea, no pegado al
            # texto anterior.
            mensaje_2 = "¿Puedes compartirnos por favor un número de WhatsApp para darle seguimiento a tu pedido? ¡Gracias!"
            mensaje_3 = "⌛"

            # 🔧 (20 ago 2026, a pedido explícito de Israel -- ver
            # DIRECCION_LOCAL arriba) Si el pedido es de recolección en
            # local (todo pedido urgente SIEMPRE lo es), se manda un
            # mensaje fijo extra con la dirección y el link de Maps, para
            # no depender de que el modelo se acuerde de mandarla. Se
            # manda justo después del "gracias por tu anticipo" y antes de
            # pedir el WhatsApp de seguimiento.
            pedido_confirmado = sesion["pedido"]
            es_recoleccion_local = bool(pedido_confirmado.get("es_urgente")) or (
                pedido_confirmado.get("tipo_entrega") == "local"
            )
            mensaje_ubicacion = None
            if es_recoleccion_local:
                mensaje_ubicacion = (
                    f"📍 Tu pedido se recoge en nuestro local: {DIRECCION_LOCAL}.\n"
                    f"Ubicación en Maps: {LINK_MAPS_LOCAL}\n\n"
                    f"Horario de entrega en local:\n{HORARIO_LOCAL_ENTRE_SEMANA}\n{HORARIO_LOCAL_SABADO}"
                )

            try:
                # sincronizar_pedido ya regresa el pedido oficial (recién
                # creado o actualizado) con su folio — no usar
                # crm.cargar_pedido aquí, porque esa función EXCLUYE a
                # propósito los pedidos en modo DALIA (ver
                # pedido_manager.obtener_pedido_activo) y ya acabamos de
                # poner este pedido en modo DALIA.
                pedido_db = crm.sincronizar_pedido(cliente, sesion["pedido"], canal=canal)
                sesion["pedido_id"] = pedido_db.id if pedido_db else None

                crm.guardar_respuesta(cliente, mensaje_1, canal=canal)
                if mensaje_ubicacion:
                    crm.guardar_respuesta(cliente, mensaje_ubicacion, canal=canal)
                crm.guardar_respuesta(cliente, mensaje_2, canal=canal)
                crm.guardar_respuesta(cliente, mensaje_3, canal=canal)
            except Exception as e:
                print("⚠️ Error guardando en CRM (el bot sigue funcionando con RAM):", repr(e))
                pedido_db = None

            time.sleep(random.uniform(2, 4))
            print("📤 Enviando mensajes fijos de confirmación de anticipo...")
            enviar_mensaje_canal(numero, mensaje_1, canal, pagina_id=pagina_id)
            time.sleep(1.5)
            if mensaje_ubicacion:
                enviar_mensaje_canal(numero, mensaje_ubicacion, canal, pagina_id=pagina_id)
                sesion["info_enviada"]["ubicacion_local"] = True
                time.sleep(1.5)
            enviar_mensaje_canal(numero, mensaje_2, canal, pagina_id=pagina_id)
            time.sleep(1.5)
            enviar_mensaje_canal(numero, mensaje_3, canal, pagina_id=pagina_id)

            print("📣 Notificando a Dalia...")
            notificar_a_dalia(pedido_db, sesion["pedido"])

            print("🏁 Fin procesamiento (anticipo confirmado)")
            print("=" * 70)
            return

        try:
            crm.guardar_respuesta(cliente, respuesta, canal=canal)

            # 🔧 CORREGIDO (Observación 7): antes aquí había una llamada
            # extra a guardar_borrador_pedido() (comentada como "CAMBIO
            # CLAVE 2"), redundante con la que ya hace ejecutar_tool_call
            # y con la que hace crm.sincronizar_pedido justo abajo — hasta
            # 3 escrituras a SQLite por un solo mensaje. Ahora solo se
            # guarda UNA vez, dentro de crm.sincronizar_pedido.
            crm.sincronizar_pedido(cliente, sesion["pedido"], canal=canal)
            pedido_db = crm.cargar_pedido(cliente)
            sesion["pedido_id"] = pedido_db.id if pedido_db else None
        except Exception as e:
            print("⚠️ Error guardando en CRM (el bot sigue funcionando con RAM):", repr(e))

        time.sleep(random.uniform(2, 4))
        # Gate: no mandar datos bancarios sin total calculado
        respuesta = filtrar_datos_bancarios_si_no_hay_total(respuesta, sesion.get("pedido") or {})
        print(f"📤 Enviando respuesta por {canal}...")
        r = enviar_mensaje_canal(numero, respuesta, canal, pagina_id=pagina_id)
        if r is not None:
            print(f"📨 {canal} respondió: {r.status_code}")
        else:
            print(f"❌ enviar_mensaje_canal ({canal}) devolvió None")

        # 🔧 Envío determinístico de imágenes cuando es el BOT quien
        # recomienda colores o menciona/cotiza una variante específica de
        # osito en su propia respuesta (antes esto dependía 100% de que el
        # modelo se acordara de llamar mostrar_foto_producto, y en la
        # práctica terminaba preguntando "¿quieres que te muestre la
        # foto?" en vez de mandarla -- ahora se manda sola, después del
        # mensaje de texto, sin preguntar).
        claves_imagen_respuesta = list(dict.fromkeys(
            detectar_imagenes_automaticas(respuesta)
            + detectar_imagen_osito_especifico(respuesta)
        ))
        # 🔧 Ver PALABRAS_CONTEXTO_PRODUCTO_DE_TOALLA arriba: si "colores_
        # disponibles" se detectó solo por la palabra suelta "color"/
        # "colores" en la respuesta, pero la respuesta no habla de ningún
        # producto de toalla/jaboncito, es un falso positivo (ej. "moño a
        # elegir color" al cotizar un abanico) -- no se manda la foto.
        if "colores_disponibles" in claves_imagen_respuesta and not _respuesta_menciona_producto_de_toalla(respuesta):
            claves_imagen_respuesta.remove("colores_disponibles")
            print("🖼️ Se omitió foto de colores (respuesta del bot) -- la respuesta no habla de un producto de toalla/jaboncito, probablemente coincidencia con otra palabra.")
        if detectar_info_enviada(respuesta).get("colores_disponibles"):
            claves_imagen_respuesta.append("colores_disponibles")
        claves_imagen_respuesta = list(dict.fromkeys(claves_imagen_respuesta))
        # 🔧 Bug real detectado: cuando la respuesta del bot es la lista
        # completa de precios de ositos (varias variantes mencionadas a la
        # vez, no una recomendación puntual), el texto contiene las frases
        # de casi todas las variantes y esto disparaba una CASCADA de 8-9
        # fotos seguidas -- entre ellas, alguna que ni siquiera aplicaba
        # (ej. "velas de toalla" por la palabra "velita" del kit). Si el
        # mensaje del bot menciona 3 o más productos distintos a la vez, es
        # una lista/resumen, no una recomendación puntual -- no se manda
        # ninguna foto automática; el cliente puede pedir la del modelo
        # específico que le interese en cuanto elija.
        if len(claves_imagen_respuesta) >= 3:
            print(f"🖼️ Se omitió el envío automático de {len(claves_imagen_respuesta)} imágenes "
                  f"({', '.join(claves_imagen_respuesta)}) -- la respuesta parece una lista completa, "
                  f"no una recomendación puntual.")
        else:
            for clave_img in claves_imagen_respuesta:
                if clave_img in imagenes_enviadas:
                    continue
                url_img = url_imagen_producto(clave_img)
                if url_img:
                    enviar_imagen_canal(numero, url_img, canal, pagina_id=pagina_id)
                    imagenes_enviadas.add(clave_img)
                    print(f"🖼️ Imagen automática (respuesta del bot) enviada a {numero}: {clave_img}")

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

        # 🔧 CORREGIDO (bug real detectado: notificación a Dalia que
        # nunca llegó, aunque el envío inicial regresó status 200).
        # Meta manda el resultado FINAL de la entrega (delivered/read/
        # failed) en un webhook APARTE, bajo "statuses" -- no en
        # "messages". Antes esto se descartaba en silencio como "sin
        # mensajes nuevos", así que nunca nos enterábamos si un mensaje
        # se aceptó pero luego falló en la entrega real. Ahora se
        # registra siempre, con el motivo del error si lo hay.
        estados = valor.get("statuses")
        if estados:
            for estado in estados:
                status = estado.get("status")
                destinatario = estado.get("recipient_id")
                if status == "failed":
                    errores = estado.get("errors", [])
                    detalle = "; ".join(
                        f"{e.get('code')}: {e.get('title')} -- {e.get('message', '')}"
                        for e in errores
                    ) or "sin detalle"
                    print(f"🚨 MENSAJE DE WHATSAPP FALLÓ AL ENTREGARSE a {destinatario}: {detalle}")
                else:
                    print(f"📬 Estado de mensaje WhatsApp: {status} -> {destinatario}")
            return jsonify({"status": "estado registrado"}), 200

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
            _lanzar_en_fondo(
                procesar_mensaje_en_fondo,
                numero, caption,
                media_id_imagen=media_id,
            )
            return jsonify({"status": "ok"}), 200

        if tipo == "audio":
            # Meta manda "audio" tanto para notas de voz como para audios
            # adjuntos normales; ambos llegan igual, con un media_id.
            media_id = mensaje["audio"]["id"]
            _lanzar_en_fondo(
                procesar_mensaje_en_fondo,
                numero, "",
                media_id_audio=media_id,
            )
            return jsonify({"status": "ok"}), 200

        if tipo != "text":
            _lanzar_en_fondo(procesar_mensaje_no_soportado, numero, tipo)
            return jsonify({"status": "tipo de mensaje no soportado"}), 200

        texto_cliente = mensaje["text"]["body"]

        _lanzar_en_fondo(procesar_mensaje_en_fondo, numero, texto_cliente)

    except (KeyError, IndexError, TypeError) as e:
        print("Evento sin mensaje de texto reconocible:", e)

    return jsonify({"status": "ok"}), 200


# ===========================
# WEBHOOK: WHATSAPP vía YCLOUD (número de prueba, coexistencia)
# ===========================
# No lleva ruta GET de verificación -- a diferencia de Meta (que usa el
# reto hub.challenge), YCloud verifica el endpoint con la firma HMAC de
# cada request (ver verificar_firma_ycloud arriba), configurada al crear
# el webhook en el panel de YCloud (Developers → Webhook).

@app.route("/webhook/ycloud", methods=["POST"])
def handle_message_ycloud():
    firma = request.headers.get("YCloud-Signature", "")
    if not verificar_firma_ycloud(request.get_data(), firma):
        print("🚫 Webhook de YCloud rechazado: la firma no coincide (el request no parece venir de YCloud)")
        return jsonify({"status": "firma inválida"}), 403

    data = request.get_json(silent=True) or {}

    if data.get("type") != "whatsapp.inbound_message.received":
        # Otros eventos (mensaje entregado/leído, actualizaciones de
        # cuenta, etc.) -- se ignoran por ahora, igual que Meta con
        # "statuses" arriba.
        print(f"ℹ️ [YCloud] Evento ignorado: {data.get('type')}")
        return jsonify({"status": "evento ignorado"}), 200

    try:
        mensaje = data["whatsappInboundMessage"]
        # YCloud manda el número con "+" (E.164); se guarda SIN "+" para
        # que quede igual que los números que ya vienen de Meta (mismo
        # formato en CRM, dedup, SILENCIAR/REACTIVAR, etc.)
        numero = (mensaje["from"] or "").lstrip("+")
        tipo = mensaje.get("type")
        mensaje_id = mensaje.get("id")

        if ya_fue_procesado(mensaje_id):
            print(f"🔁 [YCloud] Mensaje duplicado ignorado: {mensaje_id}")
            return jsonify({"status": "duplicado ignorado"}), 200

        if tipo == "image":
            media_url = mensaje["image"]["link"]
            caption = mensaje["image"].get("caption", "")
            _lanzar_en_fondo(
                procesar_mensaje_en_fondo,
                numero, caption,
                media_url_imagen_ycloud=media_url, proveedor_whatsapp="ycloud",
            )
            return jsonify({"status": "ok"}), 200

        if tipo == "audio":
            media_url = mensaje["audio"]["link"]
            _lanzar_en_fondo(
                procesar_mensaje_en_fondo,
                numero, "",
                media_url_audio_ycloud=media_url, proveedor_whatsapp="ycloud",
            )
            return jsonify({"status": "ok"}), 200

        if tipo != "text":
            _lanzar_en_fondo(
                procesar_mensaje_no_soportado,
                numero, tipo,
                proveedor_whatsapp="ycloud",
            )
            return jsonify({"status": "tipo de mensaje no soportado"}), 200

        texto_cliente = mensaje["text"]["body"]

        _lanzar_en_fondo(
            procesar_mensaje_en_fondo,
            numero, texto_cliente,
            proveedor_whatsapp="ycloud",
        )

    except (KeyError, IndexError, TypeError) as e:
        print("[YCloud] Evento sin mensaje de texto reconocible:", e)

    return jsonify({"status": "ok"}), 200


# ===========================
# WEBHOOK: MESSENGER (Facebook Page)
# ===========================

@app.route("/webhook/messenger", methods=["GET"])
def verify_webhook_messenger():
    mode = request.args.get("hub.mode")
    token = request.args.get("hub.verify_token")
    challenge = request.args.get("hub.challenge")

    if mode == "subscribe" and token == MESSENGER_VERIFY_TOKEN:
        return challenge, 200
    return "Error, verificación fallida", 403


@app.route("/webhook/messenger", methods=["POST"])
def handle_message_messenger():
    # 🔧 Apagado explícito de Messenger (ver MESSENGER_ACTIVO arriba) --
    # se responde 200 (para que Meta no reintente ni marque error el
    # webhook), pero no se procesa nada. La verificación, el token y las
    # suscripciones se quedan intactos, listos para cuando se reactive.
    if not MESSENGER_ACTIVO:
        return jsonify({"status": "messenger desactivado"}), 200

    # 🔧 A diferencia de WhatsApp, el webhook de Messenger no manda una
    # firma HMAC en el mismo header -- Meta sí soporta X-Hub-Signature-256
    # para Messenger también, pero se reutiliza el mismo WHATSAPP_APP_SECRET
    # (viven en la misma App de Meta) para verificarlo con la misma
    # función que ya existe.
    firma = request.headers.get("X-Hub-Signature-256", "")
    if WHATSAPP_APP_SECRET and not verificar_firma_webhook(request.get_data(), firma):
        print("🚫 Webhook de Messenger rechazado: la firma no coincide")
        return jsonify({"status": "firma inválida"}), 403

    data = request.get_json(silent=True) or {}

    if data.get("object") != "page":
        return jsonify({"status": "ignorado"}), 200

    try:
        for entry in data.get("entry", []):
            # 🔧 entry["id"] es el ID de la página de Facebook que
            # recibió este evento -- con esto sabemos con cuál de las
            # páginas conectadas (Recuerditos Dalia, Ventas Iris, u
            # otra que se agregue) hay que responder, para usar el
            # token correcto de esa página específica.
            pagina_id_actual = entry.get("id")
            for evento in entry.get("messaging", []):
                psid = (evento.get("sender") or {}).get("id")
                if not psid:
                    continue

                # 🔧 "Meta 3" para Messenger: cuando Dalia contesta manual
                # a un cliente desde el buzón de la página (Meta Business
                # Suite o la app de Facebook), Meta manda este mismo
                # mensaje de vuelta al webhook marcado como "echo" --
                # sender=la página, recipient=el cliente al que le
                # contestó.
                #
                # 🔧 CAMBIO DE DECISIÓN (versión anterior: CUALQUIER
                # respuesta manual de Dalia silenciaba la conversación --
                # reportado como poco confiable en la práctica). Ahora
                # solo se silencia si Dalia escribe la frase "🧸🧸🧸🧸🧸"
                # (5 ositos) DENTRO de la conversación del cliente --
                # acción deliberada, no cualquier respuesta casual.
                # También se registra SIEMPRE que llegue un echo (aunque
                # no traiga la frase), para tener evidencia real en los
                # logs si esto vuelve a fallar.
                mensaje = evento.get("message")
                if mensaje and mensaje.get("is_echo"):
                    psid_cliente = (evento.get("recipient") or {}).get("id")
                    texto_echo_original = mensaje.get("text") or ""
                    print(f"👁️ Echo recibido de Dalia -- destinatario={psid_cliente!r}, texto={texto_echo_original!r}")
                    if not psid_cliente:
                        print("⚠️ Echo sin 'recipient.id' -- no se puede saber a qué cliente aplica, se ignora.")
                        continue
                    texto_echo = texto_echo_original.strip().lower()
                    # Frase de reactivación: Dalia la escribe directo en
                    # esa misma conversación de Facebook cuando ya
                    # resolvió el problema y quiere que el bot retome esa
                    # conversación específica.
                    if "reactivar bot" in texto_echo:
                        pedido_manager.reactivar_conversacion(psid_cliente)
                        with sesiones_lock:
                            sesiones.pop(psid_cliente, None)
                        print(f"✅ Dalia reactivó el bot para {psid_cliente} (Messenger, vía echo, frase 'Reactivar bot')")
                    elif "🧸" * 5 in texto_echo_original or texto_echo_original.count("🧸") >= 5:
                        pedido_manager.silenciar_conversacion(psid_cliente)
                        print(f"🙅 Dalia mandó '5 ositos' a {psid_cliente} -- bot silenciado en esa conversación (Messenger)")
                    else:
                        print(f"ℹ️ Echo de Dalia sin la frase '🧸🧸🧸🧸🧸' -- no se silencia nada (mensaje normal suyo).")
                    continue

                if not mensaje:
                    continue

                mensaje_id = mensaje.get("mid")
                if mensaje_id and ya_fue_procesado(mensaje_id):
                    print(f"🔁 Mensaje de Messenger duplicado ignorado: {mensaje_id}")
                    continue

                texto_cliente = mensaje.get("text", "") or ""
                adjuntos = mensaje.get("attachments") or []
                imagen_url = None
                audio_url = None
                es_sticker = False
                for adj in adjuntos:
                    payload = adj.get("payload") or {}
                    if adj.get("type") == "image":
                        # 🔧 CORREGIDO (bug real detectado con clienta real):
                        # el botón de "👍 Me gusta" de Messenger (y
                        # cualquier otro sticker) llega en el webhook
                        # marcado como type="image" -- indistinguible de
                        # una foto real EXCEPTO por este campo extra
                        # "sticker_id" que solo tienen los stickers, nunca
                        # una foto de verdad. Sin este chequeo, el bot le
                        # mandaba el sticker del pulgar arriba al modelo
                        # como si fuera un comprobante de pago -- y como
                        # llegar una imagen FUERZA que el modelo decida
                        # algo en automático (para no perder comprobantes
                        # reales), terminó confirmando un anticipo que la
                        # clienta nunca pagó, solo por reaccionar 👍 a un
                        # mensaje.
                        if payload.get("sticker_id"):
                            es_sticker = True
                            break
                        imagen_url = payload.get("url")
                        break
                    # 🆕 Bug real detectado (17 ago 2026): un cliente mandó
                    # un audio por Messenger y el bot respondió "solo
                    # puedo leer texto" -- este tipo nunca se reconocía
                    # aquí, así que caía directo a "adjunto no soportado".
                    # Ahora se detecta igual que la imagen (viene como URL
                    # pública directa) y se manda a transcribir con el
                    # mismo mecanismo que ya funciona en WhatsApp.
                    if adj.get("type") == "audio":
                        audio_url = payload.get("url")
                        break

                if es_sticker:
                    # Se trata como un mensaje normal sin imagen -- el
                    # bot puede seguir la conversación con naturalidad
                    # (ej. "👍" como confirmación de que leyó algo), pero
                    # SIN forzar ninguna decisión de pedido/anticipo.
                    _lanzar_en_fondo(
                        procesar_mensaje_en_fondo,
                        psid, texto_cliente or "(el cliente reaccionó con un sticker/👍)",
                        canal="messenger", pagina_id=pagina_id_actual,
                    )
                elif imagen_url:
                    _lanzar_en_fondo(
                        procesar_mensaje_en_fondo,
                        psid, texto_cliente,
                        media_url_imagen_messenger=imagen_url, canal="messenger", pagina_id=pagina_id_actual,
                    )
                elif audio_url:
                    _lanzar_en_fondo(
                        procesar_mensaje_en_fondo,
                        psid, texto_cliente,
                        media_url_audio_messenger=audio_url, canal="messenger", pagina_id=pagina_id_actual,
                    )
                elif texto_cliente:
                    _lanzar_en_fondo(
                        procesar_mensaje_en_fondo,
                        psid, texto_cliente,
                        canal="messenger", pagina_id=pagina_id_actual,
                    )
                else:
                    _lanzar_en_fondo(
                        procesar_mensaje_no_soportado,
                        psid, "adjunto no soportado",
                        canal="messenger", pagina_id=pagina_id_actual,
                    )

            # 🔧 Comentarios públicos en publicaciones de la página
            # (campo "feed" del webhook -- separado de "messaging").
            # Solo reacciona a comentarios NUEVOS (verb == "add" sobre
            # item == "comment"); ediciones, "me gusta" en comentarios,
            # reacciones a la publicación, etc. se ignoran.
            for cambio in entry.get("changes", []):
                if cambio.get("field") != "feed":
                    continue
                valor = cambio.get("value") or {}
                if valor.get("item") != "comment" or valor.get("verb") != "add":
                    continue
                comment_id = valor.get("comment_id")
                remitente = valor.get("from") or {}
                _lanzar_en_fondo(
                    procesar_comentario_pagina,
                    pagina_id_actual, comment_id, remitente.get("id"), remitente.get("name"), valor.get("message", ""),
                )

    except Exception as e:
        print("⚠️ Error procesando webhook de Messenger:", repr(e))

    return jsonify({"status": "ok"}), 200


# ===========================
# 🆕 SEGUIMIENTO AUTOMÁTICO A CLIENTES SILENCIOSOS (ver PENDIENTES.md sección 1)
# ===========================
# Objetivo: si un cliente con un pedido/borrador en progreso deja de
# responder, mandarle un mensaje de seguimiento ANTES de que se cierre la
# ventana de 24h de mensajería (WhatsApp Y Messenger la tienen, según
# investigación de agosto 2026: developers.facebook.com/docs/messenger-
# platform/reference/send-api/ -- "RESPONSE"/"UPDATE" permiten mensajes
# promocionales y no promocionales DENTRO de esa ventana de 24h, en
# cualquiera de los dos productos). Mandado con margen, todavía cae
# DENTRO de la ventana: cuenta como mensaje normal de la conversación,
# sin plantilla aprobada por Meta ni revisión previa.
#
# 🚨 IMPORTANTE (confirmado con investigación de agosto 2026): fuera de
# esa ventana de 24h, NO existe ningún mecanismo -- ni en WhatsApp ni en
# Messenger -- que permita mandar un mensaje genérico de re-enganche como
# este. En Messenger, Meta incluso eliminó en febrero 2026 la mayoría de
# los "message tags" que antes permitían escribir fuera de ventana
# (CONFIRMED_EVENT_UPDATE, ACCOUNT_UPDATE, POST_PURCHASE_UPDATE ya no
# funcionan); lo único que queda para fuera de ventana son "Utility
# Messages" (plantillas que Meta RECHAZA si tienen contenido promocional)
# o la nueva API de Marketing Messages (requiere opt-in explícito previo
# del cliente). Ninguna de las dos aplica aquí. Por eso este mecanismo
# SOLO manda DENTRO de la ventana de 24h, nunca después -- si se pasa la
# ventana de seguridad de abajo, simplemente no se manda ese seguimiento,
# en vez de arriesgarse a mandarlo tarde.
#
# Corre en un hilo en background dentro de este mismo proceso -- seguro
# porque Render garantiza una sola instancia para este servicio (tiene
# disco adjunto y "Scaling is not supported for servers with disks",
# confirmado en el dashboard).

MENSAJE_SEGUIMIENTO_PEDIDO = "Hola! Qué tal! gustas que continuemos con tu pedido? Cualquier duda estoy a la orden!"

INTERVALO_SEGUIMIENTO_SEGUNDOS = 15 * 60

# Ventana de seguridad por canal -- cada una se revisa por separado.
# WhatsApp: 23.0-23.5h (1h de colchón antes de las 24h).
# Messenger: 22.0-22.5h -- colchón más amplio (2h) a petición explícita
# de Israel (17 ago 2026): "para que no haya falla y no nos penalice
# Meta" -- ahora mismo la gran mayoría de los clientes entran por
# Messenger, así que aquí se prioriza no arriesgarse nunca a que el hilo
# tarde/el servicio se reinicie y el mensaje se mande ya fuera de
# ventana. En AMBOS casos, si por lo que sea el hilo se atrasa y ya se
# pasó del máximo, es mejor quedarse SIN mandar ese seguimiento a
# mandarlo tarde (esto ya pasó de verdad con notificar_a_dalia, error
# 131047 en WhatsApp -- no se repite aquí, y con Messenger el riesgo es
# todavía mayor por los cambios de política de febrero 2026 arriba).
CONFIG_SEGUIMIENTO_POR_CANAL = {
    "whatsapp": {"horas_min": 23.0, "horas_max": 23.5},
    "messenger": {"horas_min": 22.0, "horas_max": 22.5},
}


def _revisar_seguimientos_canal(canal, horas_min, horas_max):
    try:
        candidatos = pedido_manager.candidatos_seguimiento_23h(horas_min, horas_max, canal=canal)
    except Exception as e:
        print(f"⚠️ [Seguimiento {canal}] Error buscando candidatos: {repr(e)}")
        return

    for c in candidatos:
        telefono = c["telefono"]
        marca = c["ultimo_ts"]
        try:
            # Solo clientes que el bot sigue atendiendo -- esto ya cubre
            # tanto conversaciones silenciadas a mano como pedidos que ya
            # llegaron a anticipo confirmado (ahí modo_atencion pasa a
            # DALIA automáticamente, así que no hace falta revisar esa
            # tabla aparte).
            if pedido_manager.obtener_modo_atencion(telefono) != ModoAtencion.BOT.value:
                continue

            # Solo si de verdad hay un pedido/borrador en progreso -- no
            # mandar "¿continuamos con tu pedido?" a quien nunca llegó a
            # mencionar un producto en concreto.
            borrador = pedido_manager.cargar_borrador_pedido(telefono) or {}
            if not (borrador.get("producto") or borrador.get("items")):
                continue

            # Candado atómico en base de datos para este teléfono/PSID +
            # este momento de silencio exacto -- si ya se reservó antes
            # (por ejemplo en una revisión previa), se detiene aquí y NO
            # se manda de nuevo.
            if not pedido_manager.reclamar_seguimiento_23h(telefono, marca, canal=canal):
                continue

            # 🔧 pagina_id=None a propósito: hoy solo hay UNA página de
            # Facebook configurada (confirmado en Render, sin
            # MESSENGER_PAGE_ID_2 ni variantes), y token_para_pagina()
            # cae de vuelta a la página principal cuando no se pasa
            # pagina_id -- así que esto manda correctamente por esa única
            # página. Si en el futuro se conecta una SEGUNDA página de
            # Facebook, este mecanismo necesitaría guardar de qué página
            # vino cada conversación para elegir el token correcto (hoy
            # no existe esa columna en historial_chat).
            r = enviar_mensaje_canal(telefono, MENSAJE_SEGUIMIENTO_PEDIDO, canal=canal)
            if r is not None:
                crm.guardar_respuesta(telefono, MENSAJE_SEGUIMIENTO_PEDIDO, canal=canal)
                print(f"📨 [Seguimiento {canal}] Mensaje enviado a {telefono} (silencio desde {marca})")
            else:
                print(f"❌ [Seguimiento {canal}] Falló el envío a {telefono} -- ya quedó reservado en "
                      f"seguimientos_23h, no se reintentará para este mismo silencio.")
        except Exception as e:
            print(f"⚠️ [Seguimiento {canal}] Error procesando a {telefono}: {repr(e)}")


def _revisar_seguimientos_una_vez():
    if pedido_manager.bot_pausado_globalmente():
        print("🙅 [Seguimiento] Bot pausado globalmente, se omite esta revisión.")
        return
    for canal, cfg in CONFIG_SEGUIMIENTO_POR_CANAL.items():
        _revisar_seguimientos_canal(canal, cfg["horas_min"], cfg["horas_max"])


def _hilo_seguimientos():
    canales = ", ".join(CONFIG_SEGUIMIENTO_POR_CANAL.keys())
    print(f"🕐 [Seguimiento] Hilo de background iniciado para [{canales}] "
          f"(revisa cada {INTERVALO_SEGUIMIENTO_SEGUNDOS // 60} min).")
    while True:
        try:
            _revisar_seguimientos_una_vez()
        except Exception as e:
            print(f"⚠️ [Seguimiento] Error en la revisión periódica: {repr(e)}")
        # 🔧 (21 ago 2026, a pedido explícito de Israel) Se reutiliza este
        # mismo hilo, que ya corre cada rato, para también limpiar
        # sesiones inactivas de RAM -- así no hace falta un hilo nuevo
        # (evita repetir el mismo problema de hilos sueltos sin control
        # que ya se había corregido antes, ver test_executor_fix.py).
        try:
            _limpiar_sesiones_inactivas()
        except Exception as e:
            print(f"⚠️ [Limpieza de memoria] Error limpiando sesiones inactivas: {repr(e)}")
        time.sleep(INTERVALO_SEGUIMIENTO_SEGUNDOS)


_seguimientos_iniciado = False


def iniciar_hilo_seguimientos():
    global _seguimientos_iniciado
    if _seguimientos_iniciado:
        return
    _seguimientos_iniciado = True
    threading.Thread(target=_hilo_seguimientos, daemon=True).start()


iniciar_hilo_seguimientos()


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    puerto = int(os.getenv("PORT", 5000))
    app.run(port=puerto, debug=debug_mode)
