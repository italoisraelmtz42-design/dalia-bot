import sqlite3
from pathlib import Path

# ==========================================
# CONFIGURACIÓN DE LA BASE DE DATOS
# ==========================================

BASE = Path(__file__).resolve().parent
DB_NAME = BASE / "dalia.db"


def get_connection():
    # timeout=10: si la BD está ocupada, espera hasta 10s antes de fallar
    # (en vez de tronar de inmediato con "database is locked").
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row

    # Activa las relaciones entre tablas
    conn.execute("PRAGMA foreign_keys = ON")

    # WAL permite que se pueda leer mientras alguien más escribe (antes,
    # con el modo por default, un cliente escribiendo podía bloquear a
    # otro que solo quería leer su historial). Con varios mensajes de
    # WhatsApp llegando casi al mismo tiempo (cada uno en su propio hilo),
    # esto evita errores intermitentes de "database is locked".
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")

    return conn


def inicializar_bd():
    conn = get_connection()
    cur = conn.cursor()

    # ==========================================
    # CLIENTES
    # ==========================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS clientes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telefono TEXT UNIQUE NOT NULL,
        nombre TEXT,
        fecha_alta TEXT,
        ultima_interaccion TEXT,
        estado TEXT DEFAULT 'ACTIVO'
    )
    """)

    # ==========================================
    # CONVERSACIONES
    # ==========================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS conversaciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        rol TEXT NOT NULL,
        mensaje TEXT NOT NULL,
        tipo TEXT DEFAULT 'texto',

        FOREIGN KEY(cliente_id)
            REFERENCES clientes(id)
            ON DELETE CASCADE
    )
    """)

    # ==========================================
    # PEDIDOS
    # ==========================================
    # NOTA: además de las columnas originales, se agregaron las que usa la
    # herramienta actualizar_pedido de OpenAI (evento, direccion,
    # color_toalla, color_mono, color_velita, datos_tarjeta,
    # anticipo_confirmado). "colores" y "forma_entrega" se dejan tal cual
    # estaban por compatibilidad; el mapeo entre nombres de campo del bot y
    # columnas de la BD vive en crm.py.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS pedidos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,

        folio TEXT UNIQUE,

        cliente_id INTEGER NOT NULL,

        producto TEXT,
        cantidad INTEGER,
        colores TEXT,

        evento TEXT,
        fecha_evento TEXT,

        forma_entrega TEXT,
        direccion TEXT,

        color_toalla TEXT,
        color_mono TEXT,
        color_velita TEXT,
        datos_tarjeta TEXT,

        anticipo REAL DEFAULT 0,
        anticipo_confirmado INTEGER DEFAULT 0,

        saldo REAL DEFAULT 0,

        estatus TEXT DEFAULT 'Cotizando',

        FOREIGN KEY(cliente_id)
            REFERENCES clientes(id)
            ON DELETE CASCADE
    )
    """)

    # Migración: si la tabla "pedidos" ya existía de antes con el schema
    # viejo (sin estas columnas), se agregan sin perder los datos que ya
    # hubiera. CREATE TABLE IF NOT EXISTS no modifica tablas existentes,
    # por eso hace falta este paso aparte.
    columnas_nuevas = {
        "evento": "TEXT",
        "direccion": "TEXT",
        "color_toalla": "TEXT",
        "color_mono": "TEXT",
        "color_velita": "TEXT",
        "datos_tarjeta": "TEXT",
        "anticipo_confirmado": "INTEGER DEFAULT 0",
    }
    cur.execute("PRAGMA table_info(pedidos)")
    columnas_existentes = {fila["name"] for fila in cur.fetchall()}
    for nombre_columna, tipo_sql in columnas_nuevas.items():
        if nombre_columna not in columnas_existentes:
            cur.execute(f"ALTER TABLE pedidos ADD COLUMN {nombre_columna} {tipo_sql}")

    # ==========================================
    # CONTADOR DE FOLIOS (Etapa 5)
    # Un contador por día para poder generar folios reales y consecutivos
    # tipo DAL-YYYYMMDD-NNNNNN sin que se repitan, incluso si dos pedidos
    # se confirman al mismo tiempo.
    # ==========================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS contador_folios (
        fecha TEXT PRIMARY KEY,
        ultimo INTEGER NOT NULL DEFAULT 0
    )
    """)

    # ==========================================
    # USO DE OPENAI (Etapa 2 / preparación Etapa 7)
    # Para poder ver más adelante, en un dashboard, cuánto se ha gastado
    # aproximadamente en OpenAI por cliente/día.
    # ==========================================
    cur.execute("""
    CREATE TABLE IF NOT EXISTS uso_openai (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER,
        fecha TEXT NOT NULL,
        modelo TEXT,
        tokens_entrada INTEGER,
        tokens_salida INTEGER,

        FOREIGN KEY(cliente_id)
            REFERENCES clientes(id)
            ON DELETE CASCADE
    )
    """)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    inicializar_bd()
    print("✅ Base de datos creada correctamente.")