# -*- coding: utf-8 -*-
"""
Base de datos de PRODUCCIÓN (pedidos confirmados) -- Recuerditos Dalia.

Esta base es completamente independiente de la del bot (dalia-bot). Aquí
solo entran pedidos que YA fueron confirmados con el cliente (la nota que
la vendedora vuelve a mandar para confirmar), nunca datos de la conversación
en vivo del bot. Por eso vive en su propio servicio y su propio archivo.

IMPORTANTE (Render): el disco de un Web Service normal es EFÍMERO -- se
borra en cada deploy/reinicio. Si este servicio no tiene un Persistent Disk
conectado, tanto esta base de datos como las fotos guardadas en
FOTOS_DIR se van a perder tarde o temprano. Ver README_DESPLIEGUE.md.
"""

import datetime
import json
import os
import re
import sqlite3
from contextlib import contextmanager

DB_PATH = os.getenv("PRODUCCION_DB_PATH", "produccion.db")


def normalizar_fecha_iso(texto_fecha):
    """Convierte 'fecha_entrega' (que el humano escribe como DD/MM/AAAA,
    el formato que usa todo el negocio) a AAAA-MM-DD para poder filtrar y
    ordenar correctamente por fecha.

    🔧 (21 ago 2026) Bug real detectado en pruebas: comparar fechas como
    texto plano (ej. "15/01/2027" >= "2026-08-21") NO funciona -- la
    comparación de strings no entiende fechas, compara caracter por
    caracter. Por eso se guarda esta columna aparte ya normalizada, y
    fecha_entrega se deja tal cual la escribió la persona (para mostrarla).
    Si el texto no se puede interpretar como fecha, regresa None -- el
    pedido simplemente no aparecerá en las vistas "hoy/mañana/semana/mes"
    hasta que se corrija la fecha, pero sí sigue apareciendo en "Todos".
    """
    if not texto_fecha:
        return None
    texto_fecha = texto_fecha.strip()
    formatos = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d")
    for fmt in formatos:
        try:
            return datetime.datetime.strptime(texto_fecha, fmt).date().isoformat()
        except ValueError:
            continue
    # Intento adicional: "15-01-2027" o "15.01.2027"
    m = re.match(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$", texto_fecha)
    if m:
        dia, mes, anio = (int(x) for x in m.groups())
        try:
            return datetime.date(anio, mes, dia).isoformat()
        except ValueError:
            return None
    return None


def _conectar():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def _cursor():
    conn = _conectar()
    try:
        cur = conn.cursor()
        yield cur
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pedidos_confirmados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha_captura TEXT NOT NULL,
                subido_por TEXT,
                cliente TEXT,
                telefono TEXT,
                municipio TEXT,
                fecha_entrega TEXT,
                fecha_entrega_iso TEXT,
                tipo_entrega TEXT,
                direccion TEXT,
                productos_json TEXT NOT NULL DEFAULT '[]',
                anticipo REAL NOT NULL DEFAULT 0,
                total REAL NOT NULL DEFAULT 0,
                notas TEXT,
                foto_archivo TEXT,
                estatus_fabricacion TEXT NOT NULL DEFAULT 'pendiente',
                estatus_entrega TEXT NOT NULL DEFAULT 'pendiente',
                fecha_entregado TEXT
            )
        """)
        # 🔧 (21 ago 2026) Migración suave: si la tabla ya existía de una
        # versión anterior sin esta columna, se agrega aquí sin perder datos.
        cur.execute("PRAGMA table_info(pedidos_confirmados)")
        columnas = {fila[1] for fila in cur.fetchall()}
        if "fecha_entrega_iso" not in columnas:
            cur.execute("ALTER TABLE pedidos_confirmados ADD COLUMN fecha_entrega_iso TEXT")
            cur.execute("SELECT id, fecha_entrega FROM pedidos_confirmados")
            for pid, fecha in cur.fetchall():
                cur.execute(
                    "UPDATE pedidos_confirmados SET fecha_entrega_iso=? WHERE id=?",
                    (normalizar_fecha_iso(fecha), pid),
                )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_fecha_entrega_iso ON pedidos_confirmados(fecha_entrega_iso)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_estatus_entrega ON pedidos_confirmados(estatus_entrega)")
    print(f"[DB producción] ✅ Lista en: {os.path.abspath(DB_PATH)}")


def guardar_pedido(data):
    """data: dict con los campos del formulario de confirmación.
    'productos' debe ser una lista de dicts -> se guarda como JSON."""
    productos_json = json.dumps(data.get("productos") or [], ensure_ascii=False)
    fecha_entrega = data.get("fecha_entrega")
    with _cursor() as cur:
        cur.execute("""
            INSERT INTO pedidos_confirmados
                (fecha_captura, subido_por, cliente, telefono, municipio,
                 fecha_entrega, fecha_entrega_iso, tipo_entrega, direccion, productos_json,
                 anticipo, total, notas, foto_archivo)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("fecha_captura"), data.get("subido_por"), data.get("cliente"),
            data.get("telefono"), data.get("municipio"), fecha_entrega,
            normalizar_fecha_iso(fecha_entrega), data.get("tipo_entrega"), data.get("direccion"), productos_json,
            float(data.get("anticipo") or 0), float(data.get("total") or 0),
            data.get("notas"), data.get("foto_archivo"),
        ))
        return cur.lastrowid


