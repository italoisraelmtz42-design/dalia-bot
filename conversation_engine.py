import os
import glob
from openai import OpenAI

def cargar_base_conocimiento() -> str:
    """
    Lee TODOS los archivos .txt de la carpeta conocimiento/ y sus subcarpetas.
    """
    knowledge_text = ""
    # 1. Definir el directorio base
    knowledge_dir = os.path.join(os.path.dirname(__file__), 'conocimiento')
    
    # 2. Buscar recursivamente en todas las subcarpetas (**/*.txt)
    try:
        files = glob.glob(os.path.join(knowledge_dir, '**', '*.txt'), recursive=True)
        files.sort() # Orden alfabético para mantener consistencia
        
        if not files:
            print("⚠️ No se encontraron archivos .txt en la carpeta conocimiento/ o sus subcarpetas.")
            return ""

        for file_path in files:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                # Extraemos el nombre del archivo para dar contexto
                relative_path = os.path.relpath(file_path, knowledge_dir)
                knowledge_text += f"\n--- INFORMACIÓN DEL ARCHIVO '{relative_path}' ---\n{content}\n"
                
        return knowledge_text.strip()
    except Exception as e:
        print(f"⚠️ Error al cargar la base de conocimiento: {e}")
        return ""

def procesar_con_gpt(telefono, texto, historial=None):
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    
    system_prompt = (
        "Eres Dalia, una vendedora mexicana muy cálida, alegre y cercana, dueña de un negocio de recuerditos personalizados (ositos de toalla, jaboncitos, velas, etc.). "
        "Hablas como si fueras una tía o una amiga que quiere ayudar al cliente a encontrar el regalo perfecto para su evento. Usa emojis ocasionales. "
        "¡IMPORTANTE! NUNCA digas que eres una inteligencia artificial, que no puedes escuchar, o que eres un modelo de lenguaje. NUNCA hables de tus capacidades técnicas. "
        "Simplemente eres Dalia, una persona que vende recuerditos. Siempre respondes en español de México. "
        "NUNCA inventes productos ni precios. Si el producto o precio no está en la información que te proporciono, pregúntale amablemente al cliente."
    )

    base_conocimiento = cargar_base_conocimiento()

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "system", "content": f"A continuación tienes la información oficial del negocio, el catálogo, los colores, las políticas y las reglas de venta. Úsala para responder al cliente:\n\n{base_conocimiento}"}
    ]
    
    if historial:
        messages.extend(historial)
    
    messages.append({"role": "user", "content": texto})

    # Cambia "gpt-4" por "gpt-4o-mini" si te da errores de límites de tokens (429)
    response = client.chat.completions.create(
        model="gpt-4", 
        messages=messages
    )

    return response.choices[0].message.content
