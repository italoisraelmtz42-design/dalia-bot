# src/crm.py
import datetime

def cargar_cliente(numero):
    """
    Busca o registra un cliente en el sistema.
    """
    print(f"🔎 [CRM] Buscando/registrando cliente con número: {numero}")
    
    # --- AQUÍ TU LÓGICA DE OBTENCIÓN DE CLIENTE ---
    cliente_data = {
        "numero": numero,
        "nombre": "Cliente Registrado",
        "fecha_creacion": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "estado": "activo"
    }
    
    return cliente_data


def guardar_mensaje_cliente(cliente, texto, tipo):
    """
    Guarda el mensaje recibido asociado al cliente.
    """
    print(f"💾 [CRM] Guardando mensaje para cliente {cliente['numero']}")
    print(f"📝 Mensaje: {texto}")
    print(f"🏷️  Tipo: {tipo}")
    
    # --- AQUÍ TU LÓGICA DE GUARDADO EN BASE DE DATOS ---
    # Aquí deberías ejecutar tu INSERT en la base de datos.
    
    return {"status": "ok", "mensaje_guardado": True}
