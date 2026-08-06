import datetime
import logging
import re
from typing import Dict, Any, List, Optional

from database import get_db_connection, init_db
import pedido_manager

init_db()
logger = logging.getLogger(__name__)

# ==============================================================================
# # Wrappers de Compatibilidad (Hacia atrás para app.py)
# ==============================================================================
def cargar_cliente(numero):
    logger.info(f"🔎 [CRM] Cliente: {numero}")
    return {"numero": numero, "nombre": "Cliente Registrado", "estado": "activo"}

def guardar_mensaje_cliente(cliente, texto, tipo):
    logger.info(f"💾 [CRM] Mensaje para {cliente['numero']}")
    telefono = cliente['numero']
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)",
                (telefono, texto, "usuario")
            )
            conn.commit()
    except Exception:
        pass
    return {"status": "ok", "mensaje_guardado": True}

def cargar_memoria(telefono: str, limite: int = 20) -> List[Dict[str, str]]:
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT mensaje, emisor FROM historial_chat WHERE telefono = ? ORDER BY timestamp DESC LIMIT ?",
                (telefono, limite)
            )
            rows = cursor.fetchall()
            historial = []
            for mensaje, emisor in reversed(rows):
                role = "user" if emisor == "usuario" else "assistant"
                historial.append({"role": role, "content": mensaje})
            return historial
    except Exception:
        return []

def registrar_uso_openai(*args, **kwargs):
    telefono = None
    if args and args[0]:
        telefono = args[0]
        if isinstance(telefono, dict):
            telefono = telefono.get('numero')
    try:
        with get_db_connection() as conn:
            conn.execute("INSERT INTO uso_openai (telefono) VALUES (?)", (telefono,))
            conn.commit()
    except Exception:
        pass

def guardar_respuesta(cliente, respuesta, tipo="texto"):
    try:
        telefono = cliente['numero'] if isinstance(cliente, dict) else cliente
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)",
                (telefono, respuesta, "bot")
            )
            conn.commit()
    except Exception:
        pass

def pedido_para_ram(*args, **kwargs):
    return {}

def cargar_pedido(pedido_id):
    return pedido_manager.obtener_pedido(pedido_id)

def sincronizar_pedido(*args, **kwargs):
    pedido_id = None
    datos = {}
    try:
        if args:
            pedido_id = args[0]
            if len(args) > 1:
                datos = args[1]
        if pedido_id and datos:
            pedido_manager.actualizar_pedido(pedido_id, **datos)
        return cargar_pedido(pedido_id)
    except Exception:
        return None

# ==============================================================================
# # Capa de Conversación (Obs 6: Traduce campo a pregunta)
# ==============================================================================
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

def _detectar_intencion_pedido(texto: str) -> bool:
    palabras_clave = ["quiero", "pedir", "comprar", "cotizar", "toalla", "jabón", "jaboncito", "moño", "regalo"]
    return sum(1 for palabra in palabras_clave if palabra in texto.lower()) >= 2

def manejar_intencion_pedido(cliente, texto: str) -> str:
    try:
        telefono = cliente['numero']
        cliente_id = cliente.get('id', 0)

        # 1. Crear pedido en BORRADOR
        pedido_id = pedido_manager.crear_pedido(cliente_id, telefono)
        
        # 2. Detectar producto
        producto_detectado = "Toalla Personalizada"
        cantidad_detectada = 1
        precio_unitario = 350.0
        
        match_cantidad = re.search(r'(\d+)\s*(toalla|jabon)', texto.lower())
        if match_cantidad:
            cantidad_detectada = int(match_cantidad.group(1))
            if 'jabon' in match_cantidad.group(2):
                producto_detectado = "Jabón Personalizado"

        # 3. Agregar producto
        pedido_manager.agregar_producto(pedido_id, producto_detectado, cantidad_detectada, precio_unitario)
        pedido_manager.cambiar_estado(pedido_id, "CAPTURANDO_DATOS")

        # 4. Preguntar por los campos faltantes (La lógica de negocio solo da el campo)
        campos_faltantes = pedido_manager.obtener_campos_faltantes(pedido_id)

        if not campos_faltantes:
            resumen = pedido_manager.generar_resumen(pedido_id)
            return f"{resumen}\n\n✅ ¡Tu pedido está completo! Para reservarlo, te solicitamos un anticipo de $50 MXN. ¿Te parece bien?"
        else:
            # La capa de conversación (CRM) decide cómo preguntar
            campo_faltante = max(campos_faltantes, key=lambda x: x['prioridad'])
            campo = campo_faltante['campo']
            pregunta = MAPEO_PREGUNTAS.get(campo, f"Por favor, indícanos el dato: {campo}")
            
            resumen = pedido_manager.generar_resumen(pedido_id)
            return f"{resumen}\n\n📝 Para completar tu pedido, necesito un dato más:\n👉 {pregunta}"

    except Exception as e:
        logger.error(f"Error en manejar_intencion_pedido: {e}")
        return "❌ Ocurrió un error técnico procesando tu solicitud. Por favor, intenta de nuevo."
