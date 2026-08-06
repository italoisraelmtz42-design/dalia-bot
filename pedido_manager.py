import sqlite3
import datetime
import json
import logging
from typing import List, Dict, Optional, Tuple
from database import get_db_connection
from constantes import (
    logger_pedidos, EstadoPedido, ModoAtencion, OrigenEvento, 
    COLUMNAS_PERMITIDAS_PEDIDOS, PESOS_COMPLETITUD, TRANSICIONES_VALIDAS
)
from types import PedidoData, ItemData, PagoData, EntregaData
from validators import validar_estado, validar_transicion

# =====================
# # UTILIDAD: PROTECCIÓN DE TIPOS PARA SQLITE (CRÍTICO)
# =====================
def _safe_sql_val(val):
    """
    Convierte cualquier valor a un tipo aceptado por SQLite (str, int, float, None, bytes).
    SI el valor es un dict, list o objeto, lo convierte a string JSON para evitar el error.
    """
    if isinstance(val, (str, int, float, type(None), bytes)):
        return val
    try:
        if hasattr(val, 'value'): # Para Enums
            return val.value
        if isinstance(val, (dict, list, tuple, set)):
            return json.dumps(val) # Convertir objetos complejos a string
        return str(val)
    except Exception:
        return str(val)

# =====================
# # Eventos Internos
# =====================
def _registrar_evento(pedido_id: int, evento: str, descripcion: str = None, 
                      origen: OrigenEvento = OrigenEvento.SISTEMA, usuario: str = "sistema", conn=None):
    logger_pedidos.info(f"[SQL DEBUG] EVENTO: pedido_id={pedido_id}({type(pedido_id)}), evento={evento}({type(evento)})")
    def _execute(connection):
        connection.execute(
            "INSERT INTO pedido_eventos (pedido_id, evento, descripcion, origen, usuario) VALUES (?, ?, ?, ?, ?)",
            (pedido_id, evento, descripcion, origen.value, usuario)
        )
    if conn: _execute(conn)
    else:
        try:
            with get_db_connection() as new_conn:
                _execute(new_conn)
                new_conn.commit()
        except Exception as e:
            logger_pedidos.error(f"Error registrando evento para pedido {pedido_id}: {e}")

def _registrar_historial(pedido_id: int, campo: str, valor_anterior: str, valor_nuevo: str, usuario: str = "sistema", conn=None):
    logger_pedidos.info(f"[SQL DEBUG] HISTORIAL: pedido_id={pedido_id}({type(pedido_id)}), campo={campo}({type(campo)})")
    def _execute(connection):
        connection.execute(
            "INSERT INTO pedido_historial (pedido_id, campo, valor_anterior, valor_nuevo, usuario) VALUES (?, ?, ?, ?, ?)",
            (pedido_id, campo, str(valor_anterior), str(valor_nuevo), usuario)
        )
    if conn: _execute(conn)
    else:
        try:
            with get_db_connection() as new_conn:
                _execute(new_conn)
                new_conn.commit()
        except Exception as e:
            logger_pedidos.error(f"Error registrando historial para pedido {pedido_id}: {e}")

# =====================
# # CRUD Base
# =====================
def generar_folio(conn) -> str:
    cursor = conn.cursor()
    cursor.execute("SELECT folio FROM pedidos WHERE folio LIKE ? ORDER BY folio DESC LIMIT 1", (f"DAL-{datetime.datetime.now().strftime('%Y')}-%",))
    row = cursor.fetchone()
    new_num = (int(row[0].split('-')[-1]) + 1) if row else 1
    return f"DAL-{datetime.datetime.now().strftime('%Y')}-{new_num:06d}"

def crear_pedido(cliente_id: int, telefono: str) -> int:
    logger_pedidos.info(f"[SQL DEBUG] CREAR PEDIDO: cliente_id={cliente_id}({type(cliente_id)}), telefono={telefono}({type(telefono)})")
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        folio = generar_folio(conn)
        cursor.execute("""
            INSERT INTO pedidos (folio, cliente_id, telefono, estado, modo_atencion) 
            VALUES (?, ?, ?, ?, ?)
        """, (folio, cliente_id, telefono, EstadoPedido.BORRADOR.value, ModoAtencion.BOT.value))
        pedido_id = cursor.lastrowid
        _registrar_evento(pedido_id, "Pedido creado", f"Folio {folio}", OrigenEvento.SISTEMA, "sistema", conn=conn)
        conn.commit()
        logger_pedidos.info(f"✅ Pedido creado: Folio {folio}, ID {pedido_id}")
        return pedido_id
    except Exception as e:
        if conn: conn.rollback()
        logger_pedidos.error(f"❌ Error al crear pedido: {e}")
        raise RuntimeError(f"No se pudo crear el pedido: {e}")
    finally:
        if conn: conn.close()

