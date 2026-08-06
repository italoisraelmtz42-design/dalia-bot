import datetime
import logging
import re
from typing import Dict, Any, List, Optional

# Importamos database para consultas SQL directas
from database import get_db_connection

logger = logging.getLogger(__name__)

# ==============================================================================
# FUNCIONES ORIGINALES Y DE COMPATIBILIDAD HACIA ATRÁS
# ==============================================================================

def cargar_cliente(numero):
    """
    Busca o registra un cliente en el sistema.
    """
    logger.info(f"🔎 [CRM] Buscando/registrando cliente con número: {numero}")
    
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

def cargar_memoria(telefono: str, limite: int = 20) -> List[Dict[str, str]]:
    """
    Función adaptadora recuperada. Carga el historial de chat desde SQLite.
    """
    try:
        with get_db_connection() as conn:
            conn.row_factory = None 
            cursor = conn.cursor()
            cursor.execute("""
                SELECT mensaje, emisor FROM historial_chat 
                WHERE telefono = ? 
                ORDER BY timestamp DESC LIMIT ?
            """, (telefono, limite))
            
            rows = cursor.fetchall()
            historial = []
            for mensaje, emisor in reversed(rows):
                role = "user" if emisor == "usuario" else "assistant"
                historial.append({"role": role, "content": mensaje})
            return historial
            
    except Exception as e:
        logger.error(f"Error cargando memoria para {telefono}: {e}")
        return []

# --- Error 4: Compatibilidad exacta de parámetros ---
def registrar_uso_openai(telefono_ou_cliente):
    """
    Función adaptadora. Acepta exactamente 1 argumento, ya sea un string (teléfono) 
    o un diccionario (cliente), tal y como app.py lo envíe.
    """
    telefono = telefono_ou_cliente
    if isinstance(telefono_ou_cliente, dict):
        telefono = telefono_ou_cliente.get('numero')
    
    try:
        with get_db_connection() as conn:
            conn.execute("INSERT INTO uso_openai (telefono) VALUES (?)", (telefono,))
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

# --- Error 2: Wrapper para cargar_pedido() ---
def cargar_pedido(pedido_id):
    """
    Wrapper de compatibilidad para cargar un pedido.
    """
    try:
        from pedido_manager import obtener_pedido
        return obtener_pedido(pedido_id)
    except Exception as e:
        logger.error(f"Error en cargar_pedido wrapper: {e}")
        return None

# --- Error 3: Wrapper para sincronizar_pedido() ---
def sincronizar_pedido(pedido_id, **kwargs):
    """
    Wrapper de compatibilidad para sincronizar un pedido.
    Soporta actualizaciones de cualquier campo que app.py envíe por kwargs.
    """
    try:
        from pedido_manager import actualizar_pedido
        if kwargs:
            actualizar_pedido(pedido_id, **kwargs)
        # Retorna el pedido actualizado para que app.py pueda usarlo si lo necesita
        return cargar_pedido(pedido_id)
    except Exception as e:
        logger.error(f"Error en sincronizar_pedido wrapper: {e}")
        return None

# ==============================================================================
# MOTOR DE PEDIDOS (Mantenido sin cambios en su lógica)
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
        from pedido_manager import (
            crear_pedido, agregar_producto, generar_resumen, 
            cambiar_estado, obtener_porcentaje_completitud,
            PedidoError
        )
        
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

    except Exception as e:
        logger.error(f"Error inesperado en manejar_intencion_pedido: {e}")
        return "❌ Ocurrió un error técnico procesando tu solicitud. Por favor, intenta de nuevo."
