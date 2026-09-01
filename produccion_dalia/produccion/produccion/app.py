# -*- coding: utf-8 -*-
"""
Producción Dalia -- herramienta interna para Dalia, Diana e Israel.

Aquí se suben las notas de pedido YA CONFIRMADAS con el cliente (las que
la vendedora vuelve a mandar para confirmar, no la conversación en vivo
del bot). Sirve para:
  - Saber qué hay que fabricar/entregar cada día.
  - Ver un resumen financiero (anticipos, saldos, total) por período.

Completamente separado del bot (dalia-bot): otro servicio de Render, otra
base de datos, ningún código compartido. Así, pase lo que pase aquí, el
bot que le vende a los clientes nunca se ve afectado.
"""

import base64
import calendar as calendario_mod
import datetime
import io
import json
import os
import re
import uuid
from urllib.parse import quote

from flask import (
    Flask, flash, redirect, render_template, request, send_from_directory,
    session, url_for,
)
from openai import OpenAI

import database

# ----------------------------------------------------------------------
# Configuración
# ----------------------------------------------------------------------
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "cambia-esta-clave-en-produccion")


# 🔧 (23 ago 2026, pedido de Israel: "quiero que los miles lleven
# separación, $4,500 y no $4500") Filtro de Jinja para mostrar cualquier
# monto en pesos con comas de miles y 2 decimales. Solo se usa en textos
# de solo lectura (Finanzas, Comisiones, detalle de pedido) -- los
# <input type="number"> de edición de precios NO llevan este filtro,
# porque las comas romperían el parseo del navegador.
@app.template_filter("moneda")
def _filtro_moneda(valor):
    try:
        valor = float(valor or 0)
    except (TypeError, ValueError):
        valor = 0.0
    return f"{valor:,.2f}"


# 🔧 (24 ago 2026, pedido de Israel: "en finanzas 2026/08/13 cámbialo por
# 13/agosto/2026") Filtro de Jinja para mostrar cualquier fecha ISO
# (AAAA-MM-DD, con o sin hora pegada) como texto largo en español. Usa el
# mismo diccionario MESES_ES que ya se usa en el resto de la app (ver más
# abajo) para que el nombre del mes salga igual en todos lados.
@app.template_filter("fecha_larga")
def _filtro_fecha_larga(valor):
    if not valor:
        return ""
    try:
        fecha = datetime.date.fromisoformat(str(valor)[:10])
    except ValueError:
        return valor
    return f"{fecha.day}/{MESES_ES[fecha.month]}/{fecha.year}"

# 🔧 (23 ago 2026, pedido de Israel: "quiero acceso para 3 personas, pero
# Diana no quiero que vea lo financiero") Antes había una sola contraseña
# compartida para los 3. Ahora cada quien tiene la suya, y la app sabe
# quién entró -- eso es lo que permite esconderle finanzas solo a Diana
# (ver exigir_login / _puede_ver_finanzas más abajo), y también hace
# confiable la nueva sección de comisiones: ya no depende de que alguien
# escriba bien su nombre a mano, se toma directo de con qué contraseña
# entró.
# Se deja PRODUCCION_PASSWORD como respaldo de la contraseña de Israel
# nada más para no romper el login de un día para otro con este mismo
# deploy -- pero para que Dalia y Diana puedan entrar hace falta agregar
# sus contraseñas nuevas en Render (ver README_DESPLIEGUE.md).
USUARIOS = {
    "israel": os.getenv("PRODUCCION_PASSWORD_ISRAEL") or os.getenv("PRODUCCION_PASSWORD", ""),
    "dalia": os.getenv("PRODUCCION_PASSWORD_DALIA", ""),
    "diana": os.getenv("PRODUCCION_PASSWORD_DIANA", ""),
}
NOMBRES_DISPLAY = {"israel": "Israel", "dalia": "Dalia", "diana": "Diana", "karo": "Karo"}

# Quién gana comisión y cuánto por cada producto vendido (pieza, no por
# pedido). 🔧 (24 ago 2026, pedido de Israel) Ahora las 3: Dalia, Diana y
# Karo, $1.00 por pieza cada una. Karo NO tiene usuario/contraseña propio
# (no entra a la webapp -- ver USUARIOS arriba), pero sí debe poder verse
# su comisión acumulada: Israel la ve desde el selector de vendedoras en
# /comisiones (ver la lógica de "otros_vendedores" más abajo).
VENDEDORES_CON_COMISION = {"dalia": 1.0, "diana": 1.0, "karo": 1.0}

# 🔧 (24 ago 2026, pedido de Israel: "quiero que algo revise a quién
# pertenecen las comisiones de cada quién") Antes la comisión se le
# atribuía a quien SUBÍA la nota (subido_por) -- pero Israel aclaró que
# eso está mal: cualquiera de las 3 puede subir la nota de otra persona
# (ej. Dalia sube una tanda que en realidad son ventas de Karo), así que
# de quién es la comisión lo dice el FOLIO escrito en la propia nota, no
# quién la sube a la app. Cada quien tiene su propio prefijo de folio
# (talonario separado):
#   - Folios que empiezan con "DE"          -> Dalia
#   - Folios que empiezan con "D" (no "DE")  -> Diana
#   - Folios que empiezan con "K"            -> Karo
# IMPORTANTE: el orden de esta lista importa -- "DE" se revisa ANTES que
# "D" porque "DE" también empieza con "D"; si "D" se revisara primero,
# todos los folios de Dalia se le atribuirían por error a Diana.
PREFIJOS_FOLIO_VENDEDORA = [
    ("DE", "dalia"),
    ("D", "diana"),
    ("K", "karo"),
]


def vendedora_por_folio(folio):
    """Regresa la clave de vendedora ('dalia'/'diana'/'karo') dueña de la
    comisión de este folio, o None si el folio está vacío o no coincide
    con ningún prefijo conocido. Un folio que regresa None NUNCA debe
    atribuirse por default a quien subió la nota -- debe quedar marcado
    con necesita_revision (ver _revisar_calidad) para que alguien lo
    corrija a mano; total dinero real de por medio, mismo principio que
    ya se usa en dalia-bot para nunca inventar un monto de anticipo."""
    folio = (folio or "").strip().upper()
    if not folio:
        return None
    for prefijo, vendedora in PREFIJOS_FOLIO_VENDEDORA:
        if folio.startswith(prefijo):
            return vendedora
    return None

_passwords_no_vacias = [p for p in USUARIOS.values() if p]
if len(_passwords_no_vacias) != len(set(_passwords_no_vacias)):
    print("⚠️ ADVERTENCIA: dos o más usuarios de Producción Dalia tienen la MISMA "
          "contraseña configurada -- el login no va a poder distinguir quién es quién.")

MESES_ES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
            "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
DIAS_SEMANA_ES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]
DIAS_SEMANA_LARGOS_ES = ["", "Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

# 🔧 (29 ago 2026, pedido de Israel: "necesito una nota idéntica a la de
# Excel, que se imprima en PDF y se le mande al cliente para que revise
# sus datos") Texto fijo del pie de la nota -- igual para todos los
# pedidos, no depende de cada uno. Si cambia el horario del local, el
# teléfono de contacto, o el texto de las notas de la nota, se edita
# aquí una sola vez y aplica a todas las notas nuevas que se impriman.
HORARIO_LOCAL_NOTA = ["Lunes a viernes 3:30 - 6:30 PM", "Sábado 11:30 - 2:00 PM"]
TELEFONO_CONTACTO_VENDEDOR_NOTA = "81 1072 5440"
NOTA_JABONES_TEXTO = "SE RECOMIENDA NO DEJAR LOS JABONES A SOL DIRECTO O MUCHO CALOR (SE PUEDEN DERRETIR)"
NOTA_HORARIO_DOMICILIO_TEXTO = "PEDIDOS A DOMICILIO SIN HORA EXACTA DE ENTREGA (ENTRE 13:00 Y 22:00 HRS)"
NOTA_TARJETITA_TEXTO = "(La tarjetita personalizada se imprime hasta que el cliente confirme que esté correcta)"

# 🔧 (29 ago 2026, pedido de Israel: "cuando se ponga la opción de punto de
# entrega debe haber esas opciones para escoger" -- lugares fijos de
# entrega, sin costo de envío asociado (a diferencia de domicilio/DHL).
# Lo que se elija aquí se guarda en el mismo campo "direccion" de
# siempre, y en la nota aparece como "LUGAR DE ENTREGA" -- igual que la
# dirección de un domicilio.
PUNTOS_ENTREGA_FIJOS = ["Metro Mitras", "MERCO Pueblo Nuevo", "Soriana Fresnos", "KFC Sendero Escobedo"]

# 🔧 (1 sep 2026, pedido de Israel: "cuando quiera anotar un producto, que
# se despliegue esa lista, ya con precios, y se carguen los precios en
# automático") Catálogo de precios para el selector de /capturar -- cada
# categoría es (nombre_categoria, [(nombre_producto, precio_base, nota_o_None), ...]).
# "nota" son las variantes o descuentos por volumen que Israel compartió
# (ej. "desde 50 pzas: $16.00", "cambio de moño: +$2.00") -- se muestran
# junto al precio en el <select> nada más para que se vean, pero el
# precio que se autocompleta SIEMPRE es el precio base (el de una sola
# pieza, sin descuento); si aplica un descuento por volumen o un cambio,
# se ajusta a mano en el campo de precio, igual que cualquier otro ajuste
# manual -- no hay lógica de "si cantidad >= X, cambia el precio solo".
# "Encendedor"/"Destapador" traían dos precios (con/sin bolsa) -- se
# separaron en dos renglones del catálogo en vez de meter esa variante
# como nota, porque ahí SÍ es un precio base distinto, no un descuento.
CATALOGO_PRECIOS_CAPTURA = [
    ("Ositos", [
        ("Osito con jaboncito", 12.00, None),
        ("Osito sencillo (sin jabón)", 12.00, None),
        ("Osito doble pie", 14.00, None),
        ("Osito inicial chica", 15.00, None),
        ("Osito doble inicial chica", 19.00, None),
        ("Osito inicial grande", 22.00, None),
        ("Osito peluche llavero", 18.00, "desde 50 pzas: $16.00"),
        ("Osito toalla afelpada", 18.00, "cambio de moño: +$2.00"),
        ("Kit osito + oración + velita", 21.00, None),
    ]),
    ("Animales de toalla", [
        ("Mariposa", 14.50, None),
        ("Elefante", 14.00, None),
        ("Unicornio", 14.00, None),
        ("Jirafa", 16.00, None),
        ("Caballo", 15.00, None),
        ("Perrito", 13.00, None),
        ("León", 14.00, None),
        ("Conejo", 13.50, None),
        ("Búho", 14.00, None),
        ("Búho con birrete", 14.00, None),
    ]),
    ("Velas", [
        ("Vela toalla chica", 12.00, None),
        ("Vela toalla grande", 16.50, None),
    ]),
    ("Otros", [
        ("Oración con decenario", 15.00, None),
        ("Oración con velita", 10.00, None),
        ("Abanico madera", 23.00, "desde 100 pzas: $21.00"),
        ("Dominó", 35.00, "desde 50 pzas: $30.00"),
        ("Encendedor (sin bolsa)", 10.00, None),
        ("Encendedor (con bolsa)", 11.00, None),
        ("Destapador (sin bolsa)", 15.50, None),
        ("Destapador (con bolsa)", 16.50, None),
    ]),
]

# 🔧 (29 ago 2026, pedido de Israel: "cuando el pedido es de Diana la nota
# va en rosa, de Dalia en amarillo, de Karo en morado -- y el teléfono de
# contacto también cambia según quién es") Mismo folio que ya decide la
# comisión (ver vendedora_por_folio) ahora también decide cómo se ve la
# nota impresa y qué teléfono trae. "banner_texto" es oscuro en el tema
# amarillo (blanco no se lee bien sobre amarillo claro) y blanco en los
# demás. "notas_imp_bg"/"notas_imp_texto" son el color del recuadro de
# "NOTAS IMPORTANTES" -- a propósito NO es el mismo color que el tema de
# la nota (para que resalte): amarillo para Diana/Karo, rosa para Dalia
# (cuyo tema YA es amarillo). Un folio no reconocido (o vacío) cae en el
# rosa de siempre.
COLORES_NOTA_POR_VENDEDORA = {
    "diana": {"borde": "#e8598b", "banner": "#ec6ea8", "banner_texto": "white",
              "etiqueta_bg": "#fbdde9", "texto_fuerte": "#b8386a",
              "notas_imp_bg": "#fff29e", "notas_imp_texto": "#7a5c00"},
    "dalia": {"borde": "#d9a600", "banner": "#f5c518", "banner_texto": "#5c4600",
              "etiqueta_bg": "#fdf1c4", "texto_fuerte": "#8a6a00",
              "notas_imp_bg": "#fbdde9", "notas_imp_texto": "#b8386a"},
    "karo": {"borde": "#7a52c9", "banner": "#9b7fe0", "banner_texto": "white",
             "etiqueta_bg": "#ece3fb", "texto_fuerte": "#5433a3",
             "notas_imp_bg": "#fff29e", "notas_imp_texto": "#7a5c00"},
}
COLOR_NOTA_DEFAULT = COLORES_NOTA_POR_VENDEDORA["diana"]

TELEFONOS_VENDEDORA_NOTA = {
    "dalia": "81 1997 9692",
    "karo": "81 2341 9013",
}

FOTOS_DIR = os.getenv("FOTOS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fotos_notas"))
os.makedirs(FOTOS_DIR, exist_ok=True)

