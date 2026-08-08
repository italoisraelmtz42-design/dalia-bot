import os
import glob
from openai import OpenAI

def cargar_base_conocimiento() -> str:
    """
    Lee TODOS los archivos .txt de la carpeta conocimiento/ y sus subcarpetas.
    Imprime diagnóstico detallado.
    """
    knowledge_text = ""
    # 1. Definir el directorio base
    knowledge_dir = os.path.join(os.path.dirname(__file__), 'conocimiento')
    
    # 2. Diagnóstico: imprimir ruta absoluta
    print("="*60)
    print("CARGANDO BASE DE CONOCIMIENTO...")
    print(f"Ruta de la carpeta conocimiento: {os.path.abspath(knowledge_dir)}")
    
    # 3. Verificar si la carpeta existe
    if not os.path.exists(knowledge_dir):
        print(f"❌ ERROR: La carpeta '{knowledge_dir}' NO EXISTE.")
        print("   Verifica que la carpeta 'conocimiento' esté en la misma ubicación que este script.")
        print("="*60)
        return ""
    
    if not os.path.isdir(knowledge_dir):
        print(f"❌ ERROR: '{knowledge_dir}' no es un directorio.")
        print("="*60)
        return ""

    # 4. Buscar recursivamente en todas las subcarpetas (**/*.txt)
    try:
        files = glob.glob(os.path.join(knowledge_dir, '**', '*.txt'), recursive=True)
        files.sort()  # Orden alfabético para consistencia
        
        total_archivos = len(files)
        total_caracteres = 0
        
        if total_archivos == 0:
            print(f"⚠️ No se encontraron archivos .txt en: {os.path.abspath(knowledge_dir)}")
            print("   Posibles causas: la carpeta está vacía, o los archivos tienen otra extensión.")
            print("="*60)
            return ""

        # 5. Procesar cada archivo y mostrar detalles
        for file_path in files:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                total_caracteres += len(content)
                relative_path = os.path.relpath(file_path, knowledge_dir)
                print(f"✅ {relative_path}  ({len(content)} caracteres)")

        print(f"\nTOTAL DE ARCHIVOS : {total_archivos}")
        print(f"TOTAL CARACTERES  : {total_caracteres}")
        print("="*60)
        
        # 6. Construir el texto completo para la IA (como antes)
        for file_path in files:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                relative_path = os.path.relpath(file_path, knowledge_dir)
                knowledge_text += f"\n--- INFORMACIÓN DEL ARCHIVO '{relative_path}' ---\n{content}\n"
                
        return knowledge_text.strip()
        
    except Exception as e:
        print(f"❌ ERROR al leer los archivos: {e}")
        print("="*60)
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
