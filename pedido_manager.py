import sqlite3
import datetime
import json
import logging
from typing import List, Dict, Optional, Tuple
from database import get_db_connection
from constantes import (
    logger_pedidos, EstadoPedido, ModoAtencion, OrigenEvento, 
    COLUMNAS_PERMITIDAS_PEDIDOS, PESOS_COMPLETITUD, TRANSICIONES_VALIDAS,
    PedidoData, ItemData, PagoData, EntregaData
)
from validators import validar_estado, validar_transicion

def _safe_sql_val(val):
    if isinstance(val, (str, int, float, type(None), bytes)): return val
    try:
        if hasattr(val, 'value'): return val.value
        if isinstance(val, (dict, list, tuple, set)): return json.dumps(val)
        return str(val)
    except Exception: return str(val)

def _registrar_evento(pedido_id: int, evento: str, descripcion: str = None, origen: OrigenEvento = OrigenEvento.SISTEMA, usuario: str = "sistema", conn=None):
    def _execute(connection):
        connection.execute("INSERT INTO pedido_eventos (pedido_id, evento, descripcion, origen, usuario) VALUES (?, ?, ?, ?, ?)", (pedido_id, evento, descripcion, origen.value, usuario))
    if conn: _execute(conn)
    else:
        try:
            with get_db_connection() as new_conn:
                _execute(new_conn); new_conn.commit()
        except Exception as e: logger_pedidos.error(f"Error evento {pedido_id}: {e}")

def _registrar_historial(pedido_id: int, campo: str, valor_anterior: str, valor_nuevo: str, usuario: str = "sistema", conn=None):
    def _execute(connection):
        connection.execute("INSERT INTO pedido_historial (pedido_id, campo, valor_anterior, valor_nuevo, usuario) VALUES (?, ?, ?, ?, ?)", (pedido_id, campo, str(valor_anterior), str(valor_nuevo), usuario))
    if conn: _execute(conn)
    else:
        try:
            with get_db_connection() as new_conn:
                _execute(new_conn); new_conn.commit()
        except Exception as e: logger_pedidos.error(f"Error historial {pedido_id}: {e}")

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
        cursor.execute("INSERT INTO pedidos (folio, cliente_id, telefono, estado, modo_atencion) VALUES (?, ?, ?, ?, ?)", (folio, cliente_id, telefono, EstadoPedido.BORRADOR.value, ModoAtencion.BOT.value))
        pedido_id = cursor.lastrowid
        _registrar_evento(pedido_id, "Pedido creado", f"Folio {folio}", OrigenEvento.SISTEMA, "sistema", conn=conn)
        conn.commit()
        logger_pedidos.info(f"✅ Pedido creado: {folio}, ID {pedido_id}")
        return pedido_id
    except Exception as e:
        if conn: conn.rollback()
        logger_pedidos.error(f"❌ Error al crear pedido: {e}")
        raise
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
    """
    PARCHE DE ESTABILIDAD: Si app.py envía columnas inválidas (como 'producto'),
    las filtra silenciosamente y no mata el proceso con ValueError.
    """
    if not kwargs: return
    
    # ⚠️ FILTRO DE SEGURIDAD PARA EVITAR EL ValueError DE TIPO COLUMNA
    valido_kwargs = {}
    for key, val in kwargs.items():
        if key in COLUMNAS_PERMITIDAS_PEDIDOS:
            valido_kwargs[key] = val
        else:
            logger_pedidos.warning(f"⚠️ Columna '{key}' ignorada (no pertenece a tabla pedidos).")
    
    if not valido_kwargs:
        logger_pedidos.info(f"✅ Pedido {pedido_id} no tuvo columnas válidas para actualizar.")
        return
    
    conn = None
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
        old_data = dict(cursor.fetchone())
        
        set_clause = ", ".join([f"{key} = ?" for key in valido_kwargs.keys()])
        valores_seguros = [_safe_sql_val(val) for val in valido_kwargs.values()]
        valores_seguros.append(pedido_id)
        
        conn.execute(f"UPDATE pedidos SET {set_clause}, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id = ?", valores_seguros)
        
        for key, new_val in valido_kwargs.items():
            old_val = old_data.get(key, None)
            if str(old_val) != str(new_val):
                _registrar_historial(pedido_id, key, old_val, new_val, usuario, conn=conn)
                if key in ['estado', 'modo_atencion', 'es_urgente']:
                    _registrar_evento(pedido_id, f"Cambio de {key}", f"{old_val} -> {new_val}", OrigenEvento.SISTEMA, usuario, conn=conn)
        
        conn.commit()
        logger_pedidos.info(f"✅ Pedido {pedido_id} actualizado.")
    except Exception as e:
        if conn: conn.rollback()
        logger_pedidos.error(f"❌ Error actualizando pedido {pedido_id}: {e}")
        raise
    finally:
        if conn: conn.close()

