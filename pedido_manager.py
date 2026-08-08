import sqlite3
import datetime
import json
import logging
from typing import List, Dict, Optional, Any

from database import get_db_connection
from constantes import (
    logger_pedidos, EstadoPedido, ModoAtencion, OrigenEvento,
    COLUMNAS_PERMITIDAS_PEDIDOS, PESOS_COMPLETITUD, TRANSICIONES_VALIDAS,
    PedidoData, ItemData, PagoData, EntregaData
)
from validators import validar_estado, validar_transicion

# ==============================================================================
# # EVENTOS INTERNOS
# ==============================================================================
def _registrar_evento(pedido_id: int, evento: str, descripcion: str = None,
                      origen: OrigenEvento = OrigenEvento.SISTEMA, usuario: str = "sistema", conn=None):
    if pedido_id is None:
        return
    sql = "INSERT INTO pedido_eventos (pedido_id, evento, descripcion, origen, usuario) VALUES (?, ?, ?, ?, ?)"
    params = (pedido_id, evento, descripcion, origen.value, usuario)
    if conn:
        conn.execute(sql, params)
    else:
        with get_db_connection() as new_conn:
            new_conn.execute(sql, params)
            new_conn.commit()

def _registrar_historial(pedido_id: int, campo: str, valor_anterior: str, valor_nuevo: str, usuario: str = "sistema", conn=None):
    sql = "INSERT INTO pedido_historial (pedido_id, campo, valor_anterior, valor_nuevo, usuario) VALUES (?, ?, ?, ?, ?)"
    params = (pedido_id, campo, str(valor_anterior), str(valor_nuevo), usuario)
    if conn:
        conn.execute(sql, params)
    else:
        with get_db_connection() as new_conn:
            new_conn.execute(sql, params)
            new_conn.commit()

# ==============================================================================
# # PERSISTENCIA DEL CHAT
# ==============================================================================
def chat_guardar_mensaje(telefono: str, mensaje: str, emisor: str):
    with get_db_connection() as conn:
        conn.execute("INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)", (telefono, mensaje, emisor))
        conn.commit()

def chat_cargar_memoria(telefono: str, limite: int = 20) -> List[Dict[str, str]]:
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT mensaje, emisor FROM historial_chat WHERE telefono = ? ORDER BY timestamp DESC LIMIT ?", (telefono, limite))
        rows = cursor.fetchall()
        return [{"role": "user" if r['emisor'] == "usuario" else "assistant", "content": r['mensaje']} for r in reversed(rows)]

def uso_registrar_openai(telefono: str):
    with get_db_connection() as conn:
        conn.execute("INSERT INTO uso_openai (telefono) VALUES (?)", (telefono,))
        conn.commit()

# ==============================================================================
# # MOTOR DE BORRADORES
# ==============================================================================
def guardar_borrador_pedido(telefono: str, datos_pedido: dict):
    """Guarda el borrador del pedido en la tabla `borradores_pedido`."""
    try:
        datos_json = json.dumps(datos_pedido, ensure_ascii=False, default=str)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM borradores_pedido WHERE telefono = ?", (telefono,))
            exists = cursor.fetchone()
            if exists:
                conn.execute(
                    "UPDATE borradores_pedido SET datos_json = ?, fecha_actualizacion = CURRENT_TIMESTAMP WHERE telefono = ?",
                    (datos_json, telefono)
                )
            else:
                conn.execute(
                    "INSERT INTO borradores_pedido (telefono, datos_json) VALUES (?, ?)",
                    (telefono, datos_json)
                )
            conn.commit()
            logger_pedidos.info(f"[guardar_borrador_pedido] Borrador guardado/actualizado para el teléfono {telefono}")
    except Exception as e:
        logger_pedidos.error(f"[guardar_borrador_pedido] Error guardando borrador: {e}")

def cargar_borrador_pedido(telefono: str) -> Optional[dict]:
    """Recupera el borrador del pedido desde la tabla `borradores_pedido`."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT datos_json FROM borradores_pedido WHERE telefono = ?", (telefono,))
            row = cursor.fetchone()
            if row:
                return json.loads(row[0])
        return None
    except Exception as e:
        logger_pedidos.error(f"[cargar_borrador_pedido] Error cargando borrador: {e}")
        return None

def eliminar_borrador_pedido(telefono: str):
    """Elimina el borrador del pedido tras crear el pedido oficial."""
    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM borradores_pedido WHERE telefono = ?", (telefono,))
            conn.commit()
            logger_pedidos.info(f"[eliminar_borrador_pedido] Borrador eliminado para el teléfono {telefono}")
    except Exception as e:
        logger_pedidos.error(f"[eliminar_borrador_pedido] Error eliminando borrador: {e}")

# ==============================================================================
# # MOTOR DE PEDIDOS OFICIALES
# ==============================================================================
def obtener_pedido_activo(telefono: str) -> Optional[int]:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id FROM pedidos
            WHERE telefono = ?
            AND estado NOT IN ('CANCELADO', 'ENTREGADO')
            AND modo_atencion != 'DALIA'
            ORDER BY id DESC LIMIT 1
        """, (telefono,))
        row = cursor.fetchone()
        return row[0] if row else None

