import sqlite3
import datetime
import logging
from typing import List, Dict, Optional, Tuple, Any

from database import get_db_connection

logger = logging.getLogger(__name__)

# =====================
# # Estados y Configuración
# =====================
ESTADOS_VALIDOS = [
    "BORRADOR", "CAPTURANDO_DATOS", "COTIZADO", "PENDIENTE_ANTICIPO", 
    "ANTICIPO_CONFIRMADO", "TRANSFERIDO_A_DALIA", "EN_PRODUCCION", 
    "LISTO", "ENTREGADO", "CANCELADO"
]

MODOS_ATENCION_VALIDOS = ["BOT", "DALIA", "SUSPENDIDO"]

TRANSICIONES_VALIDAS = {
    "BORRADOR": ["CAPTURANDO_DATOS", "CANCELADO"],
    "CAPTURANDO_DATOS": ["COTIZADO", "CANCELADO"],
    "COTIZADO": ["PENDIENTE_ANTICIPO", "CANCELADO"],
    "PENDIENTE_ANTICIPO": ["ANTICIPO_CONFIRMADO", "CANCELADO"],
    "ANTICIPO_CONFIRMADO": ["TRANSFERIDO_A_DALIA", "CANCELADO"],
    "TRANSFERIDO_A_DALIA": ["EN_PRODUCCION", "CANCELADO"],
    "EN_PRODUCCION": ["LISTO", "CANCELADO"],
    "LISTO": ["ENTREGADO", "CANCELADO"],
    "ENTREGADO": [],
    "CANCELADO": []
}

# Lista blanca para validar kwargs en actualizar_pedido (Obs 2)
COLUMNAS_PERMITIDAS_PEDIDOS = {
    'estado', 'modo_atencion', 'es_urgente', 'cliente_id', 'telefono'
}

# Pesos dinámicos para el porcentaje de completitud (Obs 7)
PESOS_COMPLETITUD = {
    'producto': 20,
    'cantidad': 20,
    'colores': 20,
    'entrega': 20,
    'nombre_bebe': 10,
    'tarjetita': 10
}

# =====================
# # Eventos Internos (Con soporte para conexión compartida - Obs 3)
# =====================
def _registrar_evento(pedido_id: int, evento: str, descripcion: str = None, usuario: str = "sistema", conn=None):
    """Registra un evento. Si se pasa conn, comparte la transacción."""
    def _execute(connection):
        connection.execute(
            "INSERT INTO pedido_eventos (pedido_id, evento, descripcion, usuario) VALUES (?, ?, ?, ?)",
            (pedido_id, evento, descripcion, usuario)
        )
    
    if conn:
        _execute(conn)
    else:
        try:
            with get_db_connection() as new_conn:
                _execute(new_conn)
                new_conn.commit()
        except Exception as e:
            logger.error(f"Error registrando evento para pedido {pedido_id}: {e}")

def _registrar_historial(pedido_id: int, campo: str, valor_anterior: str, valor_nuevo: str, usuario: str = "sistema", conn=None):
    """Registra un cambio. Si se pasa conn, comparte la transacción."""
    def _execute(connection):
        connection.execute(
            "INSERT INTO pedido_historial (pedido_id, campo, valor_anterior, valor_nuevo, usuario) VALUES (?, ?, ?, ?, ?)",
            (pedido_id, campo, str(valor_anterior), str(valor_nuevo), usuario)
        )
    
    if conn:
        _execute(conn)
    else:
        try:
            with get_db_connection() as new_conn:
                _execute(new_conn)
                new_conn.commit()
        except Exception as e:
            logger.error(f"Error registrando historial para pedido {pedido_id}: {e}")

