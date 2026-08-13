import logging
from enum import Enum
from dataclasses import dataclass
from typing import List, Optional

# --- LOGGERS ---
logger_db = logging.getLogger('database')
logger_pedidos = logging.getLogger('pedidos')
logger_crm = logging.getLogger('crm')

# --- ENUMS ---
class EstadoPedido(str, Enum):
    BORRADOR = "BORRADOR"
    CAPTURANDO_DATOS = "CAPTURANDO_DATOS"
    COTIZADO = "COTIZADO"
    PENDIENTE_ANTICIPO = "PENDIENTE_ANTICIPO"
    ANTICIPO_CONFIRMADO = "ANTICIPO_CONFIRMADO"
    TRANSFERIDO_A_DALIA = "TRANSFERIDO_A_DALIA"
    EN_PRODUCCION = "EN_PRODUCCION"
    LISTO = "LISTO"
    ENTREGADO = "ENTREGADO"
    CANCELADO = "CANCELADO"

class ModoAtencion(str, Enum):
    BOT = "BOT"
    DALIA = "DALIA"
    SUSPENDIDO = "SUSPENDIDO"

class OrigenEvento(str, Enum):
    CLIENTE = "CLIENTE"
    BOT = "BOT"
    DALIA = "DALIA"
    SISTEMA = "SISTEMA"

# --- LISTAS Y MAPEOS ---
ESTADOS_VALIDOS = [e.value for e in EstadoPedido]
MODOS_VALIDOS = [m.value for m in ModoAtencion]
ORIGENES_VALIDOS = [o.value for o in OrigenEvento]

TRANSICIONES_VALIDAS = {
    EstadoPedido.BORRADOR.value: [EstadoPedido.CAPTURANDO_DATOS.value, EstadoPedido.CANCELADO.value],
    EstadoPedido.CAPTURANDO_DATOS.value: [EstadoPedido.COTIZADO.value, EstadoPedido.CANCELADO.value],
    EstadoPedido.COTIZADO.value: [EstadoPedido.PENDIENTE_ANTICIPO.value, EstadoPedido.CANCELADO.value],
    EstadoPedido.PENDIENTE_ANTICIPO.value: [EstadoPedido.ANTICIPO_CONFIRMADO.value, EstadoPedido.CANCELADO.value],
    EstadoPedido.ANTICIPO_CONFIRMADO.value: [EstadoPedido.TRANSFERIDO_A_DALIA.value, EstadoPedido.CANCELADO.value],
    EstadoPedido.TRANSFERIDO_A_DALIA.value: [EstadoPedido.EN_PRODUCCION.value, EstadoPedido.CANCELADO.value],
    EstadoPedido.EN_PRODUCCION.value: [EstadoPedido.LISTO.value, EstadoPedido.CANCELADO.value],
    EstadoPedido.LISTO.value: [EstadoPedido.ENTREGADO.value, EstadoPedido.CANCELADO.value],
    EstadoPedido.ENTREGADO.value: [],
    EstadoPedido.CANCELADO.value: []
}

COLUMNAS_PERMITIDAS_PEDIDOS = {
    'estado', 'modo_atencion', 'es_urgente', 'cliente_id', 'telefono'
}

PESOS_COMPLETITUD = {
    'producto': 30, 'cantidad': 20, 'entrega': 15, 'fecha': 15,
    'colores': 10, 'nombre_bebe': 5, 'tarjetita': 5
}

# ==============================================================================
# REGLAS POR PRODUCTO (corrige Observación 5/6 de la auditoría forense)
# ------------------------------------------------------------------------------
# El backend, no el modelo, decide qué campos aplican a cada tipo de producto.
#
# 🔧 CORREGIDO (gap real detectado en pruebas): esta lógica existía pero
# NUNCA se llamaba desde app.py -- código muerto. El bot cerraba pedidos
# sin pedir todos los datos porque nadie le avisaba qué faltaba. Ahora se
# conecta al prompt del sistema (ver app.py) para que el modelo reciba
# un recordatorio explícito de qué falta, calculado en Python, no
# adivinado por el modelo.
#
# Clasificación revisada uno por uno contra los 28 archivos de Productos/
# y confirmada directamente contigo:
# - Los 9 animales de toalla (búho, caballo, conejo, elefante, jirafa,
#   león, perrito, unicornio) SÍ llevan moño de color aparte.
# - Mariposa es la ÚNICA excepción entre los animales: NO lleva moño.
# - Las velitas (chica/grande) SÍ llevan listón de color.
# - El kit osito oración velita SÍ lleva listón (el osito del kit).
# - Oración con decenario / Oración con velita: SIN color que preguntar.
# ==============================================================================
FALTANTES_UNIVERSALES = [
    "producto", "cantidad", "fecha_evento", "color_toalla", "color_mono",
    "color_velita", "tipo_entrega", "direccion", "municipio",
    "tipo_jaboncito", "color_jaboncito", "nombre_bebe", "tarjetita",
]

