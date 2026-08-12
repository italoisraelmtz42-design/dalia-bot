import os
import logging
from openai import OpenAI

logger = logging.getLogger(__name__)

def encontrar_carpeta_conocimiento() -> str:
    """
    Busca la carpeta 'conocimiento' en las ubicaciones más probables.
    Retorna la ruta si la encuentra, si no None.
    """
    base_path = os.path.dirname(os.path.abspath(__file__))  # carpeta src
    rutas_candidatas = [
        os.path.join(base_path, 'conocimiento'),          # src/conocimiento
        os.path.join(os.path.dirname(base_path), 'conocimiento'),  # raíz del proyecto
        os.getcwd(),                                      # directorio de trabajo
        '/opt/render/project/src/conocimiento',           # ruta hardcodeada para Render
        '/opt/render/project/conocimiento',               # ruta hardcodeada alternativa
    ]
    
    for ruta in rutas_candidatas:
        if os.path.isdir(ruta):
            logger.info(f"✅ Carpeta 'conocimiento' encontrada en: {ruta}")
            return ruta
    
    logger.error("❌ No se encontró la carpeta 'conocimiento' en ninguna ubicación.")
    return None

def cargar_base_conocimiento() -> str:
    """
    Recorre recursivamente la carpeta conocimiento/ usando os.walk,
    imprime la estructura de directorios y archivos,
    y carga todos los archivos .txt (insensible a mayúsculas).
    """
    print("="*60)
    print("CARGANDO BASE DE CONOCIMIENTO...")
    print("="*60)
    
    knowledge_dir = encontrar_carpeta_conocimiento()
    if not knowledge_dir:
        print("⚠️ No se encontró la carpeta. Verifica que 'conocimiento' esté en la raíz junto a 'src'.")
        return ""
    
    print(f"🔍 Contenido de la carpeta '{knowledge_dir}':")
    
    total_archivos = 0
    total_caracteres = 0
    archivos_encontrados = []
    
    # Recorremos recursivamente el directorio
    for root, dirs, files in os.walk(knowledge_dir):
        nivel = root.replace(knowledge_dir, '').count(os.sep)
        indent = ' ' * (nivel * 2)
        if nivel == 0:
            print(f"📁 {os.path.basename(root)}/")
        else:
            print(f"{indent}📁 {os.path.basename(root)}/")
        for file in files:
            # Filtramos solo archivos .txt (insensible a mayúsculas/minúsculas)
            if file.lower().endswith('.txt'):
                full_path = os.path.join(root, file)
                archivos_encontrados.append(full_path)
                print(f"{indent}   📄 {file} → seleccionado para carga")
            else:
                print(f"{indent}   📄 {file} → ignorado (extensión no .txt)")
    
    archivos_encontrados.sort()
    
    if not archivos_encontrados:
        print("⚠️ No se encontraron archivos .txt en la carpeta conocimiento/ ni sus subcarpetas.")
        print("   Asegúrate de que los archivos tengan extensión '.txt' (no '.TXT' ni otros).")
        print("="*60)
        return ""
    
    print("="*60)
    print("CARGANDO ARCHIVOS...")
    
    knowledge_text = ""
    for file_path in archivos_encontrados:
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                total_caracteres += len(content)
                relative_path = os.path.relpath(file_path, knowledge_dir)
                print(f"✅ {relative_path}  ({len(content)} caracteres)")
                knowledge_text += f"\n--- INFORMACIÓN DEL ARCHIVO '{relative_path}' ---\n{content}\n"
                total_archivos += 1
        except Exception as e:
            print(f"❌ Error leyendo {file_path}: {e}")
    
    print(f"\nTOTAL DE ARCHIVOS : {total_archivos}")
    print(f"TOTAL CARACTERES  : {total_caracteres}")
    print("="*60)
    
    return knowledge_text.strip() if knowledge_text else ""

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
