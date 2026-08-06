import re
import logging
from typing import Dict, Any, List, Optional

from database import get_db_connection, init_db
from constantes import logger_crm, EstadoPedido, ModoAtencion, OrigenEvento
import pedido_manager

# Inicializamos las tablas al arrancar el módulo
init_db()

# ==============================================================================
# # COMPATIBILIDAD CON APP.PY (ELIMINA EL ATTRIBUTEERROR)
# ==============================================================================
def inicializar_base_datos():
    logger_crm.info("🔄 [Compatibilidad] app.py llamó a crm.inicializar_base_datos(). Ejecutando init_db()...")
    try:
        init_db()
        logger_crm.info("✅ Base de datos inicializada exitosamente desde crm.py.")
    except Exception as e:
        logger_crm.error(f"❌ Error en crm.inicializar_base_datos: {e}")

# ==============================================================================
# # FUNCIONES DEL CRM (INSTRUMENTACIÓN PURA - SIN DEPENDENCIAS PRIVADAS)
# ==============================================================================

def cargar_cliente(numero):
    logger_crm.info("="*80)
    logger_crm.info("=== ENTRADA cargar_cliente ===")
    logger_crm.info(f"numero={repr(numero)}")
    logger_crm.info(f"tipo={type(numero).__name__}")
    logger_crm.info("="*80)
    
    if isinstance(numero, dict): numero = numero.get('numero')
    return {"numero": numero, "nombre": "Cliente Registrado", "estado": "activo"}

def guardar_mensaje_cliente(cliente, texto, tipo):
    logger_crm.info("="*80)
    logger_crm.info("=== ENTRADA guardar_mensaje_cliente ===")
    logger_crm.info(f"cliente={repr(cliente)}")
    logger_crm.info(f"tipo_cliente={type(cliente).__name__}")
    logger_crm.info(f"texto={repr(texto)}")
    logger_crm.info(f"tipo_texto={type(texto).__name__}")
    logger_crm.info("="*80)
    
    telefono = cliente
    if isinstance(cliente, dict):
        telefono = cliente.get('numero')
    
    try:
        with get_db_connection() as conn:
            logger_crm.info("=== SQL guardar_mensaje_cliente ===")
            sql = "INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)"
            params = (telefono, texto, "usuario")
            logger_crm.info(f"SQL = {sql}")
            logger_crm.info(f"PARAMS = {repr(params)}")
            logger_crm.info(f"TYPES = {[type(p).__name__ for p in params]}")
            logger_crm.info("="*80)
            
            conn.execute(sql, params)
            conn.commit()
    except Exception as e:
        logger_crm.error(f"Error guardando mensaje de usuario: {e}")
    return {"status": "ok", "mensaje_guardado": True}

def cargar_memoria(cliente, limite: int = 20) -> List[Dict[str, str]]:
    logger_crm.info("="*80)
    logger_crm.info("=== ENTRADA cargar_memoria ===")
    logger_crm.info(f"cliente={repr(cliente)}")
    logger_crm.info(f"tipo_cliente={type(cliente).__name__}")
    logger_crm.info(f"limite={limite}")
    logger_crm.info("="*80)
    
    telefono = cliente
    if isinstance(cliente, dict):
        telefono = cliente.get('numero')
    
    try:
        with get_db_connection() as conn:
            logger_crm.info("=== SQL cargar_memoria ===")
            sql = "SELECT mensaje, emisor FROM historial_chat WHERE telefono = ? ORDER BY timestamp DESC LIMIT ?"
            params = (telefono, limite)
            logger_crm.info(f"SQL = {sql}")
            logger_crm.info(f"PARAMS = {repr(params)}")
            logger_crm.info(f"TYPES = {[type(p).__name__ for p in params]}")
            logger_crm.info("="*80)
            
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            return [{"role": "user" if e == "usuario" else "assistant", "content": m} for m, e in reversed(rows)]
    except Exception as e:
        logger_crm.error(f"Error cargando memoria: {e}")
        return []

def registrar_uso_openai(*args, **kwargs):
    logger_crm.info("="*80)
    logger_crm.info("=== ENTRADA registrar_uso_openai ===")
    logger_crm.info(f"args={repr(args)}")
    logger_crm.info(f"kwargs={repr(kwargs)}")
    logger_crm.info("="*80)
    
    telefono = None
    if args and args[0]:
        telefono = args[0]
        if isinstance(telefono, dict):
            telefono = telefono.get('numero')
    
    try:
        with get_db_connection() as conn:
            logger_crm.info("=== SQL registrar_uso_openai ===")
            sql = "INSERT INTO uso_openai (telefono) VALUES (?)"
            params = (telefono,)
            logger_crm.info(f"SQL = {sql}")
            logger_crm.info(f"PARAMS = {repr(params)}")
            logger_crm.info(f"TYPES = {[type(p).__name__ for p in params]}")
            logger_crm.info("="*80)
            
            conn.execute(sql, params)
            conn.commit()
    except Exception:
        pass

