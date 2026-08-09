import sqlite3
import datetime
import json
import logging
from typing import List, Dict, Optional, Any

from database import get_db_connection
from constantes import (
    logger_pedidos, EstadoPedido, ModoAtencion, OrigenEvento,
    COLUMNAS_PERMITIDAS_PEDIDOS, PESOS_COMPLETITUD, TRANSICIONES_VALIDAS,
    PedidoData, ItemData, PagoData, EntregaData, campos_requeridos_para
)
from validators import validar_estado, validar_transicion

# ==============================================================================
# 🟢 [DEBUG] FOQUITO VERDE DE DEPURACIÓN
# ==============================================================================
print("🟢 [DEBUG] Cargando nueva versión de pedido_manager.py con obtener_pedido()")

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


def obtener_modo_atencion(telefono: str) -> str:
    """Devuelve el modo de atención vigente para este cliente: 'BOT' si el
    bot debe seguir respondiendo automáticamente, o 'DALIA'/'SUSPENDIDO' si
    un humano ya tomó el control (esto pasa automáticamente en cuanto se
    confirma el anticipo de un pedido) y el bot debe quedarse callado.

    A diferencia de obtener_pedido_activo() (que a propósito EXCLUYE los
    pedidos en modo DALIA para no tratarlos como "activos" del bot), esta
    función sí necesita poder ver ese estado, por eso mira directo el
    pedido más reciente del cliente sin filtrar por modo_atencion.
    """
    with get_db_connection() as conn:
        row = conn.execute("""
            SELECT modo_atencion FROM pedidos
            WHERE telefono = ?
            ORDER BY id DESC LIMIT 1
        """, (telefono,)).fetchone()
        return row["modo_atencion"] if row else ModoAtencion.BOT.value

def reactivar_modo_bot(telefono: str) -> bool:
    """Regresa a modo BOT todos los pedidos de este teléfono que no lo
    estuvieran ya (código de reactivación: 2 emojis de osito 🧸🧸, ver
    app.py). Devuelve True si de verdad cambió algo, False si ya estaba
    en modo BOT o si no tiene ningún pedido."""
    try:
        with get_db_connection() as conn:
            cur = conn.execute(
                "UPDATE pedidos SET modo_atencion = ?, fecha_actualizacion = CURRENT_TIMESTAMP "
                "WHERE telefono = ? AND modo_atencion != ?",
                (ModoAtencion.BOT.value, telefono, ModoAtencion.BOT.value)
            )
            conn.commit()
            cambiado = cur.rowcount > 0

        # 🔧 CORREGIDO: si el borrador persistente todavía traía
        # anticipo_confirmado=True de un pedido anterior (el que ya se
        # había pagado y por eso quedó en modo DALIA), sin este paso el
        # bot se volvía a silenciar solo apenas el modelo llamaba a
        # actualizar_pedido de nuevo por CUALQUIER motivo -- aunque el
        # cliente no hubiera mandado ningún comprobante nuevo. Se limpia
        # la bandera de anticipo (y los datos del pago viejo) para que la
        # reactivación sea de verdad completa.
        borrador = cargar_borrador_pedido(telefono)
        if borrador and borrador.get('anticipo_confirmado'):
            borrador['anticipo_confirmado'] = False
            borrador['monto_anticipo'] = None
            borrador['metodo_pago'] = None
            borrador['comprobante'] = None
            guardar_borrador_pedido(telefono, borrador)
            logger_pedidos.info(f"🧹 Bandera de anticipo limpiada del borrador de {telefono} al reactivar")

        if cambiado:
            logger_pedidos.info(f"🧸🧸 {telefono} reactivado a modo BOT por código de reactivación")
        return cambiado
    except Exception as e:
        logger_pedidos.error(f"[reactivar_modo_bot] Error: {e}")
        return False


