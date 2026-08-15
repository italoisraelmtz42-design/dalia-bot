"""
pedido_manager.py
Gestión de pedidos, borradores, items múltiples, chat history y modo de atención.
Corrige el fallo crítico de pérdida de items (elefantes + velitas) y el archivo faltante.
"""

import json
import uuid
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

from database import get_db_connection
from constantes import (
    EstadoPedido, ModoAtencion, OrigenEvento,
    ItemData, PagoData, EntregaData, PedidoData,
    logger_pedidos
)

logger = logging.getLogger("pedido_manager")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _folio() -> str:
    return f"PD-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


def _row_to_item(row) -> ItemData:
    return ItemData(
        id=row["id"],
        pedido_id=row["pedido_id"],
        producto=row["producto"],
        cantidad=row["cantidad"],
        precio_unitario=row["precio_unitario"],
        subtotal=row["subtotal"],
        color_toalla=row["color_toalla"],
        color_moño=row["color_moño"],
        tipo_jaboncito=row["tipo_jaboncito"],
        color_jaboncito=row["color_jaboncito"],
        nombre_bebe=row["nombre_bebe"],
        tarjetita=row["tarjetita"],
    )


def _row_to_pago(row) -> PagoData:
    return PagoData(
        id=row["id"],
        pedido_id=row["pedido_id"],
        tipo=row["tipo"],
        monto=row["monto"],
        metodo=row["metodo"],
        comprobante=row["comprobante"],
        confirmado=row["confirmado"],
        fecha=row["fecha"] if "fecha" in row.keys() else None,
    )


def _row_to_entrega(row) -> Optional[EntregaData]:
    if row is None:
        return None
    return EntregaData(
        pedido_id=row["pedido_id"],
        tipo_entrega=row["tipo_entrega"],
        municipio=row["municipio"],
        direccion=row["direccion"],
        fecha_entrega=row["fecha_entrega"],
        costo_envio=row["costo_envio"] or 0.0,
    )


# ---------------------------------------------------------------------------
# Chat history (usado por crm.py)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 🆘 Candado de emergencia (capa 2 -- comando por WhatsApp/Messenger)
# ---------------------------------------------------------------------------

def bot_pausado_globalmente() -> bool:
    """True si alguien con autorización mandó el comando de pausa y
    todavía no lo ha reactivado. Se revisa en cada mensaje entrante,
    para TODOS los clientes, sin importar el canal."""
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT valor FROM configuracion WHERE clave = 'bot_pausado'"
            ).fetchone()
        return bool(row and row["valor"] == "true")
    except Exception as e:
        logger.error(f"bot_pausado_globalmente: {e}")
        return False  # si falla la consulta, más seguro NO pausar por accidente


def set_bot_pausado(valor: bool) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO configuracion (clave, valor, fecha_actualizacion) "
            "VALUES ('bot_pausado', ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor, "
            "fecha_actualizacion = CURRENT_TIMESTAMP",
            ("true" if valor else "false",),
        )
        conn.commit()


def es_cliente_nuevo(telefono: str) -> bool:
    """True si este teléfono nunca le ha escrito antes al bot (cero
    mensajes en historial_chat). Se usa para forzar el saludo canónico +
    las 2 imágenes obligatorias en el primer contacto, en vez de dejarlo
    a que el modelo se acuerde de hacerlo (regla de la Base de
    Conocimiento que antes no tenía ningún respaldo en código)."""
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM historial_chat WHERE telefono = ?",
                (telefono,),
            ).fetchone()
        return (row["c"] if row else 0) == 0
    except Exception as e:
        logger.error(f"es_cliente_nuevo: {e}")
        return False


def chat_guardar_mensaje(telefono: str, mensaje: str, emisor: str):
    """emisor = 'usuario' | 'bot'"""
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO historial_chat (telefono, mensaje, emisor) VALUES (?, ?, ?)",
                (telefono, mensaje, emisor),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"chat_guardar_mensaje: {e}")


def chat_cargar_memoria(telefono: str, limite: int = 40) -> List[Dict]:
    """Devuelve lista de mensajes en formato OpenAI [{role, content}, ...]"""
    try:
        with get_db_connection() as conn:
            rows = conn.execute(
                """SELECT mensaje, emisor FROM historial_chat
                   WHERE telefono = ?
                   ORDER BY id DESC LIMIT ?""",
                (telefono, limite),
            ).fetchall()
        # invertir para orden cronológico
        mensajes = []
        for r in reversed(rows):
            role = "user" if r["emisor"] == "usuario" else "assistant"
            mensajes.append({"role": role, "content": r["mensaje"]})
        return mensajes
    except Exception as e:
        logger.error(f"chat_cargar_memoria: {e}")
        return []


