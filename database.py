import os
import sqlite3
import logging

logger_db = logging.getLogger('database')

DB_PATH = os.getenv("SQLITE_DB_PATH", "dalia_bot.db")

db_dir = os.path.dirname(DB_PATH)
if db_dir and not os.path.exists(db_dir):
    try:
        os.makedirs(db_dir, exist_ok=True)
        logger_db.info(f"Directorio de base de datos creado: {db_dir}")
    except Exception as e:
        logger_db.warning(f"No se pudo crear el directorio {db_dir}: {e}")

def get_db_connection():
    # timeout=10: si la BD está ocupada, espera hasta 10s antes de fallar
    # (en vez de tronar de inmediato con "database is locked").
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    # WAL permite lecturas mientras alguien más escribe. Con varios mensajes
    # de WhatsApp llegando casi al mismo tiempo (cada uno en su propio hilo),
    # esto evita errores intermitentes de "database is locked".
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
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
            
            # Tablas existentes
            cursor.execute("""CREATE TABLE IF NOT EXISTS pedidos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folio TEXT UNIQUE NOT NULL,
                cliente_id INTEGER,
                telefono TEXT NOT NULL,
                estado TEXT NOT NULL,
                modo_atencion TEXT NOT NULL DEFAULT 'BOT',
                es_urgente INTEGER DEFAULT 0,
                porcentaje_completitud INTEGER DEFAULT 0,
                fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS pedido_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id INTEGER NOT NULL,
                producto TEXT NOT NULL,
                cantidad INTEGER NOT NULL,
                precio_unitario REAL NOT NULL,
                subtotal REAL NOT NULL,
                color_toalla TEXT,
                color_moño TEXT,
                tipo_jaboncito TEXT,
                color_jaboncito TEXT,
                nombre_bebe TEXT,
                tarjetita TEXT,
                FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE
            )""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS pagos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id INTEGER NOT NULL,
                tipo TEXT NOT NULL,
                monto REAL NOT NULL,
                metodo TEXT NOT NULL,
                comprobante TEXT,
                confirmado INTEGER DEFAULT 0,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE
            )""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS entregas (
                pedido_id INTEGER PRIMARY KEY,
                tipo_entrega TEXT NOT NULL,
                municipio TEXT,
                direccion TEXT,
                fecha_entrega TIMESTAMP,
                costo_envio REAL DEFAULT 0.0,
                FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE
            )""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS pedido_historial (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id INTEGER NOT NULL,
                campo TEXT NOT NULL,
                valor_anterior TEXT,
                valor_nuevo TEXT,
                usuario TEXT,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE
            )""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS pedido_eventos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pedido_id INTEGER NOT NULL,
                evento TEXT NOT NULL,
                descripcion TEXT,
                origen TEXT NOT NULL DEFAULT 'SISTEMA',
                usuario TEXT,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE
            )""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS historial_chat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telefono TEXT NOT NULL,
                mensaje TEXT NOT NULL,
                emisor TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS uso_openai (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telefono TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            
            # 🔥 Nueva tabla para borradores
            cursor.execute("""CREATE TABLE IF NOT EXISTS borradores_pedido (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telefono TEXT NOT NULL UNIQUE,
                datos_json TEXT NOT NULL,
                fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

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
        raise

init_db()
