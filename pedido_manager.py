import sqlite3
import datetime
import logging
from typing import List, Dict, Optional, Tuple, Any

# Configuración de logging
logger = logging.getLogger(__name__)

# Máquina de Estados y Transiciones Válidas
ESTADOS_VALIDOS = [
    "BORRADOR", "CAPTURANDO_DATOS", "COTIZADO", "PENDIENTE_ANTICIPO", 
    "ANTICIPO_CONFIRMADO", "TRANSFERIDO_A_DALIA", "EN_PRODUCCION", 
    "LISTO", "ENTREGADO", "CANCELADO"
]

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

class PedidoError(Exception):
    """Excepción personalizada para errores de lógica de pedido."""
    pass


# --- Funciones Auxiliares Internas ---
def _log_historial(conn, pedido_id: int, cambio: str, usuario: str = "sistema"):
    """Registra un cambio en el historial del pedido."""
    conn.execute(
        "INSERT INTO pedido_historial (pedido_id, cambio, usuario) VALUES (?, ?, ?)",
        (pedido_id, cambio, usuario)
    )


# --- Funciones Públicas del Módulo ---
def generar_folio() -> str:
    """Genera un folio único para el pedido basado en timestamp y contador."""
    timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    return f"DALIA-{timestamp}"


def crear_pedido(cliente_id: int, telefono: str) -> int:
    """
    Crea un nuevo pedido en estado BORRADOR.
    Retorna el ID del pedido creado.
    """
    try:
        with db_connect() as conn:
            cursor = conn.cursor()
            folio = generar_folio()
            cursor.execute("""
                INSERT INTO pedidos (folio, cliente_id, telefono, estado, bot_activo) 
                VALUES (?, ?, ?, ?, ?)
            """, (folio, cliente_id, telefono, "BORRADOR", 1))
            
            pedido_id = cursor.lastrowid
            _log_historial(conn, pedido_id, f"Pedido creado en estado BORRADOR con folio {folio}")
            conn.commit()
            logger.info(f"✅ Pedido creado: Folio {folio}, ID {pedido_id}")
            return pedido_id
    except Exception as e:
        logger.error(f"❌ Error al crear pedido: {e}")
        raise PedidoError(f"No se pudo crear el pedido: {e}")


def obtener_pedido(pedido_id: int) -> Optional[Dict[str, Any]]:
    """
    Obtiene toda la información completa de un pedido (incluye items, pagos, entrega).
    """
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
        pedido_row = cursor.fetchone()
        if not pedido_row:
            return None
        
        pedido_data = dict(pedido_row)
        
        # Obtener Items
        cursor.execute("SELECT * FROM pedido_items WHERE pedido_id = ?", (pedido_id,))
        pedido_data['items'] = [dict(row) for row in cursor.fetchall()]
        
        # Obtener Pagos
        cursor.execute("SELECT * FROM pagos WHERE pedido_id = ?", (pedido_id,))
        pedido_data['pagos'] = [dict(row) for row in cursor.fetchall()]
        
        # Obtener Entrega
        cursor.execute("SELECT * FROM entregas WHERE pedido_id = ?", (pedido_id,))
        entrega_row = cursor.fetchone()
        pedido_data['entrega'] = dict(entrega_row) if entrega_row else None
        
        return pedido_data


def actualizar_pedido(pedido_id: int, **kwargs):
    """
    Actualiza campos genéricos del pedido.
    """
    if not kwargs:
        return
    try:
        with db_connect() as conn:
            set_clause = ", ".join([f"{key} = ?" for key in kwargs.keys()])
            values = list(kwargs.values())
            values.append(pedido_id)
            conn.execute(f"UPDATE pedidos SET {set_clause}, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id = ?", values)
            conn.commit()
            
            for key, val in kwargs.items():
                _log_historial(conn, pedido_id, f"Campo '{key}' actualizado a: {val}")
            logger.info(f"✅ Pedido {pedido_id} actualizado con: {kwargs}")
    except Exception as e:
        logger.error(f"❌ Error actualizando pedido {pedido_id}: {e}")
        raise PedidoError(f"No se pudo actualizar el pedido: {e}")


def agregar_producto(pedido_id: int, producto: str, cantidad: int, precio_unitario: float, 
                     color_toalla: str = None, color_moño: str = None, 
                     tipo_jaboncito: str = None, color_jaboncito: str = None,
                     nombre_bebe: str = None, tarjetita: str = None) -> int:
    """
    Agrega un item al pedido y actualiza el porcentaje de completitud.
    """
    try:
        with db_connect() as conn:
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
            _log_historial(conn, pedido_id, f"Producto agregado: {producto} x{cantidad}")
            
            # Recalcular porcentaje de completitud
            nuevo_porcentaje, _ = obtener_porcentaje_completitud(pedido_id)
            conn.execute("UPDATE pedidos SET porcentaje_completitud = ?, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id = ?", (nuevo_porcentaje, pedido_id))
            
            conn.commit()
            logger.info(f"✅ Producto agregado (ID {item_id}) al pedido {pedido_id}")
            return item_id
    except Exception as e:
        logger.error(f"❌ Error agregando producto al pedido {pedido_id}: {e}")
        raise PedidoError(f"No se pudo agregar el producto: {e}")


