import sqlite3
import datetime
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
    # 🛡️ GUARDIÁN PERMANENTE
    if pedido_id is None:
        logger_pedidos.warning(f"[_registrar_evento] Se omite registro de evento '{evento}' porque pedido_id es None.")
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
# # 🟢 FUNCIONES DE PERSISTENCIA DEL CHAT (RESTAURADAS)
# ==============================================================================
def chat_guardar_mensaje(telefono: str, mensaje: str, emisor: str):
    """Persiste un mensaje en el historial de chat."""
    with get_db_connection() as conn:
        conn.execute("INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)", (telefono, mensaje, emisor))
        conn.commit()

def chat_cargar_memoria(telefono: str, limite: int = 20) -> List[Dict[str, str]]:
    """Recupera el historial de chat en formato compatible con OpenAI."""
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT mensaje, emisor FROM historial_chat WHERE telefono = ? ORDER BY timestamp DESC LIMIT ?", (telefono, limite))
        rows = cursor.fetchall()
        return [{"role": "user" if r['emisor'] == "usuario" else "assistant", "content": r['mensaje']} for r in reversed(rows)]

def uso_registrar_openai(telefono: str):
    """Registra una llamada a OpenAI en la base de datos."""
    with get_db_connection() as conn:
        conn.execute("INSERT INTO uso_openai (telefono) VALUES (?)", (telefono,))
        conn.commit()

# ==============================================================================
# # MOTOR DE PEDIDOS
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

def generar_folio(conn) -> str:
    cursor = conn.cursor()
    cursor.execute("SELECT folio FROM pedidos WHERE folio LIKE ? ORDER BY folio DESC LIMIT 1", (f"DAL-{datetime.datetime.now().strftime('%Y')}-%",))
    row = cursor.fetchone()
    new_num = (int(row[0].split('-')[-1]) + 1) if row else 1
    return f"DAL-{datetime.datetime.now().strftime('%Y')}-{new_num:06d}"

def crear_pedido(cliente_id: int, telefono: str) -> int:
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        folio = generar_folio(conn)
        sql = "INSERT INTO pedidos (folio, cliente_id, telefono, estado, modo_atencion) VALUES (?, ?, ?, ?, ?)"
        params = (folio, cliente_id, telefono, EstadoPedido.BORRADOR.value, ModoAtencion.BOT.value)
        conn.execute(sql, params)
        pedido_id = cursor.lastrowid
        _registrar_evento(pedido_id, "Pedido creado", f"Folio {folio}", OrigenEvento.SISTEMA, "sistema", conn=conn)
        conn.commit()
        return pedido_id
    except Exception as e:
        if conn: conn.rollback()
        raise e
    finally:
        if conn: conn.close()

def obtener_pedido(pedido_id: int) -> Optional[PedidoData]:
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
        row = cursor.fetchone()
        if not row: return None
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
    # ... (El resto del código de actualizar_pedido, actualizar_entrega, agregar_producto y cálculos se mantiene igual) ...
    pass # (Omitido por brevedad, pero el usuario tiene la versión correcta en su código).

# Asegúrate de que el resto del archivo `pedido_manager.py` tenga el código completo de los cálculos, validaciones y estado.
