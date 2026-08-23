import os
import sqlite3
import logging
import threading
import time

logger_db = logging.getLogger('database')

# 🔧 (23 ago 2026, a pedido de Israel -- "disk I/O error" recurrente en
# ráfagas de tráfico real, AUN con journal_mode=DELETE) El Procfile usa
# --workers 1 --threads 8: un solo proceso de gunicorn, pero hasta 8
# hilos que pueden abrir su propia conexión SQLite y golpear el disco
# AL MISMO TIEMPO (ej. varios mensajes de WhatsApp/Messenger llegando
# juntos). journal_mode=DELETE + busy_timeout ya evita que se choquen
# por locks normales de SQLite, pero el disco persistente de Render es
# almacenamiento en red -- bajo varias operaciones de E/S simultáneas
# puede fallar con "disk I/O error" real (eso NO es un lock, es una
# falla de E/S), justo lo que se vio en los logs: varias funciones
# distintas (es_cliente_nuevo, chat_guardar_mensaje, uso_registrar_openai,
# guardar_borrador_pedido...) tronando juntas en ráfagas cortas.
# Este candado global fuerza a que TODO el proceso use el disco desde
# una sola conexión a la vez -- ya no hay forma de que 2+ hilos golpeen
# el archivo .db al mismo tiempo. Es RLock (no Lock normal) para que un
# mismo hilo pueda abrir una conexión "anidada" (una función que llama a
# otra que también abre conexión) sin bloquearse a sí mismo. El costo es
# una espera de milisegundos entre mensajes que llegan exactamente al
# mismo tiempo -- mucho más barato que un "disk I/O error" que tumba la
# respuesta al cliente.
#
# 🔧 (23 ago 2026, mismo día -- a los ~10 min de subir el candado) UN
# candado normal (RLock) tiene un defecto grave que no se vio hasta que
# pasó de verdad: si una sola operación se queda COLGADA de verdad (no
# truena, simplemente el disco de red nunca contesta esa lectura/
# escritura -- muy distinto a un "disk I/O error", que sí truena rápido),
# el candado se queda tomado PARA SIEMPRE, y como es global, bloquea a
# TODO el proceso -- ya nadie puede ni ver el dashboard ni el bot puede
# contestarle a un cliente, hasta reiniciar el servicio a mano. Eso fue
# justo lo que pasó: algo (muy probablemente el hilo de seguimientos de
# 23h, que corre justo al arrancar) se coló en una consulta y dejó el
# candado tomado varios minutos, con 0% de avance, aunque el disco en sí
# resultó estar bien (se probó con sqlite3 desde la Shell mientras
# estaba colgado y respondió al toque -- fue el candado de Python, no
# el disco).
#
# _CandadoConVigilante reemplaza el RLock con uno reentrante (mismo
# hilo puede volver a entrar sin bloquearse, igual que antes) pero que
# ADEMÁS un hilo vigilante aparte puede liberar a la fuerza si ve que
# lleva demasiado tiempo tomado -- un RLock normal de Python NO permite
# esto (solo el hilo dueño puede liberarlo). Con esto, lo peor que puede
# pasar ya no es "todo el proceso colgado para siempre", sino "todo el
# proceso tarda hasta VIGILANTE_LIMITE_SEGUNDOS de más esa vez" y
# después se recupera solo, sin que nadie tenga que reiniciar a mano.
class _CandadoConVigilante:
    def __init__(self):
        self._lock = threading.Lock()
        self._guardia = threading.Lock()  # protege los 3 campos de abajo
        self._dueno = None
        self._contador = 0
        self._tomado_desde = None

    def acquire(self, timeout=-1):
        yo = threading.get_ident()
        with self._guardia:
            if self._dueno == yo:
                self._contador += 1
                return True
        obtenido = self._lock.acquire(timeout=timeout)
        if obtenido:
            with self._guardia:
                self._dueno = yo
                self._contador = 1
                self._tomado_desde = time.monotonic()
        return obtenido

    def release(self):
        yo = threading.get_ident()
        with self._guardia:
            if self._dueno != yo:
                # Ya lo liberó el vigilante (o nunca fue nuestro) --
                # no reventar, simplemente no hay nada que hacer.
                return
            self._contador -= 1
            if self._contador > 0:
                return
            self._dueno = None
            self._tomado_desde = None
        try:
            self._lock.release()
        except RuntimeError:
            pass

    def liberar_si_lleva_colgado(self, limite_segundos):
        """Llamado solo por el hilo vigilante (ver más abajo). Si el
        candado lleva tomado más de limite_segundos, lo libera a la
        fuerza y devuelve True (para que el vigilante lo reporte)."""
        with self._guardia:
            if self._dueno is None or self._tomado_desde is None:
                return False
            segundos_tomado = time.monotonic() - self._tomado_desde
            if segundos_tomado <= limite_segundos:
                return False
            dueno_anterior = self._dueno
            self._dueno = None
            self._contador = 0
            self._tomado_desde = None
        try:
            self._lock.release()
        except RuntimeError:
            pass
        logger_db.critical(
            f"[VIGILANTE] El candado de disco llevaba {segundos_tomado:.0f}s "
            f"tomado (hilo id={dueno_anterior}) sin soltarse -- se liberó a "
            f"la fuerza para no dejar el proceso colgado. Es probable que "
            f"ese hilo siga atorado en una operación de disco de verdad "
            f"lenta; si vuelve a pasar seguido, revisar con soporte de "
            f"Render si el Disk está teniendo problemas."
        )
        return True


