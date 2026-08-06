import datetime
import logging
import re
from typing import Dict, Any, List, Optional

# Importamos database para consultas SQL directas y el motor de pedidos
from database import get_db_connection
from pedido_manager import (
    crear_pedido, agregar_producto, generar_resumen, 
    cambiar_estado, obtener_pedido, campos_faltantes,
    PedidoError
)

logger = logging.getLogger(__name__)

def cargar_cliente(numero):
    """
    Busca o registra un cliente en el sistema.
    """
    logger.info(f"🔎 [CRM] Buscando/registrando cliente con número: {numero}")
    
    # Lógica para retornar el objeto cliente
    cliente_data = {
        "numero": numero,
        "nombre": "Cliente Registrado",
        "fecha_creacion": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "estado": "activo"
    }
    return cliente_data

def guardar_mensaje_cliente(cliente, texto, tipo):
    """
    Guarda el mensaje recibido asociado al cliente.
    """
    logger.info(f"💾 [CRM] Guardando mensaje para cliente {cliente['numero']}")
    telefono = cliente['numero']
    
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)",
                (telefono, texto, "usuario")
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Error guardando mensaje de usuario en BD: {e}")

    return {"status": "ok", "mensaje_guardado": True}

# ==============================================================================
# FUNCIONES DE COMPATIBILIDAD HACIA ATRÁS (Las que pide app.py)
# ==============================================================================

def cargar_memoria(telefono: str, limite: int = 20) -> List[Dict[str, str]]:
    """
    Función adaptadora recuperada. Carga el historial de chat desde SQLite.
    Retorna una lista de diccionarios en formato compatible con OpenAI (role, content).
    """
    try:
        with get_db_connection() as conn:
            conn.row_factory = None # Usamos fetchall por defecto
            cursor = conn.cursor()
            cursor.execute("""
                SELECT mensaje, emisor FROM historial_chat 
                WHERE telefono = ? 
                ORDER BY timestamp DESC LIMIT ?
            """, (telefono, limite))
            
            rows = cursor.fetchall()
            
            # Convertir el historial al formato que OpenAI espera
            historial = []
            for mensaje, emisor in reversed(rows):
                role = "user" if emisor == "usuario" else "assistant"
                historial.append({"role": role, "content": mensaje})
            
            return historial
            
    except Exception as e:
        logger.error(f"Error cargando memoria para {telefono}: {e}")
        return [] # Si falla, retorna vacío para no matar la conversación

def registrar_uso_openai(telefono: str):
    """
    Función adaptadora recuperada. Registra en BD que se hizo una llamada a OpenAI.
    """
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO uso_openai (telefono) VALUES (?)",
                (telefono,)
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Error registrando uso de OpenAI para {telefono}: {e}")

def guardar_respuesta(cliente, respuesta, tipo="texto"):
    """
    Función adaptadora recuperada. Guarda la respuesta generada por el bot en la BD.
    """
    try:
        telefono = cliente['numero'] if isinstance(cliente, dict) else cliente
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)",
                (telefono, respuesta, "bot")
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Error guardando respuesta del bot en BD: {e}")

# ==============================================================================
# MOTOR DE PEDIDOS (Nuevo diseño)
# ==============================================================================

def _detectar_intencion_pedido(texto: str) -> bool:
    """Lógica simple de detección de intención de compra."""
    palabras_clave = ["quiero", "pedir", "comprar", "cotizar", "toalla", "jabón", "jaboncito", "moño", "regalo", "baby", "bebé"]
    texto_lower = texto.lower()
    
    coincidencias = sum(1 for palabra in palabras_clave if palabra in texto_lower)
    return coincidencias >= 2

def manejar_intencion_pedido(cliente, texto: str) -> str:
    """
    Procesa la intención de un pedido y devuelve el resumen al chat.
    """
    try:
        telefono = cliente['numero']
        cliente_id = cliente.get('id', 0)

        pedido_id = crear_pedido(cliente_id, telefono)
        
        producto_detectado = "Toalla Personalizada"
        cantidad_detectada = 1
        precio_unitario = 350.0
        
        match_cantidad = re.search(r'(\d+)\s*(toalla|jabon)', texto.lower())
        if match_cantidad:
            cantidad_detectada = int(match_cantidad.group(1))
            if 'jabon' in match_cantidad.group(2):
                producto_detectado = "Jabón Personalizado"

        agregar_producto(pedido_id, producto_detectado, cantidad_detectada, precio_unitario)
        cambiar_estado(pedido_id, "CAPTURANDO_DATOS")
        
        _, faltantes = obtener_porcentaje_completitud(pedido_id)
        
        resumen = generar_resumen(pedido_id)
        mensaje_respuesta = (
            f"{resumen}\n\n"
            f"📝 ¡Perfecto! He creado tu pedido. Para finalizar, necesito que me confirmes estos datos:\n"
            f"👉 **Faltan por capturar:** {', '.join(faltantes)}\n"
            f"Puedes enviarme la información en el siguiente mensaje."
        )
        
        return mensaje_respuesta

    except PedidoError as e:
        logger.error(f"Error en el motor de pedidos: {str(e)}")
        return f"❌ Ocurrió un error al intentar crear tu pedido: {str(e)}"
    except Exception as e:
        logger.error(f"Error inesperado en manejar_intencion_pedido: {e}")
        return "❌ Ocurrió un error técnico procesando tu solicitud. Por favor, intenta de nuevo."
