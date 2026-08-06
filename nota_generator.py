import os
import tempfile
from pathlib import Path
from datetime import datetime
import qrcode
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch, cm
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.lib import colors
from reportlab.platypus import Table, TableStyle, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER, TA_RIGHT, TA_LEFT
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

from config import BASE, PUBLIC_BASE_URL, ZONA_HORARIA_NEGOCIO, DALIA_PHONE
# Si no tienes config.py, usa variables de entorno directamente o define aquí
# from app import enviar_whatsapp_documento  # evita import circular

CARPETA_NOTAS = BASE / "notas"
CARPETA_NOTAS.mkdir(exist_ok=True)
LOGO_PATH = BASE / "logo.png"

# ==========================================
# FUNCIÓN PARA GENERAR LA NOTA PDF
# ==========================================

def generar_nota_pdf(pedido, cliente):
    """
    Genera el PDF de la nota de pedido con el formato exacto de la imagen.
    Retorna la ruta absoluta del archivo.
    """
    # Preparar datos
    folio = pedido.get("folio", "S/F")
    fecha_actual = datetime.now(ZONA_HORARIA_NEGOCIO).strftime("%d/%m/%Y")
    nombre_cliente = cliente.get("nombre", "Cliente") if cliente else "Cliente"
    telefono = cliente.get("telefono", "") if cliente else ""

    # Datos del pedido
    producto = pedido.get("producto", "")
    cantidad = pedido.get("cantidad", 0)
    colores = []
    if pedido.get("color_toalla"):
        colores.append(f"Toalla: {pedido['color_toalla']}")
    if pedido.get("color_moño"):
        colores.append(f"Moño: {pedido['color_moño']}")
    if pedido.get("color_velita"):
        colores.append(f"Velita: {pedido['color_velita']}")
    if pedido.get("tipo_jaboncito"):
        colores.append(f"Jabón: {pedido['tipo_jaboncito']}")
    if pedido.get("color_jaboncito"):
        colores.append(f"Color jabón: {pedido['color_jaboncito']}")

    descripcion = producto
    if colores:
        descripcion += " - " + ", ".join(colores)

    subtotal = pedido.get("subtotal", 0.0)
    envio = pedido.get("envio", 0.0)
    total = pedido.get("total", 0.0)
    anticipo = pedido.get("anticipo", 0.0)
    saldo = pedido.get("saldo", 0.0)
    tipo_entrega = pedido.get("tipo_entrega", "")
    direccion = pedido.get("direccion", "")
    fecha_evento = pedido.get("fecha_evento", "")

    # Datos para la tabla (solo un producto por ahora, pero se puede extender)
    items = [
        [cantidad, descripcion, f"${pedido.get('precio_unitario', 0.0):.2f}", f"${subtotal:.2f}"]
    ]

    # Crear el PDF
    nombre_archivo = f"nota_{folio}.pdf"
    ruta_completa = CARPETA_NOTAS / nombre_archivo

    c = canvas.Canvas(str(ruta_completa), pagesize=letter)
    width, height = letter
    margen_izq = 0.7 * inch
    margen_der = width - 0.7 * inch
    y = height - 0.7 * inch

    # ----- ENCABEZADO -----
    # Logo (si existe)
    if LOGO_PATH.exists():
        try:
            img = ImageReader(str(LOGO_PATH))
            # Ajustar tamaño y posición
            c.drawImage(img, margen_izq, y - 0.8*inch, width=1.2*inch, height=1.2*inch, mask='auto')
            y -= 1.0*inch
        except:
            pass

    # Título "RECUERDOS QUE PERDURAN"
    c.setFont("Helvetica-Bold", 16)
    c.drawString(margen_izq, y, "RECUERDOS QUE PERDURAN")
    y -= 0.3*inch
    # Teléfono
    c.setFont("Helvetica", 10)
    c.drawString(margen_izq, y, "8119979692")
    y -= 0.3*inch
    # Facebook
    c.drawString(margen_izq, y, "Visita nuestra Pág. de Facebook Recuerditos Dalia")
    y -= 0.4*inch

    # Línea separadora
    c.line(margen_izq, y, margen_der, y)
    y -= 0.3*inch

    # ----- DATOS DEL PEDIDO (izquierda) -----
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margen_izq, y, f"NO. PEDIDO: {folio}")
    y -= 0.25*inch
    c.drawString(margen_izq, y, f"CLIENTE: {nombre_cliente}")
    y -= 0.25*inch
    c.drawString(margen_izq, y, f"TELEFONO: {telefono}")
    y -= 0.4*inch

    # Tipo de entrega y fecha
    c.setFont("Helvetica-Bold", 11)
    if tipo_entrega == "domicilio":
        c.drawString(margen_izq, y, "ENTREGA EN DOMICILIO")
    elif tipo_entrega == "local":
        c.drawString(margen_izq, y, "ENTREGA EN LOCAL")
    else:
        c.drawString(margen_izq, y, "ENTREGA: " + tipo_entrega)
    y -= 0.25*inch

    c.setFont("Helvetica", 10)
    fecha_entrega = fecha_evento or fecha_actual
    c.drawString(margen_izq, y, fecha_entrega)
    y -= 0.25*inch

    if direccion:
        c.drawString(margen_izq, y, direccion)
        y -= 0.25*inch

    # Horario de paquetería (fijo)
    c.setFont("Helvetica", 9)
    c.drawString(margen_izq, y, "Horario de entrega de paquetería: 1:00 p.m. a 10:00 p.m.")
    y -= 0.2*inch
    c.drawString(margen_izq, y, "La paquetería realiza las entregas de acuerdo con su ruta diaria,")
    y -= 0.2*inch
    c.drawString(margen_izq, y, "por lo que no es posible programar una hora exacta.")
    y -= 0.4*inch

    # ----- TABLA DE PRODUCTOS -----
    data = [["CANTIDAD", "DESCRIPCIÓN", "PRECIO UNITARIO", "MONTO"]]
    data.extend(items)

    # Estilo de tabla
    table = Table(data, colWidths=[0.8*inch, 3.0*inch, 1.2*inch, 1.2*inch])
    table.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,-1), 'Helvetica'),
        ('FONTSIZE', (0,0), (-1,-1), 9),
        ('BACKGROUND', (0,0), (-1,0), colors.lightgrey),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('GRID', (0,0), (-1,-1), 1, colors.black),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
    ]))
    table.wrapOn(c, width-2*margen_izq, height)
    table.drawOn(c, margen_izq, y - 0.5*inch)
    y -= 0.8*inch  # Ajuste según altura de la tabla

    # ----- OBSERVACIONES (recomendación fija) -----
    y -= 0.3*inch
    c.setFont("Helvetica-Oblique", 9)
    c.drawString(margen_izq, y, "RECOMENDAMOS NO DEJAR EL PEDIDO EN EL SOL O CALOR")
    y -= 0.2*inch
    c.drawString(margen_izq, y, "PARA QUE NO SE DERRITA EL JABONCITO.")
    y -= 0.4*inch

    # ----- FORMA DE PAGO (fijo) -----
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margen_izq, y, "PAGO AL RECIBIR, SOLO EFECTIVO")
    y -= 0.4*inch

    # ----- TOTALES (alineados a la derecha) -----
    # Usamos coordenadas x desde la derecha
    x_total = margen_der - 0.5*inch
    c.setFont("Helvetica", 10)
    c.drawRightString(x_total, y, f"SUBTOTAL          ${subtotal:.2f}")
    y -= 0.25*inch
    c.drawRightString(x_total, y, f"ANTICIPO          ${anticipo:.2f}")
    y -= 0.25*inch
    c.drawRightString(x_total, y, f"RESTO             ${saldo:.2f}")
    y -= 0.3*inch

    # ----- CÓDIGO QR (opcional) -----
    # Lo pongo en la esquina inferior derecha
    qr_data = f"{PUBLIC_BASE_URL}/pedido/{folio}"
    qr = qrcode.make(qr_data)
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        qr.save(f.name)
        qr_img = ImageReader(f.name)
        qr_size = 1.2*inch
        qr_x = margen_der - qr_size
        qr_y = 0.7*inch
        c.drawImage(qr_img, qr_x, qr_y, width=qr_size, height=qr_size, mask='auto')
        os.unlink(f.name)

    # Pie de página
    c.setFont("Helvetica", 8)
    c.drawString(margen_izq, 0.5*inch, f"Nota generada el {fecha_actual}")

    c.save()
    return str(ruta_completa)