def uso_registrar_openai(telefono: str):
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO uso_openai (telefono) VALUES (?)",
                (telefono,),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"uso_registrar_openai: {e}")


# ---------------------------------------------------------------------------
# Borradores (persistencia de pedido en construcción)
# ---------------------------------------------------------------------------

def guardar_borrador_pedido(telefono: str, datos: Dict[str, Any]):
    """Guarda o actualiza el borrador JSON del teléfono."""
    try:
        payload = json.dumps(datos, ensure_ascii=False, default=str)
        with get_db_connection() as conn:
            conn.execute(
                """INSERT INTO borradores_pedido (telefono, datos_json, fecha_actualizacion)
                   VALUES (?, ?, ?)
                   ON CONFLICT(telefono) DO UPDATE SET
                       datos_json = excluded.datos_json,
                       fecha_actualizacion = excluded.fecha_actualizacion""",
                (telefono, payload, _now()),
            )
            conn.commit()
        logger_pedidos.info(f"Borrador guardado para {telefono}")
    except Exception as e:
        logger.error(f"guardar_borrador_pedido: {e}")


def cargar_borrador_pedido(telefono: str) -> Optional[Dict[str, Any]]:
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT datos_json FROM borradores_pedido WHERE telefono = ?",
                (telefono,),
            ).fetchone()
        if row:
            return json.loads(row["datos_json"])
        return None
    except Exception as e:
        logger.error(f"cargar_borrador_pedido: {e}")
        return None


def borrar_borrador(telefono: str):
    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM borradores_pedido WHERE telefono = ?", (telefono,))
            conn.commit()
    except Exception as e:
        logger.error(f"borrar_borrador: {e}")


# ---------------------------------------------------------------------------
# Pedidos oficiales
# ---------------------------------------------------------------------------

def obtener_pedido_activo(telefono: str) -> Optional[int]:
    """Devuelve el id del pedido activo (no cancelado/entregado) más reciente."""
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                """SELECT id FROM pedidos
                   WHERE telefono = ?
                     AND estado NOT IN (?, ?)
                   ORDER BY id DESC LIMIT 1""",
                (telefono, EstadoPedido.CANCELADO.value, EstadoPedido.ENTREGADO.value),
            ).fetchone()
        return row["id"] if row else None
    except Exception as e:
        logger.error(f"obtener_pedido_activo: {e}")
        return None


def obtener_pedido(pedido_id: int) -> Optional[PedidoData]:
    try:
        with get_db_connection() as conn:
            p = conn.execute("SELECT * FROM pedidos WHERE id = ?", (pedido_id,)).fetchone()
            if not p:
                return None
            items = [
                _row_to_item(r)
                for r in conn.execute(
                    "SELECT * FROM pedido_items WHERE pedido_id = ?", (pedido_id,)
                ).fetchall()
            ]
            pagos = [
                _row_to_pago(r)
                for r in conn.execute(
                    "SELECT * FROM pagos WHERE pedido_id = ?", (pedido_id,)
                ).fetchall()
            ]
            entrega_row = conn.execute(
                "SELECT * FROM entregas WHERE pedido_id = ?", (pedido_id,)
            ).fetchone()
            entrega = _row_to_entrega(entrega_row)
        return PedidoData(
            id=p["id"],
            folio=p["folio"],
            cliente_id=p["cliente_id"] or 0,
            telefono=p["telefono"],
            estado=p["estado"],
            modo_atencion=p["modo_atencion"],
            es_urgente=p["es_urgente"],
            porcentaje_completitud=p["porcentaje_completitud"],
            fecha_creacion=p["fecha_creacion"],
            fecha_actualizacion=p["fecha_actualizacion"],
            items=items,
            pagos=pagos,
            entrega=entrega,
        )
    except Exception as e:
        logger.error(f"obtener_pedido: {e}")
        return None


