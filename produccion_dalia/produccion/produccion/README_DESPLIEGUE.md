# Producción Dalia — cómo desplegarlo

Esta carpeta es una app completamente separada del bot (`dalia-bot`). No
comparte memoria, CPU ni base de datos con él, así que no hay ningún riesgo
de que afecte al bot que le vende a tus clientes.

## 1. Sube esta carpeta a tu repo de GitHub

Copia la carpeta `produccion/` completa dentro de tu repo `dalia-bot`
(al mismo nivel que `app.py` del bot), y súbela a GitHub como siempre.

## 2. Crea un NUEVO servicio en Render

En Render, dale a **New > Web Service**, elige el mismo repo `dalia-bot`, y
en la configuración pon:

- **Root Directory**: `produccion`
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `gunicorn app:app --workers 1 --threads 4 --timeout 120`
- **Plan**: Starter ($7/mes) es suficiente para esto.

## 3. ⚠️ MUY IMPORTANTE: agrega un Persistent Disk

Por defecto, el disco de un servicio de Render es **efímero** — se borra
cada vez que hay un deploy o un reinicio. Si no haces este paso, vas a
perder los pedidos guardados y las fotos de las notas tarde o temprano.

En la configuración del nuevo servicio, ve a **Disks > Add Disk**:
- Nombre: `produccion-data` (o el que quieras)
- Mount Path: `/var/data`
- Tamaño: 1 GB es más que suficiente para empezar (~$0.25/mes extra)

## 4. Variables de entorno

En **Environment**, agrega:

| Variable | Valor |
|---|---|
| `PRODUCCION_PASSWORD_ISRAEL` | Tu contraseña |
| `PRODUCCION_PASSWORD_DALIA` | La contraseña de Dalia |
| `PRODUCCION_PASSWORD_DIANA` | La contraseña de Diana |
| `FLASK_SECRET_KEY` | Cualquier texto largo y aleatorio (para las sesiones) |
| `OPENAI_API_KEY` | La misma que ya usa el bot |
| `PRODUCCION_DB_PATH` | `/var/data/produccion.db` |
| `FOTOS_DIR` | `/var/data/fotos_notas` |

Las dos últimas son las que hacen que la base de datos y las fotos se
guarden dentro del Persistent Disk que agregaste en el paso 3, en vez del
disco efímero.

⚠️ **Actualización 23 ago 2026 — 3 contraseñas separadas.** Antes había
una sola `PRODUCCION_PASSWORD` compartida por los 3. Ahora cada quien
tiene la suya, para poder esconderle lo financiero del negocio a Diana y
para que la comisión de cada quien se calcule sola y sin errores. Si ya
tenías `PRODUCCION_PASSWORD` configurada, se sigue usando como respaldo
de tu contraseña (Israel) mientras no agregues `PRODUCCION_PASSWORD_ISRAEL`
-- pero **Dalia y Diana no van a poder entrar hasta que agregues sus dos
contraseñas nuevas** en Render.

## 5. Listo

Cuando termine de desplegar, te va a dar una URL tipo
`https://produccion-dalia.onrender.com`. Compártela con Dalia y Diana,
cada quien con SU contraseña. Desde el celular pueden entrar, subir la
foto de la nota confirmada, revisar lo que la IA leyó, corregir si hace
falta y guardar.

Vas a poder ver ahí mismo qué hay que fabricar/entregar hoy, mañana, en
la semana, en el mes, o cualquier día del calendario (pasado o futuro);
una pestaña de finanzas con anticipos, total vendido y saldo pendiente
por período (Diana no la ve); la comisión de Diana por período; el
inventario de materia prima; y una lista imprimible de todo lo que se
entrega la próxima semana con el resumen de cuánto fabricar de cada
producto.

## Notas

- El costo total extra es de ~$7.25/mes (el servicio + el disco), separado
  de lo que ya pagas por el bot.
- El bot (`dalia-bot`) NO se tocó para nada -- esta app es 100% aparte.
- Si algún pedido tiene una fecha de entrega que la IA o alguien escribió
  en un formato raro (no DD/MM/AAAA), no va a aparecer en las pestañas de
  Hoy/Mañana/Semana/Mes hasta que se corrija -- pero sí va a aparecer en la
  pestaña "Todos" con un aviso, para que no se pierda.
- Diana puede ver y corregir los datos de una nota (incluido el anticipo
  y el total) justo cuando la sube y confirma -- son datos que ya trae
  la nota física en la mano. Lo que no puede ver después es el resumen
  financiero del negocio (Finanzas) ni los montos ya guardados de un
  pedido al abrirlo o editarlo.
- La comisión de Diana se calcula sobre los pedidos que ella misma sube
  (ahora identificado automáticamente por su login, ya no por un campo de
  texto). Pedidos que ya estaban guardados ANTES de esta actualización
  conservan el texto que se haya escrito a mano en su momento -- si
  coincide con "Diana" (sin importar mayúsculas), sí cuentan; si tenía
  alguna falta de ortografía, no. De aquí en adelante ya no depende de
  que nadie escriba nada a mano.
