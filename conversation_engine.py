import os
import glob
from openai import OpenAI

def encontrar_carpeta_conocimiento() -> str:
    """
    Busca la carpeta 'conocimiento' en la raíz del proyecto.
    """
    # En Render, el código está en /opt/render/project/src/
    # Como 'conocimiento' está en el mismo nivel que app.py, está en src/
    base_path = os.path.dirname(os.path.abspath(__file__))
    ruta_candidata = os.path.join(base_path, 'conocimiento')
    
    if os.path.isdir(ruta_candidata):
        return ruta_candidata
    
    # Fallback a la raíz del proyecto (si alguna vez cambia la estructura)
    project_root = os.path.dirname(base_path)
    ruta_candidata_2 = os.path.join(project_root, 'conocimiento')
    if os.path.isdir(ruta_candidata_2):
        return ruta_candidata_2
    
    return None

def cargar_base_conocimiento() -> str:
    """
    Lee TODOS los archivos .txt (sin importar mayúsculas/minúsculas) de la carpeta conocimiento/ y sus subcarpetas.
    """
    knowledge_text = ""
    
    print("="*60)
    print("CARGANDO BASE DE CONOCIMIENTO...")
    
    # 1. Buscar la carpeta
    knowledge_dir = encontrar_carpeta_conocimiento()
    
    if not knowledge_dir:
        print(f"❌ ERROR: No se encontró la carpeta 'conocimiento'.")
        print("   Asegúrate de que la carpeta 'conocimiento' esté en el mismo nivel que 'app.py'.")
        print("="*60)
        return ""

    print(f"✅ Carpeta encontrada en: {os.path.abspath(knowledge_dir)}")
    
    # 2. Buscar recursivamente archivos .txt y .TXT (insensible a mayúsculas)
    try:
        # Buscamos tanto .txt como .TXT para cubrir el caso de Linux
        files_txt = glob.glob(os.path.join(knowledge_dir, '**', '*.txt'), recursive=True)
        files_TXT = glob.glob(os.path.join(knowledge_dir, '**', '*.TXT'), recursive=True)
        
        # Combinamos las listas y eliminamos duplicados (por si acaso)
        files = list(set(files_txt + files_TXT))
        files.sort()
        
        total_archivos = len(files)
        total_caracteres = 0
        
        if total_archivos == 0:
            print(f"⚠️ La carpeta '{os.path.abspath(knowledge_dir)}' existe, pero no contiene archivos .txt o .TXT.")
            print("   Verifica que dentro de las subcarpetas haya archivos con estas extensiones.")
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