def crear_pedido_desde_borrador(telefono: str, cliente_id: int, borrador: Dict) -> int:
    """
    Crea un pedido oficial a partir del borrador.
    Soporta tanto formato legacy (campos planos) como formato multi-item
    (borrador["items"] = lista de dicts).
    """
    folio = _folio()
    es_urgente = 1 if borrador.get("es_urgente") or borrador.get("urgente") else 0
    try:
        with get_db_connection() as conn:
            cur = conn.execute(
                """INSERT INTO pedidos
                   (folio, cliente_id, telefono, estado, modo_atencion, es_urgente, porcentaje_completitud)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    folio,
                    cliente_id,
                    telefono,
                    EstadoPedido.ANTICIPO_CONFIRMADO.value,
                    ModoAtencion.DALIA.value,
                    es_urgente,
                    100,
                ),
            )
            pedido_id = cur.lastrowid

            # ---- items ----
            items = borrador.get("items")
            if items and isinstance(items, list):
                for it in items:
                    _insert_item(conn, pedido_id, it)
            else:
                # formato plano legacy → un solo item
                _insert_item(conn, pedido_id, borrador)

            # ---- entrega ----
            tipo_entrega = borrador.get("tipo_entrega") or "local"
            conn.execute(
                """INSERT INTO entregas
                   (pedido_id, tipo_entrega, municipio, direccion, fecha_entrega, costo_envio)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    pedido_id,
                    tipo_entrega,
                    borrador.get("municipio"),
                    borrador.get("direccion"),
                    borrador.get("fecha_evento") or borrador.get("fecha_entrega"),
                    float(borrador.get("costo_envio") or 0),
                ),
            )

            # ---- pago anticipo ----
            monto = float(borrador.get("monto_anticipo") or 50)
            conn.execute(
                """INSERT INTO pagos
                   (pedido_id, tipo, monto, metodo, comprobante, confirmado)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (
                    pedido_id,
                    "anticipo",
                    monto,
                    borrador.get("metodo_pago") or "transferencia",
                    borrador.get("comprobante"),
                ),
            )

            conn.execute(
                """INSERT INTO pedido_eventos (pedido_id, evento, descripcion, origen)
                   VALUES (?, ?, ?, ?)""",
                (pedido_id, "CREADO", f"Pedido creado desde borrador. Folio {folio}", OrigenEvento.SISTEMA.value),
            )
            conn.commit()

        borrar_borrador(telefono)
        logger_pedidos.info(f"Pedido oficial {pedido_id} ({folio}) creado para {telefono}")
        return pedido_id
    except Exception as e:
        logger.error(f"crear_pedido_desde_borrador: {e}")
        raise


def _insert_item(conn, pedido_id: int, data: Dict):
    producto = data.get("producto") or "Producto sin nombre"
    cantidad = int(data.get("cantidad") or 1)
    precio = float(data.get("precio_unitario") or 0)
    subtotal = precio * cantidad
    conn.execute(
        """INSERT INTO pedido_items
           (pedido_id, producto, cantidad, precio_unitario, subtotal,
            color_toalla, color_moño, tipo_jaboncito, color_jaboncito,
            nombre_bebe, tarjetita)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pedido_id,
            producto,
            cantidad,
            precio,
            subtotal,
            data.get("color_toalla"),
            data.get("color_mono") or data.get("color_moño") or data.get("color_velita"),
            data.get("tipo_jaboncito"),
            data.get("color_jaboncito"),
            data.get("nombre_bebe"),
            data.get("tarjetita"),
        ),
    )


def confirmar_anticipo_pedido_existente(pedido_id: int, telefono: str, borrador: Dict):
    """Actualiza un pedido ya existente cuando llega un anticipo nuevo."""
    try:
        with get_db_connection() as conn:
            conn.execute(
                """UPDATE pedidos SET
                   estado = ?, modo_atencion = ?, fecha_actualizacion = ?
                   WHERE id = ?""",
                (
                    EstadoPedido.ANTICIPO_CONFIRMADO.value,
                    ModoAtencion.DALIA.value,
                    _now(),
                    pedido_id,
                ),
            )
            monto = float(borrador.get("monto_anticipo") or 50)
            conn.execute(
                """INSERT INTO pagos
                   (pedido_id, tipo, monto, metodo, comprobante, confirmado)
                   VALUES (?, ?, ?, ?, ?, 1)""",
                (
                    pedido_id,
                    "anticipo",
                    monto,
                    borrador.get("metodo_pago") or "transferencia",
                    borrador.get("comprobante"),
                ),
            )
            conn.execute(
                """INSERT INTO pedido_eventos (pedido_id, evento, descripcion, origen)
                   VALUES (?, ?, ?, ?)""",
                (pedido_id, "ANTICIPO_CONFIRMADO", "Anticipo recibido y confirmado", OrigenEvento.SISTEMA.value),
            )
            conn.commit()
        borrar_borrador(telefono)
        logger_pedidos.info(f"Pedido {pedido_id} actualizado con anticipo")
    except Exception as e:
        logger.error(f"confirmar_anticipo_pedido_existente: {e}")