_db_lock = _CandadoConVigilante()

# Cada cuánto revisa el vigilante si el candado lleva demasiado tomado,
# y cuánto es "demasiado". 45s da bastante margen para que una consulta
# lenta de verdad (ej. el dashboard con el disco teniendo un mal
# momento) termine sola sin que el vigilante la interrumpa de más --
# pero ya no dependemos de que alguien note el problema y reinicie a
# mano como pasó hoy.
VIGILANTE_INTERVALO_SEGUNDOS = 5
VIGILANTE_LIMITE_SEGUNDOS = 45


def _hilo_vigilante_candado():
    while True:
        time.sleep(VIGILANTE_INTERVALO_SEGUNDOS)
        try:
            _db_lock.liberar_si_lleva_colgado(VIGILANTE_LIMITE_SEGUNDOS)
        except Exception as e:
            logger_db.error(f"[VIGILANTE] Error revisando el candado: {e}")


threading.Thread(target=_hilo_vigilante_candado, daemon=True).start()

DB_PATH = os.getenv("SQLITE_DB_PATH", "dalia_bot.db")

db_dir = os.path.dirname(DB_PATH)
if db_dir and not os.path.exists(db_dir):
    try:
        os.makedirs(db_dir, exist_ok=True)
        logger_db.info(f"Directorio de base de datos creado: {db_dir}")
    except Exception as e:
        logger_db.warning(f"No se pudo crear el directorio {db_dir}: {e}")

class _ConexionAutoCierre(sqlite3.Connection):
    """🔧 (22 ago 2026, a pedido de Israel -- fuga de memoria/conexiones)
    `with conn:` en sqlite3 solo hace commit/rollback automático al
    salir del bloque -- NO cierra la conexión. Los ~25 lugares del
    proyecto que hacen `with get_db_connection() as conn:` daban por
    hecho que sí se cerraba, así que la conexión (y su memoria/handle de
    archivo) se quedaba abierta hasta que el recolector de basura de
    Python decidiera reclamarla, lo cual no es inmediato ni está
    garantizado -- con mensajes llegando seguido, esto acumula
    conexiones abiertas de más. Esta subclase cierra la conexión también
    al salir del `with`, sin tener que tocar ninguno de esos ~25
    lugares -- nada más cambia cómo se crea la conexión aquí."""
    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            super().__exit__(exc_type, exc_val, exc_tb)
        finally:
            self.close()

    def close(self):
        # 🔧 (23 ago 2026) Libera el candado global _db_lock aquí (no en
        # __exit__) para que se libere tanto si se usó "with
        # get_db_connection() as conn:" como si se usó el patrón manual
        # "conn = get_connection() / try / finally: conn.close()" (hay
        # ~25 lugares en clientes.py e historial.py con ese segundo
        # patrón). getattr(..., False) evita reventar si close() se
        # llama dos veces por error.
        try:
            super().close()
        finally:
            if not getattr(self, '_lock_liberado', False):
                self._lock_liberado = True
                try:
                    _db_lock.release()
                except RuntimeError:
                    pass


