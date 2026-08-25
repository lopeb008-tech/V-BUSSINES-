# Bot de Telegram para Railway

Este paquete usa `bot.py` como archivo principal y está listo para subir a GitHub y desplegar en Railway.

## Archivos

- `bot.py`: código completo del bot.
- `requirements.txt`: dependencias Python.
- `Procfile`: comando de arranque para Railway.
- `railway.json`: configuración de despliegue.
- `.env.example`: nombres de variables necesarias, sin datos reales.
- `setup-database.sql`: tablas y datos iniciales.

## Variables en Railway

Configura estas variables en tu servicio:

```text
TELEGRAM_BOT_TOKEN=tu_token_real
SUPABASE_URL=https://tu-proyecto.supabase.co
SUPABASE_SERVICE_ROLE_KEY=tu_service_role_key
WEBHOOK_URL=https://tu-servicio.up.railway.app
WEBHOOK_ONLY=true
ADMIN_CHAT_ID=5127721601
```

Después de guardar las variables, haz Redeploy. Al arrancar, el bot registra automáticamente el webhook en Telegram.

## Prueba rápida

Abre la URL pública de Railway. Debe responder algo como:

```json
{"ok": true, "mode": "webhook", "missing": []}
```

Luego escribe `/start` al bot en Telegram.

## Importante

No subas `.env` con tokens reales a GitHub. Usa solo las variables privadas de Railway.
