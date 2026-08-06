# ==========================================
# CRM
# Único punto de entrada que app.py usa para hablar con la base de datos.
# app.py NUNCA debe importar sqlite3, database, clientes, historial o
# pedidos directamente ni ejecutar SQL — todo pasa por aquí.
#
# Este archivo fusiona lo que antes eran memoria.py y crm.py (hacían
# prácticamente lo mismo con nombres distintos); ahora hay una sola versión.
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

from pedidos import (
    obtener_pedido,
    crear_pedido,
    actualizar_campo,
    generar_folio_definitivo,
)

ZONA_HORARIA_NEGOCIO = ZoneInfo("America/Monterrey")


# ==========================================
# SETUP
# ==========================================

def inicializar_base_datos():
    """Crea las tablas si no existen (y agrega columnas nuevas a tablas que
    ya existían). Se llama una vez al arrancar la app."""
    inicializar_bd()


# ==========================================
# CLIENTE
# ==========================================

def cargar_cliente(telefono, nombre=None):
    """Crea al cliente si no existe y actualiza su última interacción.
    Devuelve el registro del cliente (sqlite3.Row, se usa como dict)."""
    cliente = obtener_o_crear_cliente(telefono, nombre)
    actualizar_ultima_interaccion(telefono)
    return cliente


def actualizar_nombre_cliente(telefono, nombre):
    guardar_nombre(telefono, nombre)


# ==========================================
# HISTORIAL / MEMORIA DE CONVERSACIÓN
# ==========================================

def cargar_memoria(cliente, limite=30):
    """Devuelve el historial de este cliente en el formato que espera
    OpenAI: [{"role": "user"/"assistant", "content": "..."}, ...]"""
    historial = obtener_historial(cliente["id"], limite)
    return [{"role": h["rol"], "content": h["mensaje"]} for h in historial]


def guardar_mensaje_cliente(cliente, texto, tipo="texto"):
    guardar_mensaje(cliente["id"], "user", texto, tipo=tipo)


def guardar_respuesta(cliente, respuesta):
    guardar_mensaje(cliente["id"], "assistant", respuesta)


# ==========================================
# PEDIDO
# ==========================================

def cargar_pedido(cliente):
    """Devuelve el pedido activo del cliente (el más reciente); si no
    tiene ninguno, crea uno vacío y lo devuelve."""
    pedido = obtener_pedido(cliente["id"])
    if pedido:
        return pedido

    crear_pedido(cliente["id"])
    return obtener_pedido(cliente["id"])


# Algunos campos que usa la herramienta actualizar_pedido (la que llama
# OpenAI, definida en TOOLS dentro de app.py) tienen un nombre distinto al
# de la columna en la base de datos. Este mapeo traduce entre uno y otro
# SIN tocar el schema que ya usa OpenAI.
_MAPEO_CAMPO_A_COLUMNA = {
    "tipo_entrega": "forma_entrega",
}

# Campos del diccionario "pedido" en RAM (sesiones[...]['pedido']) que se
# intentan persistir en SQLite. Deben existir como columna en la tabla
# pedidos (ver database.py) y estar en la lista "permitidos" de
# pedidos.actualizar_campo.
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
]


def pedido_para_ram(pedido_db):
    """Convierte una fila de la tabla pedidos (formato SQLite) al mismo
    formato de diccionario que usa pedido_vacio() en app.py. Es el mapeo
    inverso al que hace sincronizar_pedido(). Se usa para "hidratar" una
    sesión nueva en RAM con lo que ya había en la base de datos (Etapa 1:
    SQLite como fuente principal, RAM sobrevive un reinicio del proceso).
    """
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
    """Como cargar_pedido(), pero ya convertido al formato que espera
    sesiones[...]['pedido'] en app.py."""
    return pedido_para_ram(cargar_pedido(cliente))


def sincronizar_pedido(cliente, pedido_ram):
    """Copia a SQLite los campos del pedido que ya se confirmaron en RAM.

    Se llama después de cada respuesta del bot, para ir moviendo el pedido
    hacia la base de datos en paralelo a la memoria en RAM, sin tocar la
    lógica de negocio existente (preguntar_ia sigue escribiendo solo en
    sesiones[...]['pedido'], igual que antes).
    """
    pedido_db = cargar_pedido(cliente)
    pedido_id = pedido_db["id"]

    for campo in CAMPOS_PEDIDO_PERSISTIBLES:
        valor = pedido_ram.get(campo)
        if valor is None:
            continue

        columna = _MAPEO_CAMPO_A_COLUMNA.get(campo, campo)

        if campo == "anticipo_confirmado":
            valor = 1 if valor else 0

        try:
            actualizar_campo(pedido_id, columna, valor)
        except ValueError:
            # La columna no está en la lista blanca de pedidos.py todavía.
            # No tronamos el bot por esto; simplemente no se persiste ese
            # campo hasta que se agregue la migración correspondiente.
            print(f"⚠️ crm.sincronizar_pedido: campo '{campo}' no persistible todavía")

    # Etapa 5: si el cliente ya mandó su comprobante de anticipo y el
    # pedido todavía tiene folio provisional (TMP-...), se le asigna un
    # folio real y consecutivo (DAL-YYYYMMDD-NNNNNN).
    if pedido_ram.get("anticipo_confirmado"):
        pedido_actualizado = obtener_pedido(cliente["id"])
        if pedido_actualizado and pedido_actualizado["folio"].startswith("TMP-"):
            fecha_str = datetime.now(ZONA_HORARIA_NEGOCIO).strftime("%Y%m%d")
            folio_nuevo = generar_folio_definitivo(pedido_actualizado["id"], fecha_str)
            print(f"🎟️ Folio definitivo asignado al pedido de {cliente['telefono']}: {folio_nuevo}")


# ==========================================
# USO DE OPENAI (para poder ver costo aproximado más adelante en un
# dashboard — Etapa 7 del roadmap). Por ahora solo se guarda; todavía no
# hay pantalla para verlo.
# ==========================================

def registrar_uso_openai(telefono, modelo, tokens_entrada, tokens_salida):
    """Guarda cuántos tokens consumió una llamada a OpenAI para este
    cliente. Si algo falla aquí, no debe tronar el flujo del bot -> quien
    llame a esta función debe envolverla en try/except (ver app.py)."""
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