def eliminar_producto(item_id: int):
    """
    Elimina un item del pedido y recalcula el porcentaje.
    """
    try:
        with db_connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT pedido_id FROM pedido_items WHERE id = ?", (item_id,))
            res = cursor.fetchone()
            if not res:
                raise PedidoError("Ítem no encontrado")
            pedido_id = res[0]

            cursor.execute("DELETE FROM pedido_items WHERE id = ?", (item_id,))
            _log_historial(conn, pedido_id, f"Producto eliminado (Item ID: {item_id})")
            
            nuevo_porcentaje, _ = obtener_porcentaje_completitud(pedido_id)
            conn.execute("UPDATE pedidos SET porcentaje_completitud = ?, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id = ?", (nuevo_porcentaje, pedido_id))
            
            conn.commit()
            logger.info(f"✅ Producto (ID {item_id}) eliminado del pedido {pedido_id}")
    except Exception as e:
        logger.error(f"❌ Error eliminando item {item_id}: {e}")
        raise PedidoError(f"No se pudo eliminar el producto: {e}")


def calcular_subtotal(pedido_id: int) -> float:
    """Calcula la suma total de los subtotales de los items del pedido."""
    with db_connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(subtotal) FROM pedido_items WHERE pedido_id = ?", (pedido_id,))
        total = cursor.fetchone()[0]
        return total if total else 0.0


def calcular_envio(pedido_id: int) -> float:
    """Obtiene el costo de envío registrado en la tabla de entregas."""
    with db_connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT costo_envio FROM entregas WHERE pedido_id = ?", (pedido_id,))
        row = cursor.fetchone()
        return row[0] if row else 0.0


def calcular_total(pedido_id: int) -> float:
    """Calcula el total del pedido (Subtotal + Envío)."""
    return calcular_subtotal(pedido_id) + calcular_envio(pedido_id)


def calcular_saldo(pedido_id: int) -> float:
    """Calcula el saldo pendiente (Total - Pagos Confirmados)."""
    total = calcular_total(pedido_id)
    with db_connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(monto) FROM pagos WHERE pedido_id = ? AND confirmado = 1", (pedido_id,))
        pagado = cursor.fetchone()[0]
        pagado = pagado if pagado else 0.0
        return round(total - pagado, 2)


def registrar_pago(pedido_id: int, tipo: str, monto: float, metodo: str, comprobante: str = None):
    """Registra un pago en la base de datos."""
    try:
        with db_connect() as conn:
            conn.execute("""
                INSERT INTO pagos (pedido_id, tipo, monto, metodo, comprobante) 
                VALUES (?, ?, ?, ?, ?)
            """, (pedido_id, tipo, monto, metodo, comprobante))
            _log_historial(conn, pedido_id, f"Pago registrado: {tipo} por ${monto} via {metodo}")
            conn.commit()
            logger.info(f"✅ Pago registrado para pedido {pedido_id}: {tipo} ${monto}")
    except Exception as e:
        logger.error(f"❌ Error registrando pago en pedido {pedido_id}: {e}")
        raise PedidoError(f"No se pudo registrar el pago: {e}")


def registrar_anticipo(pedido_id: int, monto: float, metodo: str, comprobante: str = None):
    """Atajo para registrar un pago de tipo ANTICIPO."""
    registrar_pago(pedido_id, "ANTICIPO", monto, metodo, comprobante)


def cambiar_estado(pedido_id: int, nuevo_estado: str, usuario: str = "sistema"):
    """Cambia el estado del pedido validando la máquina de estados."""
    if nuevo_estado not in ESTADOS_VALIDOS:
        raise PedidoError(f"Estado '{nuevo_estado}' inválido.")
    
    with db_connect() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT estado FROM pedidos WHERE id = ?", (pedido_id,))
        row = cursor.fetchone()
        if not row:
            raise PedidoError("Pedido no encontrado.")
        
        estado_actual = row[0]
        
        if nuevo_estado not in TRANSICIONES_VALIDAS.get(estado_actual, []):
            raise PedidoError(f"Transición de estado inválida: {estado_actual} -> {nuevo_estado}")
        
        conn.execute("UPDATE pedidos SET estado = ?, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id = ?", (nuevo_estado, pedido_id))
        _log_historial(conn, pedido_id, f"Estado cambiado: {estado_actual} -> {nuevo_estado}", usuario)
        conn.commit()
        logger.info(f"✅ Pedido {pedido_id} cambió de estado: {estado_actual} -> {nuevo_estado}")