# ---------------------------------------------------------------------------
# Cálculo determinístico de totales (no lo redacta el modelo)
# ---------------------------------------------------------------------------

def calcular_total(borrador: Optional[Dict] = None, pedido_id: Optional[int] = None) -> Dict[str, float]:
    """
    Devuelve dict con:
      subtotal_items, cargo_urgente, costo_envio, total, incompleto,
      productos_sin_precio
    Fuente de verdad numérica — el modelo NO debe inventar el total.

    🔧 CORREGIDO (bug real detectado en pruebas): antes, un item sin
    precio_unitario resuelto simplemente sumaba $0 al total, sin ninguna
    señal de que el total estaba incompleto -- un pedido de 30 ositos +
    40 velitas dio "$710" en vez de "$1070" porque los ositos no
    encontraron precio y nadie se enteró hasta que la clienta preguntó.
    Ahora se detectan los items marcados como "_precio_pendiente" y se
    exponen en "incompleto" / "productos_sin_precio" para que el sistema
    (prompt del modelo, gates de anticipo, etc.) nunca trate un total
    incompleto como si fuera el definitivo.
    """
    subtotal = 0.0
    cargo_urgente = 0.0
    costo_envio = 0.0
    incompleto = False
    productos_sin_precio = []

    if pedido_id:
        ped = obtener_pedido(pedido_id)
        if ped:
            subtotal = sum(float(it.subtotal or 0) for it in (ped.items or []))
            if ped.es_urgente:
                cargo_urgente = 50.0
            if ped.entrega and ped.entrega.costo_envio:
                costo_envio = float(ped.entrega.costo_envio)
            return {
                "subtotal_items": subtotal,
                "cargo_urgente": cargo_urgente,
                "costo_envio": costo_envio,
                "total": subtotal + cargo_urgente + costo_envio,
                "incompleto": False,
                "productos_sin_precio": [],
            }

    if borrador:
        items = borrador.get("items")
        if items and isinstance(items, list):
            for it in items:
                cant = float(it.get("cantidad") or 0)
                precio_raw = it.get("precio_unitario")
                if it.get("_precio_pendiente") or precio_raw in (None, 0, 0.0):
                    incompleto = True
                    productos_sin_precio.append(it.get("producto") or "?")
                precio = float(precio_raw or 0)
                subtotal += cant * precio
        else:
            cant = float(borrador.get("cantidad") or 0)
            precio = float(borrador.get("precio_unitario") or 0)
            subtotal = cant * precio
        if borrador.get("es_urgente") or borrador.get("urgente"):
            cargo_urgente = 50.0
        costo_envio = float(borrador.get("costo_envio") or 0)

    return {
        "subtotal_items": subtotal,
        "cargo_urgente": cargo_urgente,
        "incompleto": incompleto,
        "productos_sin_precio": productos_sin_precio,
        "costo_envio": costo_envio,
        "total": subtotal + cargo_urgente + costo_envio,
    }


# ---------------------------------------------------------------------------
# Resumen (texto legible para el prompt y para el cliente)
# ---------------------------------------------------------------------------

