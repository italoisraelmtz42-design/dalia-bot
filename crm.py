import re
import logging
from typing import Dict, Any, List, Optional
from dataclasses import asdict

from database import init_db
from constantes import logger_crm, EstadoPedido
import pedido_manager

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
# sincronizar_pedido (sin cambios mayores, ya maneja None y conversión)
# ==============================================================================
def sincronizar_pedido(*args, **kwargs):
    cliente = args[0] if args else {}
    datos_pedido = args[1] if len(args) > 1 else {}
    if kwargs:
        datos_pedido.update(kwargs)

    if isinstance(cliente, str):
        cliente = {"numero": cliente}
    telefono = cliente.get('numero')
    if not telefono:
        logger_crm.error("sincronizar_pedido invocada sin un objeto cliente válido.")
        return {}

    borrador = pedido_manager.cargar_borrador_pedido(telefono) or {}
    if datos_pedido is None:
        datos_pedido = {}
    elif not isinstance(datos_pedido, dict):
        try:
            if hasattr(datos_pedido, '__dict__'):
                datos_pedido = datos_pedido.__dict__
            else:
                from dataclasses import asdict
                datos_pedido = asdict(datos_pedido)
        except:
            datos_pedido = {"_raw": str(datos_pedido)}
    borrador.update({k: v for k, v in datos_pedido.items() if v is not None})

    debe_crear = datos_pedido.get('anticipo_confirmado') is True
    if debe_crear:
        pedido_id = pedido_manager.obtener_pedido_activo(telefono)
        if not pedido_id:
            cliente_id = cliente.get('id', 0)
            pedido_id = pedido_manager.crear_pedido_desde_borrador(telefono, cliente_id, borrador)
            logger_crm.info(f"🆕 Pedido oficial creado desde borrador. ID: {pedido_id}")
        else:
            # 🔧 CORREGIDO: antes, si ya existía un pedido oficial para este
            # teléfono (ej. de una prueba o contacto anterior), el código
            # simplemente lo ignoraba y no hacía nada — el pedido NUNCA
            # pasaba a modo DALIA, y el bot seguía respondiendo para
            # siempre en ese número, aunque el cliente sí acabara de mandar
            # un anticipo nuevo. Ahora se actualiza el pedido existente:
            # pasa a modo DALIA y se registra el pago.
            pedido_manager.confirmar_anticipo_pedido_existente(pedido_id, telefono, borrador)
            logger_crm.info(f"🔁 Pedido existente {pedido_id} actualizado a modo DALIA con nuevo anticipo.")
        return pedido_manager.obtener_pedido(pedido_id)
    else:
        pedido_manager.guardar_borrador_pedido(telefono, borrador)
        logger_crm.info(f"📝 Borrador actualizado para el teléfono {telefono}")
    return None

# ==============================================================================
# 🔧 LIMPIEZA (Observación 11 de la auditoría forense): se eliminaron
# _detectar_intencion_pedido(), generar_respuesta_conversacional(),
# manejar_intencion_pedido() y MAPEO_PREGUNTAS — código muerto confirmado:
# ninguna otra parte del proyecto los importaba ni los llamaba. Si en el
# futuro se necesita un flujo de "detectar intención de compra", conviene
# reescribirlo usando campos_requeridos_para() de constantes.py, no el
# esquema plano de 16 campos que usaba MAPEO_PREGUNTAS.
# ==============================================================================