_CAMPOS_BASE = ["producto", "cantidad", "fecha_evento", "tipo_entrega"]

# Cada tupla: (palabras clave para hacer match, función que arma la lista
# de campos requeridos a partir del nombre ya confirmado como match).
# El ORDEN importa: se evalúa de arriba hacia abajo y gana el primer
# match -- por eso los animales específicos van ANTES que las reglas
# genéricas de "jaboncito", para que "elefante con jaboncito" no se
# confunda con la regla del osito clásico (que no pide color_mono).
_REGLAS_PRODUCTO_ORDENADAS = [
    # Productos SIN ningún color que preguntar (pero encendedor y
    # destapador sí necesitan que se pregunte con_bolsa/sin_bolsa, ver
    # más abajo -- no van en este grupo genérico).
    (["oracion con decenario", "oracion con velita", "abanico", "domino",
      "dominó", "espejo redondo", "espejito"],
     lambda n: list(_CAMPOS_BASE)),

    # Encendedor / destapador: sin color, pero SÍ hay que preguntar si
    # quiere bolsa de celofán (cambia el precio $1.00 extra).
    (["encendedor", "destapador"],
     lambda n: _CAMPOS_BASE + ["con_bolsa"]),

    # Kit osito oración velita: el osito del kit lleva listón.
    (["kit osito"],
     lambda n: _CAMPOS_BASE + ["color_toalla", "color_mono"]),

    # Osito toalla afelpada: pareja fija toalla+moño (ver
    # pareja_afelpada_es_valida en app.py).
    (["afelpada", "afelpado"],
     lambda n: _CAMPOS_BASE + ["color_toalla", "color_mono"]),

    # Osito de peluche: un solo color (el moño ya viene incluido; solo
    # es "requerido" preguntar si lo quiere personalizado, no es un
    # campo obligatorio de texto).
    (["peluche"],
     lambda n: _CAMPOS_BASE + ["color_toalla"]),

    # Mariposa: única animal de toalla SIN moño.
    (["mariposa"],
     lambda n: _CAMPOS_BASE + ["color_toalla"] + (
         ["tipo_jaboncito", "color_jaboncito"] if "jabon" in n else []
     )),

    # Los otros 9 animales de toalla: SÍ llevan moño.
    (["buho", "birrete", "caballo", "caballito", "conejo", "conejito",
      "elefante", "jirafa", "leon", "leoncito", "perrito", "unicornio"],
     lambda n: _CAMPOS_BASE + ["color_toalla", "color_mono"] + (
         ["tipo_jaboncito", "color_jaboncito"] if "jabon" in n else []
     )),

    # Osito con jaboncito de inicial/doble pie: color fijo de jaboncito,
    # sin decisión "con/sin" (siempre lo llevan).
    (["doble inicial", "inicial chica", "inicial grande", "doble pie"],
     lambda n: _CAMPOS_BASE + ["color_toalla", "color_mono", "color_jaboncito"]),

    # Velitas de toalla: SÍ llevan listón de color.
    (["velita", "vela de toalla", "vela chica", "vela grande"],
     lambda n: _CAMPOS_BASE + ["color_toalla", "color_velita"]),

    # Osito sencillo sin jabón: solo colores, sin campos de jaboncito.
    (["sencillo", "sin jabon", "sin jabón"],
     lambda n: _CAMPOS_BASE + ["color_toalla", "color_mono"]),

    # Osito clásico con jaboncito.
    (["jaboncito", "con jabon", "con jabón"],
     lambda n: _CAMPOS_BASE + ["color_toalla", "color_mono", "tipo_jaboncito", "color_jaboncito"]),
]


