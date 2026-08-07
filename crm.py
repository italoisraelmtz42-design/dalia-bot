import re
import logging
from typing import Dict, Any, List, Optional

from database import init_db
from constantes import logger_crm, EstadoPedido
import pedido_manager

# ==============================================================================
# # COMPATIBILIDAD CON APP.PY (ELIMINA EL ATTRIBUTEERROR)
# ==============================================================================
def inicializar_base_datos():
    """
    Función requerida por app.py para inicializar la base de datos.
    Mantiene la compatibilidad hacia atrás. NO contiene lógica de negocio.
    """
    logger_crm.warning("🔄 [Compatibilidad] app.py llamó a crm.inicializar_base_datos(). Ejecutando init_db()...")
    try:
        init_db()
        logger_crm.warning("✅ Base de datos inicializada exitosamente desde crm.py.")
    except Exception as e:
        logger_crm.error(f"❌ Error en crm.inicializar_base_datos: {e}")

# ==============================================================================
# # ADAPTADOR DE CRM
# ==============================================================================

def cargar_cliente(numero):
    if isinstance(numero, dict): numero = numero.get('numero')
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

def sincronizar_pedido(*args, **kwargs):
    cliente = args[0] if args else {}
    datos_pedido = args[1] if len(args) > 1 else {}
    if kwargs:
        datos_pedido.update(kwargs)

    telefono = cliente.get('numero')
    if not telefono:
        logger_crm.error("sincronizar_pedido invocada sin un objeto cliente válido.")
        return {}

    pedido_id = pedido_manager.obtener_pedido_activo(telefono)
    if not pedido_id:
        cliente_id = cliente.get('id', 0)
        pedido_id = pedido_manager.crear_pedido(cliente_id, telefono)

    if datos_pedido.get('producto') and datos_pedido.get('cantidad'):
        try:
            pedido_manager.agregar_producto(
                pedido_id, 
                datos_pedido['producto'], 
                datos_pedido['cantidad'],
                datos_pedido.get('precio_unitario', 0.0),
                color_toalla=datos_pedido.get('color_toalla'),
                color_moño=datos_pedido.get('color_moño'),
                tipo_jaboncito=datos_pedido.get('tipo_jaboncito'),
                color_jaboncito=datos_pedido.get('color_jaboncito'),
                nombre_bebe=datos_pedido.get('nombre_bebe'),
                tarjetita=datos_pedido.get('tarjetita')
            )
        except Exception as e:
            logger_crm.error(f"Error al agregar producto en sincronizar_pedido: {e}")

    if datos_pedido.get('tipo_entrega'):
        try:
            pedido_manager.actualizar_entrega(
                pedido_id,
                tipo_entrega=datos_pedido['tipo_entrega'],
                municipio=datos_pedido.get('municipio'),
                direccion=datos_pedido.get('direccion'),
                fecha_entrega=datos_pedido.get('fecha_entrega'),
                costo_envio=datos_pedido.get('costo_envio', 0.0)
            )
        except Exception as e:
            logger_crm.error(f"Error al actualizar entrega en sincronizar_pedido: {e}")

    update_kwargs = {k: v for k, v in datos_pedido.items() if k in ['estado', 'modo_atencion', 'es_urgente']}
    if update_kwargs:
        try:
            pedido_manager.actualizar_pedido(pedido_id, **update_kwargs)
        except Exception as e:
            logger_crm.error(f"Error al actualizar estado del pedido en sincronizar_pedido: {e}")

    return pedido_manager.obtener_pedido(pedido_id)

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
