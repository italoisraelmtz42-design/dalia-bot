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


# 🔧 (23 ago 2026, pedido de Israel: "hoy es 22 de agosto, en la app
# aparece que es 23") Render corre el servidor en UTC. Monterrey/Apodaca
# van 6 horas atrás y, desde la reforma de 2022, ya NO cambian de horario
# (no aplica horario de verano ahí), así que el ajuste es un número fijo
# -- no depende de una base de datos de zonas horarias que quizás no esté
# instalada en el servidor. Sin esto, entre las 6pm y la medianoche hora
# de Monterrey, el servidor (en UTC) ya "cree" que es el día siguiente,
# y toda fecha que se guarde o se compare en ese rato sale adelantada un
# día. TODO el código (aquí y en app.py) debe usar ahora_negocio()/
# hoy_negocio() en vez de datetime.datetime.now()/datetime.date.today()
# directo, para que "hoy" signifique siempre lo mismo en toda la app.
ZONA_NEGOCIO = datetime.timezone(datetime.timedelta(hours=-6), name="America/Monterrey")


def ahora_negocio():
    return datetime.datetime.now(ZONA_NEGOCIO)


def hoy_negocio():
    return ahora_negocio().date()


MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}


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

    # 🔧 (24 ago 2026, pedido de Israel: "si las notas en la fecha no
    # tienen año... que el programa entienda que son del año en curso")
    # Antes, si la nota no traía año (ej. "23 julio", "23/07"), esta
    # función regresaba None -- inofensivo por sí solo (el pedido
    # simplemente no aparecía en Hoy/Mañana/Semana/Mes hasta corregirse a
    # mano, ver docstring arriba). El problema real que Israel reportó
    # venía de otro lado: el prompt de la IA le pedía "deducir" el año
    # sin darle la fecha de hoy como referencia, y a veces terminaba
    # alucinando un año viejo sin sentido (2020, 2023) en vez de dejarlo
    # en blanco -- y ESE año inventado sí pasaba esta función sin
    # problema (es un DD/MM/AAAA válido, nada más que incorrecto). Ya se
    # corrigió el prompt para que use el año actual como año por defecto
    # (ver PROMPT_EXTRACCION en app.py). Este bloque de aquí es la red de
    # seguridad complementaria -- mismo principio que el resto del
    # código: nunca confiar en un solo lugar para que el dato salga bien.
    # Si de todos modos llega texto sin año (la IA no siguió la
    # instrucción, o alguien lo escribió a mano así en Editar), se
    # completa aquí con el año de HOY -- ahora_negocio()/hoy_negocio(),
    # NO datetime.now() directo, para no toparse otra vez con el bug de
    # zona horaria ya corregido -- en vez de fallar en silencio.
    anio_actual = hoy_negocio().year

    # "23/07", "23-07", "23.07" (día/mes, sin año)
    m = re.match(r"^(\d{1,2})[./-](\d{1,2})$", texto_fecha)
    if m:
        dia, mes = (int(x) for x in m.groups())
        try:
            return datetime.date(anio_actual, mes, dia).isoformat()
        except ValueError:
            return None

    # "23 de julio", "23 julio" (nombre de mes en español, sin año)
    m = re.match(r"^(\d{1,2})\s*(?:de\s+)?([a-zA-Z]+)$", texto_fecha)
    if m:
        dia = int(m.group(1))
        mes = MESES_ES.get(m.group(2).lower())
        if mes:
            try:
                return datetime.date(anio_actual, mes, dia).isoformat()
            except ValueError:
                return None

    return None


