# ==========================================
# CRM
# ==========================================

from datetime import datetime
from zoneinfo import ZoneInfo

from database import inicializar_bd, get_connection

from clientes import (
    obtener_o_crear_cliente,
    actualizar_ultima_interaccion,
    guardar_nombre,
)

from historial import (
    guardar_mensaje,
    obtener_historial,
)

# Importar el nuevo motor de pedidos
import pedido_manager

ZONA_HORARIA_NEGOCIO = ZoneInfo("America/Monterrey")


# ==========================================
# SETUP
# ==========================================

def inicializar_base_datos():
    inicializar_bd()


# ==========================================
# CLIENTE
# ==========================================

def cargar_cliente(telefono, nombre=None):
    cliente = obtener_o_crear_cliente(telefono, nombre)
    actualizar_ultima_interaccion(telefono)
    return cliente


def actualizar_nombre_cliente(telefono, nombre):
    guardar_nombre(telefono, nombre)


# ==========================================
# HISTORIAL / MEMORIA DE CONVERSACIÓN
# ==========================================

def cargar_memoria(cliente, limite=30):
    historial = obtener_historial(cliente["id"], limite)
    return [{"role": h["rol"], "content": h["mensaje"]} for h in historial]


def guardar_mensaje_cliente(cliente, texto, tipo="texto"):
    guardar_mensaje(cliente["id"], "user", texto, tipo=tipo)


def guardar_respuesta(cliente, respuesta):
    guardar_mensaje(cliente["id"], "assistant", respuesta)


# ==========================================
# PEDIDO (usando pedido_manager)
# ==========================================

def cargar_pedido(cliente):
    """Devuelve el pedido activo del cliente (el más reciente); si no
    tiene ninguno, crea uno vacío y lo devuelve."""
    pedido = pedido_manager.obtener_pedido_activo(cliente["id"])
    if pedido:
        return pedido

    pedido_manager.crear_pedido(cliente["id"])
    return pedido_manager.obtener_pedido_activo(cliente["id"])


_MAPEO_CAMPO_A_COLUMNA = {
    "tipo_entrega": "forma_entrega",
}

CAMPOS_PEDIDO_PERSISTIBLES = [
    "producto",
    "cantidad",
    "evento",
    "fecha_evento",
    "color_toalla",
    "color_mono",
    "color_velita",
    "datos_tarjeta",
    "tipo_entrega",
    "direccion",
    "anticipo_confirmado",
    "municipio",
    "tipo_jaboncito",
    "color_jaboncito",
    "nombre_bebe",
    "tarjetita",
    "notas",
]


def pedido_para_ram(pedido_db):
    if not pedido_db:
        return None

    columnas_disponibles = pedido_db.keys()
    resultado = {}
    for campo in CAMPOS_PEDIDO_PERSISTIBLES:
        columna = _MAPEO_CAMPO_A_COLUMNA.get(campo, campo)
        valor = pedido_db[columna] if columna in columnas_disponibles else None
        if campo == "anticipo_confirmado" and valor is not None:
            valor = bool(valor)
        resultado[campo] = valor
    return resultado


def cargar_pedido_ram(cliente):
    return pedido_para_ram(cargar_pedido(cliente))


def sincronizar_pedido(cliente, pedido_ram):
    pedido_db = cargar_pedido(cliente)
    pedido_id = pedido_db["id"]

    campos_actualizar = {}
    for campo in CAMPOS_PEDIDO_PERSISTIBLES:
        valor = pedido_ram.get(campo)
        if valor is None:
            continue

        columna = _MAPEO_CAMPO_A_COLUMNA.get(campo, campo)

        if campo == "anticipo_confirmado":
            valor = 1 if valor else 0

        campos_actualizar[columna] = valor

    if campos_actualizar:
        pedido_manager.actualizar_pedido(pedido_id, campos_actualizar)

    # Si se confirmó anticipo, asignar folio definitivo
    if pedido_ram.get("anticipo_confirmado"):
        pedido_actualizado = pedido_manager.obtener_pedido(pedido_id)
        if pedido_actualizado and pedido_actualizado["folio"].startswith("TMP-"):
            fecha_str = datetime.now(ZONA_HORARIA_NEGOCIO).strftime("%Y%m%d")
            folio_nuevo = pedido_manager.generar_folio_definitivo(pedido_id, fecha_str)
            print(f"🎟️ Folio definitivo asignado al pedido de {cliente['telefono']}: {folio_nuevo}")


# ==========================================
# USO DE OPENAI
# ==========================================

def registrar_uso_openai(telefono, modelo, tokens_entrada, tokens_salida):
    cliente = obtener_o_crear_cliente(telefono)
    conn = get_connection()
    conn.execute(
        """
        INSERT INTO uso_openai (cliente_id, fecha, modelo, tokens_entrada, tokens_salida)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            cliente["id"],
            datetime.now(ZONA_HORARIA_NEGOCIO).isoformat(),
            modelo,
            tokens_entrada,
            tokens_salida,
        ),
    )
    conn.commit()
    conn.close()
