# src/crm.py

def cargar_cliente(numero):
    """
    Esta función busca o carga los datos de un cliente.
    """
    # --- AQUÍ VA TU LÓGICA REAL ---
    print(f"Buscando datos del cliente con el número: {numero}")
    
    # Ejemplo de lo que deberías retornar:
    # Aquí iría tu consulta a la base de datos, APIs, etc.
    cliente_encontrado = {
        "id": 1,
        "nombre": "Cliente Ejemplo",
        "telefono": numero,
        "estado": "activo"
    }
    
    return cliente_encontrado