def guardar_respuesta(cliente, respuesta, tipo="texto"):
    logger_crm.info("="*80)
    logger_crm.info("=== ENTRADA guardar_respuesta ===")
    logger_crm.info(f"cliente={repr(cliente)}")
    logger_crm.info(f"tipo_cliente={type(cliente).__name__}")
    logger_crm.info(f"respuesta={repr(respuesta)}")
    logger_crm.info("="*80)
    
    telefono = cliente
    if isinstance(cliente, dict):
        telefono = cliente.get('numero')
    
    try:
        with get_db_connection() as conn:
            logger_crm.info("=== SQL guardar_respuesta ===")
            sql = "INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)"
            params = (telefono, respuesta, "bot")
            logger_crm.info(f"SQL = {sql}")
            logger_crm.info(f"PARAMS = {repr(params)}")
            logger_crm.info(f"TYPES = {[type(p).__name__ for p in params]}")
            logger_crm.info("="*80)
            
            conn.execute(sql, params)
            conn.commit()
    except Exception:
        pass

def pedido_para_ram(*args, **kwargs): return {}
def cargar_pedido(pedido_id): return pedido_manager.obtener_pedido(pedido_id)

def sincronizar_pedido(*args, **kwargs):
    logger_crm.info("="*80)
    logger_crm.info("=== ENTRADA sincronizar_pedido ===")
    logger_crm.info(f"args={repr(args)}")
    logger_crm.info(f"kwargs={repr(kwargs)}")
    logger_crm.info("="*80)
    
    pedido_id, datos = None, {}
    if args:
        pedido_id = args[0]
        if len(args) > 1: datos = args[1]
    elif kwargs.get('pedido_id'): pedido_id, datos = kwargs.get('pedido_id'), kwargs
    
    if pedido_id and datos:
        # --- CAMBIO DE SEGURIDAD: ELIMINAMOS _req_id EXPLÍCITAMENTE ---
        datos = dict(datos)
        datos.pop('_req_id', None)  # <-- Nunca debe viajar al motor
        # -----------------------------------------------------------
        
        logger_crm.info(f"🔄 [CRM] Sincronizando Pedido {pedido_id}")
        logger_crm.info(f"CLAVES_DATOS: {list(datos.keys())}")
        pedido_manager.actualizar_pedido(pedido_id, **datos)
    return cargar_pedido(pedido_id)

def _detectar_intencion_pedido(texto: str) -> bool:
    return sum(1 for p in ["quiero", "pedir", "comprar", "cotizar", "toalla", "jabón", "jaboncito", "moño", "regalo"] if p in texto.lower()) >= 2

def manejar_intencion_pedido(cliente, texto: str) -> str:
    try:
        telefono, cliente_id = cliente['numero'], cliente.get('id', 0)
        pedido_id = pedido_manager.crear_pedido(cliente_id, telefono)
        producto_detectado, cantidad_detectada, precio_unitario = "Toalla Personalizada", 1, 350.0
        match_cantidad = re.search(r'(\d+)\s*(toalla|jabon)', texto.lower())
        if match_cantidad:
            cantidad_detectada = int(match_cantidad.group(1))
            if 'jabon' in match_cantidad.group(2): producto_detectado = "Jabón Personalizado"
        pedido_manager.agregar_producto(pedido_id, producto_detectado, cantidad_detectada, precio_unitario)
        pedido_manager.cambiar_estado(pedido_id, EstadoPedido.CAPTURANDO_DATOS.value)
        campos_faltantes = pedido_manager.obtener_campos_faltantes(pedido_id)
        if not campos_faltantes:
            return f"{pedido_manager.generar_resumen(pedido_id)}\n\n✅ ¡Tu pedido está completo! Para reservarlo, te solicitamos un anticipo de $50 MXN. ¿Te parece bien?"
        else:
            max_campo = max(campos_faltantes, key=lambda x: x['prioridad'])
            pregunta = MAPEO_PREGUNTAS.get(max_campo['campo'], f"Por favor, indícanos el dato: {max_campo['campo']}")
            return f"{pedido_manager.generar_resumen(pedido_id)}\n\n📝 Para completar tu pedido, necesito un dato más:\n👉 {pregunta}"
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
