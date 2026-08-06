"""
Módulo: pedido_manager.py
Responsable de toda la lógica de negocio relacionada con pedidos.
Lee y escribe exclusivamente en SQLite, nunca en RAM.
"""
import sqlite3
import random
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional, Dict, Any, List

from database import get_connection

ZONA_HORARIA_NEGOCIO = ZoneInfo("America/Monterrey")

# ==========================================
# CONSTANTES (precios de productos, envío, etc.)
# ==========================================

PRECIOS_PRODUCTO = {
    "osito_toalla": 150.00,
    "osito_jabon": 180.00,
    "velita": 90.00,
    # ... ampliar según catálogo real
}
COSTO_ENVIO = 50.00  # por defecto, se puede ajustar por municipio

# ==========================================
# FUNCIONES AUXILIARES
# ==========================================

def _dict_from_row(row):
    """Convierte una fila sqlite3.Row a dict."""
    return dict(row) if row else None

def _obtener_siguiente_numero(fecha_str):
    """Obtiene el siguiente número de folio para la fecha."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.cursor()
        cur.execute("SELECT ultimo FROM contador_folios WHERE fecha = ?", (fecha_str,))
        row = cur.fetchone()
        siguiente = row["ultimo"] + 1 if row else 1
        cur.execute("INSERT OR REPLACE INTO contador_folios (fecha, ultimo) VALUES (?, ?)",
                    (fecha_str, siguiente))
        conn.commit()
        return siguiente
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()

def generar_folio_definitivo(pedido_id, fecha_str):
    """
    Genera un folio real y consecutivo tipo DAL-YYYYMMDD-NNNNNN para el
    día indicado (fecha_str en formato YYYYMMDD) y se lo asigna al pedido.
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

def calcular_totales(producto: str, cantidad: int, precio_unitario: Optional[float] = None,
                     envio: Optional[float] = None) -> Dict[str, float]:
    """
    Calcula subtotal, envío y total.
    Si no se pasa precio_unitario, se busca en PRECIOS_PRODUCTO.
    Si no se pasa envio, se usa COSTO_ENVIO por defecto.
    """
    if precio_unitario is None:
        precio_unitario = PRECIOS_PRODUCTO.get(producto, 0.0)
    if envio is None:
        envio = COSTO_ENVIO if cantidad > 0 else 0.0

    subtotal = cantidad * precio_unitario
    total = subtotal + envio
    return {
        "subtotal": round(subtotal, 2),
        "envio": round(envio, 2),
        "total": round(total, 2)
    }

# ==========================================
# FUNCIONES PRINCIPALES DEL MANAGER
# ==========================================

