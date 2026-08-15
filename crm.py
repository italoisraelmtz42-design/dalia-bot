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

def guardar_mensaje_cliente(cliente, texto, tipo, canal="whatsapp"):
    telefono = cliente['numero'] if isinstance(cliente, dict) else cliente
    pedido_manager.chat_guardar_mensaje(telefono, texto, "usuario", canal=canal)
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

def guardar_respuesta(cliente, respuesta, tipo="texto", canal="whatsapp"):
    telefono = cliente['numero'] if isinstance(cliente, dict) else cliente
    pedido_manager.chat_guardar_mensaje(telefono, respuesta, "bot", canal=canal)

def pedido_para_ram(pedido_db):
    """Convierte un PedidoData (o None) a dict plano usable en la sesión RAM.
    Soporta múltiples items."""
    if not pedido_db:
        return {}
    try:
        base = {
            "producto": None,
            "cantidad": None,
            "precio_unitario": None,
            "color_toalla": None,
            "color_mono": None,
            "tipo_entrega": None,
            "fecha_evento": None,
            "municipio": None,
            "direccion": None,
            "costo_envio": None,
            "es_urgente": bool(getattr(pedido_db, "es_urgente", 0)),
            "anticipo_confirmado": False,
            "items": [],
        }
        items = getattr(pedido_db, "items", []) or []
        if items:
            base["items"] = [
                {
                    "producto": it.producto,
                    "cantidad": it.cantidad,
                    "precio_unitario": it.precio_unitario,
                    "color_toalla": it.color_toalla,
                    "color_mono": it.color_moño,
                    "tipo_jaboncito": it.tipo_jaboncito,
                    "color_jaboncito": it.color_jaboncito,
                    "nombre_bebe": it.nombre_bebe,
                    "tarjetita": it.tarjetita,
                }
                for it in items
            ]
            # Compatibilidad: copiar el primer item a campos planos
            first = items[0]
            base["producto"] = first.producto
            base["cantidad"] = first.cantidad
            base["precio_unitario"] = first.precio_unitario
            base["color_toalla"] = first.color_toalla
            base["color_mono"] = first.color_moño
        entrega = getattr(pedido_db, "entrega", None)
        if entrega:
            base["tipo_entrega"] = entrega.tipo_entrega
            base["municipio"] = entrega.municipio
            base["direccion"] = entrega.direccion
            base["fecha_evento"] = entrega.fecha_entrega
            base["costo_envio"] = entrega.costo_envio
        pagos = getattr(pedido_db, "pagos", []) or []
        if any(getattr(p, "confirmado", 0) for p in pagos):
            base["anticipo_confirmado"] = True
            for p in pagos:
                if getattr(p, "confirmado", 0):
                    base["monto_anticipo"] = p.monto
                    base["metodo_pago"] = p.metodo
                    break
        return base
    except Exception as e:
        logger_crm.error(f"pedido_para_ram error: {e}")
        return {}

def cargar_pedido(cliente):
    telefono = cliente['numero'] if isinstance(cliente, dict) else cliente
    pedido_id = pedido_manager.obtener_pedido_activo(telefono)
    if pedido_id:
        return pedido_manager.obtener_pedido(pedido_id)
    return None

# ==============================================================================
# sincronizar_pedido (sin cambios mayores, ya maneja None y conversión)
# ==============================================================================
def sincronizar_pedido(*args, canal="whatsapp", **kwargs):
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
            pedido_id = pedido_manager.crear_pedido_desde_borrador(telefono, cliente_id, borrador, canal=canal)
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
