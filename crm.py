import re
import logging
import uuid
from typing import Dict, Any, List, Optional

from database import get_db_connection, init_db
from constantes import logger_crm, EstadoPedido, ModoAtencion, OrigenEvento
import pedido_manager

# Inicializamos las tablas al arrancar el módulo
init_db()

# ==============================================================================
# # RESTAURACIÓN DE COMPATIBILIDAD: ELIMINA EL ATTRIBUTEERROR
# ==============================================================================
def inicializar_base_datos():
    """
    Función pública restaurada para mantener la compatibilidad con app.py congelado.
    """
    logger_crm.info("🔄 [Compatibilidad] app.py llamó a crm.inicializar_base_datos(). Ejecutando init_db()...")
    try:
        init_db()
        logger_crm.info("✅ Base de datos inicializada exitosamente desde crm.py.")
    except Exception as e:
        logger_crm.error(f"❌ Error en crm.inicializar_base_datos: {e}")

# ==============================================================================
# # WRAPPERS DE CRM (CON LOGS DE ENTRADA Y GENERACIÓN DEL REQ-ID)
# ==============================================================================
def cargar_cliente(numero):
    req_id = f"REQ-{uuid.uuid4().hex[:6].upper()}"
    logger_crm.info("="*80)
    logger_crm.info(f"[{req_id}] [CRM] ENTRADA: cargar_cliente()")
    logger_crm.info(f"[{req_id}] [CRM] Argumento = {numero}")
    logger_crm.info(f"[{req_id}] [CRM] Tipo = {type(numero).__name__}")
    logger_crm.info("="*80)
    
    # El código de lógica queda exactamente como estaba
    if isinstance(numero, dict): numero = numero.get('numero')
    logger_crm.info(f"🔎 [CRM] Cliente: {numero}")
    return {"numero": numero, "nombre": "Cliente Registrado", "estado": "activo"}

def guardar_mensaje_cliente(cliente_ou_telefono, texto, tipo):
    req_id = f"REQ-{uuid.uuid4().hex[:6].upper()}"
    logger_crm.info("="*80)
    logger_crm.info(f"[{req_id}] [CRM] ENTRADA: guardar_mensaje_cliente()")
    logger_crm.info(f"[{req_id}] [CRM] Arg 1 (cliente) = {cliente_ou_telefono}")
    logger_crm.info(f"[{req_id}] [CRM] Arg 1 Tipo = {type(cliente_ou_telefono).__name__}")
    logger_crm.info(f"[{req_id}] [CRM] Arg 2 (texto) = {texto}")
    logger_crm.info(f"[{req_id}] [CRM] Arg 3 (tipo) = {tipo}")
    logger_crm.info("="*80)
    
    # La lógica original sin cambios
    telefono = cliente_ou_telefono
    if isinstance(cliente_ou_telefono, dict):
        telefono = cliente_ou_telefono.get('numero')
    logger_crm.info(f"💾 [CRM] Guardando mensaje para {telefono}")
    try:
        with get_db_connection() as conn:
            # NUEVO: Usamos el helper con req_id
            from database import _exec_sql
            _exec_sql(conn, "INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)", (telefono, texto, "usuario"), req_id=req_id)
            conn.commit()
    except Exception as e:
        logger_crm.error(f"Error guardando mensaje de usuario: {e}")
    return {"status": "ok", "mensaje_guardado": True}