def get_db_connection():
    # 🔧 (23 ago 2026, mismo día -- a los pocos minutos de subir el
    # candado) Con 10s de margen, abrir el dashboard en varias pestañas
    # al mismo tiempo (ej. "hoy" + "semana" + "mes") alcanzó a chocar:
    # cada pestaña hace ~14 consultas en UNA sola conexión, y si el
    # disco de Render está teniendo un momento lento (la razón de fondo
    # de todo este problema), esas ~14 consultas pueden tardar más de
    # 10s en total mientras las demás pestañas esperan su turno -- eso
    # se vio en los logs como 5 errores seguidos de "tiempo de espera
    # agotado" entre pestañas del dashboard, justo después del deploy.
    # Subir a 25s le da mucho más margen para que el turno de cada quien
    # simplemente tarde un poco más en vez de fallar. Sigue siendo mejor
    # esperar unos segundos de más que mostrar un error.
    if not _db_lock.acquire(timeout=25):
        logger_db.error("get_db_connection: tiempo de espera agotado para el candado de disco (posible hilo colgado).")
        raise sqlite3.OperationalError("tiempo de espera agotado para acceso exclusivo al disco")
    conn = None
    try:
        # timeout=10: si la BD está ocupada, espera hasta 10s antes de
        # fallar (en vez de tronar de inmediato con "database is locked").
        conn = sqlite3.connect(DB_PATH, timeout=10, factory=_ConexionAutoCierre)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        # 🔧 (23 ago 2026, a pedido de Israel -- "disk I/O error" en TODAS las
        # escrituras/lecturas de golpe) Antes usábamos journal_mode=WAL para
        # permitir lecturas mientras alguien más escribe. El problema: WAL
        # necesita archivos auxiliares (-wal y -shm) con memoria compartida
        # mapeada (mmap), y los discos persistentes de Render son
        # almacenamiento en red -- ese tipo de almacenamiento no siempre
        # soporta bien ese mecanismo, y cuando falla, CADA operación truena
        # con "disk I/O error" (justo lo que vimos: es_cliente_nuevo,
        # chat_guardar_mensaje, uso_registrar_openai, etc., todo a la vez).
        # DELETE es el modo clásico de SQLite (un solo archivo de journal,
        # sin memoria compartida) -- más lento en teoría con muchos
        # escritores al mismo tiempo, pero aquí solo hay 1 proceso (1 worker
        # de gunicorn) con varios hilos, y busy_timeout ya hace que se
        # esperen entre sí en vez de fallar. Mucho más confiable sobre disco
        # de red que WAL.
        conn.execute("PRAGMA journal_mode = DELETE;")
        conn.execute("PRAGMA busy_timeout = 5000;")
    except Exception:
        # Si algo truena antes de que la conexión quede lista, nadie
        # más va a poder llamar conn.close() para liberar el candado --
        # hay que liberarlo aquí mismo para no dejar el proceso trabado.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        else:
            _db_lock.release()
        raise
    return conn

# Alias usado por clientes.py, historial.py y pedido_manager
get_connection = get_db_connection


def reclamar_mensaje_procesado(mensaje_id):
    """Dedupe de mensajes entrantes a nivel de base de datos (ver tabla
    mensajes_webhook_procesados). Devuelve True SOLO si este mensaje_id
    YA se había procesado antes (por cualquier proceso de gunicorn) --
    en ese caso el que llama debe ignorarlo. Devuelve False la primera
    vez, y ese mismo INSERT ya lo deja marcado como procesado para la
    próxima vez. Mismo patrón atómico que reclamar_seguimiento_23h en
    pedido_manager.py."""
    if not mensaje_id:
        return False
    try:
        with get_db_connection() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO mensajes_webhook_procesados (mensaje_id) VALUES (?)",
                (mensaje_id,),
            )
            conn.commit()
            return cur.rowcount == 0  # 0 filas insertadas = ya existía = duplicado
    except Exception as e:
        logger_db.error(f"reclamar_mensaje_procesado: {e}")
        return False


