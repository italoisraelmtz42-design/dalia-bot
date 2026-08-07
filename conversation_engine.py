import os
from openai import OpenAI

def procesar_con_gpt(telefono, texto, historial=None):
    """
    Esta función es un calco exacto de la que estaba en app.py.
    Su comportamiento es idéntico.
    """
    # Inicializa el cliente de OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    
    # 1. DEFINIR EL PROMPT DEL SISTEMA (Personalidad y reglas de Dalia)
    system_prompt = (
        "Eres Dalia, una vendedora mexicana muy cálida, alegre y cercana, dueña de un negocio de recuerditos personalizados (ositos de toalla, jaboncitos, velas, etc.). "
        "Hablas como si fueras una tía o una amiga que quiere ayudar al cliente a encontrar el regalo perfecto para su evento. Usa emojis ocasionales. "
        "¡IMPORTANTE! NUNCA digas que eres una inteligencia artificial, que no puedes escuchar, o que eres un modelo de lenguaje. NUNCA hables de tus capacidades técnicas. "
        "Simplemente eres Dalia, una persona que vende recuerditos. Siempre respondes en español de México."
    )

    # 2. CONSTRUIR EL CONTEXTO DE LA CONVERSACIÓN
    messages = [{"role": "system", "content": system_prompt}]
    
    # Si hay historial de la conversación, lo agregamos
    if historial:
        messages.extend(historial)
    
    # Agregamos el mensaje actual del usuario
    messages.append({"role": "user", "content": texto})

    # 3. LLAMAR A LA IA (Se mantiene exactamente el mismo modelo original)
    response = client.chat.completions.create(
        model="gpt-4", 
        messages=messages
    )

    # Retorna la respuesta generada por la IA
    return response.choices[0].message.content
