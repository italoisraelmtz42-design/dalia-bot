# Correcciones aplicadas (auditoría completa)

## Prioridad 1 — Multi-producto
- `pedido_vacio()` incluye `items: []`
- Tool `actualizar_pedido` acepta array `items` + fusión inteligente en `aplicar_actualizacion_pedido` (agrega/actualiza por nombre de producto, no pisa los demás)
- `crear_pedido_desde_borrador` itera e inserta cada item en `pedido_items`
- `generar_resumen` lista todos los items
- `notificar_a_dalia` lista todos los items + total determinístico
- `calcular_total()` nuevo: subtotal + urgente + envío (el modelo ya no inventa el total)
- `pedido_para_ram` convierte multi-item
- `campos_faltantes_pedido()` evalúa por item (`constantes.py`)

## Prioridad 2 — Catálogo siempre presente
- `seleccionar_conocimiento_relevante` incluye SIEMPRE todos los archivos `Productos/*` (precios oficiales siempre visibles → menos invención)

## Prioridad 3 — Verdad > consistencia
- `MEMORIA_CONVERSACIONAL.txt`: excepción explícita si lo confirmado contradice catálogo
- Prompt: reglas anti-inventar y corrección de precios elevados a prioridad máxima
- `ERRORES_PROHIBIDOS.txt` reforzado

## Prioridad 4 — Variantes
- Nuevo `035_Variantes_De_Producto.txt` (preguntar chica/grande, con/sin jabón, etc. antes de cotizar)
- Referencia en `034_Como_Recomendar_Productos.txt`

## Prioridad 5/6 — Total + gate bancario
- Total oficial inyectado en el system prompt desde `calcular_total`
- Gate de código: `filtrar_datos_bancarios_si_no_hay_total` bloquea CLABE/tarjeta si no hay total válido

## Bugs base previos
- `pedido_manager.py` creado (faltaba)
- `knowledge_text = ""` en `conversation_engine.py`
- `get_connection` alias en `database.py`
- Color gris explícitamente prohibido

## NO resuelto aún (fuera de este mapa)
- Meta 3 (Dalia ve mensajes en tiempo real)
- Coexistencia número 1795
- Código 🧸☠️🧸
- Dashboard gasto OpenAI
- Reintentos WhatsApp/OpenAI
- Revisión sistemática completa KB

## P0 quirúrgico (post 93/100)
1. PRECIOS_CATALOGO en Python + resolver_precio / aplicar_precio_oficial
   - GPT ya no escribe precio_unitario; el sistema lo asigna.
2. Tools: agregar_item, actualizar_item, eliminar_item
   - actualizar_pedido solo metadatos (entrega, urgente, anticipo).
   - Ya no se reemplaza la lista completa de items desde el modelo.
3. Colores validados al agregar/actualizar items.