def crear_pedido(cliente_id: int, datos_iniciales: Optional[Dict] = None) -> int:
    """
    Crea un nuevo pedido en estado 'Cotizando' con un folio temporal.
    Retorna el ID del pedido.
    """
    ahora = datetime.now(ZONA_HORARIA_NEGOCIO).strftime("%Y%m%d%H%M%S")
    sufijo = f"{random.randint(0, 9999):04d}"
    folio = f"TMP-{ahora}-{cliente_id}-{sufijo}"

    conn = get_connection()
    cur = conn.cursor()

    # Campos base
    producto = datos_iniciales.get("producto") if datos_iniciales else None
    cantidad = datos_iniciales.get("cantidad") if datos_iniciales else None
    evento = datos_iniciales.get("evento") if datos_iniciales else None
    fecha_evento = datos_iniciales.get("fecha_evento") if datos_iniciales else None
    tipo_entrega = datos_iniciales.get("tipo_entrega") if datos_iniciales else None
    municipio = datos_iniciales.get("municipio") if datos_iniciales else None
    direccion = datos_iniciales.get("direccion") if datos_iniciales else None

    if producto and cantidad:
        precios = calcular_totales(producto, cantidad)
        precio_unitario = PRECIOS_PRODUCTO.get(producto, 0.0)
        subtotal = precios["subtotal"]
        envio = precios["envio"]
        total = precios["total"]
    else:
        precio_unitario = 0.0
        subtotal = 0.0
        envio = 0.0
        total = 0.0

    ahora_iso = datetime.now(ZONA_HORARIA_NEGOCIO).isoformat()
    cur.execute("""
        INSERT INTO pedidos (
            folio, cliente_id, estatus,
            producto, cantidad, precio_unitario, subtotal, envio, total,
            anticipo, saldo,
            evento, fecha_evento,
            tipo_entrega, municipio, direccion,
            color_toalla, color_moño, tipo_jaboncito, color_jaboncito,
            nombre_bebe, tarjetita, notas,
            bot_activo,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        folio,
        cliente_id,
        "Cotizando",
        producto,
        cantidad,
        precio_unitario,
        subtotal,
        envio,
        total,
        0.0,  # anticipo
        total,  # saldo = total (sin anticipo)
        evento,
        fecha_evento,
        tipo_entrega,
        municipio,
        direccion,
        datos_iniciales.get("color_toalla") if datos_iniciales else None,
        datos_iniciales.get("color_moño") if datos_iniciales else None,
        datos_iniciales.get("tipo_jaboncito") if datos_iniciales else None,
        datos_iniciales.get("color_jaboncito") if datos_iniciales else None,
        datos_iniciales.get("nombre_bebe") if datos_iniciales else None,
        datos_iniciales.get("tarjetita") if datos_iniciales else None,
        datos_iniciales.get("notas") if datos_iniciales else None,
        1,  # bot_activo = 1 (activo)
        ahora_iso,
        ahora_iso
    ))
    pedido_id = cur.lastrowid
    conn.commit()
    conn.close()
    return pedido_id

def obtener_pedido(pedido_id: int) -> Optional[Dict]:
    """Obtiene un pedido por su ID."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,))
    row = cur.fetchone()
    conn.close()
    return _dict_from_row(row)

def obtener_pedido_activo(cliente_id: int) -> Optional[Dict]:
    """Obtiene el pedido más reciente (activo) de un cliente."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM pedidos
        WHERE cliente_id = ?
        ORDER BY id DESC
        LIMIT 1
    """, (cliente_id,))
    row = cur.fetchone()
    conn.close()
    return _dict_from_row(row)

def actualizar_pedido(pedido_id: int, campos: Dict) -> Dict:
    """
    Actualiza uno o más campos del pedido. Recalcula totales si cambia producto/cantidad.
    Retorna el pedido actualizado completo.
    """
    pedido = obtener_pedido(pedido_id)
    if not pedido:
        raise ValueError(f"Pedido {pedido_id} no encontrado")

    nuevos = pedido.copy()
    for k, v in campos.items():
        if v is not None:
            nuevos[k] = v

    recalcular = False
    if "producto" in campos or "cantidad" in campos:
        producto = nuevos.get("producto")
        cantidad = nuevos.get("cantidad", 0)
        if producto and cantidad:
            precios = calcular_totales(producto, cantidad)
            nuevos["precio_unitario"] = PRECIOS_PRODUCTO.get(producto, 0.0)
            nuevos["subtotal"] = precios["subtotal"]
            nuevos["envio"] = precios["envio"]
            nuevos["total"] = precios["total"]
            recalcular = True
        else:
            nuevos["precio_unitario"] = 0.0
            nuevos["subtotal"] = 0.0
            nuevos["envio"] = 0.0
            nuevos["total"] = 0.0
            recalcular = True

    if "anticipo" in campos:
        anticipo = nuevos.get("anticipo", 0.0)
        nuevos["saldo"] = round(nuevos.get("total", 0.0) - anticipo, 2)
        recalcular = True

    if recalcular:
        anticipo = nuevos.get("anticipo", 0.0)
        nuevos["saldo"] = round(nuevos.get("total", 0.0) - anticipo, 2)

    set_clause = []
    params = []
    for col in [
        "estatus", "producto", "cantidad", "precio_unitario", "subtotal",
        "envio", "total", "anticipo", "saldo", "evento", "fecha_evento",
        "tipo_entrega", "municipio", "direccion", "color_toalla", "color_moño",
        "tipo_jaboncito", "color_jaboncito", "nombre_bebe", "tarjetita", "notas",
        "bot_activo"
    ]:
        if col in campos or (col in nuevos and nuevos[col] != pedido.get(col)):
            set_clause.append(f"{col} = ?")
            params.append(nuevos.get(col))

    if not set_clause:
        return pedido

    set_clause.append("updated_at = ?")
    params.append(datetime.now(ZONA_HORARIA_NEGOCIO).isoformat())
    params.append(pedido_id)
    sql = f"UPDATE pedidos SET {', '.join(set_clause)} WHERE id = ?"

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(sql, params)
    conn.commit()
    conn.close()

    return obtener_pedido(pedido_id)

