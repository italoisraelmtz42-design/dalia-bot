from datetime import datetime
from database import get_connection


# ==========================================
# GUARDAR MENSAJE
# ==========================================

def guardar_mensaje(cliente_id, rol, mensaje, tipo="texto"):

    conn = get_connection()
    try:
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
    finally:
        # 🔧 (22 ago 2026, a pedido de Israel -- fuga de memoria/conexiones)
        # Antes, si cur.execute() tronaba, conn.close() nunca se llamaba
        # y la conexión se quedaba abierta para siempre. Con try/finally
        # se cierra pase lo que pase.
        conn.close()


# ==========================================
# OBTENER HISTORIAL
# ==========================================

def obtener_historial(cliente_id, limite=100):

    conn = get_connection()
    try:
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

        return list(reversed(historial))
    finally:
        conn.close()


# ==========================================
# ELIMINAR HISTORIAL
# ==========================================

def borrar_historial(cliente_id):

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("""
            DELETE
            FROM conversaciones
            WHERE cliente_id = ?
        """, (cliente_id,))

        conn.commit()
    finally:
        conn.close()


# ==========================================
# CONTAR MENSAJES
# ==========================================

def contar_mensajes(cliente_id):

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT COUNT(*) total
            FROM conversaciones
            WHERE cliente_id = ?
        """, (cliente_id,))

        total = cur.fetchone()["total"]

        return total
    finally:
        conn.close()


# ==========================================
# ÚLTIMO MENSAJE
# ==========================================

def ultimo_mensaje(cliente_id):

    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute("""
            SELECT *
            FROM conversaciones
            WHERE cliente_id = ?
            ORDER BY id DESC
            LIMIT 1
        """, (cliente_id,))

        mensaje = cur.fetchone()

        return mensaje
    finally:
        conn.close()
