from datetime import datetime
from database import get_connection


# ==========================================
# GUARDAR MENSAJE
# ==========================================

def guardar_mensaje(cliente_id, rol, mensaje, tipo="texto"):

    conn = get_connection()
    cur = conn.cursor()

    ahora = datetime.now().isoformat()

    cur.execute("""
        INSERT INTO conversaciones
        (
            cliente_id,
            fecha,
            rol,
            mensaje,
            tipo
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        cliente_id,
        ahora,
        rol,
        mensaje,
        tipo
    ))

    conn.commit()
    conn.close()


# ==========================================
# OBTENER HISTORIAL
# ==========================================

def obtener_historial(cliente_id, limite=100):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM conversaciones
        WHERE cliente_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (
        cliente_id,
        limite
    ))

    historial = cur.fetchall()

    conn.close()

    return list(reversed(historial))


# ==========================================
# ELIMINAR HISTORIAL
# ==========================================

def borrar_historial(cliente_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        DELETE
        FROM conversaciones
        WHERE cliente_id = ?
    """, (cliente_id,))

    conn.commit()
    conn.close()


# ==========================================
# CONTAR MENSAJES
# ==========================================

def contar_mensajes(cliente_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*) total
        FROM conversaciones
        WHERE cliente_id = ?
    """, (cliente_id,))

    total = cur.fetchone()["total"]

    conn.close()

    return total


# ==========================================
# ÚLTIMO MENSAJE
# ==========================================

def ultimo_mensaje(cliente_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM conversaciones
        WHERE cliente_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (cliente_id,))

    mensaje = cur.fetchone()

    conn.close()

    return mensaje