def _conectar():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # 🔧 (23 ago 2026) journal_mode=WAL necesita archivos auxiliares con
    # memoria compartida mapeada (mmap), y el Disk persistente de Render
    # es almacenamiento en red -- eso no siempre lo soporta bien, y
    # cuando falla, TODAS las operaciones truenan con "disk I/O error"
    # (esto es justo lo que le pasó hoy a dalia-bot, que usa el mismo
    # patrón). DELETE es el modo clásico de SQLite, sin memoria
    # compartida -- más confiable sobre disco de red.
    conn.execute("PRAGMA journal_mode=DELETE;")
    conn.execute("PRAGMA busy_timeout=5000;")
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
        # 🔧 (23 ago 2026, pedido de Israel: "las notas que tengan información
        # por confirmar o error que detecte la IA hay que marcarlo como error,
        # que se puedan subir, pero marcadas para que llamen la atención y se
        # corrija ya dentro de la app") Antes, una nota dudosa DETENÍA la
        # subida hasta corregirla ahí mismo. Ahora se guarda de todos modos
        # -- con lo que se haya podido leer -- y se marca con estas 2 columnas
        # para poder mostrar el aviso y corregirla después, sin bloquear el
        # resto de la tanda.
        if "necesita_revision" not in columnas:
            cur.execute("ALTER TABLE pedidos_confirmados ADD COLUMN necesita_revision INTEGER NOT NULL DEFAULT 0")
        if "motivo_revision" not in columnas:
            cur.execute("ALTER TABLE pedidos_confirmados ADD COLUMN motivo_revision TEXT")
        # 🔧 (24 ago 2026, pedido de Israel: comisiones de Dalia/Diana/Karo
        # por folio, no por quién sube la nota) El folio es lo que la IA
        # lee de la nota y lo que de verdad dice a quién le corresponde la
        # comisión de esa venta -- ver vendedora_por_folio() en app.py.
        if "folio" not in columnas:
            cur.execute("ALTER TABLE pedidos_confirmados ADD COLUMN folio TEXT")
        # 🔧 (29 ago 2026, pedido de Israel: captura directa de notas desde
        # la app, sin pasar por foto+IA) Para distinguir de un vistazo cómo
        # entró cada nota -- 'foto' (subida + leída por IA, como siempre) o
        # 'directo' (alguien la tecleó directo en /capturar). Todo lo que
        # ya existía se marca 'foto' porque así es como se guardó.
        if "origen" not in columnas:
            cur.execute("ALTER TABLE pedidos_confirmados ADD COLUMN origen TEXT NOT NULL DEFAULT 'foto'")
        # 🔧 (29 ago 2026, pedido de Israel: precio de envío automático por
        # municipio/DHL) El costo de envío se guarda aparte del total -- el
        # servidor SIEMPRE lo recalcula a partir de la tabla de precios
        # (ver PRECIOS_ENVIO_MUNICIPIO / PRECIO_ENVIO_DHL en app.py), nunca
        # confía en un monto que venga del formulario. Sirve para mostrarlo
        # como su propio renglón en la nota impresa.
        if "envio_costo" not in columnas:
            cur.execute("ALTER TABLE pedidos_confirmados ADD COLUMN envio_costo REAL NOT NULL DEFAULT 0")
        # 🔧 (29 ago 2026, pedido de Israel: recuadro de "NOTAS IMPORTANTES"
        # en la nota impresa, aparte del campo "notas" de siempre) Campo
        # libre para instrucciones de producción (ej. "los jaboncitos
        # mitad amarillos, mitad verdes") que se ve en su propio recuadro
        # al final de la nota, en vez de mezclarse con las notas generales.
        if "notas_importantes" not in columnas:
            cur.execute("ALTER TABLE pedidos_confirmados ADD COLUMN notas_importantes TEXT")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_fecha_entrega_iso ON pedidos_confirmados(fecha_entrega_iso)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_estatus_entrega ON pedidos_confirmados(estatus_entrega)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_necesita_revision ON pedidos_confirmados(necesita_revision)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_folio ON pedidos_confirmados(folio)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pedidos_fecha_captura ON pedidos_confirmados(fecha_captura)")

        # 🔧 (23 ago 2026, pedido de Israel: control de materia prima)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS materia_prima (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                cantidad REAL NOT NULL DEFAULT 0,
                unidad TEXT NOT NULL DEFAULT 'pza',
                actualizado_en TEXT NOT NULL
            )
        """)
    print(f"[DB producción] ✅ Lista en: {os.path.abspath(DB_PATH)}")


def guardar_pedido(data):
    """data: dict con los campos del formulario de confirmación.
    'productos' debe ser una lista de dicts -> se guarda como JSON.
    'foto_archivo' puede venir None (🔧 29 ago 2026: notas capturadas
    directo en /capturar, sin foto de por medio) -- 'origen' distingue
    'foto' (default, como siempre) de 'directo'. 'envio_costo' ya viene
    calculado por app.py (nunca se recalcula aquí)."""
    productos_json = json.dumps(data.get("productos") or [], ensure_ascii=False)
    fecha_entrega = data.get("fecha_entrega")
    with _cursor() as cur:
        cur.execute("""
            INSERT INTO pedidos_confirmados
                (fecha_captura, subido_por, cliente, telefono, municipio,
                 fecha_entrega, fecha_entrega_iso, tipo_entrega, direccion, productos_json,
                 anticipo, total, notas, foto_archivo, necesita_revision, motivo_revision, folio, origen,
                 envio_costo, notas_importantes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data.get("fecha_captura"), data.get("subido_por"), data.get("cliente"),
            data.get("telefono"), data.get("municipio"), fecha_entrega,
            normalizar_fecha_iso(fecha_entrega), data.get("tipo_entrega"), data.get("direccion"), productos_json,
            float(data.get("anticipo") or 0), float(data.get("total") or 0),
            data.get("notas"), data.get("foto_archivo"),
            1 if data.get("necesita_revision") else 0, data.get("motivo_revision"), data.get("folio"),
            data.get("origen") or "foto", float(data.get("envio_costo") or 0), data.get("notas_importantes"),
        ))
        return cur.lastrowid


