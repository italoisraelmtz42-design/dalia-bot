import os
import logging
from openai import OpenAI

logger = logging.getLogger(__name__)

def encontrar_carpeta_conocimiento() -> str:
    """
    Busca la carpeta 'conocimiento' en las ubicaciones probables y muestra el contenido.
    """
    base_path = os.path.dirname(os.path.abspath(__file__))  # src/
    rutas_candidatas = [
        os.path.join(base_path, 'conocimiento'),                # src/conocimiento
        os.path.join(os.path.dirname(base_path), 'conocimiento'), # raíz del proyecto
        os.getcwd(),                                             # directorio de trabajo
    ]
    
    print("="*60)
    print("🔍 DIAGNÓSTICO DE LA CARPETA DE CONOCIMIENTO")
    for ruta in rutas_candidatas:
        print(f"   Probando: {ruta}")
        if os.path.isdir(ruta):
            print(f"   ✅ Carpeta encontrada: {ruta}")
            # Listar contenido de la carpeta y subcarpetas
            for root, dirs, files in os.walk(ruta):
                nivel = root.replace(ruta, '').count(os.sep)
                indent = ' ' * (nivel * 2)
                print(f"{indent}📁 {os.path.basename(root)}/")
                for f in files:
                    print(f"{indent}   📄 {f}")
            return ruta
    
    print("❌ No se encontró la carpeta 'conocimiento' en ninguna ubicación.")
    print("="*60)
    return None

def cargar_base_conocimiento() -> str:
    """
    Recorre recursivamente la carpeta conocimiento/ y carga archivos .txt (insensible a mayúsculas).
    """
    knowledge_text = ""
    
    print("="*60)
    print("CARGANDO BASE DE CONOCIMIENTO...")
    
    knowledge_dir = encontrar_carpeta_conocimiento()
    if not knowledge_dir:
        print("="*60)
        return ""

    total_archivos = 0
    total_caracteres = 0
    archivos_encontrados = []

    # Recorremos recursivamente el directorio
    for root, dirs, files in os.walk(knowledge_dir):
        for file in files:
            # Filtramos solo archivos con extensión .txt (sin importar mayúsculas/minúsculas)
            if file.lower().endswith('.txt'):
                full_path = os.path.join(root, file)
                archivos_encontrados.append(full_path)

    archivos_encontrados.sort()

    if not archivos_encontrados:
        print(f"⚠️ La carpeta '{knowledge_dir}' existe, pero no contiene archivos .txt.")
        print("   Revisa el diagnóstico anterior para ver qué archivos hay realmente.")
        print("   Asegúrate de que los archivos tengan extensión '.txt' (no '.TXT', '.txt~', '.md', etc.).")
        print("="*60)
        return ""

    # Procesamos cada archivo
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
