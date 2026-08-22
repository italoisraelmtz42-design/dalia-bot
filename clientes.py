from datetime import datetime
from database import get_connection


# ==========================================
# CREAR CLIENTE
# ==========================================

def crear_cliente(telefono, nombre=None):
    conn = get_connection()
    try:
        cur = conn.cursor()

        ahora = datetime.now().isoformat()

        cur.execute("""
            INSERT OR IGNORE INTO clientes
            (telefono, nombre, fecha_alta, ultima_interaccion)
            VALUES (?, ?, ?, ?)
        """, (
            telefono,
            nombre,
            ahora,
            ahora
        ))

        conn.commit()
    finally:
        # 🔧 (22 ago 2026, a pedido de Israel -- fuga de memoria/conexiones)
        # Antes, si cur.execute() tronaba (ej. base de datos ocupada),
        # conn.close() nunca se alcanzaba a llamar y la conexión se
        # quedaba abierta para siempre. Con try/finally se cierra pase lo
        # que pase.
        conn.close()


# ==========================================
# BUSCAR CLIENTE
# ==========================================

def buscar_cliente(telefono):

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT *
            FROM clientes
            WHERE telefono = ?
        """, (telefono,))

        cliente = cur.fetchone()

        return cliente
    finally:
        conn.close()


# ==========================================
# OBTENER O CREAR
# ==========================================

def obtener_o_crear_cliente(telefono, nombre=None):

    cliente = buscar_cliente(telefono)

    if cliente:
        return cliente

    crear_cliente(telefono, nombre)

    return buscar_cliente(telefono)


# ==========================================
# ACTUALIZAR ÚLTIMA INTERACCIÓN
# ==========================================

def actualizar_ultima_interaccion(telefono):

    conn = get_connection()
    try:
        cur = conn.cursor()

        ahora = datetime.now().isoformat()

        cur.execute("""
            UPDATE clientes
            SET ultima_interaccion = ?
            WHERE telefono = ?
        """, (
            ahora,
            telefono
        ))

        conn.commit()
    finally:
        conn.close()


# ==========================================
# CAMBIAR NOMBRE
# ==========================================

def guardar_nombre(telefono, nombre):

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("""
            UPDATE clientes
            SET nombre = ?
            WHERE telefono = ?
        """, (
            nombre,
            telefono
        ))

        conn.commit()
    finally:
        conn.close()


# ==========================================
# OBTENER TODOS LOS CLIENTES
# ==========================================

def obtener_clientes():

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT *
            FROM clientes
            ORDER BY ultima_interaccion DESC
        """)

        clientes = cur.fetchall()

        return clientes
    finally:
        conn.close()
