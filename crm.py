import re
import logging
from typing import Dict, Any, List, Optional
from dataclasses import asdict

from database import init_db
from constantes import logger_crm, EstadoPedido
import pedido_manager
import conversation_engine

def inicializar_base_datos():
    logger_crm.warning("🔄 [Compatibilidad] app.py llamó a crm.inicializar_base_datos(). Ejecutando init_db()...")
    try:
        init_db()
        logger_crm.warning("✅ Base de datos inicializada exitosamente desde crm.py.")
    except Exception as e:
        logger_crm.error(f"❌ Error en crm.inicializar_base_datos: {e}")

def cargar_cliente(numero):
    if isinstance(numero, dict):
        numero = numero.get('numero')
    return {"numero": numero, "nombre": "Cliente Registrado", "estado": "activo"}

def guardar_mensaje_cliente(cliente, texto, tipo):
    telefono = cliente['numero'] if isinstance(cliente, dict) else cliente
    pedido_manager.chat_guardar_mensaje(telefono, texto, "usuario")
    return {"status": "ok", "mensaje_guardado": True}

def cargar_memoria(cliente, limite: int = 20) -> List[Dict[str, str]]:
    telefono = cliente['numero'] if isinstance(cliente, dict) else cliente
    return pedido_manager.chat_cargar_memoria(telefono, limite)

def registrar_uso_openai(*args, **kwargs):
    telefono = None
    if args and args[0]:
        telefono = args[0]
        if isinstance(telefono, dict):
            telefono = telefono.get('numero')
    if telefono:
        pedido_manager.uso_registrar_openai(telefono)

def guardar_respuesta(cliente, respuesta, tipo="texto"):
    telefono = cliente['numero'] if isinstance(cliente, dict) else cliente
    pedido_manager.chat_guardar_mensaje(telefono, respuesta, "bot")

def pedido_para_ram(*args, **kwargs): return {}

def cargar_pedido(cliente):
    telefono = cliente['numero'] if isinstance(cliente, dict) else cliente
    pedido_id = pedido_manager.obtener_pedido_activo(telefono)
    if pedido_id:
        return pedido_manager.obtener_pedido(pedido_id)
    return None

# ==============================================================================
# 🔧 sincronizar_pedido CORREGIDO – maneja None y conversión segura
# ==============================================================================
def sincronizar_pedido(*args, **kwargs):
    cliente = args[0] if args else {}
    datos_pedido = args[1] if len(args) > 1 else {}
    if kwargs:
        datos_pedido.update(kwargs)

    # Normalizar cliente (puede ser string o dict)
    if isinstance(cliente, str):
        cliente = {"numero": cliente}
    telefono = cliente.get('numero')
    if not telefono:
        logger_crm.error("sincronizar_pedido invocada sin un objeto cliente válido.")
        return {}

    # 1. Cargar borrador desde SQLite
    borrador = pedido_manager.cargar_borrador_pedido(telefono) or {}
    
    # 2. Fusionar con los nuevos datos (si datos_pedido no es None ni dict, lo convertimos)
    if datos_pedido is None:
        datos_pedido = {}
    elif not isinstance(datos_pedido, dict):
        # Intentar convertir a dict usando asdict si es dataclass, o __dict__, o str()
        try:
            if hasattr(datos_pedido, '__dict__'):
                datos_pedido = datos_pedido.__dict__
            else:
                # Si es un objeto sin __dict__, intentamos serializar a dict con asdict (si es dataclass)
                from dataclasses import asdict
                datos_pedido = asdict(datos_pedido)
        except:
            # Fallback: convertir a string y ponerlo en una nota
            datos_pedido = {"_raw": str(datos_pedido)}
    
    # Ahora datos_pedido es un dict
    borrador.update({k: v for k, v in datos_pedido.items() if v is not None})

    # 3. Comprobar si debemos crear el pedido oficial
    debe_crear = (
        datos_pedido.get('anticipo_confirmado') is True
    )
    if debe_crear:
        pedido_id = pedido_manager.obtener_pedido_activo(telefono)
        if not pedido_id:
            cliente_id = cliente.get('id', 0)
            pedido_id = pedido_manager.crear_pedido_desde_borrador(telefono, cliente_id, borrador)
            logger_crm.info(f"🆕 Pedido oficial creado desde borrador. ID: {pedido_id}")
            return pedido_manager.obtener_pedido(pedido_id)
        else:
            logger_crm.info("ℹ️ Ya existe un pedido oficial, no se crea otro.")
    else:
        # Guardar/actualizar borrador
        pedido_manager.guardar_borrador_pedido(telefono, borrador)
        logger_crm.info(f"📝 Borrador actualizado para el teléfono {telefono}")

    return None

# ==============================================================================
# El resto de funciones se mantienen sin cambios
# ==============================================================================
def _detectar_intencion_pedido(texto: str) -> bool:
    return sum(1 for p in ["quiero", "pedir", "comprar", "cotizar", "toalla", "jabón", "jaboncito", "moño", "regalo"] if p in texto.lower()) >= 2

def generar_respuesta_conversacional(cliente, texto: str) -> str:
    telefono = cliente['numero'] if isinstance(cliente, dict) else cliente
    historial = pedido_manager.chat_cargar_memoria(telefono)
    return conversation_engine.procesar_con_gpt(telefono, texto, historial)

def manejar_intencion_pedido(cliente, texto: str) -> str:
    try:
        telefono, cliente_id = cliente['numero'], cliente.get('id', 0)
        borrador = pedido_manager.cargar_borrador_pedido(telefono) or {}
        if not borrador.get('producto'):
            borrador['producto'] = "Toalla Personalizada"
            borrador['cantidad'] = 1
            borrador['precio_unitario'] = 350.0
            pedido_manager.guardar_borrador_pedido(telefono, borrador)
        return "He iniciado tu borrador. ¿Qué producto deseas?"
    except Exception as e:
        logger_crm.error(f"Error en manejar_intencion_pedido: {e}")
        return "❌ Ocurrió un error técnico. Por favor, intenta de nuevo."

MAPEO_PREGUNTAS = {
    "producto": "¿Qué producto deseas pedir? (ej. Toalla, Jabón)",
    "color_toalla": "¿De qué color quieres la toallita?",
    "color_moño": "¿De qué color quieres el moño?",
    "tipo_jaboncito": "¿De qué forma quieres el jaboncito? (corazón, flor, osito, etc.)",
    "nombre_bebe": "¿Cuál es el nombre del bebé para la tarjeta?",
    "tarjetita": "¿Qué mensaje quieres que pongamos en la tarjetita?",
    "tipo_entrega": "¿Cómo quieres tu entrega? (Local o Domicilio)",
    "fecha_entrega": "¿Para qué fecha necesitas el pedido?",
    "direccion": "¿Cuál es la dirección de entrega?",
    "municipio": "¿A qué municipio pertenece la dirección de entrega?"
}