# =====================
# # Funciones Públicas - CRUD Base
# =====================
def generar_folio(conn) -> str:
    """
    Genera un folio consecutivo y humano (DAL-2026-000001).
    Recibe una conexión abierta para garantizar la atomicidad de la transacción.
    """
    current_year = datetime.datetime.now().strftime("%Y")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT folio FROM pedidos 
        WHERE folio LIKE ? 
        ORDER BY folio DESC LIMIT 1
    """, (f"DAL-{current_year}-%",))
    row = cursor.fetchone()
    
    if row:
        last_folio = row[0]
        last_num = int(last_folio.split('-')[-1])
        new_num = last_num + 1
    else:
        new_num = 1
    
    return f"DAL-{current_year}-{new_num:06d}"

def crear_pedido(cliente_id: int, telefono: str) -> int:
    """Crea el pedido y su evento inicial en una SOLA transacción atómica (Obs 3)."""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        folio = generar_folio(conn)
        
        cursor.execute("""
            INSERT INTO pedidos (folio, cliente_id, telefono, estado, modo_atencion) 
            VALUES (?, ?, ?, ?, ?)
        """, (folio, cliente_id, telefono, "BORRADOR", "BOT"))
        
        pedido_id = cursor.lastrowid
        
        # Registrar el evento dentro de la misma transacción
        _registrar_evento(pedido_id, "Pedido creado", f"Folio {folio}", "sistema", conn=conn)
        
        conn.commit()
        logger.info(f"✅ Pedido creado: Folio {folio}, ID {pedido_id}")
        return pedido_id
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Error al crear pedido: {e}")
        raise RuntimeError(f"No se pudo crear el pedido: {e}")
    finally:
        if conn:
            conn.close()

def obtener_pedido(pedido_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
        pedido_row = cursor.fetchone()
        if not pedido_row:
            return None
        
        pedido_data = dict(pedido_row)
        cursor.execute("SELECT * FROM pedido_items WHERE pedido_id = ?", (pedido_id,))
        pedido_data['items'] = [dict(row) for row in cursor.fetchall()]
        
        cursor.execute("SELECT * FROM pagos WHERE pedido_id = ?", (pedido_id,))
        pedido_data['pagos'] = [dict(row) for row in cursor.fetchall()]
        
        cursor.execute("SELECT * FROM entregas WHERE pedido_id = ?", (pedido_id,))
        entrega_row = cursor.fetchone()
        pedido_data['entrega'] = dict(entrega_row) if entrega_row else None
        
        return pedido_data

def actualizar_pedido(pedido_id: int, usuario: str = "sistema", **kwargs):
    """Actualiza campos. SOLO permite columnas de la lista blanca (Obs 2)."""
    if not kwargs:
        return
    
    # Validar columnas permitidas
    for key in kwargs:
        if key not in COLUMNAS_PERMITIDAS_PEDIDOS:
            raise ValueError(f"La columna '{key}' no está permitida para actualización.")
    
    conn = None
    try:
        conn = get_db_connection()
        conn.row_factory = sqlite3.Row
        
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
        old_data = dict(cursor.fetchone())
        
        set_clause = ", ".join([f"{key} = ?" for key in kwargs.keys()])
        values = list(kwargs.values())
        values.append(pedido_id)
        conn.execute(f"UPDATE pedidos SET {set_clause}, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id = ?", values)
        
        for key, new_val in kwargs.items():
            old_val = old_data.get(key, None)
            if str(old_val) != str(new_val):
                _registrar_historial(pedido_id, key, old_val, new_val, usuario, conn=conn)
                if key in ['estado', 'modo_atencion', 'es_urgente']:
                    _registrar_evento(pedido_id, f"Cambio de {key}", f"{old_val} -> {new_val}", usuario, conn=conn)
        
        conn.commit()
        logger.info(f"✅ Pedido {pedido_id} actualizado.")
    except Exception as e:
        if conn:
            conn.rollback()
        logger.error(f"❌ Error actualizando pedido {pedido_id}: {e}")
        raise RuntimeError(f"No se pudo actualizar el pedido: {e}")
    finally:
        if conn:
            conn.close()

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
            cursor.execute("""
                INSERT INTO pedido_items 
                (pedido_id, producto, cantidad, precio_unitario, subtotal, color_toalla, 
                 color_moño, tipo_jaboncito, color_jaboncito, nombre_bebe, tarjetita) 
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (pedido_id, producto, cantidad, precio_unitario, subtotal, color_toalla, 
                  color_moño, tipo_jaboncito, color_jaboncito, nombre_bebe, tarjetita))
            
            item_id = cursor.lastrowid
            _registrar_historial(pedido_id, "producto", "N/A", producto, conn=conn)
            _registrar_evento(pedido_id, "Producto agregado", f"{producto} x{cantidad}", "sistema", conn=conn)
            conn.commit()
            return item_id
    except Exception as e:
        logger.error(f"❌ Error agregando producto: {e}")
        raise RuntimeError(f"No se pudo agregar el producto: {e}")

