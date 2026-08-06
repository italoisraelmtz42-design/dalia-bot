import random
import sqlite3
from datetime import datetime
from database import get_connection


# ==========================================
# CREAR PEDIDO
# ==========================================

def crear_pedido(cliente_id):
    """Crea un pedido nuevo con un folio temporal único.

    NOTA: este folio "TMP-..." es provisional; el folio real y consecutivo
    (formato DAL-YYYYMMDD-NNNNNN) se genera más adelante, cuando se
    confirma el pedido (ver Etapa 5 del roadmap). Aun así, incluye
    cliente_id + un sufijo aleatorio para que dos pedidos creados en el
    mismo segundo (dos clientes distintos escribiendo casi a la vez) no
    choquen contra el UNIQUE de la columna folio, y reintenta unas pocas
    veces en el caso extremadamente improbable de que aun así colisione.
    """
    conn = get_connection()
    cur = conn.cursor()

    for _ in range(5):
        ahora = datetime.now().strftime("%Y%m%d%H%M%S")
        sufijo = f"{random.randint(0, 9999):04d}"
        folio = f"TMP-{ahora}-{cliente_id}-{sufijo}"

        try:
            cur.execute("""
                INSERT INTO pedidos
                (
                    folio,
                    cliente_id
                )
                VALUES (?, ?)
            """, (
                folio,
                cliente_id
            ))
            conn.commit()
            pedido_id = cur.lastrowid
            conn.close()
            return pedido_id
        except sqlite3.IntegrityError:
            # Folio duplicado (rarísimo) -> se intenta de nuevo con otro
            # sufijo aleatorio en vez de tronar el pedido del cliente.
            continue

    conn.close()
    raise RuntimeError(f"No se pudo generar un folio único para el cliente {cliente_id}")


# ==========================================
# OBTENER PEDIDO ACTIVO
# ==========================================

def obtener_pedido(cliente_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM pedidos
        WHERE cliente_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (cliente_id,))

    pedido = cur.fetchone()

    conn.close()

    return pedido


# ==========================================
# ACTUALIZAR UN CAMPO
# ==========================================

def actualizar_campo(pedido_id, campo, valor):

    permitidos = [
        "producto",
        "cantidad",
        "colores",
        "fecha_evento",
        "forma_entrega",
        "anticipo",
        "saldo",
        "estatus",
        # Agregados para cubrir los campos de la herramienta
        # actualizar_pedido de OpenAI (ver crm.py: sincronizar_pedido)
        "evento",
        "direccion",
        "color_toalla",
        "color_mono",
        "color_velita",
        "datos_tarjeta",
        "anticipo_confirmado",
    ]

    if campo not in permitidos:
        raise ValueError(f"Campo no permitido: {campo}")

    conn = get_connection()
    cur = conn.cursor()

    sql = f"UPDATE pedidos SET {campo}=? WHERE id=?"

    cur.execute(sql, (
        valor,
        pedido_id
    ))

    conn.commit()
    conn.close()


# ==========================================
# CAMBIAR ESTATUS
# ==========================================

def cambiar_estatus(pedido_id, estatus):

    actualizar_campo(
        pedido_id,
        "estatus",
        estatus
    )


# ==========================================
# REGISTRAR ANTICIPO
# ==========================================

def registrar_anticipo(pedido_id, monto):

    pedido = obtener_pedido_por_id(pedido_id)

    if not pedido:
        return

    anticipo_actual = pedido["anticipo"] or 0

    actualizar_campo(
        pedido_id,
        "anticipo",
        anticipo_actual + monto
    )


# ==========================================
# PEDIDO POR ID
# ==========================================

def obtener_pedido_por_id(pedido_id):

    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        SELECT *
        FROM pedidos
        WHERE id=?
    """, (
        pedido_id,
    ))

    pedido = cur.fetchone()

    conn.close()

    return pedido


# ==========================================
# FOLIO DEFINITIVO (Etapa 5)
# ==========================================

def generar_folio_definitivo(pedido_id, fecha_str):
    """Genera un folio real y consecutivo tipo DAL-YYYYMMDD-NNNNNN para el
    día indicado (fecha_str en formato YYYYMMDD) y se lo asigna al pedido.

    Es seguro ante llamadas concurrentes (dos pedidos confirmándose casi
    al mismo tiempo): usa una transacción con "BEGIN IMMEDIATE" para que
    el incremento del contador sea atómico, y reintenta unas pocas veces
    si de todos modos hay contención.
    """
    for _ in range(5):
        conn = get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO contador_folios (fecha, ultimo) VALUES (?, 0)",
                (fecha_str,),
            )
            conn.execute(
                "UPDATE contador_folios SET ultimo = ultimo + 1 WHERE fecha = ?",
                (fecha_str,),
            )
            numero = conn.execute(
                "SELECT ultimo FROM contador_folios WHERE fecha = ?", (fecha_str,)
            ).fetchone()["ultimo"]

            folio = f"DAL-{fecha_str}-{numero:06d}"

            conn.execute("UPDATE pedidos SET folio = ? WHERE id = ?", (folio, pedido_id))
            conn.commit()
            conn.close()
            return folio
        except sqlite3.OperationalError:
            conn.rollback()
            conn.close()
            continue

    raise RuntimeError(f"No se pudo generar folio definitivo para el pedido {pedido_id}")