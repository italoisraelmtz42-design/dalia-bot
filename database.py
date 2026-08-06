import sqlite3
import os
import logging

logger = logging.getLogger(__name__)

# Ruta de la base de datos (configurable por variable de entorno)
DB_PATH = os.getenv("SQLITE_DB_PATH", "dalia_bot.db")

def get_db_connection():
    """Obtiene una conexión a la base de datos SQLite."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_order_tables():
    """
    Crea las tablas correspondientes al Motor de Pedidos (Sprint 1).
    No interfiere con tablas existentes del bot.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # 1. Tabla de Pedidos
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pedidos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    folio TEXT UNIQUE NOT NULL,
                    cliente_id INTEGER,
                    telefono TEXT NOT NULL,
                    estado TEXT NOT NULL,
                    porcentaje_completitud INTEGER DEFAULT 0,
                    bot_activo INTEGER DEFAULT 1,
                    fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 2. Tabla de Items del Pedido
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

            # 3. Tabla de Pagos
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

            # 4. Tabla de Entregas
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

            # 5. Tabla de Historial de Cambios
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS pedido_historial (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    pedido_id INTEGER NOT NULL,
                    cambio TEXT NOT NULL,
                    usuario TEXT,
                    fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (pedido_id) REFERENCES pedidos(id) ON DELETE CASCADE
                )
            """)

            conn.commit()
            logger.info("✅ Tablas del Motor de Pedidos inicializadas correctamente en SQLite.")

    except Exception as e:
        logger.error(f"❌ Error al crear las tablas de pedidos: {e}")
        raise

def init_db():
    """Inicializa las tablas de la aplicación completa (incluye las nuevas de pedidos)."""
    try:
        init_order_tables()
        # Aquí irían inicializaciones de otras tablas del CRM/Usuarios si existieran
    except Exception as e:
        logger.critical(f"Fallo crítico en la inicialización de la base de datos: {e}")
