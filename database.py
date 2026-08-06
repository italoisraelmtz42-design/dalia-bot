import sqlite3
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("SQLITE_DB_PATH", "dalia_bot.db")

def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;") # Activar llaves foráneas explícitamente
    return conn

def init_order_tables():
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # 1. Tabla Pedidos (Añadida columna es_urgente para el resumen)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pedidos (
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
                )
            """)

            # 2. Tabla Items
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pedido_items (
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
                )
            """)

            # 3. Tabla Pagos
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pagos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pedido_id INTEGER NOT NULL,
                    tipo TEXT NOT NULL,
                    monto REAL NOT NULL,
                    metodo TEXT NOT NULL,
                    comprobante TEXT,
                    confirmado INTEGER DEFAULT 0,
                    fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE
                )
            """)

            # 4. Tabla Entregas (Añadido municipio)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS entregas (
                    pedido_id INTEGER PRIMARY KEY,
                    tipo_entrega TEXT NOT NULL,
                    municipio TEXT,
                    direccion TEXT,
                    fecha_entrega TIMESTAMP,
                    costo_envio REAL DEFAULT 0.0,
                    FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE
                )
            """)

            # 5. Tabla Historial
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pedido_historial (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pedido_id INTEGER NOT NULL,
                    campo TEXT NOT NULL,
                    valor_anterior TEXT,
                    valor_nuevo TEXT,
                    usuario TEXT,
                    fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE
                )
            """)

            # 6. Tabla Eventos
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pedido_eventos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pedido_id INTEGER NOT NULL,
                    evento TEXT NOT NULL,
                    descripcion TEXT,
                    usuario TEXT,
                    fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE
                )
            """)

            # 7. Historial Chat (Compatibilidad)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS historial_chat (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telefono TEXT NOT NULL,
                    mensaje TEXT NOT NULL,
                    emisor TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 8. Uso OpenAI (Compatibilidad)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS uso_openai (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telefono TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            conn.commit()
            logger.info("✅ Tablas de Motor de Pedidos inicializadas correctamente.")
    except Exception as e:
        logger.error(f"❌ Error al crear las tablas: {e}")
        raise

def init_db():
    try:
        init_order_tables()
    except Exception as e:
        logger.critical(f"Fallo crítico en la inicialización de la base de datos: {e}")