def campos_requeridos_para(nombre_producto: str) -> list:
    """Devuelve la lista de campos que de verdad aplican para este producto.
    Si no hace match con ninguna regla conocida, regresa la lista universal
    completa (comportamiento anterior, más seguro que asumir de más)."""
    if not nombre_producto:
        return FALTANTES_UNIVERSALES

    texto = nombre_producto.lower()
    for palabras_clave, armar_campos in _REGLAS_PRODUCTO_ORDENADAS:
        if any(palabra in texto for palabra in palabras_clave):
            return armar_campos(texto)

    return FALTANTES_UNIVERSALES


def campos_faltantes_pedido(pedido: dict) -> list:
    """Evalúa campos faltantes por ITEM cuando hay lista multi-producto.
    También valida campos a nivel pedido (entrega, fecha).
    Devuelve lista de strings legibles para el prompt."""
    # 🔧 Campos booleanos (ej. con_bolsa): False es una respuesta VÁLIDA
    # ("sin bolsa"), no significa "todavía no contestado". Si se trata
    # como cualquier otro campo con "not it.get(c)", un False legítimo
    # se malinterpreta como faltante para siempre. Por eso estos campos
    # se consideran completos si el valor es exactamente True o False
    # (no None y no ausente).
    CAMPOS_BOOLEANOS = {"con_bolsa", "mono_personalizado"}

    def _falta(valor):
        if valor is None:
            return True
        if isinstance(valor, bool):
            return False  # True o False, cualquiera de los dos ya es una respuesta
        return not valor

    faltantes = []
    items = pedido.get("items") if isinstance(pedido.get("items"), list) else []
    if not items:
        # formato plano
        req = campos_requeridos_para(pedido.get("producto") or "")
        for c in req:
            if _falta(pedido.get(c)):
                faltantes.append(c)
        return faltantes

    for i, it in enumerate(items, 1):
        req = campos_requeridos_para(it.get("producto") or "")
        # campos que viven en el item
        item_campos = {
            "producto", "cantidad", "color_toalla", "color_mono", "color_velita",
            "tipo_jaboncito", "color_jaboncito", "nombre_bebe", "tarjetita",
            "precio_unitario", "con_bolsa", "mono_personalizado",
        }
        for c in req:
            if c in item_campos and _falta(it.get(c)):
                faltantes.append(f"item{i}.{c} ({it.get('producto') or '?'})")
            elif c not in item_campos and _falta(pedido.get(c)):
                # fecha_evento, tipo_entrega, etc. a nivel pedido
                tag = f"{c}"
                if tag not in faltantes:
                    faltantes.append(tag)
    return faltantes

# --- DATACLASSES (Movidas aquí para evitar ImportError) ---
@dataclass
class ItemData:
    id: int
    pedido_id: int
    producto: str
    cantidad: int
    precio_unitario: float
    subtotal: float
    color_toalla: Optional[str] = None
    color_moño: Optional[str] = None
    tipo_jaboncito: Optional[str] = None
    color_jaboncito: Optional[str] = None
    nombre_bebe: Optional[str] = None
    tarjetita: Optional[str] = None

@dataclass
class PagoData:
    id: int
    pedido_id: int
    tipo: str
    monto: float
    metodo: str
    comprobante: Optional[str] = None
    confirmado: int = 0
    # 🔧 AGREGADO: la tabla `pagos` tiene columna `fecha` (con default
    # CURRENT_TIMESTAMP), pero este dataclass no la declaraba. Nunca se
    # notó porque, antes de la corrección del bug crítico de pagos
    # perdidos, la tabla `pagos` siempre estaba vacía para cualquier
    # pedido (ver Observación adicional de la auditoría forense).
    fecha: Optional[str] = None

@dataclass
class EntregaData:
    pedido_id: int
    tipo_entrega: str
    municipio: Optional[str] = None
    direccion: Optional[str] = None
    fecha_entrega: Optional[str] = None
    costo_envio: float = 0.0

@dataclass
class PedidoData:
    id: int
    folio: str
    cliente_id: int
    telefono: str
    estado: str
    modo_atencion: str
    es_urgente: int
    porcentaje_completitud: int
    fecha_creacion: str
    fecha_actualizacion: str
    items: List[ItemData]
    pagos: List[PagoData]
    entrega: Optional[EntregaData] = None