def obtener_pedido(pedido_id: int) -> Optional[PedidoData]:
    logger_pedidos.info(f"[SQL DEBUG] OBTENER PEDIDO: pedido_id={pedido_id}({type(pedido_id)})")
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
    CORRECCIÓN DE SEGURIDAD Y AUDITORÍA DE TIPOS.
    """
    if not kwargs: return
    for key in kwargs:
        if key not in COLUMNAS_PERMITIDAS_PEDIDOS:
            raise ValueError(f"Columna '{key}' no permitida.")
    
    conn = None
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
        old_data = dict(cursor.fetchone())
        
        set_clause = ", ".join([f"{key} = ?" for key in kwargs.keys()])
        
        # 👇 AUDITORÍA DE TIPOS Y CONVERSIÓN A SEGUROS PARA SQLITE
        valores_seguros = []
        for key, val in kwargs.items():
            original_type = type(val)
            safe_val = _safe_sql_val(val)
            valores_seguros.append(safe_val)
            logger_pedidos.info(f"[SQL DEBUG] ACTUALIZAR: {key} -> original={val}({original_type}), seguro={safe_val}({type(safe_val)})")
        
        valores_seguros.append(pedido_id)
        conn.execute(f"UPDATE pedidos SET {set_clause}, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id = ?", valores_seguros)
        
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
        raise RuntimeError(f"No se pudo actualizar el pedido: {e}")
    finally:
        if conn: conn.close()

# =====================
# # Productos (Logs agregados para depuración)
# =====================
def agregar_producto(pedido_id: int, producto: str, cantidad: int, precio_unitario: float, 
                     color_toalla: str = None, color_moño: str = None, 
                     tipo_jaboncito: str = None, color_jaboncito: str = None,
                     nombre_bebe: str = None, tarjetita: str = None) -> int:
    logger_pedidos.info(f"[SQL DEBUG] AGREGAR PRODUCTO: pedido_id={pedido_id}({type(pedido_id)}), producto={producto}({type(producto)})")
    try:
        with get_db_connection() as conn:
            subtotal = cantidad * precio_unitario
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO pedido_items 
                (pedido_id, producto, cantidad, precio_unitario, subtotal, color_toalla, 
                 color_moño, tipo_jaboncito, color_jaboncito, nombre_bebe, tarjetita) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (pedido_id, producto, cantidad, precio_unitario, subtotal, color_toalla, 
                  color_moño, tipo_jaboncito, color_jaboncito, nombre_bebe, tarjetita))
            item_id = cursor.lastrowid
            _registrar_historial(pedido_id, "producto", "N/A", producto, conn=conn)
            _registrar_evento(pedido_id, "Producto agregado", f"{producto} x{cantidad}", OrigenEvento.CLIENTE, "sistema", conn=conn)
            conn.commit()
            return item_id
    except Exception as e:
        logger_pedidos.error(f"❌ Error agregando producto: {e}")
        raise RuntimeError(f"No se pudo agregar el producto: {e}")

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
# # Pagos y Cálculos
# =====================
def registrar_pago(pedido_id: int, tipo: str, monto: float, metodo: str, comprobante: str = None):
    logger_pedidos.info(f"[SQL DEBUG] PAGO: pedido_id={pedido_id}({type(pedido_id)}), tipo={tipo}({type(tipo)})")
    try:
        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO pagos (pedido_id, tipo, monto, metodo, comprobante) 
                VALUES (?, ?, ?, ?, ?)
            """, (pedido_id, tipo, monto, metodo, comprobante))
            _registrar_evento(pedido_id, f"Pago registrado ({tipo})", f"${monto} via {metodo}", OrigenEvento.CLIENTE, "cliente", conn=conn)
            conn.commit()
    except Exception as e:
        logger_pedidos.error(f"❌ Error registrando pago: {e}")
        raise RuntimeError(f"No se pudo registrar el pago: {e}")

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

def calcular_total(pedido_id: int) -> float: return calcular_subtotal(pedido_id) + calcular_envio(pedido_id)

def calcular_saldo(pedido_id: int) -> float:
    total = calcular_total(pedido_id)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(monto) FROM pagos WHERE pedido_id = ? AND confirmado = 1", (pedido_id,))
        pagado = cursor.fetchone()[0] or 0.0
        return round(total - pagado, 2)

# =====================
# # Validaciones, Porcentajes y Estados (Añadidos logs menores)
# =====================
def pedido_es_cotizable(pedido_id: int) -> bool:
    pedido = obtener_pedido(pedido_id)
    if not pedido or not pedido.items: return False
    return all(item.producto and item.cantidad > 0 for item in pedido.items)