def obtener_porcentaje_completitud(pedido_id: int) -> Tuple[int, List[str]]:
    """
    Calcula el porcentaje de completitud del pedido y devuelve la lista de campos faltantes.
    Retorna: (porcentaje: int, campos_faltantes: list[str])
    """
    pedido = obtener_pedido(pedido_id)
    if not pedido:
        return 0, ["Pedido no existe"]

    total_campos = 9
    completados = 0
    faltantes = []

    # 1. Producto y Cantidad (depende de que existan items)
    if pedido['items']:
        completados += 2
    else:
        completados += 0 # No sumamos nada
        faltantes.extend(["Producto", "Cantidad"])
    
    # 2. Colores (Toalla y Moño) y Tipo Jaboncito
    item_colores_pendientes = False
    for item in pedido['items']:
        if not item['color_toalla'] or not item['color_moño'] or not item['tipo_jaboncito']:
            item_colores_pendientes = True
            break
    
    if pedido['items'] and not item_colores_pendientes:
        completados += 3
    else:
        faltantes.extend(["Colores", "Jaboncito"])

    # 3. Entrega (Fecha y Dirección)
    entrega = pedido.get('entrega')
    if entrega and entrega.get('fecha_entrega'):
        completados += 2
    else:
        faltantes.extend(["Fecha de Entrega", "Dirección o Municipio"])

    # 4. Nombre del Bebé y Tarjetita
    nombre_bebe_pendiente = False
    tarjetita_pendiente = False
    
    # Si hay items, revisamos el primero para estos datos compartidos
    if pedido['items']:
        first_item = pedido['items'][0]
        if not first_item.get('nombre_bebe'):
            nombre_bebe_pendiente = True
        if not first_item.get('tarjetita'):
            tarjetita_pendiente = True
    
    if pedido['items']:
        if not nombre_bebe_pendiente:
            completados += 1
        else:
            faltantes.append("Nombre del Bebé")
        
        if not tarjetita_pendiente:
            completados += 1
        else:
            faltantes.append("Texto Tarjetita")
    
    # Cálculo final
    porcentaje = int((completados / total_campos) * 100) if total_campos > 0 else 0
    
    return porcentaje, faltantes


def campos_faltantes(pedido_id: int) -> List[str]:
    """Wrapper para devolver únicamente la lista de campos faltantes."""
    _, faltantes = obtener_porcentaje_completitud(pedido_id)
    return faltantes


def generar_resumen(pedido_id: int) -> str:
    """
    Genera el resumen del pedido usando datos puros de SQLite (SIN GPT).
    """
    pedido = obtener_pedido(pedido_id)
    if not pedido:
        return "❌ No se encontró el pedido."
    
    subtotal = calcular_subtotal(pedido_id)
    envio = calcular_envio(pedido_id)
    total = calcular_total(pedido_id)
    saldo = calcular_saldo(pedido_id)
    
    items_str = []
    for item in pedido['items']:
        line = f"   • {item['producto']} (x{item['cantidad']}) - ${item['subtotal']:.2f}"
        if item.get('color_toalla'): line += f"\n     Colores: Toalla {item['color_toalla']}, Moño {item['color_moño']}"
        if item.get('nombre_bebe'): line += f"\n     Bebé: {item['nombre_bebe']}"
        if item.get('tarjetita'): line += f"\n     Tarjeta: {item['tarjetita']}"
        items_str.append(line)

    entrega = pedido.get('entrega')
    entrega_str = "No especificada" if not entrega else f"{entrega.get('tipo_entrega', 'N/A')} a {entrega.get('municipio', 'N/A')} ({entrega.get('direccion', 'N/A')}) - Fecha: {entrega.get('fecha_entrega', 'Pendiente')}"

    resumen = f"""
📦 **RESUMEN DEL PEDIDO**
Folio: {pedido['folio']}

👤 Cliente
Teléfono: {pedido['telefono']}

🛍️ Productos
{chr(10).join(items_str) if items_str else "   No hay productos agregados."}

🚚 Entrega
{entrega_str}

💰 Finanzas
Subtotal: ${subtotal:.2f}
Envío: ${envio:.2f}
Total: ${total:.2f}
Anticipo: ${total - saldo:.2f}
Saldo Pendiente: ${saldo:.2f}

Estado actual: {pedido['estado']}
Completitud: {pedido['porcentaje_completitud']}%
    """
    return resumen.strip()


def desactivar_bot(pedido_id: int):
    """Desactiva el bot para este pedido específico."""
    actualizar_pedido(pedido_id, bot_activo=0)
    logger.info(f"🚫 Bot desactivado para el pedido {pedido_id}")


def activar_bot(pedido_id: int):
    """Re-activa el bot para este pedido específico."""
    actualizar_pedido(pedido_id, bot_activo=1)
    logger.info(f"✅ Bot reactivado para el pedido {pedido_id}")


# Helper local para usar dentro del módulo (dado que database.py ya está importado fuera, lo importamos dentro para evitar dependencias circulares)
def db_connect():
    from database import get_db_connection
    return get_db_connection()
