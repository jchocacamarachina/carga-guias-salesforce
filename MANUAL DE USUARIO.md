# MANUAL DE USUARIO
## Sistema de Guías de Entrega

---

> **Para quién es este manual:** Personal de entregas y supervisores que usan la app para registrar fotos de entregas al cliente.

---

## ¿QUÉ HACE ESTA APLICACIÓN?

Esta app te permite **tomar fotos como prueba de una entrega** y guardarlas automáticamente en el sistema. Solo necesitas el número de orden (OP) y la cámara de tu celular.

---

## PARTE 1: CÓMO REGISTRAR UNA ENTREGA

### Paso 1 — Buscar la Orden

1. Abre la aplicación en tu celular o computadora.
2. Verás un campo que dice **"OP-"** con un cuadro vacío al lado.
3. Escribe el **número de la orden** (solo los números, por ejemplo: `13563`).
4. Toca el botón azul que dice **"Buscar"**.

**¿Qué pasa después?**
- Si la orden existe: aparece una caja verde con el nombre de la orden y un ✅. Ya puedes continuar.
- Si la orden NO existe: aparece un mensaje rojo ❌ que dice "Orden no encontrada". Revisa que el número esté bien escrito.

> **Tip:** También puedes presionar la tecla **Enter** en lugar de tocar "Buscar".

---

### Paso 2 — (Opcional) Agregar más órdenes del mismo cliente

Si el cliente tiene **más de una orden** que entregar al mismo tiempo, puedes agregarlas todas juntas:

1. Después de encontrar la primera orden, toca el botón **"+ Agregar otra OP"**.
2. Escribe el número de la segunda orden y toca **"Agregar"**.
3. Repite para todas las órdenes que necesites.

**Importante:** Todas las órdenes deben ser del **mismo cliente**. Si intentas agregar una orden de otro cliente, la app te mostrará un error.

Para quitar una orden de la lista, toca el botón rojo **"Quitar"** que aparece al lado de cada una.

---

### Paso 3 — Tomar las Fotos

1. Toca el botón azul grande que dice **"TOMAR FOTO"**.
2. La cámara de tu dispositivo se abrirá automáticamente.
3. Toma la foto de la entrega.
4. La foto aparecerá en la parte de abajo como una imagen pequeña (miniatura).
5. Puedes tomar **más de una foto** tocando el botón "TOMAR FOTO" cada vez que necesites.

**Para borrar una foto específica:** Toca la **"×"** que aparece encima de la foto.

**Para borrar TODAS las fotos:** Toca el botón gris **"LIMPIAR"**.

> **Nota:** El botón "TOMAR FOTO" solo se activa después de encontrar una orden. Si está gris, primero busca la orden.

---

### Paso 4 — Subir las Fotos

Cuando ya tienes la orden y las fotos listas:

1. Toca el botón verde grande que dice **"SUBIR FOTOS"**.
2. Espera. Verás mensajes que indican el progreso:
   - 🔎 *Verificando órdenes...* — Está confirmando el número de orden.
   - ✅ *Órdenes verificadas* — Todo correcto.
   - ⬆️ *Subiendo fotos (1/2)...* — Está enviando las fotos.
   - 🧾 *Guardando en Salesforce...* — Está registrando en el sistema.
   - ✅ *Guardado en Salesforce* — ¡Listo!
3. Cuando termina, aparece una **pantalla verde grande** con un ✓ y el mensaje **"¡Fotos guardadas!"**.

> **No cierres la app mientras ves los mensajes de progreso.**

---

### Paso 5 — Nueva entrega

Después de que aparece la pantalla verde de éxito, tienes dos opciones:
- Tocar el botón **"Nueva entrega"** para registrar otra entrega.
- Esperar 6 segundos: la app se reinicia sola automáticamente.

---

## PARTE 2: ERRORES COMUNES Y SOLUCIONES

| Mensaje que ves | Qué significa | Qué hacer |
|---|---|---|
| ❌ Orden no encontrada | El número de orden no existe en el sistema | Verifica que el número esté bien escrito |
| "Esa OP ya está agregada" | Intentaste agregar una orden que ya está en la lista | No es necesario agregarla de nuevo |
| "Esa OP es de otro cliente" | Las órdenes son de clientes distintos | Sube cada cliente por separado |
| "Archivo no permitido (solo fotos)" | Intentaste subir un archivo que no es imagen | Solo se aceptan fotos |
| "Debes seleccionar al menos 1 foto" | Tocaste "Subir fotos" sin tomar ninguna foto | Toma al menos una foto primero |

---

## PARTE 3: VER EL HISTORIAL DE ENTREGAS (Para Supervisores)

### Cómo acceder

Abre en el navegador la dirección de la app y agrega **/registro** al final.

Por ejemplo, si la app está en:
```
http://mi-app.com/
```
El historial está en:
```
http://mi-app.com/registro
```

### Qué muestra el historial

Cada entrega registrada aparece como una tarjeta con:
- **Número de orden** (OP-XXXXXXX) en negritas.
- **Fecha y hora** de cuando se subieron las fotos.
- **Nombre del cliente**.
- **Botones de foto** (Foto 1, Foto 2, etc.) — toca cualquiera para ver la imagen.

### Cómo actualizar

Toca el botón **"Actualizar"** en la parte de arriba para ver las entregas más recientes.

> **Importante:** El historial se reinicia cada vez que el servidor se reinicia. Solo muestra las entregas registradas desde el último reinicio.

---

## PARTE 4: PREGUNTAS FRECUENTES

**¿Cuántas fotos puedo subir por entrega?**
No hay un límite fijo. Puedes tomar las fotos que necesites.

**¿Puedo usar la app desde mi celular?**
Sí. La app está diseñada para funcionar en celulares. Al tocar "TOMAR FOTO", se abre directamente la cámara trasera.

**¿Las fotos quedan guardadas si cierro la app antes de subirlas?**
No. Si cierras la app o el navegador antes de tocar "SUBIR FOTOS", las fotos se pierden. Deberás tomarlas de nuevo.

**¿Qué pasa si la conexión a internet se corta mientras subo?**
Verás un mensaje de error en el área de progreso. Intenta de nuevo cuando tengas conexión estable.

**¿Puedo agregar más fotos después de ya haberlas subido?**
No directamente. Si necesitas agregar fotos a una orden que ya fue registrada, la app mostrará una advertencia de que ya tiene guía de entrega. Si subes de nuevo, las fotos anteriores serán reemplazadas.

**¿Dónde quedan guardadas las fotos?**
Las fotos se guardan automáticamente en Salesforce y también se genera un enlace público para compartir con el cliente.

---

## RESUMEN RÁPIDO (GUÍA DE BOLSILLO)

```
1. Escribe el número de la orden → toca "Buscar"
2. ✅ Aparece el nombre de la orden → continúa
3. Toca "TOMAR FOTO" → toma las fotos necesarias
4. Toca "SUBIR FOTOS" → espera los mensajes de progreso
5. ✓ Pantalla verde = entrega registrada con éxito
6. Toca "Nueva entrega" para continuar con la siguiente
```

---

*Para soporte técnico, contacta al administrador del sistema.*
