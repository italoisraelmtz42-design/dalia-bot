import os
import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_ID")

DESTINO = "528119791795"  # Tu número con código de país

url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"

headers = {
    "Authorization": f"Bearer {ACCESS_TOKEN}",
    "Content-Type": "application/json",
}

data = {
    "messaging_product": "whatsapp",
    "to": DESTINO,
    "type": "text",
    "text": {
        "body": "Hola 👋 Soy DALIA. Este es mi primer mensaje enviado desde Python.",
    },
}

respuesta = requests.post(url, headers=headers, json=data)

print(respuesta.status_code)
print(respuesta.text)
