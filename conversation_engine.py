import os
import glob
from openai import OpenAI

def encontrar_carpeta_conocimiento() -> str:
    """
    Busca la carpeta 'conocimiento' en las 3 ubicaciones más probables del contenedor.
    Retorna la ruta absoluta de la carpeta si la encuentra y tiene archivos .txt.
    """
    # Candidatas: 1. directorio actual (src), 2. junto a este script, 3. raíz del proyecto
    base_paths = [
        os.getcwd(),                            # /opt/render/project/src
        os.path.dirname(os.path.abspath(__file__)), # /opt/render/project/src
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))) # /opt/render/project
    ]
    
    # Eliminar duplicados y probar cada una
    for base in base_paths:
        ruta_candidata = os.path.join(base, 'conocimiento')
        if os.path.isdir(ruta_candidata):
            # Verificar si tiene archivos .txt
            archivos = glob.glob(os.path.join(ruta_candidata, '**', '*.txt'), recursive=True)
            if archivos:
                print(f"✅ ENCONTRADA: Carpeta 'conocimiento' en {ruta_candidata}")
                return ruta_candidata
            else:
                print(f"⚠️ Encontrada carpeta '{ruta_candidata}', pero está vacía de archivos .txt.")
    
    # Si no la encuentra, retorna None
    print("❌ No se encontró la carpeta 'conocimiento' en ninguna ubicación común.")
    return None

def cargar_base_conocimiento() -> str:
    """
    Lee TODOS los archivos .txt de la carpeta conocimiento/ y sus subcarpetas.
    """
    knowledge_text = ""
    
    print("="*60)
    print("CARGANDO BASE DE CONOCIMIENTO...")
    
    # 1. Buscar la carpeta
    knowledge_dir = encontrar_carpeta_conocimiento()
    
    if not knowledge_dir:
        print("   Verifica que la carpeta 'conocimiento' esté en la raíz del proyecto (junto a 'src').")
        print("="*60)
        return ""

    # 2. Buscar recursivamente en todas las subcarpetas (**/*.txt)
    try:
        files = glob.glob(os.path.join(knowledge_dir, '**', '*.txt'), recursive=True)
        files.sort()
        
        total_archivos = len(files)
        total_caracteres = 0
        
        if total_archivos == 0:
            print(f"⚠️ La carpeta '{knowledge_dir}' está vacía o no tiene archivos .txt.")
            print("="*60)
            return ""

        # 3. Procesar cada archivo y mostrar detalles
        for file_path in files:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                total_caracteres += len(content)
                relative_path = os.path.relpath(file_path, knowledge_dir)
                print(f"✅ {relative_path}  ({len(content)} caracteres)")

        print(f"\nTOTAL DE ARCHIVOS : {total_archivos}")
        print(f"TOTAL CARACTERES  : {total_caracteres}")
        print("="*60)
        
        # 4. Construir el texto completo para la IA
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

    response = client.chat.completions.create(
        model="gpt-4", 
        messages=messages
    )

    return response.choices[0].message.content
