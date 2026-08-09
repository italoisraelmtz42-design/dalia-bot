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
# "requiere": campos que SÍ deben pedirse para ese producto.
# Si un producto no hace match con ninguna clave de aquí, se usa la lista
# COMPLETA de FALTANTES_UNIVERSALES como respaldo (comportamiento anterior).
#
# Cómo hace el match: busca si alguna de las palabras clave aparece dentro
# del texto de pedido["producto"] (en minúsculas). El primer match gana.
# ==============================================================================
FALTANTES_UNIVERSALES = [
    "producto", "cantidad", "fecha_evento", "color_toalla", "color_mono",
    "color_velita", "tipo_entrega", "direccion", "municipio",
    "tipo_jaboncito", "color_jaboncito", "nombre_bebe", "tarjetita",
]

REGLAS_PRODUCTO = {
    # clave: (palabras clave para hacer match contra pedido["producto"], campos requeridos)
    "sencillo": {
        "palabras_clave": ["sencillo", "sin jabon", "sin jabón"],
        "requiere": ["producto", "cantidad", "fecha_evento", "color_toalla", "tipo_entrega"],
    },
    "jaboncito": {
        "palabras_clave": ["jaboncito", "con jabon", "con jabón"],
        "requiere": [
            "producto", "cantidad", "fecha_evento", "color_toalla",
            "tipo_jaboncito", "color_jaboncito", "tipo_entrega",
        ],
    },
    "velita": {
        "palabras_clave": ["velita", "vela"],
        "requiere": [
            "producto", "cantidad", "fecha_evento", "color_toalla",
            "color_velita", "tipo_entrega",
        ],
    },
}


def campos_requeridos_para(nombre_producto: str) -> list:
    """Devuelve la lista de campos que de verdad aplican para este producto.
    Si no hace match con ninguna regla conocida, regresa la lista universal
    completa (comportamiento anterior, más seguro que asumir de más)."""
    if not nombre_producto:
        return FALTANTES_UNIVERSALES

    texto = nombre_producto.lower()
    for regla in REGLAS_PRODUCTO.values():
        if any(palabra in texto for palabra in regla["palabras_clave"]):
            return regla["requiere"]

    return FALTANTES_UNIVERSALES

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