# ==========================================
# FUNCIÓN PARA GENERAR Y ENVIAR LA NOTA
# ==========================================

def generar_y_enviar_nota(cliente, pedido_ram, pedido_db):
    """
    Genera la nota de pedido, la envía al cliente y a Dalia,
    y actualiza el pedido con la ruta del PDF.
    Se asume que el anticipo ya está confirmado.
    """
    if pedido_db and pedido_db.get("nota_pdf"):
        print(f"📄 Nota ya generada para el pedido {pedido_db['folio']}, omitiendo.")
        return

    try:
        print(f"📄 Generando nota para pedido {pedido_db['folio']}...")
        # Usar pedido_ram que ya tiene todos los campos (o combinarlos con pedido_db)
        # Para asegurar que tenemos todos los datos, fusionamos
        datos_pedido = dict(pedido_db) if pedido_db else {}
        if pedido_ram:
            datos_pedido.update(pedido_ram)
        # Asegurar campos requeridos
        if 'tipo_entrega' in datos_pedido:
            datos_pedido['tipo_entrega'] = datos_pedido.get('tipo_entrega')
        else:
            datos_pedido['tipo_entrega'] = datos_pedido.get('forma_entrega')

        ruta_pdf = generar_nota_pdf(datos_pedido, cliente)

        # Actualizar el pedido con la ruta del PDF
        pedido_id = pedido_db["id"]
        from pedidos import actualizar_campo  # import local para evitar circular
        actualizar_campo(pedido_id, "nota_pdf", ruta_pdf)
        print(f"✅ Nota guardada en: {ruta_pdf}")

        # Enviar al cliente (usando función de app.py, la importamos aquí o la recibimos)
        # Para evitar import circular, usamos un callback o definimos la función en app.py
        # Por ahora, importamos desde app.py (pero app.py importa nota_generator -> circular)
        # Mejor usamos una función global que se establece en app.py, o simplemente llamamos a una función de envío definida en este módulo
        # Como solución, voy a importar la función de envío desde app.py al final, usando import dentro de la función.
        # O mejor, creo una función de envío aquí que use requests.
        # Pero para no duplicar, usaré la misma que ya existe en app.py: enviar_whatsapp_documento
        # Como no está definida, la voy a definir aquí.
        from app import enviar_whatsapp_documento  # se definirá en app.py

        nombre_archivo = Path(ruta_pdf).name
        url_publica = f"{PUBLIC_BASE_URL}/notas/{nombre_archivo}"
        caption = f"📄 Nota de pedido {folio}\n\nGracias por tu pedido, {cliente['nombre'] or ''}. ¡Pronto estará listo!"
        enviar_whatsapp_documento(cliente["telefono"], url_publica, nombre_archivo, caption)

        # Enviar a Dalia
        if DALIA_PHONE:
            caption_dalia = f"📄 Nueva nota de pedido: {folio}\nCliente: {cliente['nombre'] or cliente['telefono']}"
            enviar_whatsapp_documento(DALIA_PHONE, url_publica, nombre_archivo, caption_dalia)
        else:
            print("⚠️ DALIA_PHONE_NUMBER no configurado, no se envía copia a Dalia.")

    except Exception as e:
        print(f"❌ Error generando/enviando nota: {e}")
        import traceback
        traceback.print_exc()