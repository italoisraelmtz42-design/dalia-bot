import re
import logging
from typing import Dict, Any, List, Optional

from database import init_db
from constantes import logger_crm, EstadoPedido
import pedido_manager
import conversation_engine

# COMPATIBILIDAD CON APP.PY
def inicializar_base_datos():
    logger_crm.warning("🔄 [Compatibilidad] app.py llamó a crm.inicializar_base_datos(). Ejecutando init_db()...")
    try:
        init_db()
        logger_crm.warning("✅ Base de datos inicializada exitosamente desde crm.py.")
    except Exception as e:
        logger_crm.error(f"❌ Error en crm.inicializar_base_datos: {e}")

def cargar_cliente(numero):
    if isinstance(numero, dict): numero = numero.get('numero')
    return {"numero": numero, "nombre": "Cliente Registrado", "estado": "activo"}

def guardar_mensaje_cliente(cliente, texto, tipo):
    telefono = cliente['numero'] if isinstance(cliente, dict) else cliente
    pedido_manager.chat_guardar_mensaje(telefono, texto, "usuario") # AHORA ESTO SÍ EXISTE
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

def sincronizar_pedido(*args, **kwargs):
    # ... (El adaptador de pedidos completo que ya tienes) ...
    pass

def _detectar_intencion_pedido(texto: str) -> bool:
    return sum(1 for p in ["quiero", "pedir", "comprar", "cotizar", "toalla", "jabón", "jaboncito", "moño", "regalo"] if p in texto.lower()) >= 2

def generar_respuesta_conversacional(cliente, texto: str) -> str:
    telefono = cliente['numero'] if isinstance(cliente, dict) else cliente
    historial = pedido_manager.chat_cargar_memoria(telefono)
    return conversation_engine.procesar_con_gpt(telefono, texto, historial)

def manejar_intencion_pedido(cliente, texto: str) -> str:
    try:
        # ... (Lógica de creación de pedidos y resumen) ...
        return f"{pedido_manager.generar_resumen(pedido_id)}\n\n✅ ¡Tu pedido está listo!"
    except Exception as e:
        logger_crm.error(f"Error en manejar_intencion_pedido: {e}")
        return "❌ Ocurrió un error técnico. Por favor, intenta de nuevo."
