import sqlite3
import datetime
import logging
from typing import List, Dict, Optional, Tuple, Any

from database import get_db_connection
from constantes import (
    logger_pedidos, EstadoPedido, ModoAtencion, OrigenEvento, 
    COLUMNAS_PERMITIDAS_PEDIDOS, PESOS_COMPLETITUD, TRANSICIONES_VALIDAS,
    ESTADOS_VALIDOS, MODOS_VALIDOS,
    PedidoData, ItemData, PagoData, EntregaData
)
from validators import (
    validar_estado, validar_modo_atencion, validar_transicion,
    validar_entrega, validar_producto
)

# =====================
# # Eventos Internos (Nativo, sin _exec_sql)
# =====================
def _registrar_evento(pedido_id: int, evento: str, descripcion: str = None, 
                      origen: OrigenEvento = OrigenEvento.SISTEMA, usuario: str = "sistema", conn=None):
    sql = "INSERT INTO pedido_eventos (pedido_id, evento, descripcion, origen, usuario) VALUES (?, ?, ?, ?, ?)"
    params = (pedido_id, evento, descripcion, origen.value, usuario)
    
    if conn:
        conn.execute(sql, params)
    else:
        try:
            with get_db_connection() as new_conn:
                new_conn.execute(sql, params)
                new_conn.commit()
        except Exception as e:
            logger_pedidos.error(f"Error evento {pedido_id}: {e}")

def _registrar_historial(pedido_id: int, campo: str, valor_anterior: str, valor_nuevo: str, usuario: str = "sistema", conn=None):
    sql = "INSERT INTO pedido_historial (pedido_id, campo, valor_anterior, valor_nuevo, usuario) VALUES (?, ?, ?, ?, ?)"
    params = (pedido_id, campo, str(valor_anterior), str(valor_nuevo), usuario)
    
    if conn:
        conn.execute(sql, params)
    else:
        try:
            with get_db_connection() as new_conn:
                new_conn.execute(sql, params)
                new_conn.commit()
        except Exception as e:
            logger_pedidos.error(f"Error historial {pedido_id}: {e}")

# =====================
# # Funciones Públicas - CRUD Base
# =====================
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
    # ================================================================
    # [INSTRUMENTACIÓN TEMPORAL] - LOG DE ENTRADA
    # ================================================================
    logger_pedidos.info("="*80)
    logger_pedidos.info("=== ENTRADA actualizar_pedido (pedido_manager) ===")
    logger_pedidos.info(f"pedido_id = {pedido_id} ({type(pedido_id).__name__})")
    logger_pedidos.info(f"usuario = {usuario}")
    logger_pedidos.info(f"kwargs = {repr(kwargs)}")
    logger_pedidos.info(f"TIPO_KWARGS = {type(kwargs).__name__}")
    logger_pedidos.info(f"CLAVES_KWARGS = {list(kwargs.keys())}")
    logger_pedidos.info("="*80)
    # ================================================================
    
    if not kwargs: return
    
    conn = None
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
        old_data = dict(cursor.fetchone())
        
        set_clause = ", ".join([f"{key} = ?" for key in kwargs.keys()])
        params = list(kwargs.values())
        params.append(pedido_id)
        
        conn.execute(f"UPDATE pedidos SET {set_clause}, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id = ?", params)
        
        for key, new_val in kwargs.items():
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

