
"""
Modulo para transcribir audios que el cliente manda por WhatsApp.

Flujo: WhatsApp manda un mensaje tipo "audio" -> app.py descarga los bytes
(reusando descargar_imagen_whatsapp, que es generica pese al nombre) ->
esta funcion manda esos bytes a la API de transcripcion de OpenAI -> el
texto resultante se trata exactamente igual que si el cliente lo hubiera
escrito (se le pasa a preguntar_ia sin cambios).
"""

import io


def transcribir_audio(client, audio_bytes, mime_type="audio/ogg"):
    """Transcribe un audio a texto usando la API de OpenAI.

    Args:
        client: instancia de OpenAI ya inicializada (la misma que usa
            app.py para las respuestas de texto, se le pasa como parametro
            para no crear una segunda instancia ni depender de imports
            circulares con app.py).
        audio_bytes: contenido binario del audio ya descargado de WhatsApp.
        mime_type: tipo MIME que reporto WhatsApp (ej. "audio/ogg" para
            notas de voz, que es el formato mas comun en WhatsApp).

    Returns:
        El texto transcrito, o None si algo fallo (nunca lanza excepcion
        hacia afuera, para no tumbar el procesamiento del mensaje).
    """
    if not audio_bytes:
        return None

    extension = _extension_desde_mime(mime_type)
    archivo_en_memoria = io.BytesIO(audio_bytes)
    # La libreria de OpenAI necesita un nombre de archivo con extension
    # para saber como interpretar el audio; no usa esto para nada mas
    # que detectar el formato.
    archivo_en_memoria.name = f"audio.{extension}"

    try:
        resultado = client.audio.transcriptions.create(
            model="whisper-1",
            file=archivo_en_memoria,
            language="es",
        )
        texto = (resultado.text or "").strip()
        return texto if texto else None
    except Exception as e:
        print("⚠️ Error transcribiendo audio con OpenAI:", repr(e))
        return None


def _extension_desde_mime(mime_type):
    """WhatsApp casi siempre manda notas de voz como audio/ogg (codec
    opus). Este mapeo cubre los formatos mas comunes que WhatsApp puede
    llegar a mandar, con 'ogg' como respaldo razonable."""
    mapeo = {
        "audio/ogg": "ogg",
        "audio/opus": "ogg",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/mp4": "mp4",
        "audio/aac": "aac",
        "audio/amr": "amr",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
    }
    if not mime_type:
        return "ogg"
    tipo_base = mime_type.split(";")[0].strip().lower()
    return mapeo.get(tipo_base, "ogg")
