import sqlite3
import os
import logging

# Logger dedicado para database
logger_db = logging.getLogger('database')

DB_PATH = os.getenv("SQLITE_DB_PATH", "dalia_bot.db")

def get_db_connection():
    """Obtiene una conexión a la base de datos SQLite y logea la apertura."""
    logger_db.info(f"Nueva conexión SQLite abierta: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_order_tables():
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'")
            if not cursor.fetchone():
                current_version = 0
            else:
                cursor.execute("SELECT version FROM schema_version ORDER BY version DESC LIMIT 1")
                row = cursor.fetchone()
                current_version = row[0] if row else 0
            
            # Las tablas se crean con IF NOT EXISTS
            cursor.execute("""CREATE TABLE IF NOT EXISTS pedidos (id INTEGER PRIMARY KEY AUTOINCREMENT, folio TEXT UNIQUE NOT NULL, cliente_id INTEGER, telefono TEXT NOT NULL, estado TEXT NOT NULL, modo_atencion TEXT NOT NULL DEFAULT 'BOT', es_urgente INTEGER DEFAULT 0, porcentaje_completitud INTEGER DEFAULT 0, fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP, fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS pedido_items (id INTEGER PRIMARY KEY AUTOINCREMENT, pedido_id INTEGER NOT NULL, producto TEXT NOT NULL, cantidad INTEGER NOT NULL, precio_unitario REAL NOT NULL, subtotal REAL NOT NULL, color_toalla TEXT, color_moño TEXT, tipo_jaboncito TEXT, color_jaboncito TEXT, nombre_bebe TEXT, tarjetita TEXT, FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS pagos (id INTEGER PRIMARY KEY AUTOINCREMENT, pedido_id INTEGER NOT NULL, tipo TEXT NOT NULL, monto REAL NOT NULL, metodo TEXT NOT NULL, comprobante TEXT, confirmado INTEGER DEFAULT 0, fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS entregas (pedido_id INTEGER PRIMARY KEY, tipo_entrega TEXT NOT NULL, municipio TEXT, direccion TEXT, fecha_entrega TIMESTAMP, costo_envio REAL DEFAULT 0.0, FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS pedido_historial (id INTEGER PRIMARY KEY AUTOINCREMENT, pedido_id INTEGER NOT NULL, campo TEXT NOT NULL, valor_anterior TEXT, valor_nuevo TEXT, usuario TEXT, fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS pedido_eventos (id INTEGER PRIMARY KEY AUTOINCREMENT, pedido_id INTEGER NOT NULL, evento TEXT NOT NULL, descripcion TEXT, origen TEXT NOT NULL DEFAULT 'SISTEMA', usuario TEXT, fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS historial_chat (id INTEGER PRIMARY KEY AUTOINCREMENT, telefono TEXT NOT NULL, mensaje TEXT NOT NULL, emisor TEXT NOT NULL, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS uso_openai (id INTEGER PRIMARY KEY AUTOINCREMENT, telefono TEXT NOT NULL, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

            # Migración inicial
            if current_version == 0:
                cursor.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, fecha TEXT DEFAULT CURRENT_TIMESTAMP)")
                cursor.execute("INSERT INTO schema_version (version) VALUES (1)")
                conn.commit()
                logger_db.info("[DB] Migración inicial completada.")

            logger_db.info("[DB] ✅ Tablas del sistema verificadas y listas.")
    except Exception as e:
        logger_db.error(f"[DB] ❌ Error al crear las tablas: {e}")
        raise

def init_db():
    try:
        init_order_tables()
    except Exception as e:
        logger_db.critical(f"[DB] Fallo crítico en la inicialización: {e}")