def generar_resumen(pedido_id: Optional[int] = None, borrador: Optional[Dict] = None) -> str:
    """
    Genera un resumen textual del pedido.
    Prioriza pedido oficial; si no hay, usa el borrador.
    Soporta múltiples items.
    """
    lineas = []

    if pedido_id:
        ped = obtener_pedido(pedido_id)
        if ped:
            lineas.append(f"Folio: {ped.folio} | Estado: {ped.estado}")
            if ped.es_urgente:
                lineas.append("⚠ PEDIDO URGENTE (+$50)")
            for it in ped.items:
                lineas.append(
                    f"• {it.cantidad} x {it.producto} @ ${it.precio_unitario:.2f} = ${it.subtotal:.2f}"
                )
                extras = []
                if it.color_toalla:
                    extras.append(f"toalla {it.color_toalla}")
                if it.color_moño:
                    extras.append(f"moño/listón {it.color_moño}")
                if it.tipo_jaboncito:
                    extras.append(f"jabón {it.tipo_jaboncito}" + (f" {it.color_jaboncito}" if it.color_jaboncito else ""))
                if extras:
                    lineas.append("  (" + ", ".join(extras) + ")")
            if ped.entrega:
                e = ped.entrega
                lineas.append(f"Entrega: {e.tipo_entrega}" + (f" – {e.direccion or e.municipio or ''}" if e.tipo_entrega != "local" else " en local"))
                if e.fecha_entrega:
                    lineas.append(f"Fecha entrega: {e.fecha_entrega}")
                if e.costo_envio:
                    lineas.append(f"Costo envío: ${e.costo_envio:.2f}")
            total = sum(it.subtotal for it in ped.items)
            if ped.entrega and ped.entrega.costo_envio:
                total += ped.entrega.costo_envio
            if ped.es_urgente:
                total += 50
            lineas.append(f"TOTAL: ${total:.2f} MXN")
            return "\n".join(lineas) if lineas else "Sin datos de pedido."

    # ---- borrador ----
    if not borrador:
        return "Sin pedido en construcción."

    lineas.append("--- Pedido en construcción (borrador) ---")
    items = borrador.get("items")
    if not items or not isinstance(items, list) or len(items) == 0:
        # migrar formato plano a lista temporal para mostrar
        if borrador.get("producto"):
            items = [{
                "producto": borrador.get("producto"),
                "cantidad": borrador.get("cantidad"),
                "precio_unitario": borrador.get("precio_unitario"),
                "color_toalla": borrador.get("color_toalla"),
                "color_mono": borrador.get("color_mono") or borrador.get("color_velita"),
                "tipo_jaboncito": borrador.get("tipo_jaboncito"),
                "color_jaboncito": borrador.get("color_jaboncito"),
            }]
        else:
            return "Sin pedido en construcción."

    for it in items:
        prod = it.get("producto") or "?"
        cant = int(it.get("cantidad") or 0)
        precio = float(it.get("precio_unitario") or 0)
        sub = cant * precio
        lineas.append(f"• {cant} x {prod} @ ${precio:.2f} = ${sub:.2f}")
        extras = []
        if it.get("color_toalla"):
            extras.append(f"toalla {it['color_toalla']}")
        mono = it.get("color_mono") or it.get("color_moño") or it.get("color_velita")
        if mono:
            extras.append(f"moño/listón {mono}")
        if it.get("tipo_jaboncito"):
            extras.append(f"jabón {it['tipo_jaboncito']}" + (f" {it.get('color_jaboncito')}" if it.get("color_jaboncito") else ""))
        if extras:
            lineas.append("  (" + ", ".join(extras) + ")")

    if borrador.get("tipo_entrega"):
        lineas.append(f"Entrega: {borrador.get('tipo_entrega')}")
    if borrador.get("fecha_evento") or borrador.get("fecha_entrega"):
        lineas.append(f"Fecha entrega: {borrador.get('fecha_evento') or borrador.get('fecha_entrega')}")

    tot = calcular_total(borrador=borrador)
    if tot["cargo_urgente"]:
        lineas.append(f"Cargo urgente: ${tot['cargo_urgente']:.2f}")
    if tot["costo_envio"]:
        lineas.append(f"Costo envío: ${tot['costo_envio']:.2f}")
    lineas.append(f"TOTAL: ${tot['total']:.2f} MXN")

    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Utilidades de sesión
# ---------------------------------------------------------------------------

def conversacion_silenciada(telefono: str) -> bool:
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM conversaciones_silenciadas WHERE telefono = ?",
                (telefono,),
            ).fetchone()
        return row is not None
    except Exception as e:
        logger.error(f"conversacion_silenciada: {e}")
        return False


def silenciar_conversacion(telefono: str) -> None:
    with get_db_connection() as conn:
        conn.execute(
            "INSERT INTO conversaciones_silenciadas (telefono, fecha_actualizacion) "
            "VALUES (?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(telefono) DO UPDATE SET fecha_actualizacion = CURRENT_TIMESTAMP",
            (telefono,),
        )
        conn.commit()