def actualizar_pedido(pedido_id, data):
    productos_json = json.dumps(data.get("productos") or [], ensure_ascii=False)
    fecha_entrega = data.get("fecha_entrega")
    with _cursor() as cur:
        cur.execute("""
            UPDATE pedidos_confirmados SET
                cliente=?, telefono=?, municipio=?, fecha_entrega=?, fecha_entrega_iso=?, tipo_entrega=?,
                direccion=?, productos_json=?, anticipo=?, total=?, notas=?
            WHERE id=?
        """, (
            data.get("cliente"), data.get("telefono"), data.get("municipio"),
            fecha_entrega, normalizar_fecha_iso(fecha_entrega), data.get("tipo_entrega"), data.get("direccion"),
            productos_json, float(data.get("anticipo") or 0), float(data.get("total") or 0),
            data.get("notas"), pedido_id,
        ))


def actualizar_estatus(pedido_id, campo, valor):
    assert campo in ("estatus_fabricacion", "estatus_entrega")
    with _cursor() as cur:
        if campo == "estatus_entrega" and valor == "entregado":
            cur.execute(
                f"UPDATE pedidos_confirmados SET {campo}=?, fecha_entregado=? WHERE id=?",
                (valor, _hoy_iso(), pedido_id),
            )
        else:
            cur.execute(f"UPDATE pedidos_confirmados SET {campo}=? WHERE id=?", (valor, pedido_id))


def eliminar_pedido(pedido_id):
    with _cursor() as cur:
        cur.execute("DELETE FROM pedidos_confirmados WHERE id=?", (pedido_id,))


def obtener_pedido(pedido_id):
    with _cursor() as cur:
        cur.execute("SELECT * FROM pedidos_confirmados WHERE id=?", (pedido_id,))
        row = cur.fetchone()
        return _fila_a_dict(row) if row else None


def listar_pedidos(fecha_entrega_desde=None, fecha_entrega_hasta=None, solo_pendientes_entrega=False,
                    incluir_sin_fecha=False):
    """fecha_entrega_desde/hasta deben venir en formato ISO (AAAA-MM-DD) --
    se comparan contra fecha_entrega_iso, NUNCA contra fecha_entrega (que
    está en DD/MM/AAAA, el formato que usa la persona)."""
    query = "SELECT * FROM pedidos_confirmados WHERE 1=1"
    params = []
    if fecha_entrega_desde or fecha_entrega_hasta:
        if incluir_sin_fecha:
            query += " AND (fecha_entrega_iso IS NULL"
        else:
            query += " AND (fecha_entrega_iso IS NOT NULL"
        if fecha_entrega_desde:
            query += " AND fecha_entrega_iso >= ?"
            params.append(fecha_entrega_desde)
        if fecha_entrega_hasta:
            query += " AND fecha_entrega_iso <= ?"
            params.append(fecha_entrega_hasta)
        query += ")"
    if solo_pendientes_entrega:
        query += " AND estatus_entrega != 'entregado'"
    query += " ORDER BY (fecha_entrega_iso IS NULL) ASC, fecha_entrega_iso ASC, id ASC"
    with _cursor() as cur:
        cur.execute(query, params)
        return [_fila_a_dict(r) for r in cur.fetchall()]


def listar_capturados_en_rango(fecha_captura_desde, fecha_captura_hasta):
    """Para la vista financiera: pedidos CAPTURADOS (no necesariamente
    entregados) en un rango de fechas -- ej. 'cuántos anticipos entraron esta semana'."""
    with _cursor() as cur:
        cur.execute("""
            SELECT * FROM pedidos_confirmados
            WHERE substr(fecha_captura, 1, 10) >= ? AND substr(fecha_captura, 1, 10) <= ?
            ORDER BY fecha_captura ASC
        """, (fecha_captura_desde, fecha_captura_hasta))
        return [_fila_a_dict(r) for r in cur.fetchall()]


def _fila_a_dict(row):
    d = dict(row)
    try:
        d["productos"] = json.loads(d.get("productos_json") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["productos"] = []
    d["saldo"] = round((d.get("total") or 0) - (d.get("anticipo") or 0), 2)
    return d


def _hoy_iso():
    # Se pasa desde afuera casi siempre (ver app.py); esta es solo una
    # red de seguridad si algún caller no lo manda.
    import datetime
    return datetime.date.today().isoformat()
