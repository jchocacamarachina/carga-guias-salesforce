# Cómo enterarse de que el servicio se cayó

El problema que esto resuelve: el repartidor sube sus fotos, algo falla en el camino, y
en la oficina nadie se entera hasta que un cliente reclama que nunca le llegó nada.

Hay **dos mitades** y hacen falta las dos, porque una app caída no puede avisar de su
propia caída.

---

## Mitad 1 — La app avisa cuando algo se rompe

Ya está implementado. Configurá `ALERT_WEBHOOK_URL` y la app manda un POST con este JSON
cada vez que detecta un problema de servicio:

```json
{
  "app": "guias-entrega",
  "clave": "webhook_make_caido",
  "titulo": "El aviso al cliente no se está enviando",
  "detalle": "La entrega de ACME (OP-0013563) se guardó bien en Salesforce, pero...",
  "timestamp": "2026-07-30T14:22:09-05:00"
}
```

### Qué dispara una alerta

| `clave` | Cuándo salta | Por qué importa |
|---|---|---|
| `salesforce_caido` | No se puede iniciar sesión en Salesforce | Ninguna entrega se registra; quedan en cola en los teléfonos |
| `imgbb_caido` | Ninguna foto obtuvo URL pública | Sin links no se le puede mandar nada al cliente |
| `webhook_make_caido` | Make no respondió o devolvió error | **La entrega se guardó pero el cliente NO recibió su mensaje** |
| `db_escritura_falla` | No se pudo escribir el historial | La entrega existe en Salesforce pero no queda registrada |

Lo que **no** dispara alerta, a propósito: que el repartidor tipee mal una OP, o que
mezcle órdenes de dos clientes. Eso es error de uso, no servicio caído. Si alertara por
eso, en dos semanas nadie leería las alertas.

Cada `clave` tiene un silencio de `ALERT_COOLDOWN_MIN` (30 min por defecto): una caída
de Salesforce durante una ruta de 30 entregas manda **un** mensaje, no 30.

### Armarlo con Make (lo más rápido, ya lo usás)

1. Escenario nuevo → módulo **Webhooks → Custom webhook** → copiar la URL.
2. Conectarle un módulo de WhatsApp / Gmail / SMS al supervisor.
3. En el mensaje, usar los campos `titulo` y `detalle` del webhook.
4. Poner esa URL en `ALERT_WEBHOOK_URL` y reiniciar la app.

### Probar que funciona

```
https://tu-app/health/probar-alerta?token=EL_ADMIN_TOKEN
```

Manda una alerta de prueba salteando el cooldown. Si no te llega el mensaje, el canal
está mal configurado — y conviene descubrirlo ahora y no el día que se caiga Salesforce.

---

## Mitad 2 — Alguien de afuera vigila que la app siga viva

**Esta es la parte imprescindible.** Si el servidor está apagado, dormido, o el deploy
quedó roto, la app no puede mandar ninguna alerta: está muerta. Hace falta algo externo
que la pinchee.

La app expone `/health` sin token (los servicios de uptime gratuitos no mandan headers,
y ahí no se expone nada sensible):

```
GET https://tu-app/health

200 OK   → todo sano
503      → algo anda mal (el detalle viene en el JSON)
```

```json
{
  "ok": false,
  "checks": {
    "db": "ok",
    "salesforce": "error: INVALID_LOGIN",
    "config": "ok",
    "alertas": "ok",
    "jobs_fallidos_24h": 3,
    "avisos_no_enviados_24h": 2,
    "ultima_entrega": "2026-07-30T11:04:00-05:00"
  }
}
```

### Configurarlo (5 minutos, gratis)

Con **UptimeRobot** (o cron-job.org, Better Stack, el que prefieras):

1. Crear cuenta gratuita en uptimerobot.com
2. **Add New Monitor** → tipo **HTTP(s)**
3. URL: `https://tu-app/health`
4. Intervalo: 5 minutos
5. Alert contact: tu mail y/o tu celular

Con eso te avisan tanto si la app no responde (servidor caído, deploy roto, plan
gratuito dormido) como si responde 503 (Salesforce caído, falta una credencial, hay
avisos que nunca se enviaron).

> **Bonus en hostings gratuitos:** el ping cada 5 minutos también evita que el servicio
> se duerma por inactividad, que es una causa habitual de "la app tarda 40 segundos en
> abrir" cuando el repartidor la usa después de un rato.

---

## Qué mirar cuando llega una alerta

1. **`/health`** — el diagnóstico rápido, dice qué check está en rojo.
2. **`/admin/logs?token=...`** — el detalle, filtrable por ERROR y CRITICAL.
3. **`/historial`** — las entregas marcadas con "⚠️ El cliente no recibió el aviso" son
   las que hay que reenviar a mano.

---

## Lo que esto todavía NO cubre

Vale tenerlo claro para no confiarse de más:

- **Fotos trabadas en el teléfono del repartidor.** Si se queda sin señal toda la tarde,
  los trabajos quedan en 🕓 Pendiente en su celular y el servidor nunca se entera de que
  existen. Desde la oficina eso se ve como "hoy entregó menos", no como un error.
  Detectarlo requeriría que la app avise al entrar a la cola, no al subir.
- **Que Make reciba el webhook pero el WhatsApp no salga.** La app se entera de que Make
  respondió 200; lo que pase después es responsabilidad del escenario de Make. Conviene
  configurar el aviso de error propio de Make.
- **El silencio entre alertas iguales vive en memoria del proceso.** Con `--workers 1`
  (la configuración actual) es exacto. Si algún día se suben los workers, en el peor caso
  llegarían 2-3 mensajes repetidos en vez de uno.
- **Un reinicio borra el silencio**: si el servicio se reinicia en loop, cada arranque
  puede mandar de nuevo la misma alerta.