MODELO = "gpt-4.1-mini"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=60.0, max_retries=2)

TIPOS_ENTREGA_VALIDOS = ("domicilio", "punto_de_entrega", "dhl", "local")

# 🔧 (29 ago 2026, pedido de Israel: "cuando se ponga domicilio debe haber
# opciones de escoger el municipio y que en automático se cambie el
# precio y se añada a la nota -- también DHL a precio fijo de $300")
# Tabla única de precios de envío -- el costo SIEMPRE se recalcula aquí
# en el servidor a partir de esta tabla (nunca se confía en un monto que
# venga del formulario, mismo principio que ya se usa con el folio/
# comisión). Lista en vez de dict para que el <select> del municipio
# mantenga este orden exacto. Si cambia un precio, se edita aquí una
# sola vez y aplica a todas las notas nuevas.
PRECIOS_ENVIO_MUNICIPIO = [
    ("Monterrey", 90), ("Apodaca", 90), ("San Nicolás", 90), ("Escobedo", 90), ("Guadalupe", 90),
    ("Santa Catarina", 100),
    ("San Pedro", 120), ("Juárez", 120),
    ("Pesquería", 150),
]
PRECIOS_ENVIO_MUNICIPIO_DICT = dict(PRECIOS_ENVIO_MUNICIPIO)
PRECIOS_ENVIO_MUNICIPIO_DICT_NORMALIZADO = {k.lower(): v for k, v in PRECIOS_ENVIO_MUNICIPIO}
PRECIO_ENVIO_DHL = 300


def _costo_envio(tipo_entrega, municipio):
    """Costo de envío autoritativo -- SIEMPRE se calcula aquí a partir de
    PRECIOS_ENVIO_MUNICIPIO / PRECIO_ENVIO_DHL, nunca se toma tal cual de
    lo que haya mandado el formulario (por si el JS del navegador falló o
    alguien mandó el formulario a mano). DHL es precio fijo sin importar
    el municipio; domicilio depende del municipio elegido (comparación
    sin importar mayúsculas -- por si viene de la lectura de la IA en vez
    del selector); cualquier otro tipo de entrega no tiene costo de envío."""
    if tipo_entrega == "dhl":
        return float(PRECIO_ENVIO_DHL)
    if tipo_entrega == "domicilio":
        return float(PRECIOS_ENVIO_MUNICIPIO_DICT_NORMALIZADO.get((municipio or "").strip().lower(), 0))
    return 0.0


# Extracciones ya leídas por IA (o pendientes de leer) pero todavía sin
# confirmar por un humano. Viven solo en memoria mientras alguien las
# revisa -- si el proceso se reinicia justo en ese momento se pierden,
# pero no importa: es solo el paso intermedio antes de guardar en la
# base de datos real.
EXTRACCIONES_PENDIENTES = {}

# 🔧 (22 ago 2026, pedido de Israel: "quisiera poder subir 42 imágenes de
# una vez") Cuando se suben varias fotos juntas, se guardan todas de
# inmediato (rápido, no llama a la IA) y se arma un "lote": la lista
# ordenada de temp_ids de esa tanda. La lectura con IA de cada nota se
# hace DESPUÉS, una por una, justo cuando la persona llega a revisarla en
# /confirmar -- nunca las 42 de golpe en la misma petición. Si se hiciera
# de golpe, con 42 fotos y ~3-5s por foto fácilmente se pasa del límite de
# tiempo del servidor (120s) y en el plan Free (0.1 CPU) sería aún peor.
# Leerlas una por una conforme se van revisando reparte ese trabajo en
# muchas peticiones cortas y funciona igual de bien con 2 fotos que con 200.
LOTES = {}

# Contador de cada tanda: cuántas notas se guardaron solas (la IA las leyó
# claras) vs. cuántas necesitaron que alguien las revisara a mano. Se
# muestra como resumen al terminar la tanda.
LOTES_RESUMEN = {}

database.init_db()


# ----------------------------------------------------------------------
# Autenticación (una contraseña por persona)
# ----------------------------------------------------------------------
def _puede_ver_finanzas():
    return session.get("usuario") != "diana"