def registrar_anticipo(pedido_id: int, monto: float) -> Dict:
    """Registra un anticipo, actualiza el campo anticipo y recalcula saldo."""
    pedido = obtener_pedido(pedido_id)
    if not pedido:
        raise ValueError(f"Pedido {pedido_id} no encontrado")

    nuevo_anticipo = pedido.get("anticipo", 0.0) + monto
    campos = {"anticipo": nuevo_anticipo}
    if nuevo_anticipo >= pedido.get("total", 0.0):
        campos["estatus"] = "Pagado"
    return actualizar_pedido(pedido_id, campos)

def generar_resumen(pedido_id: int) -> str:
    """Genera un resumen legible del pedido para mostrar al cliente o en el prompt."""
    pedido = obtener_pedido(pedido_id)
    if not pedido:
        return "No hay un pedido activo."

    lineas = []
    lineas.append(f"📋 Pedido: {pedido['folio']}")
    lineas.append(f"📅 Fecha: {pedido['created_at']}")
    lineas.append(f"👤 Cliente ID: {pedido['cliente_id']}")
    if pedido.get("producto"):
        lineas.append(f"🛍️ Producto: {pedido['producto']}")
    if pedido.get("cantidad"):
        lineas.append(f"🔢 Cantidad: {pedido['cantidad']}")
    if pedido.get("precio_unitario"):
        lineas.append(f"💰 Precio unitario: ${pedido['precio_unitario']:.2f}")
    if pedido.get("subtotal"):
        lineas.append(f"🧾 Subtotal: ${pedido['subtotal']:.2f}")
    if pedido.get("envio"):
        lineas.append(f"📦 Envío: ${pedido['envio']:.2f}")
    if pedido.get("total"):
        lineas.append(f"💵 Total: ${pedido['total']:.2f}")
    if pedido.get("anticipo"):
        lineas.append(f"✅ Anticipo: ${pedido['anticipo']:.2f}")
    if pedido.get("saldo"):
        lineas.append(f"💰 Saldo pendiente: ${pedido['saldo']:.2f}")
    if pedido.get("evento"):
        lineas.append(f"🎉 Evento: {pedido['evento']}")
    if pedido.get("fecha_evento"):
        lineas.append(f"📆 Fecha evento: {pedido['fecha_evento']}")
    if pedido.get("tipo_entrega"):
        lineas.append(f"🚚 Tipo entrega: {pedido['tipo_entrega']}")
    if pedido.get("municipio"):
        lineas.append(f"🏙️ Municipio: {pedido['municipio']}")
    if pedido.get("direccion"):
        lineas.append(f"📍 Dirección: {pedido['direccion']}")
    if pedido.get("color_toalla"):
        lineas.append(f"🧵 Color toalla: {pedido['color_toalla']}")
    if pedido.get("color_moño"):
        lineas.append(f"🎀 Color moño: {pedido['color_moño']}")
    if pedido.get("tipo_jaboncito"):
        lineas.append(f"🧼 Tipo jaboncito: {pedido['tipo_jaboncito']}")
    if pedido.get("color_jaboncito"):
        lineas.append(f"🎨 Color jaboncito: {pedido['color_jaboncito']}")
    if pedido.get("nombre_bebe"):
        lineas.append(f"👶 Nombre bebé: {pedido['nombre_bebe']}")
    if pedido.get("tarjetita"):
        lineas.append(f"💌 Tarjetita: {pedido['tarjetita']}")
    if pedido.get("notas"):
        lineas.append(f"📝 Notas: {pedido['notas']}")
    lineas.append(f"📊 Estatus: {pedido['estatus']}")
    if pedido.get("bot_activo"):
        lineas.append("🤖 Bot activo: Sí")
    else:
        lineas.append("🤖 Bot activo: No")

    return "\n".join(lineas)

def desactivar_bot(pedido_id: int) -> Dict:
    """Desactiva el bot para este pedido."""
    return actualizar_pedido(pedido_id, {"bot_activo": 0})

def activar_bot(pedido_id: int) -> Dict:
    """Activa el bot para este pedido."""
    return actualizar_pedido(pedido_id, {"bot_activo": 1})
