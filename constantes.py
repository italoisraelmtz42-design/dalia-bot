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
