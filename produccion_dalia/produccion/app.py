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

PRODUCCION_PASSWORD = os.getenv("PRODUCCION_PASSWORD", "")
FOTOS_DIR = os.getenv("FOTOS_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fotos_notas"))
os.makedirs(FOTOS_DIR, exist_ok=True)

MODELO = "gpt-4.1-mini"
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"), timeout=60.0, max_retries=2)

TIPOS_ENTREGA_VALIDOS = ("domicilio", "local", "punto_de_entrega")

# Extracciones ya leídas por IA pero todavía sin confirmar por un humano.
# Viven solo en memoria mientras alguien las revisa -- si el proceso se
# reinicia justo en ese momento se pierden, pero no importa: es solo el
# paso intermedio antes de guardar en la base de datos real.
EXTRACCIONES_PENDIENTES = {}

database.init_db()


# ----------------------------------------------------------------------
# Autenticación (contraseña compartida)
# ----------------------------------------------------------------------
@app.before_request
def exigir_login():
    rutas_publicas = {"login", "static"}
    if request.endpoint in rutas_publicas:
        return None
    if not session.get("autenticado"):
        return redirect(url_for("login", siguiente=request.path))
    return None


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        clave = request.form.get("password", "")
        if PRODUCCION_PASSWORD and clave == PRODUCCION_PASSWORD:
            session["autenticado"] = True
            session.permanent = True
            siguiente = request.args.get("siguiente") or url_for("dashboard")
            return redirect(siguiente)
        flash("Contraseña incorrecta.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ----------------------------------------------------------------------
# Utilidades de fecha
# ----------------------------------------------------------------------
def _hoy():
    return datetime.date.today()


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


# ----------------------------------------------------------------------
# Dashboard de producción
# ----------------------------------------------------------------------
@app.route("/")
def raiz():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    vista = request.args.get("vista", "hoy")
    hoy = _hoy()

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
    if periodo == "semana":
        ini, fin = _rango_semana(hoy)
        titulo = f"Esta semana ({ini.strftime('%d/%m')} al {fin.strftime('%d/%m')})"
    elif periodo == "mes":
        ini, fin = _rango_mes(hoy)
        titulo = f"Este mes ({ini.strftime('%B %Y')})"
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
    )


# ----------------------------------------------------------------------
# Subir nota -> IA extrae datos -> humano confirma
# ----------------------------------------------------------------------
PROMPT_EXTRACCION = """Eres un asistente que lee notas de pedidos de una tienda mexicana de \
recuerdos para eventos (Recuerditos Dalia: ositos de toalla, jaboncitos, \
abanicos, dominós, etc. para baby showers, XV años, bodas, etc.).

Se te va a mostrar una foto de una nota de pedido YA CONFIRMADA con el \
cliente (normalmente escrita a mano o en una nota de WhatsApp con el \
resumen del pedido, colores, fecha de entrega y el anticipo pagado).

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
  "notas": "cualquier detalle extra relevante (tarjetita, urgente, etc.) o null"
}

Reglas importantes:
- Si un dato no aparece claramente en la nota, pon null (o lista vacía para productos) -- NO inventes ni adivines.
- "cantidad" y los montos deben ser números (sin signo de $ ni comas), nunca texto.
- Un humano va a revisar y corregir esto después, así que prioriza no inventar sobre completar todo.
"""


@app.route("/subir", methods=["GET", "POST"])
def subir():
    if request.method == "GET":
        return render_template("subir.html")

    archivo = request.files.get("foto")
    if not archivo or not archivo.filename:
        flash("Selecciona una foto de la nota primero.")
        return redirect(url_for("subir"))

    contenido = archivo.read()
    if len(contenido) > 15 * 1024 * 1024:
        flash("La foto es demasiado pesada (máximo 15MB).")
        return redirect(url_for("subir"))

    mime = archivo.mimetype or "image/jpeg"
    contenido_reducido, mime = _preparar_imagen(contenido, mime)

    try:
        datos = _extraer_datos_nota(contenido_reducido, mime)
    except Exception as e:
        print(f"⚠️ Error leyendo la nota con IA: {repr(e)}")
        flash("No se pudo leer la nota automáticamente. Puedes llenar los datos a mano abajo.")
        datos = {}

    temp_id = uuid.uuid4().hex[:12]
    EXTRACCIONES_PENDIENTES[temp_id] = {
        "datos": datos,
        "foto_bytes": contenido_reducido,
        "foto_mime": mime,
    }
    return redirect(url_for("confirmar", temp_id=temp_id))


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


@app.route("/confirmar/<temp_id>", methods=["GET", "POST"])
def confirmar(temp_id):
    pendiente = EXTRACCIONES_PENDIENTES.get(temp_id)
    if not pendiente:
        flash("Esta nota ya fue confirmada o expiró. Súbela de nuevo si hace falta.")
        return redirect(url_for("subir"))

    if request.method == "GET":
        return render_template(
            "confirmar.html", temp_id=temp_id, datos=pendiente["datos"],
            tipos_entrega=TIPOS_ENTREGA_VALIDOS,
        )

    productos = _leer_productos_del_form(request.form)
    data = {
        "fecha_captura": datetime.datetime.now().isoformat(timespec="seconds"),
        "subido_por": (request.form.get("subido_por") or "").strip() or None,
        "cliente": (request.form.get("cliente") or "").strip() or None,
        "telefono": (request.form.get("telefono") or "").strip() or None,
        "municipio": (request.form.get("municipio") or "").strip() or None,
        "fecha_entrega": (request.form.get("fecha_entrega") or "").strip() or None,
        "tipo_entrega": (request.form.get("tipo_entrega") or "").strip() or None,
        "direccion": (request.form.get("direccion") or "").strip() or None,
        "productos": productos,
        "anticipo": request.form.get("anticipo") or 0,
        "total": request.form.get("total") or 0,
        "notas": (request.form.get("notas") or "").strip() or None,
    }

    nombre_archivo = f"{uuid.uuid4().hex}.jpg"
    ruta_absoluta = os.path.join(FOTOS_DIR, nombre_archivo)
    with open(ruta_absoluta, "wb") as f:
        f.write(pendiente["foto_bytes"])
    data["foto_archivo"] = nombre_archivo

    pedido_id = database.guardar_pedido(data)
    EXTRACCIONES_PENDIENTES.pop(temp_id, None)
    flash("Pedido guardado correctamente.")
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
    data = {
        "cliente": (request.form.get("cliente") or "").strip() or None,
        "telefono": (request.form.get("telefono") or "").strip() or None,
        "municipio": (request.form.get("municipio") or "").strip() or None,
        "fecha_entrega": (request.form.get("fecha_entrega") or "").strip() or None,
        "tipo_entrega": (request.form.get("tipo_entrega") or "").strip() or None,
        "direccion": (request.form.get("direccion") or "").strip() or None,
        "productos": productos,
        "anticipo": request.form.get("anticipo") or 0,
        "total": request.form.get("total") or 0,
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