def obtener_nombre_messenger_cache(psid):
    """Devuelve el nombre de Facebook ya guardado en caché para este PSID
    de Messenger, o None si todavía no se ha resuelto. No llama a ningún
    API externo -- solo lee la tabla nombres_messenger."""
    if not psid:
        return None
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT nombre FROM nombres_messenger WHERE psid = ?", (psid,)
            ).fetchone()
            return row["nombre"] if row else None
    except Exception as e:
        logger_db.error(f"obtener_nombre_messenger_cache: {e}")
        return None


def obtener_nombres_messenger_cache_multiples(psids):
    """Igual que obtener_nombre_messenger_cache pero para varios PSIDs a
    la vez (un solo query) -- pensado para listas del dashboard, para no
    hacer N queries por N filas. Devuelve un dict {psid: nombre}, solo
    con los que sí tienen nombre en caché."""
    psids = [p for p in (psids or []) if p]
    if not psids:
        return {}
    try:
        with get_db_connection() as conn:
            marcadores = ",".join("?" for _ in psids)
            filas = conn.execute(
                f"SELECT psid, nombre FROM nombres_messenger WHERE psid IN ({marcadores})",
                psids,
            ).fetchall()
            return {f["psid"]: f["nombre"] for f in filas}
    except Exception as e:
        logger_db.error(f"obtener_nombres_messenger_cache_multiples: {e}")
        return {}