@app.before_request
def exigir_login():
    rutas_publicas = {"login", "static"}
    if request.endpoint in rutas_publicas:
        return None
    if not session.get("autenticado"):
        return redirect(url_for("login", siguiente=request.path))
    # 🔧 (23 ago 2026) A Diana no le corresponde ver lo financiero del
    # negocio -- se bloquea aquí, a nivel de ruta, y no solo escondiendo
    # el botón en la pantalla (aunque también se esconde, ver base.html).
    # 🔧 (24 ago 2026, pedido de Israel: nuevas secciones de Ventas e
    # Indicadores) Ambas muestran dinero/volumen de ventas del negocio,
    # así que se les aplica la misma regla que a Finanzas -- Israel
    # confirmó que deben quedar ocultas para Diana igual que Finanzas.
    if request.endpoint in ("finanzas", "ventas", "indicadores") and not _puede_ver_finanzas():
        flash("Esa sección no está disponible con tu usuario.")
        return redirect(url_for("dashboard"))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        clave = request.form.get("password", "")
        usuario_encontrado = None
        for usuario, clave_correcta in USUARIOS.items():
            if clave_correcta and clave == clave_correcta:
                usuario_encontrado = usuario
                break
        if usuario_encontrado:
            session["autenticado"] = True
            session["usuario"] = usuario_encontrado
            session.permanent = True
            siguiente = request.args.get("siguiente") or url_for("dashboard")
            return redirect(siguiente)
        flash("Contraseña incorrecta.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.context_processor
def _inyectar_contexto_usuario():
    """Disponible en TODOS los templates sin tener que pasarlo a mano en
    cada render_template: quién entró, cómo se le muestra su nombre, y si
    puede ver dinero del negocio."""
    usuario = session.get("usuario")
    return {
        "usuario_actual": usuario,
        "nombre_usuario": NOMBRES_DISPLAY.get(usuario, usuario or ""),
        "puede_ver_finanzas": _puede_ver_finanzas(),
        "tiene_comision": usuario in VENDEDORES_CON_COMISION,
        "hay_comisiones_configuradas": bool(VENDEDORES_CON_COMISION),
    }


# ----------------------------------------------------------------------
# Utilidades de fecha
# ----------------------------------------------------------------------
def _hoy():
    # 🔧 (23 ago 2026, pedido de Israel: "hoy es 22 de agosto, en la app
    # aparece que es 23") Antes usaba la hora del servidor (Render corre
    # en UTC), así que entre las 6pm y la medianoche hora de Monterrey la
    # app ya mostraba el día siguiente. Ver database.hoy_negocio().
    return database.hoy_negocio()


def _rango_semana(hoy):
    inicio = hoy - datetime.timedelta(days=hoy.weekday())
    fin = inicio + datetime.timedelta(days=6)
    return inicio, fin


def _rango_mes(hoy):
    inicio = hoy.replace(day=1)
    if hoy.month == 12:
        fin = hoy.replace(day=31)
    else:
        fin = hoy.replace(month=hoy.month + 1, day=1) - datetime.timedelta(days=1)
    return inicio, fin


def _normalizar_anio_mes(anio, mes):
    """Envuelve un número de mes fuera de 1-12 hacia el año que le
    corresponde -- así 'mes siguiente' de diciembre 2026 da enero 2027,
    y 'mes anterior' de enero 2026 da diciembre 2025, sin casos especiales."""
    anio_ajustado = anio + (mes - 1) // 12
    mes_ajustado = (mes - 1) % 12 + 1
    return anio_ajustado, mes_ajustado


# ----------------------------------------------------------------------
# Dashboard de producción
# ----------------------------------------------------------------------
@app.route("/")
def raiz():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    hoy = _hoy()

    # 🔧 (23 ago 2026, pedido de Israel: "habilita un buscador de cliente
    # por nombre para las notas") Si viene una búsqueda, tiene prioridad
    # sobre las pestañas de período -- busca en TODOS los pedidos, sin
    # importar la fecha de entrega.
    busqueda = (request.args.get("q") or "").strip()
    if busqueda:
        pedidos = database.buscar_pedidos_por_cliente(busqueda)
        return render_template(
            "dashboard.html", pedidos=pedidos, vista="busqueda",
            titulo=f'Resultados para "{busqueda}"', hoy=hoy.isoformat(),
            sin_fecha_reconocida=0, busqueda=busqueda,
        )

    vista = request.args.get("vista", "hoy")

    if vista == "calendario":
        return _vista_calendario(hoy)

    # 🔧 (29 ago 2026, pedido de Israel: "quiero saber que se subió una
    # nota hoy de un pedido que se entregará en noviembre") A diferencia
    # de las demás pestañas (que filtran por FECHA DE ENTREGA), esta
    # ordena por FECHA DE CAPTURA -- para ver el orden real en que van
    # entrando las notas y notar si a alguien se le está acumulando
    # trabajo sin subir, sin importar qué tan lejos entregue cada una.
    if vista == "recientes":
        pedidos = database.listar_pedidos_recientes(limite=60)
        return render_template(
            "dashboard.html", pedidos=pedidos, vista="recientes",
            titulo="Últimas notas capturadas", hoy=hoy.isoformat(),
            sin_fecha_reconocida=0,
        )

    if vista == "hoy":
        desde = hasta = hoy.isoformat()
        titulo = f"Hoy — {hoy.strftime('%d/%m/%Y')}"
    elif vista == "manana":
        manana = hoy + datetime.timedelta(days=1)
        desde = hasta = manana.isoformat()
        titulo = f"Mañana — {manana.strftime('%d/%m/%Y')}"
    elif vista == "semana":
        ini, fin = _rango_semana(hoy)
        desde, hasta = ini.isoformat(), fin.isoformat()
        titulo = f"Esta semana ({ini.strftime('%d/%m')} al {fin.strftime('%d/%m')})"
    elif vista == "mes":
        ini, fin = _rango_mes(hoy)
        desde, hasta = ini.isoformat(), fin.isoformat()
        titulo = f"Este mes ({ini.strftime('%B %Y')})"
    elif vista == "atrasados":
        desde, hasta = None, (hoy - datetime.timedelta(days=1)).isoformat()
        titulo = "Atrasados (fecha de entrega ya pasó y no se ha entregado)"
    elif vista == "todos":
        desde = hasta = None
        titulo = "Todos los pedidos"
    else:
        desde = hasta = hoy.isoformat()
        titulo = f"Hoy — {hoy.strftime('%d/%m/%Y')}"
        vista = "hoy"

    pedidos = database.listar_pedidos(
        fecha_entrega_desde=desde,
        fecha_entrega_hasta=hasta,
        solo_pendientes_entrega=(vista == "atrasados"),
    )
    sin_fecha_reconocida = sum(1 for p in pedidos if not p.get("fecha_entrega_iso")) if vista == "todos" else 0
    return render_template(
        "dashboard.html", pedidos=pedidos, vista=vista, titulo=titulo, hoy=hoy.isoformat(),
        sin_fecha_reconocida=sin_fecha_reconocida,
    )


def _vista_calendario(hoy):
    """🔧 (23 ago 2026, pedido de Israel: "que haya un calendario del mes
    y poder ver qué se entregará o entregó en días atrás") A diferencia
    de Hoy/Mañana/Semana/Mes, aquí se puede navegar mes a mes (pasado o
    futuro) y darle clic a CUALQUIER día para ver justo ese día -- sin
    quedar limitado a los rangos fijos de las otras pestañas."""
    try:
        anio = int(request.args.get("anio", hoy.year))
        mes = int(request.args.get("mes", hoy.month))
    except (TypeError, ValueError):
        anio, mes = hoy.year, hoy.month
    anio, mes = _normalizar_anio_mes(anio, mes)

    primer_dia = datetime.date(anio, mes, 1)
    ultimo_dia_num = calendario_mod.monthrange(anio, mes)[1]
    ultimo_dia = datetime.date(anio, mes, ultimo_dia_num)

    pedidos_del_mes = database.listar_pedidos(
        fecha_entrega_desde=primer_dia.isoformat(), fecha_entrega_hasta=ultimo_dia.isoformat(),
    )
    conteo_por_dia = {}
    for p in pedidos_del_mes:
        f = p.get("fecha_entrega_iso")
        if f:
            conteo_por_dia[f] = conteo_por_dia.get(f, 0) + 1

    fecha_str = request.args.get("fecha")
    fecha_sel = None
    if fecha_str:
        try:
            fecha_sel = datetime.date.fromisoformat(fecha_str)
        except ValueError:
            fecha_sel = None

    pedidos = []
    titulo = f"Calendario — {MESES_ES[mes].capitalize()} {anio}"
    if fecha_sel:
        pedidos = database.listar_pedidos(
            fecha_entrega_desde=fecha_sel.isoformat(), fecha_entrega_hasta=fecha_sel.isoformat(),
        )
        titulo = f"Entregas del {fecha_sel.strftime('%d/%m/%Y')}"

    semanas = calendario_mod.Calendar(firstweekday=0).monthdayscalendar(anio, mes)
    mes_ant_anio, mes_ant_mes = _normalizar_anio_mes(anio, mes - 1)
    mes_sig_anio, mes_sig_mes = _normalizar_anio_mes(anio, mes + 1)

    return render_template(
        "dashboard.html", pedidos=pedidos, vista="calendario", titulo=titulo, hoy=hoy.isoformat(),
        sin_fecha_reconocida=0,
        cal_semanas=semanas, cal_anio=anio, cal_mes=mes, cal_dias_semana=DIAS_SEMANA_ES,
        cal_conteo=conteo_por_dia, cal_fecha_sel=fecha_sel.isoformat() if fecha_sel else None,
        cal_mes_ant=(mes_ant_anio, mes_ant_mes), cal_mes_sig=(mes_sig_anio, mes_sig_mes),
    )


@app.route("/pedido/<int:pedido_id>/estatus", methods=["POST"])
def cambiar_estatus(pedido_id):
    campo = request.form.get("campo")
    valor = request.form.get("valor")
    if campo not in ("estatus_fabricacion", "estatus_entrega"):
        flash("Campo de estatus inválido.")
        return redirect(request.referrer or url_for("dashboard"))
    database.actualizar_estatus(pedido_id, campo, valor)
    return redirect(request.referrer or url_for("dashboard"))


# ----------------------------------------------------------------------
# Vista financiera
# ----------------------------------------------------------------------
@app.route("/finanzas")
def finanzas():
    periodo = request.args.get("periodo", "hoy")
    hoy = _hoy()
    nav_anterior = nav_siguiente = nav_actual = None
    es_actual = True

    # 🔧 (23 ago 2026, pedido de Israel: "aquí no es el mismo caso que se
    # pierde al terminar el mes?" -- mismo hueco que ya se corrigió en
    # Comisiones) Antes "Semana"/"Mes" siempre eran el período ACTUAL, sin
    # forma de consultar los totales de un mes/semana ya cerrado. Ahora se
    # navega igual que en Comisiones y en el calendario de Entregas.
    if periodo == "semana":
        try:
            inicio_qs = request.args.get("inicio")
            inicio_pedido = datetime.date.fromisoformat(inicio_qs) if inicio_qs else hoy
        except ValueError:
            inicio_pedido = hoy
        ini, fin = _rango_semana(inicio_pedido)
        es_actual = (ini == _rango_semana(hoy)[0])
        if es_actual:
            titulo = f"Esta semana ({ini.strftime('%d/%m')} al {fin.strftime('%d/%m')})"
        else:
            titulo = f"Semana del {ini.strftime('%d/%m')} al {fin.strftime('%d/%m/%Y')}"
        nav_anterior = {"periodo": "semana", "inicio": (ini - datetime.timedelta(days=7)).isoformat()}
        nav_siguiente = {"periodo": "semana", "inicio": (ini + datetime.timedelta(days=7)).isoformat()}
        nav_actual = {"periodo": "semana", "inicio": _rango_semana(hoy)[0].isoformat()}
    elif periodo == "mes":
        try:
            anio = int(request.args.get("anio", hoy.year))
            mes = int(request.args.get("mes", hoy.month))
        except (TypeError, ValueError):
            anio, mes = hoy.year, hoy.month
        anio, mes = _normalizar_anio_mes(anio, mes)
        ini = datetime.date(anio, mes, 1)
        fin = datetime.date(anio, mes, calendario_mod.monthrange(anio, mes)[1])
        es_actual = (anio == hoy.year and mes == hoy.month)
        titulo = f"{'Este mes' if es_actual else 'Mes'} ({MESES_ES[mes].capitalize()} {anio})"
        anio_ant, mes_ant = _normalizar_anio_mes(anio, mes - 1)
        anio_sig, mes_sig = _normalizar_anio_mes(anio, mes + 1)
        nav_anterior = {"periodo": "mes", "anio": anio_ant, "mes": mes_ant}
        nav_siguiente = {"periodo": "mes", "anio": anio_sig, "mes": mes_sig}
        nav_actual = {"periodo": "mes", "anio": hoy.year, "mes": hoy.month}
    else:
        ini = fin = hoy
        titulo = f"Hoy — {hoy.strftime('%d/%m/%Y')}"
        periodo = "hoy"

    pedidos = database.listar_capturados_en_rango(ini.isoformat(), fin.isoformat())
    total_anticipos = round(sum(p.get("anticipo") or 0 for p in pedidos), 2)
    total_ventas = round(sum(p.get("total") or 0 for p in pedidos), 2)
    total_saldos = round(sum(p.get("saldo") or 0 for p in pedidos), 2)

    # 🔧 (24 ago 2026, pedido de Israel: "resumen de ventas por mes, que se
    # actualice al momento de subir notas") Aparte del total del período
    # que se esté viendo (Hoy/Semana/Mes), siempre se calcula también el
    # del MES ACTUAL para la tarjeta fija de arriba -- así se ve de
    # entrada sin tener que cambiarle al tab "Mes". Se recalcula desde
    # cero en cada visita a la página (no se guarda en ningún lado), así
    # que en cuanto se sube o se corrige una nota, la próxima vez que se
    # entre aquí ya sale actualizado.
    ini_mes_actual, fin_mes_actual = _rango_mes(hoy)
    pedidos_mes_actual = database.listar_capturados_en_rango(ini_mes_actual.isoformat(), fin_mes_actual.isoformat())
    resumen_venta_mes = round(sum(p.get("total") or 0 for p in pedidos_mes_actual), 2)

    return render_template(
        "finanzas.html", pedidos=pedidos, periodo=periodo, titulo=titulo,
        total_anticipos=total_anticipos, total_ventas=total_ventas, total_saldos=total_saldos,
        resumen_venta_mes=resumen_venta_mes, resumen_titulo_mes=f"{MESES_ES[hoy.month].capitalize()} {hoy.year}",
        nav_anterior=nav_anterior, nav_siguiente=nav_siguiente, nav_actual=nav_actual, es_actual=es_actual,
    )


# ----------------------------------------------------------------------
# Comisiones (23 ago 2026, pedido de Israel: "$1 por producto que se
# vende" -- aparte de su sueldo, a Diana le toca $1 por cada pieza
# vendida. Diana entra y ve la suya; Israel/Dalia también la pueden
# consultar para saber cuánto pagarle.)
#
# 🔧 (23 ago 2026, aclaración de Israel: "la comisión de Diana solo debe
# ser de productos, no por pedido urgente ni por envío a domicilio") El
# prompt de extracción ya le pide a la IA que NUNCA meta el envío/urgencia
# como si fueran un producto -- pero, como en el resto del código, no se
# confía ciegamente en que la IA lo respete: aquí se vuelve a filtrar en
# código cualquier renglón que se parezca a un cargo de envío/urgencia
# antes de contar piezas para la comisión.
# ----------------------------------------------------------------------
PALABRAS_NO_PRODUCTO = ("envio", "envío", "domicilio", "flete", "urgente", "urgencia")


def _es_producto_real(nombre_producto):
    """False si el renglón claramente no es mercancía (envío, urgencia),
    aunque haya quedado guardado por error dentro de 'productos'."""
    nombre = (nombre_producto or "").strip().lower()
    if not nombre:
        return False
    return not any(palabra in nombre for palabra in PALABRAS_NO_PRODUCTO)


def _total_piezas_y_comision(pedidos_de_vendedor, monto_por_producto):
    """Suma las piezas 'reales' (ver _es_producto_real) de una lista de
    pedidos ya filtrada a una sola vendedora, y calcula la comisión."""
    total_piezas = 0.0
    for p in pedidos_de_vendedor:
        for prod in (p.get("productos") or []):
            if not _es_producto_real(prod.get("producto")):
                continue
            try:
                total_piezas += float(prod.get("cantidad") or 0)
            except (TypeError, ValueError):
                pass
    return total_piezas, round(total_piezas * monto_por_producto, 2)


@app.route("/comisiones")
def comisiones():
    usuario = session.get("usuario")
    if not VENDEDORES_CON_COMISION:
        flash("No hay comisiones configuradas todavía.")
        return redirect(url_for("dashboard"))

    # 🔧 (24 ago 2026, pedido de Israel: "que cada vendedora pueda ver las
    # comisiones de los demás") ANTES: quien tenía su propia comisión
    # (Dalia/Diana) solo podía ver la suya -- era privacidad a propósito
    # entre vendedoras. Israel confirmó que ya no quiere esa restricción:
    # ahora cualquiera que entre (tenga o no comisión propia) puede
    # cambiar de vendedora con el selector de abajo y ver la comisión de
    # cualquiera de las 3.
    vendedor = request.args.get("vendedor") or usuario or next(iter(VENDEDORES_CON_COMISION))
    if vendedor not in VENDEDORES_CON_COMISION:
        vendedor = usuario if usuario in VENDEDORES_CON_COMISION else next(iter(VENDEDORES_CON_COMISION))

    monto_por_producto = VENDEDORES_CON_COMISION[vendedor]
    # 🔧 (24 ago 2026, pedido de Israel: "debe verse el acumulado de
    # comisiones por mes") Antes el default al entrar era "Semana". Ahora
    # es "Mes" -- se puede seguir cambiando a semana con el tab de arriba,
    # pero lo que se ve de entrada ya es el acumulado del mes.
    periodo = request.args.get("periodo", "mes")
    hoy = _hoy()

    # 🔧 (23 ago 2026, pedido de Israel: "si cambia de mes, ¿podemos ver
    # las comisiones del mes anterior si no se le han pagado?") Antes
    # "Semana"/"Mes" siempre eran la semana/mes ACTUAL, sin forma de ver
    # atrás. Ahora se puede navegar período por período (como ya se podía
    # en el calendario de Entregas), para poder cuadrar un pago aunque ya
    # haya cambiado la semana o el mes.
    if periodo == "mes":
        try:
            anio = int(request.args.get("anio", hoy.year))
            mes = int(request.args.get("mes", hoy.month))
        except (TypeError, ValueError):
            anio, mes = hoy.year, hoy.month
        anio, mes = _normalizar_anio_mes(anio, mes)
        ini = datetime.date(anio, mes, 1)
        fin = datetime.date(anio, mes, calendario_mod.monthrange(anio, mes)[1])
        es_actual = (anio == hoy.year and mes == hoy.month)
        titulo_periodo = f"{'Este mes' if es_actual else 'Mes'} ({MESES_ES[mes].capitalize()} {anio})"
        anio_ant, mes_ant = _normalizar_anio_mes(anio, mes - 1)
        anio_sig, mes_sig = _normalizar_anio_mes(anio, mes + 1)
        nav_anterior = {"periodo": "mes", "anio": anio_ant, "mes": mes_ant}
        nav_siguiente = {"periodo": "mes", "anio": anio_sig, "mes": mes_sig}
        nav_actual = {"periodo": "mes", "anio": hoy.year, "mes": hoy.month}
    else:
        periodo = "semana"
        try:
            inicio_qs = request.args.get("inicio")
            inicio_pedido = datetime.date.fromisoformat(inicio_qs) if inicio_qs else hoy
        except ValueError:
            inicio_pedido = hoy
        ini, fin = _rango_semana(inicio_pedido)
        es_actual = (ini == _rango_semana(hoy)[0])
        if es_actual:
            titulo_periodo = f"Esta semana ({ini.strftime('%d/%m')} al {fin.strftime('%d/%m')})"
        else:
            titulo_periodo = f"Semana del {ini.strftime('%d/%m')} al {fin.strftime('%d/%m/%Y')}"
        nav_anterior = {"periodo": "semana", "inicio": (ini - datetime.timedelta(days=7)).isoformat()}
        nav_siguiente = {"periodo": "semana", "inicio": (ini + datetime.timedelta(days=7)).isoformat()}
        nav_actual = {"periodo": "semana", "inicio": _rango_semana(hoy)[0].isoformat()}

    # 🔧 (24 ago 2026, pedido de Israel: "quiero que algo revise a quién
    # pertenecen las comisiones de cada quién") Antes se filtraba en SQL
    # por subido_por. Ahora se trae TODO lo capturado en el período (sin
    # importar quién lo subió) y se separa aquí por el prefijo del folio
    # -- ver vendedora_por_folio() arriba. Las notas sin folio reconocido
    # (folio vacío o con un prefijo que no es de nadie) NO se le suman a
    # ninguna comisión por default -- se cuentan aparte para que Israel
    # las vea y las corrija (deberían haber quedado marcadas con
    # necesita_revision desde que se subieron, ver _revisar_calidad).
    todos_en_rango = database.listar_capturados_en_rango(ini.isoformat(), fin.isoformat())
    pedidos = [p for p in todos_en_rango if vendedora_por_folio(p.get("folio")) == vendedor]
    sin_folio_reconocido = sum(1 for p in todos_en_rango if vendedora_por_folio(p.get("folio")) is None)

    total_piezas, total_comision = _total_piezas_y_comision(pedidos, monto_por_producto)

    # 🔧 (24 ago 2026, pedido de Israel: "debe aparecer, comisiones dalia,
    # diana y karo") Todos ven de un vistazo el total de las 3 -- ya no hay
    # que ir cambiando de una en una con el selector para comparar.
    # 🔧 (24 ago 2026, pedido de Israel: quitar la privacidad entre
    # vendedoras) Antes esta comparación solo se armaba para quien NO
    # tuviera comisión propia (Israel). Ahora se arma siempre, para
    # cualquiera que entre -- Dalia y Diana también pueden ver aquí la
    # comisión de las otras dos.
    resumen_vendedores = []
    for v, monto_v in VENDEDORES_CON_COMISION.items():
        pedidos_v = [p for p in todos_en_rango if vendedora_por_folio(p.get("folio")) == v]
        piezas_v, comision_v = _total_piezas_y_comision(pedidos_v, monto_v)
        resumen_vendedores.append({
            "vendedor": v,
            "nombre": NOMBRES_DISPLAY.get(v, v.title()),
            "piezas": piezas_v,
            "comision": comision_v,
            "activo": v == vendedor,
        })

    return render_template(
        "comisiones.html", pedidos=pedidos, periodo=periodo, titulo_periodo=titulo_periodo,
        vendedor=vendedor, nombre_vendedor=NOMBRES_DISPLAY.get(vendedor, vendedor.title() if vendedor else ""),
        monto_por_producto=monto_por_producto, total_piezas=total_piezas, total_comision=total_comision,
        es_propia=(usuario == vendedor),
        otros_vendedores=[v for v in VENDEDORES_CON_COMISION if v != vendedor],
        resumen_vendedores=resumen_vendedores,
        nav_anterior=nav_anterior, nav_siguiente=nav_siguiente, nav_actual=nav_actual, es_actual=es_actual,
        sin_folio_reconocido=sin_folio_reconocido,
    )


# ----------------------------------------------------------------------
# Ventas por producto (24 ago 2026, pedido de Israel: "productos vendidos
# del mes" + "añadir ventas, con la imagen de los productos + el %
# porcentaje de ventas del mes"). Mismo criterio de privacidad que
# Finanzas -- ver exigir_login().
#
# 🔧 (24 ago 2026, pedido de Israel, con las fotos de catalogo_2026.pdf)
# Israel aclaró que agrupar por el nombre EXACTO que escribió la
# vendedora (como se hacía antes) está mal para "osito con jaboncito":
# cada nota lo describe distinto -- color de toalla, color/forma de
# moño, con o sin jabón, jabón doble, inicial chica/grande -- y eso
# fragmentaba lo que en realidad es UN solo producto en decenas de
# renglones diferentes. Su instrucción textual: "un osito con jaboncito
# (puede ser de cualquier color de toalla y de cualquier color o forma
# de moño y jabón, o no llevar jabón)" cuenta como UNA sola cosa. Los
# demás productos del catálogo si se diferencian entre sí (por nombre e
# imagen), como pidió.
#
# CATALOGO_PRODUCTOS es una lista ORDENADA de reglas -- gana la primera
# que haga match (mismo principio que PREFIJOS_FOLIO_VENDEDORA arriba:
# el orden importa). Los animalitos/artículos específicos van primero;
# el "osito de toalla" genérico queda AL FINAL como cajón de sastre,
# para que cualquier variante de color/moño/jabón que no matcheó nada
# más específico caiga ahí -- exactamente el comportamiento que pidió
# Israel. "rosa" (la flor) sólo cuenta si el renglón no menciona
# oso/osito, para no confundirla con "OSITO DE TOALLA ROSA" (rosa como
# color, no como flor).
def _normalizar_texto(txt):
    txt = (txt or "").lower()
    for a, b in (("á", "a"), ("é", "e"), ("í", "i"), ("ó", "o"), ("ú", "u"), ("ü", "u")):
        txt = txt.replace(a, b)
    return txt


# 🐻 (24 ago 2026) "oso" NO es substring de "osito"/"ositos" (o-s-i-t-o
# no trae "o-s-o" seguido) -- un bug real que se coló al escribir el
# cajón de sastre de abajo con `"oso" in t` a secas, y que en producción
# dejaba "Osito de toalla con jabón" separado de "Osito de toalla" en
# vez de colapsarlos como pidió Israel. _es_osito() cubre el diminutivo
# ("osito"/"ositos", lo que casi siempre escriben las vendedoras) y
# también "oso"/"osos" como palabra completa (para no atrapar "oso" como
# parte de otra palabra que no tenga nada que ver).
def _es_osito(t):
    return "osito" in t or "ositos" in t or bool(re.search(r"\boso(s)?\b", t))


CATALOGO_PRODUCTOS = [
    # (slug_imagen, nombre_canonico, precio_referencia, funcion_de_match)
    ("osito-de-toalla-afelpada", "Osito de toalla afelpada", 18.00, lambda t: "afelpad" in t),
    ("unicornios-de-toalla", "Unicornios de toalla", 14.00, lambda t: "unicornio" in t),
    ("perrito-de-toalla", "Perrito de toalla", 13.00, lambda t: "perrit" in t or "perrito" in t or re.search(r"\bperro\b", t)),
    ("leoncito-de-toalla", "Leoncito de toalla", 14.00, lambda t: "leon" in t),
    ("jirafa-de-toalla", "Jirafa de toalla", 16.00, lambda t: "jirafa" in t),
    ("elefantitos-de-toalla", "Elefantitos de toalla", 14.00, lambda t: "elefant" in t),
    ("mariposa-de-toalla", "Mariposa de toalla", 14.50, lambda t: "mariposa" in t),
    ("buho-de-toalla", "Búho de toalla", 14.00, lambda t: "buho" in t),
    ("conejo-de-toalla", "Conejo de toalla", 13.50, lambda t: "conejo" in t),
    ("caballo-de-toalla", "Caballo de toalla", 15.00, lambda t: "caballo" in t),
    ("kit-osito-oracion-y-velita", "Kit Osito Oración y Velita", 21.00,
        lambda t: "kit" in t and ("oracion" in t or "velita" in t)),
    ("recuerdo-de-oracion-con-decenario", "Recuerdos de Oración con Decenario", 15.00,
        lambda t: "decenario" in t),
    ("oracion-con-velita", "Oración con Velita", 10.00,
        lambda t: "oracion" in t and "velita" in t),
    ("vela-de-toalla", "Vela de toalla", 12.00, lambda t: "vela" in t or "velita" in t),
    ("destapador-corcholata", "Destapador Corcholata", 15.50,
        lambda t: "destapador" in t or "corcholata" in t),
    ("encendedores-personalizados", "Encendedores personalizados", 10.00, lambda t: "encendedor" in t),
    ("abanico-tipo-madera", "Abanico tipo madera", 23.00, lambda t: "abanico" in t),
    ("espejito-de-recuerdito", "Espejito de recuerdito", 14.00,
        lambda t: "espejo" in t or "espejito" in t),
    ("llavero-osito-peluche", "Llavero Osito Peluche", 18.00,
        lambda t: "llavero" in t or "peluche" in t),
    ("rosa-de-toalla", "Rosa de toalla", 15.00, lambda t: "rosa" in t and not _es_osito(t)),
    # 🔧 Cajón de sastre: CUALQUIER variante de "osito"/"oso" de toalla
    # (color, moño, con o sin jabón, inicial) que no haya matcheado nada
    # más arriba cae aquí, como UN solo producto -- ver nota de arriba.
    ("osito-de-toalla", "Osito de toalla", 12.00, lambda t: _es_osito(t)),
]


def _clasificar_producto(nombre_producto):
    """Regresa (nombre_canonico, precio_referencia_catalogo, slug_imagen)
    para un renglón de producto de una nota. Si no matchea ninguna regla
    del catálogo, se deja tal cual lo escribió la vendedora -- no todo
    lo que se vende viene del catálogo (ej. gel antibacterial, dominó),
    y forzarlo a una categoría inventada estaría peor que dejarlo suelto."""
    texto = _normalizar_texto(nombre_producto)
    for slug, nombre_canonico, precio_ref, coincide in CATALOGO_PRODUCTOS:
        if coincide(texto):
            return nombre_canonico, precio_ref, slug
    nombre_propio = (nombre_producto or "Producto sin nombre").strip() or "Producto sin nombre"
    slug_propio = re.sub(r"[^a-z0-9]+", "-", _normalizar_texto(nombre_propio)).strip("-")
    return nombre_propio, None, slug_propio


def _imagen_producto(slug):
    """🔧 (24 ago 2026) Busca static/productos/<slug>.(jpg/jpeg/png/webp).
    Las fotos de los productos del catálogo ya vienen incluidas (se
    sacaron de catalogo_2026.pdf); si algún producto nuevo no tiene
    imagen todavía, regresa None y la plantilla muestra un ícono
    genérico en su lugar -- sin romper nada."""
    if not slug:
        return None
    for ext in ("jpg", "jpeg", "png", "webp"):
        ruta = os.path.join(app.static_folder, "productos", f"{slug}.{ext}")
        if os.path.isfile(ruta):
            return url_for("static", filename=f"productos/{slug}.{ext}")
    return None


@app.route("/ventas")
def ventas():
    hoy = _hoy()
    try:
        anio = int(request.args.get("anio", hoy.year))
        mes = int(request.args.get("mes", hoy.month))
    except (TypeError, ValueError):
        anio, mes = hoy.year, hoy.month
    anio, mes = _normalizar_anio_mes(anio, mes)
    ini = datetime.date(anio, mes, 1)
    fin = datetime.date(anio, mes, calendario_mod.monthrange(anio, mes)[1])
    es_actual = (anio == hoy.year and mes == hoy.month)
    titulo = f"{'Este mes' if es_actual else 'Mes'} ({MESES_ES[mes].capitalize()} {anio})"
    anio_ant, mes_ant = _normalizar_anio_mes(anio, mes - 1)
    anio_sig, mes_sig = _normalizar_anio_mes(anio, mes + 1)
    nav_anterior = {"anio": anio_ant, "mes": mes_ant}
    nav_siguiente = {"anio": anio_sig, "mes": mes_sig}
    nav_actual = {"anio": hoy.year, "mes": hoy.month}

    pedidos = database.listar_capturados_en_rango(ini.isoformat(), fin.isoformat())

    # 🔧 (24 ago 2026) Se agrupa por nombre de producto igual que en
    # imprimir_semana_proxima(), para sumar piezas vendidas. El % se
    # calcula sobre PIEZAS y no sobre dinero: no todas las notas traen
    # precio_unitario en cada línea (depende de qué tan detallada quedó
    # la nota), así que un % basado en $ dejaría fuera ventas reales solo
    # porque falta ese dato -- mismo principio que el resto del código:
    # nunca dejar que un dato faltante invente o esconda información.
    # Cuando SÍ hay precio_unitario en todas las líneas de un producto,
    # también se muestra su total en dinero, aparte, para no mezclar un
    # número confiable con uno estimado.
    # 🔧 (24 ago 2026, corrección de Israel: "está mal... un osito con
    # jaboncito puede ser de cualquier color de toalla y de cualquier
    # color o forma de moño y jabón, o no llevar jabón" -- todas esas
    # variantes cuentan como UN producto) Antes se agrupaba por el texto
    # EXACTO de la nota, así que "Osito rosa con jabón" y "OSITO AZUL
    # SIN JABON" salían como renglones distintos. Ahora se agrupa por el
    # nombre CANÓNICO del catálogo (_clasificar_producto), que colapsa
    # todas las variantes de un mismo producto en un solo renglón con su
    # foto real del catálogo.
    resumen = {}
    for p in pedidos:
        for prod in (p.get("productos") or []):
            if not _es_producto_real(prod.get("producto")):
                continue
            nombre_canonico, precio_ref, slug = _clasificar_producto(prod.get("producto"))
            try:
                cantidad = float(prod.get("cantidad") or 0)
            except (TypeError, ValueError):
                cantidad = 0
            entrada = resumen.setdefault(
                nombre_canonico, {"piezas": 0.0, "monto": 0.0, "con_precio": True, "slug": slug}
            )
            entrada["piezas"] += cantidad
            precio = prod.get("precio_unitario")
            if precio not in (None, ""):
                try:
                    entrada["monto"] += cantidad * float(precio)
                except (TypeError, ValueError):
                    entrada["con_precio"] = False
            else:
                entrada["con_precio"] = False

    total_piezas_mes = sum(e["piezas"] for e in resumen.values())
    productos = []
    for nombre, e in resumen.items():
        pct = round((e["piezas"] / total_piezas_mes) * 100, 1) if total_piezas_mes else 0
        productos.append({
            "nombre": nombre,
            "piezas": e["piezas"],
            "porcentaje": pct,
            "monto": round(e["monto"], 2) if e["con_precio"] else None,
            "imagen": _imagen_producto(e["slug"]),
        })
    productos.sort(key=lambda x: x["piezas"], reverse=True)

    return render_template(
        "ventas.html", productos=productos, titulo=titulo, total_piezas_mes=total_piezas_mes,
        nav_anterior=nav_anterior, nav_siguiente=nav_siguiente, nav_actual=nav_actual, es_actual=es_actual,
    )


# ----------------------------------------------------------------------
# Indicadores (24 ago 2026, pedido de Israel: "nueva sección -- venta
# promedio por día, venta promedio por semana, venta mensual"). Mismo
# criterio de privacidad que Finanzas -- ver exigir_login().
# ----------------------------------------------------------------------
@app.route("/indicadores")
def indicadores():
    hoy = _hoy()
    ini_mes, fin_mes = _rango_mes(hoy)
    pedidos_mes = database.listar_capturados_en_rango(ini_mes.isoformat(), fin_mes.isoformat())
    venta_mensual = round(sum(p.get("total") or 0 for p in pedidos_mes), 2)

    # 🔧 (24 ago 2026) Los promedios se calculan sobre los días QUE YA
    # PASARON del mes actual (no sobre los 28-31 días del mes completo) --
    # si no, a inicios de mes el promedio saldría artificialmente bajo,
    # como si ya se supiera que no se va a vender nada el resto del mes.
    dias_transcurridos = (hoy - ini_mes).days + 1
    promedio_diario_exacto = (venta_mensual / dias_transcurridos) if dias_transcurridos else 0.0
    venta_promedio_dia = round(promedio_diario_exacto, 2)
    venta_promedio_semana = round(promedio_diario_exacto * 7, 2)

    return render_template(
        "indicadores.html",
        venta_mensual=venta_mensual, venta_promedio_dia=venta_promedio_dia,
        venta_promedio_semana=venta_promedio_semana, dias_transcurridos=dias_transcurridos,
        titulo_mes=f"{MESES_ES[hoy.month].capitalize()} {hoy.year}",
    )


# ----------------------------------------------------------------------
# Lista imprimible de una semana (23 ago 2026, pedido de Israel: poder
# imprimir el viernes/sábado todo lo que se entrega la próxima semana,
# con un resumen de cuánto fabricar de cada producto; 25 ago 2026,
# pedido de Israel: "ahora quiero que se pueda imprimir la semana
# actual de pedidos" -- misma lista, pero para lo que ya se debe estar
# fabricando/entregando ESTA semana, no la que sigue). Las dos vistas
# comparten toda la lógica -- solo cambia el rango de fechas y el
# título -- así que se factorizó en un solo helper para no duplicar
# nada (y que un futuro ajuste al reporte aplique a ambas por igual).
# ----------------------------------------------------------------------
def _imprimir_semana(ini, fin, titulo):
    pedidos = database.listar_pedidos(fecha_entrega_desde=ini.isoformat(), fecha_entrega_hasta=fin.isoformat())

    resumen = {}
    for p in pedidos:
        for prod in (p.get("productos") or []):
            if not _es_producto_real(prod.get("producto")):
                continue
            nombre = (prod.get("producto") or "Producto sin nombre").strip() or "Producto sin nombre"
            try:
                cantidad = float(prod.get("cantidad") or 0)
            except (TypeError, ValueError):
                cantidad = 0
            resumen[nombre] = resumen.get(nombre, 0) + cantidad
    resumen_ordenado = sorted(resumen.items(), key=lambda item: item[0].lower())

    return render_template(
        "imprimir_semana.html", pedidos=pedidos, resumen=resumen_ordenado,
        ini=ini, fin=fin, generado=database.ahora_negocio(), titulo=titulo,
    )


@app.route("/imprimir/semana-actual")
def imprimir_semana_actual():
    ini, fin = _rango_semana(_hoy())
    return _imprimir_semana(ini, fin, "Pedidos a entregar esta semana")


@app.route("/imprimir/semana-proxima")
def imprimir_semana_proxima():
    ini_semana_actual, _ = _rango_semana(_hoy())
    ini = ini_semana_actual + datetime.timedelta(days=7)
    fin = ini + datetime.timedelta(days=6)
    return _imprimir_semana(ini, fin, "Pedidos a entregar la próxima semana")


# ----------------------------------------------------------------------
# Inventario de materia prima (23 ago 2026, pedido de Israel)
# ----------------------------------------------------------------------
@app.route("/inventario")
def inventario():
    items = database.listar_materia_prima()
    return render_template("inventario.html", items=items)


@app.route("/inventario/nuevo", methods=["POST"])
def inventario_nuevo():
    nombre = (request.form.get("nombre") or "").strip()
    if not nombre:
        flash("Ponle un nombre al material.")
        return redirect(url_for("inventario"))
    cantidad = request.form.get("cantidad") or 0
    unidad = (request.form.get("unidad") or "").strip() or "pza"
    database.crear_materia_prima(nombre, cantidad, unidad)
    flash(f"'{nombre}' agregado al inventario.")
    return redirect(url_for("inventario"))


@app.route("/inventario/<int:item_id>/editar", methods=["POST"])
def inventario_editar(item_id):
    item = database.obtener_materia_prima(item_id)
    if not item:
        flash("Ese material ya no existe.")
        return redirect(url_for("inventario"))
    nombre = (request.form.get("nombre") or "").strip() or item["nombre"]
    cantidad = request.form.get("cantidad")
    if cantidad is None or cantidad == "":
        cantidad = item["cantidad"]
    unidad = (request.form.get("unidad") or "").strip() or item["unidad"]
    database.actualizar_materia_prima(item_id, nombre, cantidad, unidad)
    flash(f"'{nombre}' actualizado.")
    return redirect(url_for("inventario"))


@app.route("/inventario/<int:item_id>/eliminar", methods=["POST"])
def inventario_eliminar(item_id):
    database.eliminar_materia_prima(item_id)
    flash("Material eliminado del inventario.")
    return redirect(url_for("inventario"))


# ----------------------------------------------------------------------
# Subir nota -> IA extrae datos -> humano confirma
# ----------------------------------------------------------------------
# 🔧 (24 ago 2026, pedido de Israel: "si las notas en la fecha no tienen
# año... que el programa entienda que son del año en curso") Antes este
# prompt era un texto fijo que le pedía a la IA "deducir" el año sin
# darle ninguna fecha de referencia -- sin saber qué día es hoy, a veces
# terminaba alucinando un año viejo sin sentido (2020, 2023) en vez de
# usar el año actual. Ahora es una función que arma el prompt cada vez
# con la fecha de HOY (hora de negocio, no la del servidor en UTC) como
# referencia explícita, y con el campo "folio" agregado -- necesario para
# saber a quién le corresponde la comisión de esa venta (ver
# vendedora_por_folio() arriba).
def _prompt_extraccion():
    hoy = database.hoy_negocio()
    return f"""Eres un asistente que lee notas de pedidos de una tienda mexicana de \
recuerdos para eventos (Recuerditos Dalia: ositos de toalla, jaboncitos, \
abanicos, dominós, etc. para baby showers, XV años, bodas, etc.).

Hoy es {hoy.strftime('%d/%m/%Y')}. Usa esta fecha como referencia para \
completar años que falten -- NUNCA un año de tu entrenamiento ni uno \
inventado (ver regla de "fecha_entrega" más abajo).

Se te va a mostrar una foto de una nota de pedido YA CONFIRMADA con el \
cliente (normalmente escrita a mano o en una nota de WhatsApp con el \
resumen del pedido, colores, fecha de entrega y el anticipo pagado).

Lee la nota con MUCHO cuidado -- revisa cada número y cada letra dos veces \
antes de contestar (cantidades, precios, anticipo, total, folio). Estos \
datos alimentan directamente lo que se fabrica, se cobra, y a quién se le \
paga de comisión, así que un error aquí es un error real en el negocio, \
no un detalle menor.

Tu trabajo es leer la nota y devolver ÚNICAMENTE un JSON con esta forma \
exacta (sin texto extra, sin explicaciones):

{{
  "folio": "el folio o número de nota tal como está escrito (ej. D-142, DE-014, K-023), o null si no aparece",
  "cliente": "nombre del cliente o null si no aparece",
  "telefono": "teléfono o null",
  "municipio": "municipio/ciudad de entrega o null",
  "fecha_entrega": "fecha de entrega en formato DD/MM/AAAA. Si la nota indica día y mes pero NO el año, completa con el año actual ({hoy.year}) -- nunca otro año. Si de plano no hay fecha legible, entonces sí null.",
  "tipo_entrega": "uno de: domicilio, local, punto_de_entrega, dhl -- o null si no está claro",
  "direccion": "dirección exacta SOLO si aparece escrita en la nota, si no null",
  "productos": [
    {{"producto": "nombre del producto", "cantidad": numero, "colores": "descripción breve de colores/detalles", "precio_unitario": numero_o_null}}
  ],
  "anticipo": numero_o_null,
  "total": numero_o_null,
  "notas": "cualquier detalle extra relevante (tarjetita, urgente, etc.) o null",
  "necesita_revision": true_o_false,
  "motivo_revision": "explicación breve y concreta de qué no quedó claro, o null si no necesita revisión"
}}

Reglas importantes:
- "folio" determina a quién le corresponde la comisión de esta venta -- léelo letra por letra, con mucho cuidado. Si no lo alcanzas a leer con certeza, ponlo null y NO adivines ni completes un folio a medias.
- "productos" es SOLO para mercancía física que se fabrica y se entrega (ositos, jaboncitos, abanicos, dominós, etc.). El cobro de envío a domicilio y el cargo por pedido urgente NO son productos -- nunca los pongas como un renglón de "productos", aunque la nota los liste junto a los productos. Van reflejados nada más en el "total" y, si quieres anotarlos, en "notas" (ej. "incluye $100 de envío a domicilio", "es urgente").
- Si un dato no aparece claramente en la nota, pon null (o lista vacía para productos) -- NO inventes ni adivines.
- "cantidad" y los montos deben ser números (sin signo de $ ni comas), nunca texto.
- Marca "necesita_revision": true si ocurre CUALQUIERA de estas cosas: falta el folio o no se alcanza a leer con certeza, falta el nombre del cliente, no hay ningún producto legible, algún producto no tiene cantidad clara, falta la fecha de entrega, la letra o la foto están borrosas en alguna parte importante, el total no cuadra aproximadamente con la suma de los productos (déjale margen por envío/redondeo), o simplemente no estás seguro de algo relevante.
- Si TODOS los datos esenciales (folio, cliente, al menos un producto con cantidad, fecha de entrega, y montos) se leyeron claros y consistentes, marca "necesita_revision": false.
- Ante la duda, marca necesita_revision: true -- es preferible que un humano la revise de más a que se guarde un dato incorrecto.
"""


MAX_FOTOS_POR_LOTE = 100


@app.route("/subir", methods=["GET", "POST"])
def subir():
    if request.method == "GET":
        return render_template("subir.html")

    archivos = [a for a in request.files.getlist("fotos") if a and a.filename]
    if not archivos:
        flash("Selecciona al menos una foto de nota primero.")
        return redirect(url_for("subir"))

    if len(archivos) > MAX_FOTOS_POR_LOTE:
        flash(f"Mejor sube máximo {MAX_FOTOS_POR_LOTE} fotos por tanda -- divide el resto en otra subida.")
        return redirect(url_for("subir"))

    # 🔧 (23 ago 2026, pedido de Israel: comisiones de Diana confiables)
    # Antes esto era un campo de texto libre ("¿quién sube esta tanda?").
    # Ahora que cada quien entra con su propia contraseña, se toma
    # directo de la sesión -- ya no puede haber typos ni notas
    # atribuidas a la persona equivocada.
    subido_por = NOMBRES_DISPLAY.get(session.get("usuario"), session.get("usuario"))

    temp_ids = []
    demasiado_pesadas = 0
    for archivo in archivos:
        contenido = archivo.read()
        if len(contenido) > 15 * 1024 * 1024:
            demasiado_pesadas += 1
            continue
        mime = archivo.mimetype or "image/jpeg"
        contenido_reducido, mime = _preparar_imagen(contenido, mime)
        temp_id = uuid.uuid4().hex[:12]
        EXTRACCIONES_PENDIENTES[temp_id] = {
            "datos": {},
            "procesado": False,   # la IA todavía no la ha leído -- se lee al llegar a /confirmar
            "foto_bytes": contenido_reducido,
            "foto_mime": mime,
            "subido_por": subido_por,
        }
        temp_ids.append(temp_id)

    if demasiado_pesadas:
        flash(f"{demasiado_pesadas} foto(s) se saltaron por pesar más de 15MB.")

    if not temp_ids:
        return redirect(url_for("subir"))

    if len(temp_ids) == 1:
        return redirect(url_for("confirmar", temp_id=temp_ids[0]))

    lote_id = uuid.uuid4().hex[:10]
    LOTES[lote_id] = temp_ids
    LOTES_RESUMEN[lote_id] = {"auto": 0, "error": 0}
    return redirect(url_for("confirmar", temp_id=temp_ids[0], lote=lote_id))


def _preparar_imagen(contenido, mime):
    """Reduce el tamaño de la imagen antes de mandarla a OpenAI y antes de
    guardarla, para no gastar de más ni llenar el disco de fotos enormes."""
    try:
        from PIL import Image, ImageOps
        img = Image.open(io.BytesIO(contenido))
        img = ImageOps.exif_transpose(img)  # respeta la orientación de fotos de celular
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((1600, 1600))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"⚠️ No se pudo reprocesar la imagen, se usa la original: {repr(e)}")
        return contenido, mime


