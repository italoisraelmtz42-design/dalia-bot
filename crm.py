import re
import logging
from typing import Dict, Any, List, Optional

from database import get_db_connection, init_db
from constantes import logger_crm, EstadoPedido, ModoAtencion, OrigenEvento
import pedido_manager

init_db()

def inicializar_base_datos():
    logger_crm.warning("🔄 [Compatibilidad] app.py llamó a crm.inicializar_base_datos(). Ejecutando init_db()...")
    try:
        init_db()
        logger_crm.warning("✅ Base de datos inicializada exitosamente.")
    except Exception as e:
        logger_crm.error(f"❌ Error en crm.inicializar_base_datos: {e}")

# -------------------------------------------------------------
# WRAPPERS ORIGINALES (CRM)
# -------------------------------------------------------------
def cargar_cliente(numero):
    if isinstance(numero, dict): numero = numero.get('numero')
    return {"numero": numero, "nombre": "Cliente Registrado", "estado": "activo"}

def guardar_mensaje_cliente(cliente, texto, tipo):
    telefono = cliente
    if isinstance(cliente, dict): telefono = cliente.get('numero')
    try:
        with get_db_connection() as conn:
            conn.execute("INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)", (telefono, texto, "usuario"))
            conn.commit()
    except Exception as e:
        logger_crm.error(f"Error guardando mensaje de usuario: {e}")
    return {"status": "ok", "mensaje_guardado": True}

def cargar_memoria(cliente, limite: int = 20) -> List[Dict[str, str]]:
    telefono = cliente
    if isinstance(cliente, dict): telefono = cliente.get('numero')
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT mensaje, emisor FROM historial_chat WHERE telefono = ? ORDER BY timestamp DESC LIMIT ?", (telefono, limite))
            rows = cursor.fetchall()
            return [{"role": "user" if e == "usuario" else "assistant", "content": m} for m, e in reversed(rows)]
    except Exception as e:
        logger_crm.error(f"Error cargando memoria: {e}")
        return []

def registrar_uso_openai(*args, **kwargs):
    telefono = None
    if args and args[0]:
        telefono = args[0]
        if isinstance(telefono, dict): telefono = telefono.get('numero')
    try:
        with get_db_connection() as conn:
            conn.execute("INSERT INTO uso_openai (telefono) VALUES (?)", (telefono,))
            conn.commit()
    except Exception: pass

def guardar_respuesta(cliente, respuesta, tipo="texto"):
    telefono = cliente
    if isinstance(cliente, dict): telefono = cliente.get('numero')
    try:
        with get_db_connection() as conn:
            conn.execute("INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)", (telefono, respuesta, "bot"))
            conn.commit()
    except Exception: pass

def pedido_para_ram(*args, **kwargs): return {}
def cargar_pedido(pedido_id): return pedido_manager.obtener_pedido(pedido_id)

# -------------------------------------------------------------
# 🔥 LA CORRECCIÓN FINAL Y EL GUARDIÁN DE SEGURIDAD 🔥
# -------------------------------------------------------------
def sincronizar_pedido(*args, **kwargs):
    # =====================================================================
    # 1. INSTRUMENTACIÓN CON PRINT (GARANTIZADO QUE SE VEA EN RENDER)
    # =====================================================================
    print("="*80)
    print("=== ENTRADA sincronizar_pedido (DEBUG) ===")
    print(f"args = {args}")
    print(f"kwargs = {kwargs}")
    for i, a in enumerate(args):
        print(f"args[{i}] = {type(a).__name__} -> {a}")
    print("="*80)
    # =====================================================================

    # 2. GUARDIÁN DE SEGURIDAD: Detecta el dict en args[0] y lo anula
    pedido_id = None
    datos = {}

    if args:
        # 🚨 ¡EL ASESINO! Si args[0] es un dict, es el CLIENTE. Lo ignoramos.
        if isinstance(args[0], dict):
            print("⚠️ [GUARDIÁN] Detectado dict en args[0] (Cliente). Ignorando.")
            if len(args) > 1:
                if isinstance(args[1], int):
                    pedido_id = args[1]
                elif isinstance(args[1], dict):
                    datos = args[1]
        else:
            # args[0] no es dict, entonces es el pedido_id
            pedido_id = args[0]
            if len(args) > 1:
                datos = args[1]

    # 3. Buscar en kwargs si no lo encontramos
    if not pedido_id and kwargs.get('pedido_id'):
        pedido_id = kwargs.get('pedido_id')
        datos = kwargs
        datos.pop('pedido_id', None)

    # Limpieza final
    if datos and isinstance(datos, dict):
        datos.pop('_req_id', None)

    # 4. Ejecutar SOLO SI pedido_id es un entero válido
    if pedido_id and isinstance(pedido_id, int):
        print(f"✅ sincronizar_pedido: Actualizando pedido {pedido_id}")
        pedido_manager.actualizar_pedido(pedido_id, **datos)
    elif pedido_id:
        print(f"❌ sincronizar_pedido: pedido_id no es un entero ({type(pedido_id).__name__})")
    else:
        print(f"ℹ️ sincronizar_pedido: No se encontró pedido_id válido.")

    return cargar_pedido(pedido_id) if isinstance(pedido_id, int) else {}

# ... Resto de funciones sin cambios ...
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
