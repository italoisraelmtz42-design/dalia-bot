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
        logger_pedidos.warning(f"[_registrar_evento] Se omite registro de evento '{evento}' porque pedido_id es None. Descripción: {descripcion}")
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
# # FUNCIONES DE PERSISTENCIA DEL CHAT
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

def crear_pedido(cliente_id: int, telefono: str) -> int:
    """
    Crea un pedido con folio único de forma atómica.
    Utiliza una transacción `BEGIN IMMEDIATE` para evitar colisiones.
    """
    conn = None
    try:
        conn = get_db_connection()
        conn.execute("BEGIN IMMEDIATE")

        cursor = conn.cursor()
        cursor.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM pedidos")
        next_seq = cursor.fetchone()[0]

        current_year = datetime.datetime.now().strftime("%Y")
        folio = f"DAL-{current_year}-{next_seq:06d}"

        sql = "INSERT INTO pedidos (folio, cliente_id, telefono, estado, modo_atencion) VALUES (?, ?, ?, ?, ?)"
        params = (folio, cliente_id, telefono, EstadoPedido.BORRADOR.value, ModoAtencion.BOT.value)
        cursor.execute(sql, params)

        pedido_id = cursor.lastrowid
        if not pedido_id:
            raise Exception("No se pudo obtener el ID del pedido después del INSERT")

        _registrar_evento(pedido_id, "Pedido creado", f"Folio {folio}", OrigenEvento.SISTEMA, "sistema", conn=conn)

        conn.commit()
        logger_pedidos.info(f"✅ Pedido creado con folio {folio}, ID {pedido_id}")
        return pedido_id

    except sqlite3.IntegrityError as e:
        if conn:
            conn.rollback()
        logger_pedidos.error(f"❌ Error de integridad al crear pedido (folio duplicado): {e}")
        raise RuntimeError("No se pudo crear el pedido debido a un conflicto de folio. Intenta de nuevo.")
    except Exception as e:
        if conn:
            conn.rollback()
        logger_pedidos.error(f"❌ Error inesperado al crear pedido: {e}")
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

def generar_resumen(pedido_id: int) -> str:
    """
    Genera un resumen en texto plano del pedido, basado en SQLite.
    """
    pedido = obtener_pedido(pedido_id)
    if not pedido:
        return "❌ No se encontró el pedido."

    subtotal = calcular_subtotal(pedido_id)
    envio = calcular_envio(pedido_id)
    total = calcular_total(pedido_id)
    saldo = calcular_saldo(pedido_id)

    items_str = []
    for item in pedido.items:
        line = f"Producto: {item.producto} (x{item.cantidad})"
        if item.color_toalla or item.color_moño:
            line += f"\n  Colores: Toalla {item.color_toalla or 'No especificado'}, Moño {item.color_moño or 'No especificado'}"
        if item.nombre_bebe:
            line += f"\n  Bebé: {item.nombre_bebe}"
        if item.tarjetita:
            line += f"\n  Tarjeta: {item.tarjetita}"
        items_str.append(line)

    entrega = pedido.entrega
    entrega_str = "No especificada" if not entrega else f"{entrega.tipo_entrega} ({entrega.municipio or 'N/A'}) - Fecha: {entrega.fecha_entrega or 'Pendiente'}"

    resumen = f"""
RESUMEN DEL PEDIDO
===================
Folio: {pedido.folio}
Modo atencion: {pedido.modo_atencion}
Estado: {pedido.estado}
Fecha creacion: {pedido.fecha_creacion}

Cliente
Telefono: {pedido.telefono}

Productos
{chr(10).join(items_str) if items_str else "No hay productos agregados."}

Entrega
{entrega_str}

Finanzas
Subtotal: ${subtotal:.2f}
Envio: ${envio:.2f}
Total: ${total:.2f}
Saldo: ${saldo:.2f}
    """
    return resumen.strip()

def actualizar_pedido(pedido_id: int, usuario: str = "sistema", **kwargs):
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
        raise e
    finally:
        if conn:
            conn.close()