# =====================
# # Productos
# =====================
def agregar_producto(pedido_id: int, producto: str, cantidad: int, precio_unitario: float, 
                     color_toalla: str = None, color_moño: str = None, 
                     tipo_jaboncito: str = None, color_jaboncito: str = None,
                     nombre_bebe: str = None, tarjetita: str = None) -> int:
    try:
        with get_db_connection() as conn:
            subtotal = cantidad * precio_unitario
            cursor = conn.cursor()
            sql = """INSERT INTO pedido_items (pedido_id, producto, cantidad, precio_unitario, subtotal, color_toalla, color_moño, tipo_jaboncito, color_jaboncito, nombre_bebe, tarjetita) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            params = (pedido_id, producto, cantidad, precio_unitario, subtotal, color_toalla, color_moño, tipo_jaboncito, color_jaboncito, nombre_bebe, tarjetita)
            
            conn.execute(sql, params)
            item_id = cursor.lastrowid
            
            _registrar_historial(pedido_id, "producto", "N/A", producto, conn=conn)
            _registrar_evento(pedido_id, "Producto agregado", f"{producto} x{cantidad}", OrigenEvento.CLIENTE, "sistema", conn=conn)
            
            conn.commit()
            return item_id
    except Exception as e:
        logger_pedidos.error(f"❌ Error agregando producto: {e}")
        raise

def eliminar_producto(item_id: int):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT pedido_id, producto FROM pedido_items WHERE id = ?", (item_id,))
            res = cursor.fetchone()
            if not res: raise RuntimeError("Ítem no encontrado")
            pedido_id, producto = res[0], res[1]
            
            cursor.execute("DELETE FROM pedido_items WHERE id = ?", (item_id,))
            _registrar_historial(pedido_id, "producto_eliminado", producto, "Eliminado", conn=conn)
            _registrar_evento(pedido_id, "Producto eliminado", producto, OrigenEvento.SISTEMA, "sistema", conn=conn)
            conn.commit()
    except Exception as e:
        logger_pedidos.error(f"❌ Error eliminando item {item_id}: {e}")
        raise

# =====================
# # Pagos y Anticipos
# =====================
def registrar_pago(pedido_id: int, tipo: str, monto: float, metodo: str, comprobante: str = None):
    try:
        with get_db_connection() as conn:
            sql = "INSERT INTO pagos (pedido_id, tipo, monto, metodo, comprobante) VALUES (?, ?, ?, ?, ?)"
            params = (pedido_id, tipo, monto, metodo, comprobante)
            conn.execute(sql, params)
            
            _registrar_evento(pedido_id, f"Pago registrado ({tipo})", f"${monto} via {metodo}", OrigenEvento.CLIENTE, "cliente", conn=conn)
            conn.commit()
    except Exception as e:
        logger_pedidos.error(f"❌ Error registrando pago: {e}")
        raise

def registrar_anticipo(pedido_id: int, monto: float, metodo: str, comprobante: str = None):
    registrar_pago(pedido_id, "ANTICIPO", monto, metodo, comprobante)

# =====================
# # Cálculos y Validaciones
# =====================
def calcular_subtotal(pedido_id: int) -> float:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(subtotal) FROM pedido_items WHERE pedido_id = ?", (pedido_id,))
        return cursor.fetchone()[0] or 0.0

def calcular_envio(pedido_id: int) -> float:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT costo_envio FROM entregas WHERE pedido_id = ?", (pedido_id,))
        row = cursor.fetchone()
        return row[0] if row else 0.0

def calcular_total(pedido_id: int) -> float:
    return calcular_subtotal(pedido_id) + calcular_envio(pedido_id)

def calcular_saldo(pedido_id: int) -> float:
    total = calcular_total(pedido_id)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(monto) FROM pagos WHERE pedido_id = ? AND confirmado = 1", (pedido_id,))
        pagado = cursor.fetchone()[0] or 0.0
        return round(total - pagado, 2)

def validar_integridad_pedido(pedido_id: int) -> List[str]:
    errores = []
    pedido = obtener_pedido(pedido_id)
    if not pedido: return ["El pedido no existe."]
    
    if pedido['estado'] not in ESTADOS_VALIDOS: errores.append(f"Estado '{pedido['estado']}' inválido.")
    if pedido['modo_atencion'] not in MODOS_VALIDOS: errores.append(f"Modo de atención '{pedido['modo_atencion']}' inválido.")
    if not pedido['items']: errores.append("El pedido debe tener al menos un producto.")
    
    saldo = calcular_saldo(pedido_id)
    total = calcular_total(pedido_id)
    if saldo < 0: errores.append("El saldo no puede ser negativo.")
    
    pagos_confirmados = sum(pago['monto'] for pago in pedido['pagos'] if pago['confirmado'] == 1)
    if pagos_confirmados > total: errores.append("El total de pagos confirmados no puede superar el total del pedido.")
    
    return errores

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

# =====================
# # Campos y Porcentaje
# =====================
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
   