def confirmar_anticipo_pedido_existente(pedido_id: int, telefono: str, borrador: dict):
    """Se usa cuando YA existía un pedido oficial para este teléfono (de un
    contacto o prueba anterior) y se confirma un anticipo nuevo. En vez de
    ignorarlo (que era el bug: el pedido nunca pasaba a modo DALIA y el
    bot seguía respondiendo para siempre), se actualiza ese pedido: pasa a
    modo DALIA y se registra el pago nuevo.
    """
    try:
        with get_db_connection() as conn:
            conn.execute(
                "UPDATE pedidos SET modo_atencion = ?, fecha_actualizacion = CURRENT_TIMESTAMP WHERE id = ?",
                (ModoAtencion.DALIA.value, pedido_id)
            )
            if borrador.get('anticipo_confirmado'):
                conn.execute(
                    """INSERT INTO pagos (pedido_id, tipo, monto, metodo, comprobante, confirmado)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        pedido_id,
                        'ANTICIPO',
                        borrador.get('monto_anticipo') or 0.0,
                        borrador.get('metodo_pago') or 'no especificado',
                        borrador.get('comprobante'),
                        1
                    )
                )
            conn.commit()
            logger_pedidos.info(f"🔁 Pedido existente {pedido_id} pasó a modo DALIA con nuevo anticipo registrado")
        eliminar_borrador_pedido(telefono)
    except Exception as e:
        logger_pedidos.error(f"[confirmar_anticipo_pedido_existente] Error: {e}")


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
        # 🆕 El pedido oficial SOLO se crea cuando ya se confirmó el
        # anticipo (ver crm.sincronizar_pedido: debe_crear depende de
        # anticipo_confirmado). Por eso nace directo en modo DALIA: el
        # bot ya hizo su parte (tomar el pedido y cobrar el anticipo) y
        # a partir de aquí Dalia toma el control de la conversación.
        sql_pedido = """
            INSERT INTO pedidos (folio, cliente_id, telefono, estado, modo_atencion)
            VALUES (?, ?, ?, ?, ?)
        """
        cursor.execute(sql_pedido, (
            folio, cliente_id, telefono,
            EstadoPedido.BORRADOR.value,
            ModoAtencion.DALIA.value
        ))
        pedido_id = cursor.lastrowid
        if not pedido_id:
            raise Exception("No se pudo obtener el ID del pedido")

        # Insertar items (si existen)
        if borrador.get('producto') and borrador.get('cantidad'):
            # 🔧 CORREGIDO: antes leía 'precio_unitario' de un campo que
            # nunca existía en el borrador (siempre quedaba en $0.00).
            # Ahora el modelo lo captura vía actualizar_pedido cuando le
            # informa el precio al cliente (ver TOOLS en app.py).
            precio = borrador.get('precio_unitario') or 0.0
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
                # 🔧 CORREGIDO: el borrador guarda 'color_mono' (sin tilde,
                # así está definido en TOOLS/pedido_vacio de app.py). Antes
                # se leía 'color_moño' (con tilde), que nunca existía, y el
                # color del moño se perdía silenciosamente al confirmar.
                borrador.get('color_mono'),
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
                # 🔧 CORREGIDO: el borrador guarda 'fecha_evento' (así está
                # en TOOLS de app.py). Antes se leía 'fecha_entrega', que
                # nunca existía, y la fecha se perdía al confirmar.
                borrador.get('fecha_evento'),
                borrador.get('costo_envio', 0.0)
            ))

        # Insertar pago (si existe anticipo)
        # 🔧 CORREGIDO: antes exigía 'metodo_pago', un campo que el modelo
        # nunca podía llenar (no existía en TOOLS), así que esta condición
        # nunca era verdadera y NINGÚN anticipo se guardaba en la tabla
        # `pagos`, aunque el bot le confirmara al cliente que lo recibió.
        # Ahora solo exige que el anticipo esté confirmado; si no hay
        # método de pago explícito, se guarda como "no especificado" en
        # vez de perder el registro del pago por completo.
        if borrador.get('anticipo_confirmado'):
            sql_pago = """
                INSERT INTO pagos (pedido_id, tipo, monto, metodo, comprobante, confirmado)
                VALUES (?, ?, ?, ?, ?, ?)
            """
            cursor.execute(sql_pago, (
                pedido_id,
                'ANTICIPO',
                borrador.get('monto_anticipo') or 0.0,
                borrador.get('metodo_pago') or 'no especificado',
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

def generar_resumen(pedido_id: Optional[int] = None, borrador: Optional[dict] = None) -> str:
    """
    Genera el texto de resumen del pedido que se inserta en el system prompt.
    - Si hay pedido_id (pedido oficial confirmado), resume desde la tabla `pedidos`.
    - Si no, resume desde el borrador (dict con los campos que el cliente ha
      ido confirmando, aunque el pedido oficial todavía no exista).
    - Si no hay ni pedido oficial ni borrador con datos, indica que no hay
      pedido activo.
    """
    if pedido_id:
        pedido = obtener_pedido(pedido_id)
        if pedido:
            partes = [f"Pedido oficial confirmado (folio {pedido.folio}, estado {pedido.estado})."]
            for item in pedido.items:
                partes.append(
                    f"- {item.cantidad} x {item.producto} "
                    f"(toalla {item.color_toalla or 'sin especificar'}, "
                    f"moño {item.color_moño or 'sin especificar'}, "
                    f"jaboncito {item.tipo_jaboncito or 'sin especificar'} "
                    f"color {item.color_jaboncito or 'sin especificar'})"
                )
                if item.nombre_bebe:
                    partes.append(f"  Nombre para personalizar: {item.nombre_bebe}")
                if item.tarjetita:
                    partes.append(f"  Tarjetita: {item.tarjetita}")
            if pedido.entrega:
                partes.append(
                    f"Entrega: {pedido.entrega.tipo_entrega}"
                    + (f", municipio {pedido.entrega.municipio}" if pedido.entrega.municipio else "")
                    + (f", dirección {pedido.entrega.direccion}" if pedido.entrega.direccion else "")
                    + (f", fecha {pedido.entrega.fecha_entrega}" if pedido.entrega.fecha_entrega else "")
                )
            if pedido.pagos:
                partes.append("Anticipo/pago ya confirmado por el cliente.")
            return "\n".join(partes)

    if borrador and any(v not in (None, "", []) for v in borrador.values()):
        etiquetas = {
            "producto": "Producto",
            "cantidad": "Cantidad",
            "evento": "Evento",
            "fecha_evento": "Fecha de entrega/evento",
            "color_toalla": "Color de toalla",
            "color_mono": "Color de moño",
            "color_velita": "Color de velita",
            "tipo_entrega": "Tipo de entrega",
            "direccion": "Dirección",
            "municipio": "Municipio",
            "anticipo_confirmado": "Anticipo confirmado",
            "tipo_jaboncito": "Tipo de jaboncito",
            "color_jaboncito": "Color de jaboncito",
            "nombre_bebe": "Nombre del bebé",
            "tarjetita": "Tarjetita",
            "notas": "Notas",
        }
        lineas = ["Borrador en progreso (aún no confirmado formalmente):"]
        for campo, etiqueta in etiquetas.items():
            valor = borrador.get(campo)
            if valor not in (None, "", []):
                lineas.append(f"- {etiqueta}: {valor}")

        # 🔧 CORREGIDO (Observación 5/6 de la auditoría): antes se listaban
        # como "faltantes" los 16 campos del schema completo, sin importar
        # si aplicaban al producto elegido (por eso el bot preguntaba por
        # la velita en un pedido "sin jabón"). Ahora el backend decide, según
        # el producto, cuáles campos son realmente relevantes.
        campos_relevantes = campos_requeridos_para(borrador.get("producto"))
        faltantes = [
            etiquetas[c] for c in campos_relevantes
            if c in etiquetas and borrador.get(c) in (None, "", [])
        ]
        if faltantes:
            lineas.append(f"Datos aún faltantes (solo los que aplican a este producto): {', '.join(faltantes)}")
        return "\n".join(lineas)

    return "Sin pedido activo."


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
