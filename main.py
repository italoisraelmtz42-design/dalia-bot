from openai import OpenAI
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime
import os
import random
import time

# ===========================
# CARGAR API
# ===========================

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ===========================
# RUTAS
# ===========================

BASE = Path(__file__).resolve().parent
CARPETA = BASE / "conocimiento"

# ===========================
# CARGAR CONOCIMIENTO
# ===========================

knowledge = ""
archivos = sorted(CARPETA.glob("*.txt"))

print("\n" + "="*60)
print("CARGANDO BASE DE CONOCIMIENTO...")
print("="*60)

for i, archivo in enumerate(archivos, start=1):
    print(f"[{i:02}/{len(archivos)}] ✅ {archivo.name}")
    try:
        contenido = archivo.read_text(encoding="utf-8", errors="ignore")
        knowledge += f"""

==================================================
ARCHIVO: {archivo.name}
==================================================

{contenido}

==================================================
FIN DEL ARCHIVO
==================================================

"""
    except Exception as e:
        print(f"❌ Error leyendo {archivo.name}: {e}")

print("\n" + "="*60)
print("TOTAL DE ARCHIVOS :", len(archivos))
print("TOTAL CARACTERES  :", len(knowledge))
print("="*60)

# ===========================
# FECHA Y HORA
# ===========================

ahora = datetime.now()
fecha = ahora.strftime("%d/%m/%Y")
hora = ahora.strftime("%H:%M")

# ===========================
# PROMPT
# ===========================

system_prompt = f"""
Eres DALIA, asesora de ventas de Recuerditos Dalia.

Hoy es {fecha}.
La hora actual es {hora}.

Toda la información oficial está en la Base de Conocimiento.

REGLAS:
- Usa únicamente la Base de Conocimiento.
- Nunca inventes datos.
- Nunca inventes productos, precios o políticas.
- Si algo no existe en la Base de Conocimiento, indícalo.
- Responde como una asesora humana por WhatsApp.
- Sé amable, natural y orientada a cerrar ventas.

BASE DE CONOCIMIENTO:

{knowledge}
"""


# ===========================
# ESTADO DEL PEDIDO
# ===========================
pedido={
    "producto":None,
    "cantidad":None,
    "evento":None,
    "fecha_evento":None,
    "color_toalla":None,
    "color_mono":None,
    "color_velita":None,
    "datos_tarjeta":None,
    "tipo_entrega":None,
    "direccion":None
}

def resumen_pedido():
    datos=[]
    for k,v in pedido.items():
        if v:
            datos.append(f"{k}: {v}")
    return "\n".join(datos) if datos else "Sin datos confirmados."


messages=[{"role":"system","content":system_prompt}]

print("\n========================================")
print(" DALIA V12 VENDEDORA PRO ")
print("========================================")

while True:

    q=input("\nCliente: ").strip()

    if q.lower()=="salir":
        break

    messages.append({"role":"user","content":q})

    estado=f"""ESTADO ACTUAL DEL PEDIDO

{resumen_pedido()}

No vuelvas a preguntar datos ya confirmados.
Pregunta únicamente los datos faltantes.
"""

    messages[0]["content"]=system_prompt+"\n\n"+estado

    if len(messages)>20:
        messages=[messages[0]]+messages[-19:]

    try:

        r=client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=messages,
            temperature=0.4,
            top_p=0.9,
            max_tokens=350
        )

        texto=r.choices[0].message.content

        print("\nDALIA está escribiendo",end="",flush=True)

        espera=random.uniform(5,8)
        inicio=time.time()

        while time.time()-inicio<espera:
            print(".",end="",flush=True)
            time.sleep(1)

        print("\n")
        print("DALIA:",texto,"\n")

        messages.append({"role":"assistant","content":texto})

    except Exception as e:
        print("\nERROR:",e)