def actualizar_entrega(pedido_id: int, tipo_entrega: str, municipio: str = None, direccion: str = None,
                       fecha_entrega: str = None, costo_envio: float = 0.0):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pedido_id FROM entregas WHERE pedido_id = ?", (pedido_id,))
        exists = cursor.fetchone()
        if exists:
            conn.execute("""
                UPDATE entregas SET tipo_entrega=?, municipio=?, direccion=?, fecha_entrega=?, costo_envio=?
                WHERE pedido_id=?
            """, (tipo_entrega, municipio, direccion, fecha_entrega, costo_envio, pedido_id))
        else:
            conn.execute("""
                INSERT INTO entregas (pedido_id, tipo_entrega, municipio, direccion, fecha_entrega, costo_envio)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (pedido_id, tipo_entrega, municipio, direccion, fecha_entrega, costo_envio))
        conn.commit()

def agregar_producto(pedido_id: int, producto: str, cantidad: int, precio_unitario: float,
                     color_toalla: str = None, color_moño: str = None,
                     tipo_jaboncito: str = None, color_jaboncito: str = None,
                     nombre_bebe: str = None, tarjetita: str = None) -> int:
    try:
        with get_db_connection() as conn:
            subtotal = cantidad * precio_unitario
            cursor = conn.cursor()
            sql = """INSERT INTO pedido_items (pedido_id, producto, cantidad, precio_unitario, subtotal, color_toalla,
                     color_moño, tipo_jaboncito, color_jaboncito, nombre_bebe, tarjetita)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
            params = (pedido_id, producto, cantidad, precio_unitario, subtotal,
                      color_toalla, color_moño, tipo_jaboncito, color_jaboncito,
                      nombre_bebe, tarjetita)
            cursor.execute(sql, params)
            item_id = cursor.lastrowid
            _registrar_historial(pedido_id, "producto", "N/A", producto, conn=conn)
            _registrar_evento(pedido_id, "Producto agregado", f"{producto} x{cantidad}",
                              OrigenEvento.CLIENTE, "sistema", conn=conn)
            conn.commit()
            return item_id
    except Exception as e:
        raise e

# ==============================================================================
# # CÁLCULOS
# ==============================================================================
def calcular_subtotal(pedido_id: int) -> float:
    with get_db_connection() as conn:
        return conn.cursor().execute("SELECT SUM(subtotal) FROM pedido_items WHERE pedido_id = ?", (pedido_id,)).fetchone()[0] or 0.0

def calcular_envio(pedido_id: int) -> float:
    with get_db_connection() as conn:
        row = conn.cursor().execute("SELECT costo_envio FROM entregas WHERE pedido_id = ?", (pedido_id,)).fetchone()
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

# ==============================================================================
# # VALIDACIONES Y ESTADOS
# ==============================================================================
def pedido_es_cotizable(pedido_id: int) -> bool:
    p = obtener_pedido(pedido_id)
    return bool(p and p.items and all(i.producto and i.cantidad > 0 for i in p.items))

def pedido_esta_completo(pedido_id: int) -> bool:
    p = obtener_pedido(pedido_id)
    if not p or not p.items:
        return False
    e = p.entrega
    if not e or not e.fecha_entrega or not e.tipo_entrega:
        return False
    if e.tipo_entrega == "Domicilio" and (not e.direccion or not e.municipio):
        return False
    return all((i.producto and i.cantidad and i.color_toalla and i.color_moño and
                i.tipo_jaboncito and i.nombre_bebe and i.tarjetita) for i in p.items)

def obtener_campos_faltantes(pedido_id: int) -> List[Dict[str, int]]:
    p = obtener_pedido(pedido_id)
    if not p:
        return []
    if not p.items:
        return [{"campo": "producto", "prioridad": 10}]
    i, e = p.items[0], p.entrega
    f = []
    if not i.color_toalla:
        f.append({"campo": "color_toalla", "prioridad": 5})
    if not i.color_moño:
        f.append({"campo": "color_moño", "prioridad": 5})
    if not i.tipo_jaboncito:
        f.append({"campo": "tipo_jaboncito", "prioridad": 4})
    if not i.nombre_bebe:
        f.append({"campo": "nombre_bebe", "prioridad": 3})
    if not i.tarjetita:
        f.append({"campo": "tarjetita", "prioridad": 2})
    if not e:
        f.append({"campo": "tipo_entrega", "prioridad": 7})
    else:
        if not e.fecha_entrega:
            f.append({"campo": "fecha_entrega", "prioridad": 6})
        if e.tipo_entrega == "Domicilio":
            if not e.direccion:
                f.append({"campo": "direccion", "prioridad": 7})
            if not e.municipio:
                f.append({"campo": "municipio", "prioridad": 7})
    return f

def obtener_porcentaje_completitud(pedido_id: int) -> int:
    p = obtener_pedido(pedido_id)
    if not p or not p.items:
        return 0
    i, e = p.items[0], p.entrega
    w = 0
    if i.producto:
        w += PESOS_COMPLETITUD['producto']
    if i.cantidad > 0:
        w += PESOS_COMPLETITUD['cantidad']
    if i.color_toalla and i.color_moño:
        w += PESOS_COMPLETITUD['colores']
    if e and e.fecha_entrega and e.tipo_entrega:
        w += PESOS_COMPLETITUD['fecha']
        if (e.tipo_entrega == "Domicilio" and e.direccion and e.municipio) or (e.tipo_entrega == "Local"):
            w += PESOS_COMPLETITUD['entrega']
    if i.nombre_bebe:
        w += PESOS_COMPLETITUD['nombre_bebe']
    if i.tarjetita:
        w += PESOS_COMPLETITUD['tarjetita']
    return int((w / sum(PESOS_COMPLETITUD.values())) * 100)

def cambiar_estado(pedido_id: int, nuevo_estado: str, usuario: str = "sistema"):
    if not validar_estado(nuevo_estado):
        raise ValueError(f"Estado '{nuevo_estado}' inválido.")
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT estado FROM pedidos WHERE id = ?", (pedido_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError("Pedido no encontrado.")
        if not validar_transicion(row[0], nuevo_estado):
            raise ValueError(f"Transición inválida: {row[0]} -> {nuevo_estado}")
        actualizar_pedido(pedido_id, usuario=usuario, estado=nuevo_estado)

def desactivar_bot(pedido_id: int):
    actualizar_pedido(pedido_id, modo_atencion=ModoAtencion.DALIA.value)

def activar_bot(pedido_id: int):
    actualizar_pedido(pedido_id, modo_atencion=ModoAtencion.BOT.value)