def pedido_esta_completo(pedido_id: int) -> bool:
    pedido = obtener_pedido(pedido_id)
    if not pedido or not pedido.items: return False
    entrega = pedido.entrega
    if not entrega or not entrega.fecha_entrega or not entrega.tipo_entrega: return False
    if entrega.tipo_entrega == "Domicilio" and (not entrega.direccion or not entrega.municipio): return False
    for item in pedido.items:
        if not (item.producto and item.cantidad and item.color_toalla and 
                item.color_moño and item.tipo_jaboncito and 
                item.nombre_bebe and item.tarjetita):
            return False
    return True

def obtener_campos_faltantes(pedido_id: int) -> List[Dict[str, int]]:
    pedido = obtener_pedido(pedido_id)
    if not pedido: return []
    faltantes = []
    if not pedido.items:
        faltantes.append({"campo": "producto", "prioridad": 10})
        return faltantes
    item = pedido.items[0]
    if not item.color_toalla: faltantes.append({"campo": "color_toalla", "prioridad": 5})
    if not item.color_moño: faltantes.append({"campo": "color_moño", "prioridad": 5})
    if not item.tipo_jaboncito: faltantes.append({"campo": "tipo_jaboncito", "prioridad": 4})
    if not item.nombre_bebe: faltantes.append({"campo": "nombre_bebe", "prioridad": 3})
    if not item.tarjetita: faltantes.append({"campo": "tarjetita", "prioridad": 2})
    entrega = pedido.entrega
    if not entrega:
        faltantes.append({"campo": "tipo_entrega", "prioridad": 7})
    else:
        if not entrega.fecha_entrega: faltantes.append({"campo": "fecha_entrega", "prioridad": 6})
        if entrega.tipo_entrega == "Domicilio":
            if not entrega.direccion: faltantes.append({"campo": "direccion", "prioridad": 7})
            if not entrega.municipio: faltantes.append({"campo": "municipio", "prioridad": 7})
    return faltantes

def obtener_porcentaje_completitud(pedido_id: int) -> int:
    pedido = obtener_pedido(pedido_id)
    if not pedido or not pedido.items: return 0
    peso_obtenido = 0
    item, entrega = pedido.items[0], pedido.entrega
    if item.producto: peso_obtenido += PESOS_COMPLETITUD['producto']
    if item.cantidad > 0: peso_obtenido += PESOS_COMPLETITUD['cantidad']
    if item.color_toalla and item.color_moño: peso_obtenido += PESOS_COMPLETITUD['colores']
    if entrega and entrega.fecha_entrega and entrega.tipo_entrega:
        peso_obtenido += PESOS_COMPLETITUD['fecha']
        if entrega.tipo_entrega == "Domicilio" and entrega.direccion and entrega.municipio:
            peso_obtenido += PESOS_COMPLETITUD['entrega']
        elif entrega.tipo_entrega == "Local":
            peso_obtenido += PESOS_COMPLETITUD['entrega']
    if item.nombre_bebe: peso_obtenido += PESOS_COMPLETITUD['nombre_bebe']
    if item.tarjetita: peso_obtenido += PESOS_COMPLETITUD['tarjetita']
    return int((peso_obtenido / sum(PESOS_COMPLETITUD.values())) * 100)

def cambiar_estado(pedido_id: int, nuevo_estado: str, usuario: str = "sistema"):
    logger_pedidos.info(f"[SQL DEBUG] CAMBIAR ESTADO: pedido_id={pedido_id}({type(pedido_id)}), nuevo_estado={nuevo_estado}({type(nuevo_estado)})")
    if not validar_estado(nuevo_estado): raise ValueError(f"Estado '{nuevo_estado}' inválido.")
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT estado FROM pedidos WHERE id = ?", (pedido_id,))
        row = cursor.fetchone()
        if not row: raise ValueError("Pedido no encontrado.")
        if not validar_transicion(row[0], nuevo_estado):
            raise ValueError(f"Transición inválida: {row[0]} -> {nuevo_estado}")
        actualizar_pedido(pedido_id, usuario=usuario, estado=nuevo_estado)

def desactivar_bot(pedido_id: int):
    actualizar_pedido(pedido_id, modo_atencion=ModoAtencion.DALIA.value)
    _registrar_evento(pedido_id, "Bot desactivado", "Transferido a Dalia", OrigenEvento.SISTEMA, "sistema")

def activar_bot(pedido_id: int):
    actualizar_pedido(pedido_id, modo_atencion=ModoAtencion.BOT.value)
    _registrar_evento(pedido_id, "Bot reactivado", "El bot retomó la atención", OrigenEvento.SISTEMA, "sistema")