def cargar_memoria(telefono_ou_cliente, limite: int = 20) -> List[Dict[str, str]]:
    req_id = f"REQ-{uuid.uuid4().hex[:6].upper()}"
    logger_crm.info("="*80)
    logger_crm.info(f"[{req_id}] [CRM] ENTRADA: cargar_memoria()")
    logger_crm.info(f"[{req_id}] [CRM] Arg 1 (cliente) = {telefono_ou_cliente}")
    logger_crm.info(f"[{req_id}] [CRM] Arg 1 Tipo = {type(telefono_ou_cliente).__name__}")
    logger_crm.info(f"[{req_id}] [CRM] Arg 2 (limite) = {limite}")
    logger_crm.info("="*80)
    
    # La lógica original sin cambios
    telefono = telefono_ou_cliente
    if isinstance(telefono_ou_cliente, dict):
        telefono = telefono_ou_cliente.get('numero')
    logger_crm.info(f"🧠 [CRM] Cargando memoria para {telefono}")
    try:
        with get_db_connection() as conn:
            from database import _exec_sql
            cursor = _exec_sql(conn, "SELECT mensaje, emisor FROM historial_chat WHERE telefono = ? ORDER BY timestamp DESC LIMIT ?", (telefono, limite), req_id=req_id)
            rows = cursor.fetchall()
            return [{"role": "user" if e == "usuario" else "assistant", "content": m} for m, e in reversed(rows)]
    except Exception as e:
        logger_crm.error(f"Error cargando memoria: {e}")
        return []

def registrar_uso_openai(*args, **kwargs):
    req_id = f"REQ-{uuid.uuid4().hex[:6].upper()}"
    logger_crm.info("="*80)
    logger_crm.info(f"[{req_id}] [CRM] ENTRADA: registrar_uso_openai()")
    logger_crm.info(f"[{req_id}] [CRM] args = {args}")
    logger_crm.info(f"[{req_id}] [CRM] kwargs = {kwargs}")
    logger_crm.info("="*80)
    
    # Lógica original sin cambios
    telefono = None
    if args and args[0]:
        telefono = args[0]
        if isinstance(telefono, dict):
            telefono = telefono.get('numero')
    logger_crm.info(f"🤖 [CRM] Registrando uso OpenAI para {telefono}")
    try:
        with get_db_connection() as conn:
            from database import _exec_sql
            _exec_sql(conn, "INSERT INTO uso_openai (telefono) VALUES (?)", (telefono,), req_id=req_id)
            conn.commit()
    except Exception:
        pass

def guardar_respuesta(cliente_ou_telefono, respuesta, tipo="texto"):
    req_id = f"REQ-{uuid.uuid4().hex[:6].upper()}"
    logger_crm.info("="*80)
    logger_crm.info(f"[{req_id}] [CRM] ENTRADA: guardar_respuesta()")
    logger_crm.info(f"[{req_id}] [CRM] Arg 1 (cliente) = {cliente_ou_telefono}")
    logger_crm.info(f"[{req_id}] [CRM] Arg 1 Tipo = {type(cliente_ou_telefono).__name__}")
    logger_crm.info(f"[{req_id}] [CRM] Arg 2 (respuesta) = {respuesta}")
    logger_crm.info("="*80)
    
    # Lógica original sin cambios
    telefono = cliente_ou_telefono
    if isinstance(cliente_ou_telefono, dict):
        telefono = cliente_ou_telefono.get('numero')
    logger_crm.info(f"📤 [CRM] Guardando respuesta para {telefono}")
    try:
        with get_db_connection() as conn:
            from database import _exec_sql
            _exec_sql(conn, "INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)", (telefono, respuesta, "bot"), req_id=req_id)
            conn.commit()
    except Exception:
        pass

def pedido_para_ram(*args, **kwargs): return {}
def cargar_pedido(pedido_id): return pedido_manager.obtener_pedido(pedido_id)

def sincronizar_pedido(*args, **kwargs):
    req_id = f"REQ-{uuid.uuid4().hex[:6].upper()}"
    logger_crm.info("="*80)
    logger_crm.info(f"[{req_id}] [CRM] ENTRADA: sincronizar_pedido()")
    logger_crm.info(f"[{req_id}] [CRM] args = {args}")
    logger_crm.info(f"[{req_id}] [CRM] kwargs = {kwargs}")
    logger_crm.info("="*80)
    
    # Lógica original sin cambios
    pedido_id, datos = None, {}
    if args:
        pedido_id = args[0]
        if len(args) > 1: datos = args[1]
    elif kwargs.get('pedido_id'): pedido_id, datos = kwargs.get('pedido_id'), kwargs
    if pedido_id and datos:
        logger_crm.info(f"🔄 [CRM] Sincronizando Pedido {pedido_id}")
        pedido_manager.actualizar_pedido(pedido_id, _req_id=req_id, **datos)
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