def guardar_nombre_messenger_cache(psid, nombre):
    """Guarda (o actualiza) el nombre de Facebook resuelto para este PSID."""
    if not psid or not nombre:
        return
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO nombres_messenger (psid, nombre, fecha_actualizacion) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(psid) DO UPDATE SET nombre = excluded.nombre, "
                "fecha_actualizacion = CURRENT_TIMESTAMP",
                (psid, nombre),
            )
            conn.commit()
    except Exception as e:
        logger_db.error(f"guardar_nombre_messenger_cache: {e}")


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

            # 🆕 (20 ago 2026, pedido explícito de Israel) Caché del nombre
            # real de Facebook de cada cliente de Messenger -- el PSID por
            # sí solo no le dice nada a Israel en el dashboard. Se llena
            # una sola vez por PSID (ver resolver_nombre_messenger() en
            # app.py) para no tener que llamarle al Graph API en cada
            # mensaje.
            cursor.execute("""CREATE TABLE IF NOT EXISTS nombres_messenger (
                psid TEXT PRIMARY KEY,
                nombre TEXT NOT NULL,
                fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
            
            # 🔥 Nueva tabla para borradores
            cursor.execute("""CREATE TABLE IF NOT EXISTS borradores_pedido (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telefono TEXT NOT NULL UNIQUE,
                datos_json TEXT NOT NULL,
                fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

            # 🆘 Configuración global simple (clave/valor) -- se usa para
            # el candado de emergencia por WhatsApp (pausar/reanudar el
            # bot para TODOS los clientes sin tocar Render). Pensada para
            # crecer a futuro si se necesita guardar algún otro ajuste
            # global sin agregar una tabla nueva cada vez.
            cursor.execute("""CREATE TABLE IF NOT EXISTS configuracion (
                clave TEXT PRIMARY KEY,
                valor TEXT NOT NULL,
                fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

            # 🆕 Seguimiento automático de ~23h a clientes silenciosos con
            # un pedido en progreso (ver PENDIENTES.md sección 1). Cada
            # fila = "ya se le mandó el mensaje de seguimiento a este
            # teléfono en este canal". marca_ultimo_mensaje_cliente queda
            # guardada solo como referencia/diagnóstico (desde cuándo
            # estaba callado cuando se le mandó).
            # 🔧 (18 ago 2026, decisión explícita de Israel) Máximo UN
            # seguimiento por telefono+canal EN TOTAL, para siempre -- ya
            # NO se manda otro aunque el cliente responda y se quede
            # callado de nuevo más adelante. El filtro real que aplica
            # esto vive en candidatos_seguimiento_23h() (pedido_manager.py),
            # que excluye a cualquier telefono con una fila aquí antes de
            # considerarlo candidato. El UNIQUE(telefono, marca) de abajo
            # sigue existiendo solo como candado extra a nivel de base de
            # datos (por si el hilo corriera dos veces), no como la regla
            # de negocio.
            cursor.execute("""CREATE TABLE IF NOT EXISTS seguimientos_23h (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telefono TEXT NOT NULL,
                canal TEXT NOT NULL DEFAULT 'whatsapp',
                marca_ultimo_mensaje_cliente TIMESTAMP NOT NULL,
                fecha_enviado TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(telefono, marca_ultimo_mensaje_cliente)
            )""")

            # 🔧 Conversaciones silenciadas por Dalia (por cliente
            # individual, no global). Se usa cuando Dalia contesta manual
            # a un cliente específico desde Messenger porque notó que el
            # bot se equivocó -- el bot deja de responder SOLO en esa
            # conversación, sin afectar a nadie más. Independiente de si
            # ya existe un pedido oficial confirmado o no (a diferencia
            # de modo_atencion en la tabla pedidos, que solo aplica una
            # vez que hay un pedido creado).
            cursor.execute("""CREATE TABLE IF NOT EXISTS conversaciones_silenciadas (
                telefono TEXT PRIMARY KEY,
                fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

            # 🔧 (19 ago 2026) Dedupe de mensajes entrantes (webhook) a
            # nivel de base de datos, no solo en memoria. Antes esto vivía
            # en un set() de Python dentro de app.py -- funcionaba bien
            # con UN solo proceso de gunicorn, pero al pasar a 2+ procesos
            # (ver Procfile, cambio para que el servicio no se quede
            # colgado por completo si un proceso se traba) cada proceso
            # tendría su propio set() separado, y un mismo mensaje
            # reintentado por Meta/YCloud podría caer en otro proceso y
            # procesarse dos veces (respuesta duplicada al cliente). Usa
            # el mismo patrón atómico ya probado en seguimientos_23h:
            # INSERT OR IGNORE + revisar rowcount, que SQLite garantiza
            # correcto aunque dos procesos lo intenten al mismo tiempo.
            cursor.execute("""CREATE TABLE IF NOT EXISTS mensajes_webhook_procesados (
                mensaje_id TEXT PRIMARY KEY,
                fecha TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")

            # 🔧 Migración segura: agrega la columna "canal" (whatsapp /
            # messenger) a historial_chat y pedidos, si todavía no
            # existe. SQLite no soporta "ALTER TABLE ... ADD COLUMN IF
            # NOT EXISTS" directamente, así que se revisa primero con
            # PRAGMA table_info -- así no truena en despliegues donde la
            # columna ya se agregó antes.
            def _agregar_columna_si_falta(tabla, columna, tipo_sql):
                cursor.execute(f"PRAGMA table_info({tabla})")
                columnas_existentes = {fila[1] for fila in cursor.fetchall()}
                if columna not in columnas_existentes:
                    cursor.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {tipo_sql}")
                    logger_db.info(f"[DB] Columna '{columna}' agregada a '{tabla}'.")

            _agregar_columna_si_falta("historial_chat", "canal", "TEXT DEFAULT 'whatsapp'")
            _agregar_columna_si_falta("pedidos", "canal", "TEXT DEFAULT 'whatsapp'")
            _agregar_columna_si_falta("uso_openai", "modelo", "TEXT")
            _agregar_columna_si_falta("uso_openai", "tokens_entrada", "INTEGER DEFAULT 0")
            _agregar_columna_si_falta("uso_openai", "tokens_salida", "INTEGER DEFAULT 0")
            _agregar_columna_si_falta("uso_openai", "tokens_cache", "INTEGER DEFAULT 0")
            _agregar_columna_si_falta("uso_openai", "costo_estimado_usd", "REAL DEFAULT 0.0")
            conn.commit()

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