def agregar_producto(pedido_id: int, producto: str, cantidad: int, precio_unitario: float, color_toalla: str = None, color_moño: str = None, tipo_jaboncito: str = None, color_jaboncito: str = None, nombre_bebe: str = None, tarjetita: str = None) -> int:
    try:
        with get_db_connection() as conn:
            subtotal = cantidad * precio_unitario
            cursor = conn.cursor()
            cursor.execute("""INSERT INTO pedido_items (pedido_id, producto, cantidad, precio_unitario, subtotal, color_toalla, color_moño, tipo_jaboncito, color_jaboncito, nombre_bebe, tarjetita) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (pedido_id, producto, cantidad, precio_unitario, subtotal, color_toalla, color_moño, tipo_jaboncito, color_jaboncito, nombre_bebe, tarjetita))
            item_id = cursor.lastrowid
            _registrar_historial(pedido_id, "producto", "N/A", producto, conn=conn)
            _registrar_evento(pedido_id, "Producto agregado", f"{producto} x{cantidad}", OrigenEvento.CLIENTE, "sistema", conn=conn)
            conn.commit()
            return item_id
    except Exception as e: logger_pedidos.error(f"❌ Error agregando producto: {e}"); raise

# Funciones de cálculo y validación se mantienen idénticas a la versión anterior...
def calcular_subtotal(pedido_id: int) -> float:
    with get_db_connection() as conn: return conn.cursor().execute("SELECT SUM(subtotal) FROM pedido_items WHERE pedido_id = ?", (pedido_id,)).fetchone()[0] or 0.0
def calcular_envio(pedido_id: int) -> float:
    with get_db_connection() as conn:
        row = conn.cursor().execute("SELECT costo_envio FROM entregas WHERE pedido_id = ?", (pedido_id,)).fetchone()
        return row[0] if row else 0.0
def calcular_total(pedido_id: int) -> float: return calcular_subtotal(pedido_id) + calcular_envio(pedido_id)
def calcular_saldo(pedido_id: int) -> float:
    total = calcular_total(pedido_id)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(monto) FROM pagos WHERE pedido_id = ? AND confirmado = 1", (pedido_id,))
        pagado = cursor.fetchone()[0] or 0.0
        return round(total - pagado, 2)

def pedido_es_cotizable(pedido_id: int) -> bool:
    p = obtener_pedido(pedido_id)
    return bool(p and p.items and all(i.producto and i.cantidad > 0 for i in p.items))

def pedido_esta_completo(pedido_id: int) -> bool:
    p = obtener_pedido(pedido_id)
    if not p or not p.items: return False
    e = p.entrega
    if not e or not e.fecha_entrega or not e.tipo_entrega: return False
    if e.tipo_entrega == "Domicilio" and (not e.direccion or not e.municipio): return False
    return all((i.producto and i.cantidad and i.color_toalla and i.color_moño and i.tipo_jaboncito and i.nombre_bebe and i.tarjetita) for i in p.items)

def obtener_campos_faltantes(pedido_id: int) -> List[Dict[str, int]]:
    p = obtener_pedido(pedido_id)
    if not p: return []
    if not p.items: return [{"campo": "producto", "prioridad": 10}]
    i, e = p.items[0], p.entrega
    f = []
    if not i.color_toalla: f.append({"campo": "color_toalla", "prioridad": 5})
    if not i.color_moño: f.append({"campo": "color_moño", "prioridad": 5})
    if not i.tipo_jaboncito: f.append({"campo": "tipo_jaboncito", "prioridad": 4})
    if not i.nombre_bebe: f.append({"campo": "nombre_bebe", "prioridad": 3})
    if not i.tarjetita: f.append({"campo": "tarjetita", "prioridad": 2})
    if not e: f.append({"campo": "tipo_entrega", "prioridad": 7})
    else:
        if not e.fecha_entrega: f.append({"campo": "fecha_entrega", "prioridad": 6})
        if e.tipo_entrega == "Domicilio":
            if not e.direccion: f.append({"campo": "direccion", "prioridad": 7})
            if not e.municipio: f.append({"campo": "municipio", "prioridad": 7})
    return f

def obtener_porcentaje_completitud(pedido_id: int) -> int:
    p = obtener_pedido(pedido_id)
    if not p or not p.items: return 0
    i, e = p.items[0], p.entrega
    w = 0
    if i.producto: w += PESOS_COMPLETITUD['producto']
    if i.cantidad > 0: w += PESOS_COMPLETITUD['cantidad']
    if i.color_toalla and i.color_moño: w += PESOS_COMPLETITUD['colores']
    if e and e.fecha_entrega and e.tipo_entrega:
        w += PESOS_COMPLETITUD['fecha']
        if (e.tipo_entrega == "Domicilio" and e.direccion and e.municipio) or (e.tipo_entrega == "Local"): w += PESOS_COMPLETITUD['entrega']
    if i.nombre_bebe: w += PESOS_COMPLETITUD['nombre_bebe']
    if i.tarjetita: w += PESOS_COMPLETITUD['tarjetita']
    return int((w / sum(PESOS_COMPLETITUD.values())) * 100)

def cambiar_estado(pedido_id: int, nuevo_estado: str, usuario: str = "sistema"):
    if not validar_estado(nuevo_estado): raise ValueError(f"Estado '{nuevo_estado}' inválido.")
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT estado FROM pedidos WHERE id = ?", (pedido_id,))
        row = cursor.fetchone()
        if not row: raise ValueError("Pedido no encontrado.")
        if not validar_transicion(row[0], nuevo_estado): raise ValueError(f"Transición inválida: {row[0]} -> {nuevo_estado}")
        actualizar_pedido(pedido_id, usuario=usuario, estado=nuevo_estado)

def desactivar_bot(pedido_id: int): actualizar_pedido(pedido_id, modo_atencion=ModoAtencion.DALIA.value)
def activar_bot(pedido_id: int): actualizar_pedido(pedido_id, modo_atencion=ModoAtencion.BOT.value)
