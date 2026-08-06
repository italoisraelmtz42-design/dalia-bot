import sqlite3
from pathlib import Path

# ==========================================
# CONFIGURACIÓN DE LA BASE DE DATOS
# ==========================================

BASE = Path(__file__).resolve().parent
DB_NAME = BASE / "dalia.db"


def get_connection():
    conn = sqlite3.connect(DB_NAME, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def inicializar_bd():
    conn = get_connection()
    cur = conn.cursor()

    # CLIENTES
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

    # CONVERSACIONES
    cur.execute("""
    CREATE TABLE IF NOT EXISTS conversaciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER NOT NULL,
        fecha TEXT NOT NULL,
        rol TEXT NOT NULL,
        mensaje TEXT NOT NULL,
        tipo TEXT DEFAULT 'texto',
        FOREIGN KEY(cliente_id) REFERENCES clientes(id) ON DELETE CASCADE
    )
    """)

    # PEDIDOS (nueva estructura completa)
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
        -- Nuevas columnas
        precio_unitario REAL DEFAULT 0,
        subtotal REAL DEFAULT 0,
        envio REAL DEFAULT 0,
        total REAL DEFAULT 0,
        municipio TEXT,
        tipo_jaboncito TEXT,
        color_jaboncito TEXT,
        nombre_bebe TEXT,
        tarjetita TEXT,
        notas TEXT,
        bot_activo INTEGER DEFAULT 1,
        created_at TEXT,
        updated_at TEXT,
        FOREIGN KEY(cliente_id) REFERENCES clientes(id) ON DELETE CASCADE
    )
    """)

    # Migración: agregar columnas nuevas si no existen
    columnas_nuevas = {
        "precio_unitario": "REAL DEFAULT 0",
        "subtotal": "REAL DEFAULT 0",
        "envio": "REAL DEFAULT 0",
        "total": "REAL DEFAULT 0",
        "municipio": "TEXT",
        "tipo_jaboncito": "TEXT",
        "color_jaboncito": "TEXT",
        "nombre_bebe": "TEXT",
        "tarjetita": "TEXT",
        "notas": "TEXT",
        "bot_activo": "INTEGER DEFAULT 1",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    }
    cur.execute("PRAGMA table_info(pedidos)")
    columnas_existentes = {fila["name"] for fila in cur.fetchall()}
    for nombre_columna, tipo_sql in columnas_nuevas.items():
        if nombre_columna not in columnas_existentes:
            cur.execute(f"ALTER TABLE pedidos ADD COLUMN {nombre_columna} {tipo_sql}")

    # CONTADOR DE FOLIOS
    cur.execute("""
    CREATE TABLE IF NOT EXISTS contador_folios (
        fecha TEXT PRIMARY KEY,
        ultimo INTEGER NOT NULL DEFAULT 0
    )
    """)

    # USO DE OPENAI
    cur.execute("""
    CREATE TABLE IF NOT EXISTS uso_openai (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cliente_id INTEGER,
        fecha TEXT NOT NULL,
        modelo TEXT,
        tokens_entrada INTEGER,
        tokens_salida INTEGER,
        FOREIGN KEY(cliente_id) REFERENCES clientes(id) ON DELETE CASCADE
    )
    """)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    inicializar_bd()
    print("✅ Base de datos creada correctamente.")
