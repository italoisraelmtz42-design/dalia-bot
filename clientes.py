from datetime import datetime
from database import get_connection


# ==========================================
# CREAR CLIENTE
# ==========================================

def crear_cliente(telefono, nombre=None):
    conn = get_connection()
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
    conn.close()


# ==========================================
# BUSCAR CLIENTE
# ==========================================

def buscar_cliente(telefono):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM clientes
        WHERE telefono = ?
    """, (telefono,))

    cliente = cur.fetchone()

    conn.close()

    return cliente


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
    conn.close()


# ==========================================
# CAMBIAR NOMBRE
# ==========================================

def guardar_nombre(telefono, nombre):

    conn = get_connection()
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
    conn.close()


# ==========================================
# OBTENER TODOS LOS CLIENTES
# ==========================================

def obtener_clientes():

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM clientes
        ORDER BY ultima_interaccion DESC
    """)

    clientes = cur.fetchall()

    conn.close()

    return clientes