def eliminar_producto(item_id: int):
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT pedido_id, producto FROM pedido_items WHERE id = ?", (item_id,))
            res = cursor.fetchone()
            if not res:
                raise RuntimeError("Ítem no encontrado")
            pedido_id, producto = res[0], res[1]

            cursor.execute("DELETE FROM pedido_items WHERE id = ?", (item_id,))
            _registrar_historial(pedido_id, "producto_eliminado", producto, "Eliminado", conn=conn)
            _registrar_evento(pedido_id, "Producto eliminado", producto, "sistema", conn=conn)
            conn.commit()
    except Exception as e:
        logger.error(f"❌ Error eliminando item {item_id}: {e}")
        raise

# =====================
# # Pagos y Anticipos
# =====================
def registrar_pago(pedido_id: int, tipo: str, monto: float, metodo: str, comprobante: str = None):
    try:
        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO pagos (pedido_id, tipo, monto, metodo, comprobante) 
                VALUES (?, ?, ?, ?, ?)
            """, (pedido_id, tipo, monto, metodo, comprobante))
            _registrar_evento(pedido_id, f"Pago registrado ({tipo})", f"${monto} via {metodo}", "cliente", conn=conn)
            conn.commit()
    except Exception as e:
        logger.error(f"❌ Error registrando pago: {e}")
        raise RuntimeError(f"No se pudo registrar el pago: {e}")

def registrar_anticipo(pedido_id: int, monto: float, metodo: str, comprobante: str = None):
    registrar_pago(pedido_id, "ANTICIPO", monto, metodo, comprobante)

# =====================
# # Cálculos (Nunca almacenados)
# =====================
def calcular_subtotal(pedido_id: int) -> float:
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(subtotal) FROM pedido_items WHERE pedido_id = ?", (pedido_id,))
        total = cursor.fetchone()[0]
        return total if total else 0.0

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
        pagado = cursor.fetchone()[0]
        pagado = pagado if pagado else 0.0
        return round(total - pagado, 2)

# =====================
# # Validaciones y Campos Faltantes
# =====================
def validar_integridad_pedido(pedido_id: int) -> List[str]:
    errores = []
    pedido = obtener_pedido(pedido_id)
    if not pedido:
        return ["El pedido no existe."]
    
    if pedido['estado'] not in ESTADOS_VALIDOS:
        errores.append(f"Estado '{pedido['estado']}' inválido.")
    if pedido['modo_atencion'] not in MODOS_ATENCION_VALIDOS:
        errores.append(f"Modo de atención '{pedido['modo_atencion']}' inválido.")
    if not pedido['items']:
        errores.append("El pedido debe tener al menos un producto.")
    
    saldo = calcular_saldo(pedido_id)
    total = calcular_total(pedido_id)
    if saldo < 0:
        errores.append("El saldo no puede ser negativo.")
    
    pagos_confirmados = sum(pago['monto'] for pago in pedido['pagos'] if pago['confirmado'] == 1)
    if pagos_confirmados > total:
        errores.append("El total de pagos confirmados no puede superar el total del pedido.")
    
    return errores

def pedido_es_cotizable(pedido_id: int) -> bool:
    """
    Para cotizar solo se necesita producto y cantidad (Obs 4).
    """
    pedido = obtener_pedido(pedido_id)
    if not pedido or not pedido['items']:
        return False
    for item in pedido['items']:
        if not item['producto'] or not item['cantidad'] or item['cantidad'] <= 0:
            return False
    return True

def pedido_esta_completo(pedido_id: int) -> bool:
    pedido = obtener_pedido(pedido_id)
    if not pedido or not pedido['items']:
        return False
    
    entrega = pedido.get('entrega')
    if not entrega or not entrega.get('fecha_entrega') or not entrega.get('tipo_entrega'):
        return False
    
    # Si es a domicilio, exige dirección y MUNICIPIO (Obs 5)
    if entrega['tipo_entrega'] == "Domicilio":
        if not entrega.get('direccion') or not entrega.get('municipio'):
            return False
    
    for item in pedido['items']:
        if not (item['producto'] and item['cantidad'] and 
                item['color_toalla'] and item['color_moño'] and
                item['tipo_jaboncito'] and item['nombre_bebe'] and item['tarjetita']):
            return False
    
    return True

# =====================
# # Campos Faltantes Estructurados (Puro negocio, sin preguntas)
# =====================
def obtener_campos_faltantes(pedido_id: int) -> List[Dict[str, Any]]:
    """
    Retorna SOLO el campo y la prioridad. (Obs 6)
    La conversación (crm.py) decide cómo preguntarlo.
    """
    pedido = obtener_pedido(pedido_id)
    if not pedido:
        return []
    
    faltantes = []
    
    if not pedido['items']:
        faltantes.append({"campo": "producto", "prioridad": 10})
        return faltantes
    
    item = pedido['items'][0]
    
    if not item['color_toalla']: faltantes.append({"campo": "color_toalla", "prioridad": 5})
    if not item['color_moño']: faltantes.append({"campo": "color_moño", "prioridad": 5})
    if not item['tipo_jaboncito']: faltantes.append({"campo": "tipo_jaboncito", "prioridad": 4})
    if not item['nombre_bebe']: faltantes.append({"campo": "nombre_bebe", "prioridad": 3})
    if not item['tarjetita']: faltantes.append({"campo": "tarjetita", "prioridad": 2})
    
    entrega = pedido.get('entrega')
    if not entrega:
        faltantes.append({"campo": "tipo_entrega", "prioridad": 7})
    else:
        if not entrega.get('fecha_entrega'):
            faltantes.append({"campo": "fecha_entrega", "prioridad": 6})
        if entrega['tipo_entrega'] == "Domicilio" and (not entrega.get('direccion') or not entrega.get('municipio')):
            if not entrega.get('direccion'): faltantes.append({"campo": "direccion", "prioridad": 7})
            if not entrega.get('municipio'): faltantes.append({"campo": "municipio", "prioridad": 7})
    
    return faltantes

def obtener_porcentaje_completitud(pedido_id: int) -> int:
    """
    Calcula el % de completitud basado en PESOS dinámicos (Obs 7).
    """
    pedido = obtener_pedido(pedido_id)
    if not pedido or not pedido['items']:
        return 0
    
    peso_obtenido = 0
    item = pedido['items'][0]
    entrega = pedido.get('entrega')
    
    # Validación de secciones
    if item['producto']: peso_obtenido += PESOS_COMPLETITUD['producto']
    if item['cantidad'] and item['cantidad'] > 0: peso_obtenido += PESOS_COMPLETITUD['cantidad']
    if item['color_toalla'] and item['color_moño']: peso_obtenido += PESOS_COMPLETITUD['colores']
    if entrega and entrega.get('fecha_entrega') and entrega.get('tipo_entrega'):
        if entrega['tipo_entrega'] == "Domicilio" and entrega.get('direccion') and entrega.get('municipio'):
            peso_obtenido += PESOS_COMPLETITUD['entrega']
        elif entrega['tipo_entrega'] == "Local":
            peso_obtenido += PESOS_COMPLETITUD['entrega']
    if item['nombre_bebe']: peso_obtenido += PESOS_COMPLETITUD['nombre_bebe']
    if item['tarjetita']: peso_obtenido += PESOS_COMPLETITUD['tarjetita']
    
    total_posible = sum(PESOS_COMPLETITUD.values())
    return int((peso_obtenido / total_posible) * 100)

# =====================
# # Resúmenes y Estados
# =====================
def generar_resumen(pedido_id: int) -> str:
    """
    Genera resumen en TEXTO PLANO. Sin Markdown para WhatsApp (Obs 8).
    """
    pedido = obtener_pedido(pedido_id)
    if not pedido:
        return "No se encontró el pedido."
    
    subtotal = calcular_subtotal(pedido_id)
    envio = calcular_envio(pedido_id)
    total = calcular_total(pedido_id)
    saldo = calcular_saldo(pedido_id)
    
    # Obtener datos para el resumen (Obs 8)
    entrega = pedido.get('entrega')
    municipio = entrega.get('municipio', 'No especificado') if entrega else 'No especificado'
    tipo_entrega = entrega.get('tipo_entrega', 'No especificada') if entrega else 'No especificada'
    fecha_entrega = entrega.get('fecha_entrega', 'Pendiente') if entrega else 'Pendiente'
    es_urgente = "Sí" if pedido.get('es_urgente') else "No"
    
    # Obtener forma de pago (primer pago registrado)
    forma_pago = "No registrado"
    if pedido['pagos']:
        forma_pago = pedido['pagos'][0].get('metodo', 'No especificado')
    
    items_str = []
    for item in pedido['items']:
        line = f"Producto: {item['producto']} (x{item['cantidad']})"
        if item.get('color_toalla'): line += f"\n  Colores: Toalla {item['color_toalla']}, Moño {item['color_moño']}"
        if item.get('nombre_bebe'): line += f"\n  Bebé: {item['nombre_bebe']}"
        if item.get('tarjetita'): line += f"\n  Tarjeta: {item['tarjetita']}"
        items_str.append(line)

    resumen = f"""