def crear_pedido_desde_borrador(telefono: str, cliente_id: int, borrador: dict) -> int:
    """
    Crea un pedido oficial a partir del borrador.
    - Genera folio
    - Inserta pedido, items, entrega, pagos
    - Elimina el borrador
    Retorna el ID del pedido.
    """
    conn = None
    try:
        conn = get_db_connection()
        conn.execute("BEGIN IMMEDIATE")

        cursor = conn.cursor()
        # Generar folio basado en el próximo ID
        cursor.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM pedidos")
        next_seq = cursor.fetchone()[0]
        current_year = datetime.datetime.now().strftime("%Y")
        folio = f"DAL-{current_year}-{next_seq:06d}"

        # Insertar pedido
        sql_pedido = """
            INSERT INTO pedidos (folio, cliente_id, telefono, estado, modo_atencion)
            VALUES (?, ?, ?, ?, ?)
        """
        cursor.execute(sql_pedido, (
            folio, cliente_id, telefono,
            EstadoPedido.BORRADOR.value,
            ModoAtencion.BOT.value
        ))
        pedido_id = cursor.lastrowid
        if not pedido_id:
            raise Exception("No se pudo obtener el ID del pedido")

        # Insertar items (si existen)
        if borrador.get('producto') and borrador.get('cantidad'):
            precio = borrador.get('precio_unitario', 0.0)
            subtotal = borrador['cantidad'] * precio
            sql_item = """
                INSERT INTO pedido_items (pedido_id, producto, cantidad, precio_unitario, subtotal,
                    color_toalla, color_moño, tipo_jaboncito, color_jaboncito,
                    nombre_bebe, tarjetita)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            cursor.execute(sql_item, (
                pedido_id,
                borrador['producto'],
                borrador['cantidad'],
                precio,
                subtotal,
                borrador.get('color_toalla'),
                borrador.get('color_moño'),
                borrador.get('tipo_jaboncito'),
                borrador.get('color_jaboncito'),
                borrador.get('nombre_bebe'),
                borrador.get('tarjetita')
            ))

        # Insertar entrega (si existe)
        if borrador.get('tipo_entrega'):
            sql_entrega = """
                INSERT INTO entregas (pedido_id, tipo_entrega, municipio, direccion, fecha_entrega, costo_envio)
                VALUES (?, ?, ?, ?, ?, ?)
            """
            cursor.execute(sql_entrega, (
                pedido_id,
                borrador['tipo_entrega'],
                borrador.get('municipio'),
                borrador.get('direccion'),
                borrador.get('fecha_entrega'),
                borrador.get('costo_envio', 0.0)
            ))

        # Insertar pago (si existe anticipo)
        if borrador.get('anticipo_confirmado') and borrador.get('metodo_pago'):
            sql_pago = """
                INSERT INTO pagos (pedido_id, tipo, monto, metodo, comprobante, confirmado)
                VALUES (?, ?, ?, ?, ?, ?)
            """
            cursor.execute(sql_pago, (
                pedido_id,
                'ANTICIPO',
                borrador.get('monto_anticipo', 0.0),
                borrador.get('metodo_pago'),
                borrador.get('comprobante'),
                1
            ))

        # Registrar evento
        _registrar_evento(pedido_id, "Pedido creado", f"Folio {folio}", OrigenEvento.SISTEMA, "sistema", conn=conn)

        conn.commit()
        logger_pedidos.info(f"✅ Pedido oficial creado. Folio: {folio}, ID: {pedido_id}")

        # Eliminar borrador
        eliminar_borrador_pedido(telefono)

        return pedido_id

    except Exception as e:
        if conn:
            conn.rollback()
        logger_pedidos.error(f"[crear_pedido_desde_borrador] ❌ Error: {e}")
        raise e
    finally:
        if conn:
            conn.close()

def obtener_pedido(pedido_id: int) -> Optional[PedidoData]:
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
        row = cursor.fetchone()
        if not row:
            return None
        pedido_dict = dict(row)
        cursor.execute("SELECT * FROM pedido_items WHERE pedido_id = ?", (pedido_id,))
        items = [ItemData(**dict(r)) for r in cursor.fetchall()]
        cursor.execute("SELECT * FROM pagos WHERE pedido_id = ?", (pedido_id,))
        pagos = [PagoData(**dict(r)) for r in cursor.fetchall()]
        cursor.execute("SELECT * FROM entregas WHERE pedido_id = ?", (pedido_id,))
        entrega_row = cursor.fetchone()
        entrega = EntregaData(**dict(entrega_row)) if entrega_row else None
        return PedidoData(**pedido_dict, items=items, pagos=pagos, entrega=entrega)

def actualizar_pedido(pedido_id: int, usuario: str = "sistema", **kwargs):
    # Solo actualiza campos de la tabla pedidos (estado, modo_atencion, etc.)
    if not kwargs:
        return
    if any(k not in COLUMNAS_PERMITIDAS_PEDIDOS for k in kwargs):
        raise ValueError("Intento de actualizar columna no permitida en la tabla pedidos.")
    conn = None
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        conn.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
        old_data = dict(conn.cursor().fetchone())
        set_clause = ", ".join([f"{key} = ?" for key in kwargs.keys()])
        params_update = list(kwargs.values())
        params_update.append(pedido_id)
        conn.execute(f"UPDATE pedidos SET {set_clause}, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id = ?", params_update)
        for key, new_val in kwargs.items():
            old_val = old_data.get(key, None)
            if str(old_val) != str(new_val):
                _registrar_historial(pedido_id, key, old_val, new_val, usuario, conn=conn)
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        logger_pedidos.error(f"[actualizar_pedido] ❌ Error: {e}")
        raise e
    finally:
        if conn:
            conn.close()

# ... (las funciones de cálculo y validación se mantienen igual, se omiten por brevedad)
