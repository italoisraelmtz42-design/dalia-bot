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
import uuid

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
NOMBRES_DISPLAY = {"israel": "Israel", "dalia": "Dalia", "diana": "Diana"}

# Quién gana comisión y cuánto por cada producto vendido (pieza, no por
# pedido). Ahorita solo Diana -- si más adelante alguien más gana
# comisión, nada más se agrega aquí.
VENDEDORES_CON_COMISION = {"diana": 1.0}

_passwords_no_vacias = [p for p in USUARIOS.values() if p]
if len(_passwords_no_vacias) != len(set(_passwords_no_vacias)):
    print("⚠️ ADVERTENCIA: dos o más usuarios de Producción Dalia tienen la MISMA "
          "contraseña configurada -- el login no va a poder distinguir quién es quién.")

MESES_ES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
            "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
DIAS_SEMANA_ES = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]

FOTOS_DIR = os.getenv("FOTOS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fotos_notas"))
os.makedirs(FOTOS_DIR, exist_ok=True)

MODELO = "gpt-4.1-mini"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=60.0, max_retries=2)

TIPOS_ENTREGA_VALIDOS = ("domicilio", "local", "punto_de_entrega")

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
    if request.endpoint == "finanzas" and not _puede_ver_finanzas():
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

    return render_template(
        "finanzas.html", pedidos=pedidos, periodo=periodo, titulo=titulo,
        total_anticipos=total_anticipos, total_ventas=total_ventas, total_saldos=total_saldos,
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


@app.route("/comisiones")
def comisiones():
    usuario = session.get("usuario")
    if not VENDEDORES_CON_COMISION:
        flash("No hay comisiones configuradas todavía.")
        return redirect(url_for("dashboard"))

    if usuario in VENDEDORES_CON_COMISION:
        vendedor = usuario  # Diana (o quien tenga comisión) solo ve la suya
    else:
        vendedor = request.args.get("vendedor") or next(iter(VENDEDORES_CON_COMISION))
        if vendedor not in VENDEDORES_CON_COMISION:
            vendedor = next(iter(VENDEDORES_CON_COMISION))

    monto_por_producto = VENDEDORES_CON_COMISION[vendedor]
    periodo = request.args.get("periodo", "semana")
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

    pedidos = database.listar_capturados_por_vendedor_en_rango(vendedor, ini.isoformat(), fin.isoformat())
    total_piezas = 0.0
    for p in pedidos:
        for prod in (p.get("productos") or []):
            if not _es_producto_real(prod.get("producto")):
                continue
            try:
                total_piezas += float(prod.get("cantidad") or 0)
            except (TypeError, ValueError):
                pass
    total_comision = round(total_piezas * monto_por_producto, 2)

    return render_template(
        "comisiones.html", pedidos=pedidos, periodo=periodo, titulo_periodo=titulo_periodo,
        vendedor=vendedor, nombre_vendedor=NOMBRES_DISPLAY.get(vendedor, vendedor.title() if vendedor else ""),
        monto_por_producto=monto_por_producto, total_piezas=total_piezas, total_comision=total_comision,
        es_propia=(usuario == vendedor),
        otros_vendedores=[v for v in VENDEDORES_CON_COMISION if v != vendedor] if usuario not in VENDEDORES_CON_COMISION else [],
        nav_anterior=nav_anterior, nav_siguiente=nav_siguiente, nav_actual=nav_actual, es_actual=es_actual,
    )


# ----------------------------------------------------------------------
# Lista imprimible de la semana siguiente (23 ago 2026, pedido de Israel:
# poder imprimir el viernes/sábado todo lo que se entrega la próxima
# semana, con un resumen de cuánto fabricar de cada producto)
# ----------------------------------------------------------------------
@app.route("/imprimir/semana-proxima")
def imprimir_semana_proxima():
    hoy = _hoy()
    ini_semana_actual, _ = _rango_semana(hoy)
    ini = ini_semana_actual + datetime.timedelta(days=7)
    fin = ini + datetime.timedelta(days=6)

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
        ini=ini, fin=fin, generado=database.ahora_negocio(),
    )


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
PROMPT_EXTRACCION = """Eres un asistente que lee notas de pedidos de una tienda mexicana de \
recuerdos para eventos (Recuerditos Dalia: ositos de toalla, jaboncitos, \
abanicos, dominós, etc. para baby showers, XV años, bodas, etc.).

Se te va a mostrar una foto de una nota de pedido YA CONFIRMADA con el \
cliente (normalmente escrita a mano o en una nota de WhatsApp con el \
resumen del pedido, colores, fecha de entrega y el anticipo pagado).

Lee la nota con MUCHO cuidado -- revisa cada número dos veces antes de \
contestar (cantidades, precios, anticipo, total). Estos datos alimentan \
directamente lo que se fabrica y se cobra, así que un error aquí es un \
error real en el negocio, no un detalle menor.

Tu trabajo es leer la nota y devolver ÚNICAMENTE un JSON con esta forma \
exacta (sin texto extra, sin explicaciones):

{
  "cliente": "nombre del cliente o null si no aparece",
  "telefono": "teléfono o null",
  "municipio": "municipio/ciudad de entrega o null",
  "fecha_entrega": "fecha en formato DD/MM/AAAA si se puede deducir el año, si no como aparece en la nota, o null",
  "tipo_entrega": "uno de: domicilio, local, punto_de_entrega -- o null si no está claro",
  "direccion": "dirección exacta SOLO si aparece escrita en la nota, si no null",
  "productos": [
    {"producto": "nombre del producto", "cantidad": numero, "colores": "descripción breve de colores/detalles", "precio_unitario": numero_o_null}
  ],
  "anticipo": numero_o_null,
  "total": numero_o_null,
  "notas": "cualquier detalle extra relevante (tarjetita, urgente, etc.) o null",
  "necesita_revision": true_o_false,
  "motivo_revision": "explicación breve y concreta de qué no quedó claro, o null si no necesita revisión"
}

Reglas importantes:
- "productos" es SOLO para mercancía física que se fabrica y se entrega (ositos, jaboncitos, abanicos, dominós, etc.). El cobro de envío a domicilio y el cargo por pedido urgente NO son productos -- nunca los pongas como un renglón de "productos", aunque la nota los liste junto a los productos. Van reflejados nada más en el "total" y, si quieres anotarlos, en "notas" (ej. "incluye $100 de envío a domicilio", "es urgente").
- Si un dato no aparece claramente en la nota, pon null (o lista vacía para productos) -- NO inventes ni adivines.
- "cantidad" y los montos deben ser números (sin signo de $ ni comas), nunca texto.
- Marca "necesita_revision": true si ocurre CUALQUIERA de estas cosas: falta el nombre del cliente, no hay ningún producto legible, algún producto no tiene cantidad clara, falta la fecha de entrega, la letra o la foto están borrosas en alguna parte importante, el total no cuadra aproximadamente con la suma de los productos (déjale margen por envío/redondeo), o simplemente no estás seguro de algo relevante.
- Si TODOS los datos esenciales (cliente, al menos un producto con cantidad, fecha de entrega, y montos) se leyeron claros y consistentes, marca "necesita_revision": false.
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
            {"role": "system", "content": PROMPT_EXTRACCION},
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
@app.route("/pedido/<int:pedido_id>")
def pedido_detalle(pedido_id):
    pedido = database.obtener_pedido(pedido_id)
    if not pedido:
        flash("Ese pedido ya no existe.")
        return redirect(url_for("dashboard"))
    return render_template("pedido_detalle.html", p=pedido)


@app.route("/pedido/<int:pedido_id>/editar", methods=["GET", "POST"])
def pedido_editar(pedido_id):
    pedido = database.obtener_pedido(pedido_id)
    if not pedido:
        flash("Ese pedido ya no existe.")
        return redirect(url_for("dashboard"))

    if request.method == "GET":
        return render_template("pedido_editar.html", p=pedido, tipos_entrega=TIPOS_ENTREGA_VALIDOS)

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
    }
    database.actualizar_pedido(pedido_id, data)
    flash("Pedido actualizado.")
    return redirect(url_for("pedido_detalle", pedido_id=pedido_id))


@app.route("/pedido/<int:pedido_id>/eliminar", methods=["POST"])
def pedido_eliminar(pedido_id):
    database.eliminar_pedido(pedido_id)
    flash("Pedido eliminado.")
    return redirect(url_for("dashboard"))


@app.route("/fotos/<path:nombre_archivo>")
def servir_foto(nombre_archivo):
    return send_from_directory(FOTOS_DIR, nombre_archivo)


if __name__ == "__main__":
    app.run(debug=True, port=int(os.getenv("PORT", 5001)))