def _extraer_datos_nota(imagen_bytes, imagen_mime):
    b64 = base64.b64encode(imagen_bytes).decode("utf-8")
    r = client.chat.completions.create(
        model=MODELO,
        messages=[
            {"role": "system", "content": _prompt_extraccion()},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Lee esta nota y devuelve el JSON."},
                    {"type": "image_url", "image_url": {"url": f"data:{imagen_mime};base64,{b64}"}},
                ],
            },
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
        max_tokens=1200,
    )
    return json.loads(r.choices[0].message.content)


def _revisar_calidad(datos):
    """🔧 (22 ago 2026, pedido de Israel: "que la IA revise más a detalle y
    si nota un error, no suba la nota hasta que sea más clara") No basta con
    que la IA diga "necesita_revision: false" -- por eso, además de respetar
    lo que diga el modelo, se vuelve a checar aquí mismo, en código, que los
    datos esenciales de verdad estén completos y sean razonables (mismo
    principio que usa el bot: nunca confiar 100% en que el modelo siguió las
    instrucciones, verificar también de forma determinística).
    Devuelve (necesita_revision: bool, motivo: str|None)."""
    if datos.get("necesita_revision"):
        return True, (datos.get("motivo_revision") or "La IA marcó que algo no quedó claro en la nota.")

    motivos = []
    # 🔧 (24 ago 2026, pedido de Israel: comisiones por folio) Si el folio
    # no se leyó o no coincide con ningún prefijo conocido, la nota se
    # queda sin dueña de comisión -- nunca se le atribuye por default a
    # quien la subió. Se marca para revisión, igual que cualquier otro
    # dato esencial faltante.
    folio = datos.get("folio")
    if not folio or not str(folio).strip():
        motivos.append("falta el folio (necesario para saber a quién le corresponde la comisión)")
    elif not vendedora_por_folio(str(folio)):
        motivos.append(f"el folio ('{folio}') no coincide con ningún prefijo conocido (DE=Dalia, D=Diana, K=Karo)")

    cliente = datos.get("cliente")
    if not cliente or not str(cliente).strip():
        motivos.append("falta el nombre del cliente")

    productos = datos.get("productos") or []
    if not productos:
        motivos.append("no se detectó ningún producto")
    else:
        for p in productos:
            cant = p.get("cantidad")
            cant_valida = False
            try:
                cant_valida = float(cant) > 0
            except (TypeError, ValueError):
                cant_valida = False
            if not cant_valida:
                motivos.append(f"falta la cantidad de '{p.get('producto') or 'un producto'}'")

    fecha = datos.get("fecha_entrega")
    if not fecha:
        motivos.append("falta la fecha de entrega")
    elif not database.normalizar_fecha_iso(str(fecha)):
        motivos.append(f"la fecha de entrega ('{fecha}') no se reconoce como DD/MM/AAAA")

    total = datos.get("total")
    total_valido = False
    try:
        total_valido = float(total) > 0
    except (TypeError, ValueError):
        total_valido = False
    if not total_valido:
        motivos.append("falta el total del pedido")

    if motivos:
        return True, "Revisar: " + "; ".join(motivos) + "."
    return False, None


