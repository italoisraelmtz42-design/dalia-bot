import os
import requests
import tempfile
from openai import OpenAI
import logging

logger = logging.getLogger(__name__)

def _descargar_media(media_id: str, whatsapp_token: str) -> str:
    """
    Descarga el archivo de audio de WhatsApp y guarda la ruta temporal.
    """
    url = f"https://graph.facebook.com/v18.0/{media_id}"
    headers = {"Authorization": f"Bearer {whatsapp_token}"}

    # 1. Obtener la URL de descarga real
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"No se pudo obtener info del media: {response.text}")
    
    data = response.json()
    download_url = data["url"]

    # 2. Descargar el archivo
    audio_response = requests.get(download_url, headers=headers)
    if audio_response.status_code != 200:
        raise Exception(f"No se pudo descargar el audio: {audio_response.text}")

    # 3. Guardar en archivo temporal con extensión .ogg (formato común de WhatsApp)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".ogg") as tmp_file:
        tmp_file.write(audio_response.content)
        tmp_path = tmp_file.name
    
    logger.info(f"Audio descargado y guardado temporalmente en {tmp_path}")
    return tmp_path

def transcribir_audio(ruta_archivo: str) -> str:
    """
    Envía el archivo a OpenAI Whisper y devuelve el texto.
    """
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    try:
        with open(ruta_archivo, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
            logger.info("Audio transcrito exitosamente.")
            return transcript.text
    except Exception as e:
        logger.error(f"Error en la transcripción de OpenAI: {e}")
        raise e
    finally:
        # Limpieza del archivo temporal
        if os.path.exists(ruta_archivo):
            os.remove(ruta_archivo)

def procesar_audio(media_id: str, whatsapp_token: str) -> str:
    """
    Función pública de orquestación para el flujo de audio.
    """
    ruta_audio = _descargar_media(media_id, whatsapp_token)
    return transcribir_audio(ruta_audio)