def reactivar_conversacion(telefono: str) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM conversaciones_silenciadas WHERE telefono = ?", (telefono,))
        # 🔧 CORREGIDO (hueco real detectado): el comando REACTIVAR solo
        # limpiaba conversaciones_silenciadas, pero el silencio también
        # puede venir de otra fuente -- el pedido oficial más reciente en
        # la tabla `pedidos` con modo_atencion=DALIA (esto pasa
        # automático en cuanto se confirma -- o se confirma por error,
        # como pasó con Blanca -- un anticipo). Sin esto, REACTIVAR no
        # hacía nada si el silencio venía de esa otra fuente.
        conn.execute(
            """UPDATE pedidos SET modo_atencion = ?
               WHERE telefono = ? AND id = (
                   SELECT id FROM pedidos WHERE telefono = ? ORDER BY id DESC LIMIT 1
               )""",
            (ModoAtencion.BOT.value, telefono, telefono),
        )
        conn.commit()


def obtener_modo_atencion(telefono: str) -> str:
    # 🔧 El silencio manual por conversación (ver conversacion_silenciada)
    # tiene prioridad sobre lo que diga la tabla pedidos -- así funciona
    # incluso en conversaciones que todavía no llegan a tener un pedido
    # oficial creado.
    if conversacion_silenciada(telefono):
        return ModoAtencion.DALIA.value
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                """SELECT modo_atencion FROM pedidos
                   WHERE telefono = ?
                   ORDER BY id DESC LIMIT 1""",
                (telefono,),
            ).fetchone()
        return row["modo_atencion"] if row else ModoAtencion.BOT.value
    except Exception:
        return ModoAtencion.BOT.value


def resetear_cliente_completo(telefono: str) -> bool:
    """Borra TODO lo relacionado a ese teléfono: historial de chat,
    borrador, y pedido(s) oficial(es) con sus items/pagos/entrega
    (en cascada). Destructivo e irreversible -- pensado para pruebas.

    🔧 CORREGIDO (bug real detectado en pruebas): antes solo borraba
    borradores_pedido e historial_chat -- la tabla `pedidos` (los
    pedidos YA CONFIRMADOS con anticipo) nunca se tocaba. Si un cliente
    de prueba había llegado a confirmar un anticipo (modo_atencion
    pasa a DALIA en ese pedido), el reset "limpiaba" todo excepto esa
    fila -- y como obtener_modo_atencion() lee el pedido más reciente en
    la tabla `pedidos`, el bot se quedaba en silencio para siempre con
    ese número después del reset, aunque pareciera haber funcionado (el
    mensaje de confirmación del reset sí se mandaba bien).
    """
    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM borradores_pedido WHERE telefono = ?", (telefono,))
            conn.execute("DELETE FROM historial_chat WHERE telefono = ?", (telefono,))
            # Con ON DELETE CASCADE + PRAGMA foreign_keys=ON (ver
            # database.py), esto también borra en cascada: pedido_items,
            # pagos, entregas, pedido_historial y pedido_eventos de ese
            # pedido -- incluye el modo_atencion=DALIA que dejaba al bot
            # en silencio.
            conn.execute("DELETE FROM pedidos WHERE telefono = ?", (telefono,))
            conn.commit()
        return True
    except Exception as e:
        logger.error(f"resetear_cliente_completo: {e}")
        return False


# ---------------------------------------------------------------------------
# Helpers para actualizar borrador con múltiples items desde el LLM
# ---------------------------------------------------------------------------

def agregar_item_a_borrador(telefono: str, item: Dict) -> Dict:
    """
    Añade (o actualiza) un item al borrador.
    Si ya existe un item con el mismo producto, actualiza cantidad/colores.
    """
    borrador = cargar_borrador_pedido(telefono) or {}
    items = borrador.get("items")
    if not isinstance(items, list):
        # migrar formato plano a lista
        items = []
        if borrador.get("producto"):
            items.append({k: v for k, v in borrador.items() if k != "items"})
            # limpiar campos planos para evitar confusión
            for k in list(borrador.keys()):
                if k not in ("items", "tipo_entrega", "direccion", "municipio",
                             "fecha_evento", "fecha_entrega", "es_urgente",
                             "urgente", "costo_envio", "anticipo_confirmado",
                             "monto_anticipo", "metodo_pago", "comprobante"):
                    borrador.pop(k, None)

    producto = (item.get("producto") or "").strip().lower()
    actualizado = False
    for existing in items:
        if (existing.get("producto") or "").strip().lower() == producto:
            existing.update({k: v for k, v in item.items() if v is not None})
            actualizado = True
            break
    if not actualizado:
        items.append(item)

    borrador["items"] = items
    guardar_borrador_pedido(telefono, borrador)
    return borrador
