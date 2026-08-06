import datetime
import logging
import re
from typing import Dict, Any

# Importamos nuestro nuevo módulo
from pedido_manager import (
    crear_pedido, agregar_producto, generar_resumen, 
    cambiar_estado, obtener_pedido, campos_faltantes,
    PedidoError, calcular_saldo
)

logger = logging.getLogger(__name__)

def cargar_cliente(numero):
    """
    Busca o registra un cliente en el sistema.
    """
    logger.info(f"🔎 [CRM] Buscando/registrando cliente con número: {numero}")
    
    # --- AQUÍ TU LÓGICA DE OBTENCIÓN DE CLIENTE (BASE DE DATOS EXISTENTE) ---
    # Ejemplo simulado de retorno de datos
    cliente_data = {
        "numero": numero,
        "nombre": "Cliente Registrado",
        "fecha_creacion": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "estado": "activo"
    }
    return cliente_data


def guardar_mensaje_cliente(cliente, texto, tipo):
    """
    Guarda el mensaje recibido asociado al cliente (Función original).
    """
    logger.info(f"💾 [CRM] Guardando mensaje para cliente {cliente['numero']}")
    logger.info(f"📝 Mensaje: {texto}")
    logger.info(f"🏷️  Tipo: {tipo}")
    
    # Aquí iría tu INSERT original en la tabla de mensajes o bitácora.
    return {"status": "ok", "mensaje_guardado": True}


# --- NUEVAS FUNCIONES DE ORQUESTACIÓN DEL MOTOR DE PEDIDOS ---

def _detectar_intencion_pedido(texto: str) -> bool:
    """Lógica simple de detección de intención de compra."""
    palabras_clave = ["quiero", "pedir", "comprar", "cotizar", "toalla", "jabón", "jaboncito", "moño", "regalo", "baby", "bebé"]
    texto_lower = texto.lower()
    
    # Si contiene al menos 2 palabras clave (para no disparar falsos positivos con saludos simples)
    coincidencias = sum(1 for palabra in palabras_clave if palabra in texto_lower)
    return coincidencias >= 2


def manejar_intencion_pedido(cliente, texto: str) -> str:
    """
    Procesa la intención de un pedido.
    Crea el pedido en BORRADOR, agrega el producto base, y devuelve el resumen al chat.
    """
    try:
        telefono = cliente['numero']
        cliente_id = cliente.get('id', 0) # Si no tenemos ID de BD, usamos 0 para cliente externo

        # 1. Crear el pedido (Estado: BORRADOR)
        pedido_id = crear_pedido(cliente_id, telefono)
        
        # 2. Lógica de parseo simple para detectar producto (Ejemplo)
        # En un sistema real, aquí se usarían modelos de IA o RegEx más robustos,
        # pero por restricción de prompt, usaremos un parseo básico para no romper el sistema actual.
        producto_detectado = "Toalla Personalizada"
        cantidad_detectada = 1
        precio_unitario = 350.0 # Precio default de ejemplo
        
        # Si el texto dice "2 toallas", intentamos capturar la cantidad
        match_cantidad = re.search(r'(\d+)\s*(toalla|jabon)', texto.lower())
        if match_cantidad:
            cantidad_detectada = int(match_cantidad.group(1))
            if 'jabon' in match_cantidad.group(2):
                producto_detectado = "Jabón Personalizado"

        # 3. Agregar el producto al pedido
        agregar_producto(pedido_id, producto_detectado, cantidad_detectada, precio_unitario)
        
        # 4. Cambiar el estado automáticamente a CAPTURANDO_DATOS
        cambiar_estado(pedido_id, "CAPTURANDO_DATOS")
        
        # 5. Obtener porcentaje y campos faltantes para guiar al usuario
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
        logger.error(f"Error en el motor de pedidos para cliente {telefono}: {str(e)}")
        return f"❌ Ocurrió un error al intentar crear tu pedido: {str(e)}"
    except Exception as e:
        logger.error(f"Error inesperado en manejar_intencion_pedido: {e}")
        return "❌ Ocurrió un error técnico procesando tu solicitud. Por favor, intenta de nuevo."
