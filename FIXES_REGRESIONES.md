# Correcciones a las 2 regresiones detectadas en la auditoría del rediseño

## 1. Código de reactivación/reset — vuelve a ser 🧸☠️🧸 + ahora restringido

**Qué estaba mal:** el rediseño había vuelto a usar 🧸🧸 (dos ositos) como
disparador, el mismo problema que ya se había corregido antes ("osito" es
el nombre de un producto real; un cliente cotizando 2 ositos podía
borrarse su propio historial y pedido sin querer). Tampoco validaba quién
lo mandaba.

**Qué se hizo:**
- El disparador vuelve a ser la secuencia `🧸☠️🧸` (no dos ositos sueltos).
- Se agregó `NUMEROS_AUTORIZADOS_RESET`: solo Dalia (`DALIA_WHATSAPP_NUMERO`)
  y cualquier número extra que agregues en `.env` vía
  `RESET_NUMEROS_AUTORIZADOS` (separados por comas) pueden usar el reset.
  Si alguien no autorizado lo manda, no pasa nada (ni se le confirma ni se
  le desmiente que existe el código — el mensaje solo se guarda en el
  historial como cualquier otro).
- Si no configuras ni `DALIA_WHATSAPP_NUMERO` ni `RESET_NUMEROS_AUTORIZADOS`,
  el reset queda deshabilitado para todos (antes, sin configurar nada,
  quedaba abierto a cualquiera).

**Debes hacer:** llenar `DALIA_WHATSAPP_NUMERO` en tu `.env` (y opcionalmente
`RESET_NUMEROS_AUTORIZADOS` con tu número de pruebas) antes de poder usar
el código de reset otra vez.

## 2. `requirements.txt` faltante

**Qué estaba mal:** el `Procfile` apunta a `gunicorn app:app`, pero no
existía ningún `requirements.txt` en el proyecto. Un deploy a Render tal
cual habría fallado por no encontrar las dependencias.

**Qué se hizo:** se creó `requirements.txt` con las 5 dependencias reales
que usa el código (`flask`, `requests`, `openai`, `python-dotenv`,
`gunicorn`), verificado revisando todos los `import` del proyecto.

## 3. (Extra, detectado en la misma revisión) Datos bancarios reales
   hardcodeados en `app.py`

**Qué estaba mal:** el número de tarjeta y la CLABE reales estaban
escritos directo en `app.py` (para poder detectarlos y filtrarlos en dos
funciones). Si el repo se sube a GitHub, esos datos quedan expuestos en
el historial de commits aunque el repo sea privado.

**Qué se hizo:** se movieron a variables de entorno nuevas:
`DATOS_BANCARIOS_TARJETA`, `DATOS_BANCARIOS_CLABE`, `DATOS_BANCARIOS_BANCO`.
El código ya no tiene esos números escritos.

**Debes hacer:** llenar esas 3 variables en tu `.env` con los datos reales
(revisa `.env.example` para el formato). Mientras no las llenes, verás una
advertencia en los logs al arrancar, y las dos funciones que dependen de
ellas (detectar si ya se mandaron los datos de pago, y bloquear que se
manden antes de tener un total) no van a funcionar bien.

---

Todo lo demás del rediseño (multi-producto, precios oficiales en Python,
validación de colores, gate bancario, etc.) se revisó y quedó igual — no
se tocó porque ya resolvía correctamente los problemas de la auditoría
anterior.