RESUMEN DEL PEDIDO
===================
Folio: {pedido['folio']}
Modo atencion: {pedido['modo_atencion']}
Estado: {pedido['estado']}
Fecha creacion: {pedido['fecha_creacion']}

Cliente
Telefono: {pedido['telefono']}

Productos
{chr(10).join(items_str) if items_str else "No hay productos agregados."}

Entrega
Forma: {tipo_entrega}
Municipio: {municipio}
Direccion: {entrega.get('direccion', 'No especificada') if entrega else 'No especificada'}
Fecha entrega: {fecha_entrega}
Entrega urgente: {es_urgente}

Finanzas
Subtotal: ${subtotal:.2f}
Envio: ${envio:.2f}
Forma de pago: {forma_pago}
Total: ${total:.2f}
Anticipo: ${total - saldo:.2f}
Saldo Pendiente: ${saldo:.2f}
    """
    _registrar_evento(pedido_id, "Resumen generado", "El sistema generó el resumen mediante SQLite", "sistema")
    return resumen.strip()

def cambiar_estado(pedido_id: int, nuevo_estado: str, usuario: str = "sistema"):
    if nuevo_estado not in ESTADOS_VALIDOS:
        raise ValueError(f"Estado '{nuevo_estado}' inválido.")
    
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT estado FROM pedidos WHERE id = ?", (pedido_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError("Pedido no encontrado.")
        
        estado_actual = row[0]
        if nuevo_estado not in TRANSICIONES_VALIDAS.get(estado_actual, []):
            raise ValueError(f"Transición de estado inválida: {estado_actual} -> {nuevo_estado}")
        
        actualizar_pedido(pedido_id, usuario=usuario, estado=nuevo_estado)

# =====================
# # Modo de Atención (Bot / Dalia)
# =====================
def desactivar_bot(pedido_id: int):
    actualizar_pedido(pedido_id, modo_atencion="DALIA")
    _registrar_evento(pedido_id, "Bot desactivado", "El bot fue desactivado y se transfirió a Dalia", "sistema")

def activar_bot(pedido_id: int):
    actualizar_pedido(pedido_id, modo_atencion="BOT")
    _registrar_evento(pedido_id, "Bot reactivado", "El bot retomó la atención del pedido", "sistema")

# =====================
# # Nueva Joya: Integridad Global
# =====================
def validar_integridad_global() -> List[str]:
    """
    Auditoría global de todas las tablas y referencias del sistema.
    """
    errores_globales = []
    with get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Verificar todos los pedidos
        cursor.execute("SELECT id FROM pedidos")
        pedidos = cursor.fetchall()
        
        for row in pedidos:
            pedido_id = row['id']
            # Verificar integridad del pedido
            errores_pedido = validar_integridad_pedido(pedido_id)
            if errores_pedido:
                errores_globales.append(f"Pedido {pedido_id}:\n  - " + "\n  - ".join(errores_pedido))
            
            # Verificar huérfanos en items
            cursor.execute("SELECT id FROM pedido_items WHERE pedido_id = ?", (pedido_id,))
            if not cursor.fetchone():
                errores_globales.append(f"Pedido {pedido_id}: No tiene items.")
            
            # Verificar huérfanos en pagos
            cursor.execute("SELECT id FROM pagos WHERE pedido_id = ?", (pedido_id,))
            # No es obligatorio tener pagos, solo verificar si la referencia es válida
            
            # Verificar huérfanos en entregas
            cursor.execute("SELECT pedido_id FROM entregas WHERE pedido_id = ?", (pedido_id,))
            if not cursor.fetchone():
                errores_globales.append(f"Pedido {pedido_id}: No tiene registro de entrega.")
            
            # Verificar huérfanos en historial y eventos (que siempre tengan al menos el evento de creación)
            cursor.execute("SELECT id FROM pedido_eventos WHERE pedido_id = ? AND evento = 'Pedido creado'", (pedido_id,))
            if not cursor.fetchone():
                errores_globales.append(f"Pedido {pedido_id}: Carece del evento 'Pedido creado'.")
        
        # Verificar huérfanos en tabla items (referencias a pedidos que no existen)
        cursor.execute("""
            SELECT pi.id FROM pedido_items pi 
            LEFT JOIN pedidos p ON pi.pedido_id = p.id 
            WHERE p.id IS NULL
        """)
        items_huérfanos = cursor.fetchall()
        for row in items_huérfanos:
            errores_globales.append(f"Item huérfano encontrado (ID: {row['id']}) sin pedido asociado.")
            
    return errores_globales
