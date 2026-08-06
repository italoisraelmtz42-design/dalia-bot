import logging
from enum import Enum

# --- LOGGERS ESPECIALIZADOS (Cambio 4) ---
logger_db = logging.getLogger('database')
logger_pedidos = logging.getLogger('pedidos')
logger_crm = logging.getLogger('crm')

# --- ENUMS Y ESTADOS (Cambio 2) ---
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

# --- LISTAS Y MAPEOS DE TRANSICIONES ---
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

# --- LISTAS BLANCAS Y CONFIGURACIÓN (Cambio 3) ---
COLUMNAS_PERMITIDAS_PEDIDOS = {
    'estado', 'modo_atencion', 'es_urgente', 'cliente_id', 'telefono'
}

# --- PESOS PARA PORCENTAJE DE COMPLETITUD (Cambio 10) ---
PESOS_COMPLETITUD = {
    'producto': 30,
    'cantidad': 20,
    'entrega': 15,
    'fecha': 15,
    'colores': 10,
    'nombre_bebe': 5,
    'tarjetita': 5
}