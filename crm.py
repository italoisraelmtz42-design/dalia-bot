# src/crm.py
import datetime

def cargar_cliente(numero):
    """
    Función que busca o registra la información de un cliente por su número.
    """
    print(f"🔎 [CRM] Buscando/registrando cliente con número: {numero}")

    # --- AQUÍ DEBES PONER TU LÓGICA DE BASE DE DATOS ---
    # Ejemplo de simulación. Si tienes una base de datos, reemplaza esto
    # con una consulta SQL o una llamada a tu ORM.
    
    # (Ejemplo simulado en memoria)
    cliente = {
        "numero": numero,
        "nombre": "Cliente Registrado",
        "fecha_creacion": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "estado": "activo"
    }
    
    return cliente