def _texto_o_none(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _guardar_pedido_desde_datos(datos, foto_bytes, subido_por=None, necesita_revision=False, motivo_revision=None):
    """Construye el dict final y lo guarda.

    🔧 (23 ago 2026, pedido de Israel: "que se puedan subir, pero marcadas
    como error para que llamen la atención y se corrija ya dentro de la
    app") Ya no hay un formulario manual aparte para las notas que
    necesitan revisión -- TODO pasa por aquí, y si algo no quedó claro se
    guarda de todos modos con necesita_revision=True/motivo_revision para
    poder corregirlo después desde la pantalla normal de Editar."""
    productos = []
    for p in (datos.get("productos") or []):
        productos.append({
            "producto": p.get("producto") or "",
            "cantidad": p.get("cantidad") or "",
            "colores": p.get("colores") or "",
            "precio_unitario": p.get("precio_unitario") or "",
        })
    data = {
        "fecha_captura": database.ahora_negocio().isoformat(timespec="seconds"),
        "subido_por": subido_por,
        "folio": _texto_o_none(datos.get("folio")),
        "cliente": _texto_o_none(datos.get("cliente")),
        "telefono": _texto_o_none(datos.get("telefono")),
        "municipio": _texto_o_none(datos.get("municipio")),
        "fecha_entrega": _texto_o_none(datos.get("fecha_entrega")),
        "tipo_entrega": _texto_o_none(datos.get("tipo_entrega")),
        "direccion": _texto_o_none(datos.get("direccion")),
        "productos": productos,
        "anticipo": datos.get("anticipo") or 0,
        "total": datos.get("total") or 0,
        "notas": _texto_o_none(datos.get("notas")),
        "necesita_revision": necesita_revision,
        "motivo_revision": _texto_o_none(motivo_revision),
    }
    # 🔧 (29 ago 2026, pedido de Israel: "no lo implementes en las fotos,
    # que las notas subidas por foto sigan funcionando como hasta hoy")
    # El costo de envío automático (ver PRECIOS_ENVIO_MUNICIPIO /
    # PRECIO_ENVIO_DHL / _costo_envio) es SOLO para /capturar -- las notas
    # que vienen de foto+IA se quedan sin tocar, tal como funcionaban
    # antes de esa función existir (envio_costo se queda en su default
    # de 0 -- ver guardar_pedido en database.py).
    nombre_archivo = f"{uuid.uuid4().hex}.jpg"
    with open(os.path.join(FOTOS_DIR, nombre_archivo), "wb") as f:
        f.write(foto_bytes)
    data["foto_archivo"] = nombre_archivo
    return database.guardar_pedido(data)


def _siguiente_del_lote(temp_id, lote_id):
    """Busca el próximo temp_id del lote que siga pendiente de confirmar (por
    si alguno ya se guardó/saltó fuera de orden). None si ya no queda ninguno."""
    lista = LOTES.get(lote_id)
    if not lista or temp_id not in lista:
        return None
    for candidato in lista[lista.index(temp_id) + 1:]:
        if candidato in EXTRACCIONES_PENDIENTES:
            return candidato
    return None


@app.route("/capturar", methods=["GET", "POST"])
def capturar():
    """🔧 (29 ago 2026, pedido de Israel: "las notas se hacen en Excel y
    luego se suben por foto -- ahora quiero que se hagan directo en la
    app") Formulario para dar de alta una nota SIN foto ni IA de por
    medio -- se teclea directo aquí mismo (folio, cliente, productos,
    anticipo, total... los mismos datos que hoy lee la IA de la foto).

    Convive con /subir (no lo reemplaza en el código -- Israel pidió
    dejarlo funcionando unos días antes de decidir si se quita).

    Reutiliza exactamente la misma validación que usa /confirmar
    (_revisar_calidad): si falta el folio, no hay productos, falta la
    fecha o el total, la nota se guarda IGUAL pero marcada con
    necesita_revision -- mismo criterio para una nota manual que para
    una leída por IA, nunca se pierde una captura por un dato incompleto."""
    if request.method == "GET":
        return render_template(
            "capturar.html", tipos_entrega=TIPOS_ENTREGA_VALIDOS,
            municipios_envio=PRECIOS_ENVIO_MUNICIPIO, precio_envio_dhl=PRECIO_ENVIO_DHL,
            puntos_entrega=PUNTOS_ENTREGA_FIJOS, catalogo_precios=CATALOGO_PRECIOS_CAPTURA,
        )

    productos = _leer_productos_del_form(request.form)
    # 🔧 (30 ago 2026, pedido de Israel: "bloquea para Diana lo que ya
    # tenía bloqueado -- editar la nota sí lo debe poder hacer") Mismo
    # candado que ya existe en pedido_editar(): a Diana no le corresponde
    # ver ni fijar cifras de dinero, pero SÍ debe poder capturar la nota
    # (cliente, productos, colores, fecha...). No se bloquea la ruta
    # completa (a diferencia de finanzas/ventas/indicadores) -- solo se
    # ignoran los montos que venga a mandar en el formulario, igual que
    # ya se hace con anticipo/total al editar un pedido existente.
    if not _puede_ver_finanzas():
        for p in productos:
            p["precio_unitario"] = ""
    anticipo = request.form.get("anticipo") if _puede_ver_finanzas() else 0
    total = request.form.get("total") if _puede_ver_finanzas() else 0
    datos = {
        "folio": (request.form.get("folio") or "").strip() or None,
        "cliente": (request.form.get("cliente") or "").strip() or None,
        "telefono": (request.form.get("telefono") or "").strip() or None,
        "municipio": (request.form.get("municipio") or "").strip() or None,
        "fecha_entrega": (request.form.get("fecha_entrega") or "").strip() or None,
        "tipo_entrega": (request.form.get("tipo_entrega") or "").strip() or None,
        "direccion": (request.form.get("direccion") or "").strip() or None,
        "productos": productos,
        "anticipo": anticipo or 0,
        "total": total or 0,
        "notas": (request.form.get("notas") or "").strip() or None,
    }
    necesita_revision, motivo = _revisar_calidad(datos)

    subido_por = NOMBRES_DISPLAY.get(session.get("usuario"), session.get("usuario"))
    data = dict(datos)
    data["fecha_captura"] = database.ahora_negocio().isoformat(timespec="seconds")
    data["subido_por"] = subido_por
    data["necesita_revision"] = necesita_revision
    data["motivo_revision"] = motivo
    data["foto_archivo"] = None
    data["origen"] = "directo"
    # 🔧 (29 ago 2026) "Notas importantes" es un campo APARTE de "notas" --
    # notas es de uso general (ej. "incluye envío, pago en efectivo") y
    # sale como renglón dentro de la tabla de productos; esto es
    # específicamente para instrucciones de producción (ej. "los
    # jaboncitos mitad amarillos, mitad verdes") y sale en su propio
    # recuadro, hasta el final de la nota impresa.
    data["notas_importantes"] = (request.form.get("notas_importantes") or "").strip() or None
    # 🔧 (29 ago 2026) El costo de envío SIEMPRE se calcula aquí, con lo
    # que de verdad quedó guardado (tipo_entrega + municipio) -- ver
    # _costo_envio arriba. El navegador ya lo sumó al "Total" como
    # ayuda visual mientras se capturaba, pero eso es solo comodidad;
    # este es el monto que de verdad se guarda para la nota impresa.
    data["envio_costo"] = _costo_envio(datos["tipo_entrega"], datos["municipio"])
    pedido_id = database.guardar_pedido(data)

    cliente_nombre = datos["cliente"] or "cliente sin nombre"
    if necesita_revision:
        flash(f"⚠️ Guardada CON ERROR (revisar y corregir): {cliente_nombre} -- {motivo}")
    else:
        flash(f"✅ Nota guardada: {cliente_nombre}")
    return redirect(url_for("pedido_detalle", pedido_id=pedido_id))


@app.route("/confirmar/<temp_id>")
def confirmar(temp_id):
    """🔧 (23 ago 2026, pedido de Israel: "las notas que tengan información
    por confirmar o error que detecte la IA hay que marcarlo como error,
    que se puedan subir, pero marcadas para que llamen la atención y se
    corrija ya dentro de la app") Antes, si una nota no quedaba clara, esta
    pantalla se detenía a pedir que alguien la llenara a mano antes de
    guardarla -- eso hacía que subir una tanda se sintiera como "revisar
    nota por nota". Ahora SIEMPRE se guarda de una vez, sea cual sea el
    resultado: si algo no quedó claro se guarda de todos modos, marcada con
    necesita_revision para corregirla después con calma desde Editar (igual
    que cualquier otro pedido) -- ver database.actualizar_pedido()."""
    pendiente = EXTRACCIONES_PENDIENTES.get(temp_id)
    lote_id = request.args.get("lote") or None
    if not pendiente:
        flash("Esta nota ya fue confirmada o expiró. Súbela de nuevo si hace falta.")
        return redirect(url_for("subir"))

    # 🔧 Lectura diferida: si viene de un lote y todavía no se ha leído
    # con IA, se lee justo ahora -- una nota a la vez, nunca las 42 de
    # la tanda juntas en la misma petición (ver comentario en /subir).
    error_lectura = False
    if not pendiente.get("procesado"):
        try:
            pendiente["datos"] = _extraer_datos_nota(pendiente["foto_bytes"], pendiente["foto_mime"])
        except Exception as e:
            print(f"⚠️ Error leyendo la nota con IA: {repr(e)}")
            pendiente["datos"] = {}
            error_lectura = True
        pendiente["procesado"] = True

    necesita_revision, motivo = (True, "No se pudo leer la foto automáticamente.") if error_lectura \
        else _revisar_calidad(pendiente["datos"])

    pedido_id = _guardar_pedido_desde_datos(
        pendiente["datos"], pendiente["foto_bytes"], pendiente.get("subido_por"),
        necesita_revision=necesita_revision, motivo_revision=motivo,
    )
    EXTRACCIONES_PENDIENTES.pop(temp_id, None)
    cliente_nombre = pendiente["datos"].get("cliente") or "cliente sin nombre"
    if necesita_revision:
        flash(f"⚠️ Guardado CON ERROR (revisar y corregir): {cliente_nombre} -- {motivo}")
    else:
        flash(f"✅ Guardado automático (nota clara): {cliente_nombre}")

    if lote_id:
        resumen = LOTES_RESUMEN.setdefault(lote_id, {"auto": 0, "error": 0})
        resumen["error" if necesita_revision else "auto"] += 1
        siguiente = _siguiente_del_lote(temp_id, lote_id)
        if siguiente:
            return redirect(url_for("confirmar", temp_id=siguiente, lote=lote_id))
        LOTES.pop(lote_id, None)
        LOTES_RESUMEN.pop(lote_id, None)
        if resumen["error"]:
            flash(f"¡Listo! Tanda terminada -- {resumen['auto']} guardadas bien, {resumen['error']} guardadas CON ERROR (corrígelas desde Editar). 🎉")
        else:
            flash(f"¡Listo! Tanda terminada -- {resumen['auto']} guardadas, todas claras. 🎉")
        return redirect(url_for("dashboard"))

    return redirect(url_for("pedido_detalle", pedido_id=pedido_id))


def _leer_productos_del_form(form):
    """El formulario manda listas paralelas: producto_0, cantidad_0, ..."""
    productos = []
    i = 0
    while f"producto_{i}" in form:
        nombre = (form.get(f"producto_{i}") or "").strip()
        if nombre:
            productos.append({
                "producto": nombre,
                "cantidad": form.get(f"cantidad_{i}") or "",
                "colores": (form.get(f"colores_{i}") or "").strip(),
                "precio_unitario": form.get(f"precio_{i}") or "",
            })
        i += 1
    return productos


# ----------------------------------------------------------------------
# Detalle / edición de un pedido
# ----------------------------------------------------------------------
def _regresar_seguro(valor):
    """🔧 (25 ago 2026) Valida que el "regresar" que llega por query/form
    sea una ruta interna (empieza con "/" y no con "//", que en un
    navegador se interpreta como otro dominio) antes de usarla como
    destino de un redirect -- así nadie puede mandar a alguien a un
    sitio externo con un link armado a mano."""
    if valor and valor.startswith("/") and not valor.startswith("//"):
        return valor
    return None


@app.route("/pedido/<int:pedido_id>")
def pedido_detalle(pedido_id):
    pedido = database.obtener_pedido(pedido_id)
    if not pedido:
        flash("Ese pedido ya no existe.")
        return redirect(url_for("dashboard"))
    vendedora_folio = vendedora_por_folio(pedido.get("folio"))
    # 🔧 (25 ago 2026) "regresar" se valida AQUÍ (server-side) y no en el
    # template -- si el template leyera request.args.get('regresar') tal
    # cual, alguien podría armar a mano un link con
    # ?regresar=https://sitio-malo.com y el botón "Volver" lo mandaría
    # ahí derechito. Ya validado, se le pasa al template listo para usar.
    regresar = _regresar_seguro(request.args.get("regresar"))
    return render_template(
        "pedido_detalle.html", p=pedido, regresar=regresar,
        nombre_vendedora_folio=NOMBRES_DISPLAY.get(vendedora_folio) if vendedora_folio else None,
    )


def _dia_entrega_largo(fecha_entrega_iso):
    """'2026-09-08' -> 'Martes 08 de Septiembre' (mismo formato que la
    nota de Excel -- día de la semana, día con cero a la izquierda, mes
    en letra, SIN año). None si la fecha no se pudo reconocer."""
    if not fecha_entrega_iso:
        return None
    try:
        fecha = datetime.date.fromisoformat(str(fecha_entrega_iso)[:10])
    except ValueError:
        return None
    return f"{DIAS_SEMANA_LARGOS_ES[fecha.isoweekday()]} {fecha.day:02d} de {MESES_ES[fecha.month].capitalize()}"


@app.route("/pedido/<int:pedido_id>/nota")
def pedido_nota(pedido_id):
    """🔧 (29 ago 2026, pedido de Israel: "necesito una nota idéntica a
    la de Excel -- se imprime en PDF y se le manda al cliente para que
    revise sus datos") Página imprimible de UN pedido, en el mismo
    formato que ya usan (folio, datos del cliente, tabla de productos,
    notas del negocio, anticipo/total). Se guarda como PDF con
    Imprimir -> Guardar como PDF del navegador -- mismo mecanismo que
    ya usan /imprimir/semana-actual y /imprimir/semana-proxima, sin
    depender de ninguna librería extra de PDF en el servidor.

    Solo lee datos -- si algo está mal, el botón "Editar antes de
    imprimir" manda a /pedido/<id>/editar con "regresar" apuntando de
    vuelta aquí mismo, para corregir y volver a imprimir sin perder el
    lugar (ver _regresar_seguro arriba)."""
    pedido = database.obtener_pedido(pedido_id)
    if not pedido:
        flash("Ese pedido ya no existe.")
        return redirect(url_for("dashboard"))
    # Igual que en /imprimir/semana-*: nunca se listan como "producto"
    # los renglones de envío/urgencia que hayan quedado guardados por
    # error -- esos van reflejados en "notas", no aquí.
    productos = [p for p in (pedido.get("productos") or []) if _es_producto_real(p.get("producto"))]
    vendedora_folio = vendedora_por_folio(pedido.get("folio"))
    colores_nota = COLORES_NOTA_POR_VENDEDORA.get(vendedora_folio, COLOR_NOTA_DEFAULT)
    telefono_vendedor = TELEFONOS_VENDEDORA_NOTA.get(vendedora_folio, TELEFONO_CONTACTO_VENDEDOR_NOTA)
    return render_template(
        "nota_cliente.html", p=pedido, productos=productos,
        dia_entrega_largo=_dia_entrega_largo(pedido.get("fecha_entrega_iso")),
        horario_local=HORARIO_LOCAL_NOTA, telefono_vendedor=telefono_vendedor,
        colores=colores_nota,
        nota_jabones=NOTA_JABONES_TEXTO, nota_horario_domicilio=NOTA_HORARIO_DOMICILIO_TEXTO,
        nota_tarjetita=NOTA_TARJETITA_TEXTO,
    )


@app.route("/pedido/<int:pedido_id>/editar", methods=["GET", "POST"])
def pedido_editar(pedido_id):
    pedido = database.obtener_pedido(pedido_id)
    if not pedido:
        flash("Ese pedido ya no existe.")
        return redirect(url_for("dashboard"))

    if request.method == "GET":
        regresar = _regresar_seguro(request.args.get("regresar"))
        return render_template(
            "pedido_editar.html", p=pedido, tipos_entrega=TIPOS_ENTREGA_VALIDOS, regresar=regresar,
        )

    productos = _leer_productos_del_form(request.form)
    # 🔧 (23 ago 2026) A Diana no le corresponde tocar cifras de dinero --
    # esto ya está oculto en pedido_editar.html, pero se refuerza aquí
    # también por si alguien manda el formulario a mano sin pasar por la
    # pantalla (siempre se guardan los valores que YA tenía el pedido).
    if _puede_ver_finanzas():
        anticipo = request.form.get("anticipo") or 0
        total = request.form.get("total") or 0
    else:
        anticipo = pedido.get("anticipo") or 0
        total = pedido.get("total") or 0
    data = {
        "folio": (request.form.get("folio") or "").strip() or None,
        "cliente": (request.form.get("cliente") or "").strip() or None,
        "telefono": (request.form.get("telefono") or "").strip() or None,
        "municipio": (request.form.get("municipio") or "").strip() or None,
        "fecha_entrega": (request.form.get("fecha_entrega") or "").strip() or None,
        "tipo_entrega": (request.form.get("tipo_entrega") or "").strip() or None,
        "direccion": (request.form.get("direccion") or "").strip() or None,
        "productos": productos,
        "anticipo": anticipo,
        "total": total,
        "notas": (request.form.get("notas") or "").strip() or None,
        # 🔧 (29 ago 2026) Campo aparte de "notas" -- ver la nota en
        # capturar() sobre por qué van separados.
        "notas_importantes": (request.form.get("notas_importantes") or "").strip() or None,
    }
    database.actualizar_pedido(pedido_id, data)
    flash("Pedido actualizado.")
    destino = url_for("pedido_detalle", pedido_id=pedido_id)
    regresar = _regresar_seguro(request.args.get("regresar"))
    if regresar:
        # 🔧 (25 ago 2026) OJO: url_for(..., regresar=regresar) NO sirve
        # aquí -- no escapa el "?" que trae adentro el propio "regresar"
        # (es una URL completa con su propia query, ej.
        # "/dashboard?vista=mes"), así que el resultado queda con DOS "?"
        # y el parámetro se corta a la mitad. Por eso se arma a mano con
        # quote(), igual que en los templates con el filtro |urlencode.
        destino += "?regresar=" + quote(regresar, safe="")
    return redirect(destino)


@app.route("/pedido/<int:pedido_id>/eliminar", methods=["POST"])
def pedido_eliminar(pedido_id):
    # 🔧 (25 ago 2026) Si al eliminar venías de una pestaña/mes/búsqueda
    # específicos (guardado en el campo oculto "regresar" del formulario),
    # regresa ahí -- si no, al dashboard normal.
    destino = _regresar_seguro(request.form.get("regresar")) or url_for("dashboard")
    database.eliminar_pedido(pedido_id)
    flash("Pedido eliminado.")
    return redirect(destino)


@app.route("/pedidos/eliminar_varios", methods=["POST"])
def pedidos_eliminar_varios():
    ids = request.form.getlist("pedido_id")
    n = database.eliminar_pedidos(ids)
    if n:
        flash(f"{n} pedido(s) eliminado(s).")
    else:
        flash("No se eliminó ningún pedido (¿no seleccionaste ninguno?).")
    volver = request.form.get("volver") or ""
    if not volver.startswith("/"):
        volver = url_for("dashboard")
    return redirect(volver)


@app.route("/fotos/<path:nombre_archivo>")
def servir_foto(nombre_archivo):
    return send_from_directory(FOTOS_DIR, nombre_archivo)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", 5001)))