def actualizar_pedido(pedido_id, data):
    """🔧 (23 ago 2026) Editar un pedido -- desde la app, ya con calma -- es
    justo la forma en que se corrige una nota marcada con error. Por eso,
    cada vez que se guarda una edición, se apaga la bandera de
    necesita_revision: se asume que quien editó ya dejó los datos bien.

    🔧 (29 ago 2026, pedido de Israel: "no lo implementes en las fotos --
    que las notas subidas por foto sigan funcionando como hasta hoy")
    A propósito NO toca 'envio_costo' -- ese campo solo lo calcula
    /capturar al CREAR la nota (ver app.py); editar un pedido nunca lo
    recalcula ni lo borra, se queda tal cual estaba."""
    productos_json = json.dumps(data.get("productos") or [], ensure_ascii=False)
    fecha_entrega = data.get("fecha_entrega")
    with _cursor() as cur:
        cur.execute("""
            UPDATE pedidos_confirmados SET
                cliente=?, telefono=?, municipio=?, fecha_entrega=?, fecha_entrega_iso=?, tipo_entrega=?,
                direccion=?, productos_json=?, anticipo=?, total=?, notas=?, folio=?, notas_importantes=?,
                necesita_revision=0, motivo_revision=NULL
            WHERE id=?
        """, (
            data.get("cliente"), data.get("telefono"), data.get("municipio"),
            fecha_entrega, normalizar_fecha_iso(fecha_entrega), data.get("tipo_entrega"), data.get("direccion"),
            productos_json, float(data.get("anticipo") or 0), float(data.get("total") or 0),
            data.get("notas"), data.get("folio"), data.get("notas_importantes"), pedido_id,
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


def eliminar_pedidos(ids):
    """Elimina varios pedidos a la vez (ver eliminar_pedido para uno solo).

    ids: lista/iterable de valores que representen ids (pueden venir como
    strings del formulario). Ignora silenciosamente cualquier valor que no
    sea un entero válido -- así una casilla mal formada nunca tumba el borrado
    de las demás.

    Devuelve cuántos pedidos se borraron realmente.
    """
    ids_validos = []
    for i in ids or []:
        try:
            ids_validos.append(int(str(i).strip()))
        except (TypeError, ValueError):
            continue
    if not ids_validos:
        return 0
    with _cursor() as cur:
        marcador = ",".join("?" * len(ids_validos))
        cur.execute(f"DELETE FROM pedidos_confirmados WHERE id IN ({marcador})", ids_validos)
        return cur.rowcount


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


def buscar_pedidos_por_cliente(texto):
    """🔧 (23 ago 2026, pedido de Israel: "habilita un buscador de cliente
    por nombre para las notas") Busca en TODOS los pedidos (sin límite de
    fecha), coincidencia parcial y sin importar mayúsculas/acentos básicos,
    más reciente primero -- para encontrar una nota vieja de un cliente sin
    tener que ir período por período."""
    texto = (texto or "").strip()
    if not texto:
        return []
    with _cursor() as cur:
        cur.execute("""
            SELECT * FROM pedidos_confirmados
            WHERE lower(COALESCE(cliente, '')) LIKE ?
            ORDER BY fecha_captura DESC
        """, (f"%{texto.lower()}%",))
        return [_fila_a_dict(r) for r in cur.fetchall()]


def listar_pedidos_recientes(limite=60):
    """🔧 (29 ago 2026, pedido de Israel: "quiero saber que se subió una
    nota hoy de un pedido que se entregará en noviembre") A diferencia de
    listar_pedidos() (que filtra/ordena por FECHA DE ENTREGA), esto
    ordena por FECHA DE CAPTURA -- la nota más recién guardada primero,
    sin importar qué tan lejos esté su fecha de entrega. Es lo que
    alimenta la pestaña "Recientes" del dashboard, para notar rápido si
    a alguien se le están acumulando notas sin capturar."""
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM pedidos_confirmados ORDER BY fecha_captura DESC, id DESC LIMIT ?",
            (limite,),
        )
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


def listar_capturados_por_vendedor_en_rango(vendedor, fecha_captura_desde, fecha_captura_hasta):
    """🔧 (23 ago 2026, pedido de Israel: comisiones de Diana -- $1 por
    producto vendido) Igual que listar_capturados_en_rango, pero solo los
    pedidos subidos por 'vendedor' (comparación sin importar mayúsculas ni
    espacios de sobra). Ahora 'subido_por' se llena automáticamente con
    quien inició sesión (ver app.py) -- ya no es un campo de texto libre
    donde alguien pudo escribir "diana", "Diana " o con una falta -- así
    que este filtro es confiable para calcular un pago real.

    ⚠️ SUPERADA (24 ago 2026, pedido de Israel): esto asumía que quien SUBE
    la nota es de quien es la venta -- pero Israel aclaró que no siempre es
    así (cualquiera puede subir la nota de otra persona). La comisión ahora
    se calcula por el FOLIO de la nota (ver vendedora_por_folio() en
    app.py), no por 'subido_por'. Esta función ya NO se usa para
    comisiones -- se deja aquí sin borrar por si sirve para otra cosa más
    adelante (ej. saber quién sube más notas, sin relación a comisiones)."""
    with _cursor() as cur:
        cur.execute("""
            SELECT * FROM pedidos_confirmados
            WHERE substr(fecha_captura, 1, 10) >= ? AND substr(fecha_captura, 1, 10) <= ?
              AND lower(trim(COALESCE(subido_por, ''))) = lower(trim(?))
            ORDER BY fecha_captura ASC
        """, (fecha_captura_desde, fecha_captura_hasta, vendedor))
        return [_fila_a_dict(r) for r in cur.fetchall()]


# ----------------------------------------------------------------------
# Inventario de materia prima
# ----------------------------------------------------------------------
def listar_materia_prima():
    with _cursor() as cur:
        cur.execute("SELECT * FROM materia_prima ORDER BY nombre COLLATE NOCASE ASC")
        return [dict(r) for r in cur.fetchall()]


def obtener_materia_prima(item_id):
    with _cursor() as cur:
        cur.execute("SELECT * FROM materia_prima WHERE id=?", (item_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def crear_materia_prima(nombre, cantidad, unidad):
    with _cursor() as cur:
        cur.execute(
            "INSERT INTO materia_prima (nombre, cantidad, unidad, actualizado_en) VALUES (?, ?, ?, ?)",
            (nombre, float(cantidad or 0), unidad or "pza", ahora_negocio().isoformat(timespec="seconds")),
        )
        return cur.lastrowid


def actualizar_materia_prima(item_id, nombre, cantidad, unidad):
    with _cursor() as cur:
        cur.execute(
            "UPDATE materia_prima SET nombre=?, cantidad=?, unidad=?, actualizado_en=? WHERE id=?",
            (nombre, float(cantidad or 0), unidad or "pza",
             ahora_negocio().isoformat(timespec="seconds"), item_id),
        )


def eliminar_materia_prima(item_id):
    with _cursor() as cur:
        cur.execute("DELETE FROM materia_prima WHERE id=?", (item_id,))


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
    return hoy_negocio().isoformat()
