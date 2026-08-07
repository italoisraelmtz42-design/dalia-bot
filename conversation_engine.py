import os
import glob
from openai import OpenAI

def cargar_base_conocimiento() -> str:
    """
    Lee TODOS los archivos .txt de la carpeta conocimiento/ y los convierte en un
    bloque de contexto para la IA. Esto evita que el bot invente información.
    """
    knowledge_text = ""
    knowledge_path = os.path.join(os.path.dirname(__file__), 'conocimiento', '*.txt')
    
    try:
        files = glob.glob(knowledge_path)
        # Ordenamos los archivos alfabéticamente (por el número de prefijo)
        files.sort()
        
        for file_path in files:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                file_name = os.path.basename(file_path)
                # Agregamos el contenido del archivo al contexto
                knowledge_text += f"\n--- INFORMACIÓN DEL ARCHIVO '{file_name}' ---\n{content}\n"
                
        return knowledge_text.strip()
    except Exception as e:
        # Si falla la carga, al menos el bot seguirá funcionando con el prompt base
        print(f"⚠️ Error al cargar la base de conocimiento: {e}")
        return ""

def procesar_con_gpt(telefono, texto, historial=None):
    """
    Esta función inyecta la base de conocimiento (de la carpeta 'conocimiento/')
    antes de llamar a OpenAI.
    """
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    
    # 1. DEFINIR EL PROMPT DEL SISTEMA (Personalidad y reglas de Dalia)
    system_prompt = (
        "Eres Dalia, una vendedora mexicana muy cálida, alegre y cercana, dueña de un negocio de recuerditos personalizados (ositos de toalla, jaboncitos, velas, etc.). "
        "Hablas como si fueras una tía o una amiga que quiere ayudar al cliente a encontrar el regalo perfecto para su evento. Usa emojis ocasionales. "
        "¡IMPORTANTE! NUNCA digas que eres una inteligencia artificial, que no puedes escuchar, o que eres un modelo de lenguaje. NUNCA hables de tus capacidades técnicas. "
        "Simplemente eres Dalia, una persona que vende recuerditos. Siempre respondes en español de México. "
        "NUNCA inventes productos ni precios. Si el producto o precio no está en la información que te proporciono, pregúntale amablemente al cliente."
    )

    # 2. CARGAR LA BASE DE CONOCIMIENTO REAL DE LA CARPETA
    base_conocimiento = cargar_base_conocimiento()

    # 3. CONSTRUIR EL CONTEXTO DE LA CONVERSACIÓN
    messages = [
        {"role": "system", "content": system_prompt},
        # Inyectamos el conocimiento como parte del contexto para que la IA lo tenga presente
        {"role": "system", "content": f"A continuación tienes la información oficial del negocio, el catálogo, los colores, las políticas y las reglas de venta. Úsala para responder al cliente:\n\n{base_conocimiento}"}
    ]
    
    if historial:
        messages.extend(historial)
    
    messages.append({"role": "user", "content": texto})

    # 4. LLAMAR A LA IA 
    response = client.chat.completions.create(
        model="gpt-4", 
        messages=messages
    )

    return response.choices[0].message.content
