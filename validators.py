from constantes import ESTADOS_VALIDOS, MODOS_VALIDOS, TRANSICIONES_VALIDAS, ORIGENES_VALIDOS

def validar_estado(estado: str) -> bool:
    return estado in ESTADOS_VALIDOS

def validar_modo_atencion(modo: str) -> bool:
    return modo in MODOS_VALIDOS

def validar_origen(origen: str) -> bool:
    return origen in ORIGENES_VALIDOS

def validar_transicion(estado_actual: str, nuevo_estado: str) -> bool:
    return nuevo_estado in TRANSICIONES_VALIDAS.get(estado_actual, [])