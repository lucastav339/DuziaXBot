
# Bot do Telegram (Webhook) + Webhook de Pagamentos — Unificado (Render)

Este projeto sobe um **único Web Service no Render** com:
- **Webhook do Telegram** (`/tg` por padrão) usando `python-telegram-bot` v20
- **Webhook de pagamentos** em `/payments/webhook`
- **/health** (GET) para health check do Render

## Variáveis de ambiente (Render → Environment)
- `TELEGRAM_TOKEN` — Token do BotFather
- `PUBLIC_URL` — URL do seu serviço no Render, ex.: `https://seu-bot.onrender.com` (sem / no fim)
- `REDIS_URL` — URL do Redis (Upstash/Render), ex.: `rediss://...`
- `SUB_DAYS` — Dias de assinatura (ex.: `30`)
- (opcional) `TG_PATH` — caminho do webhook do Telegram (default: `tg`)

## Build/Start (Render)
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `python main.py`

## Rotas HTTP
- `POST /<TG_PATH>` — webhook do Telegram (ex.: `/tg`)
- `POST /payments/webhook` — webhook do PSP (Ex.: `{"status":"paid","user_id":123456789}`)
- `GET /health` — resposta 200 OK

## Dicas
- Depois do deploy, verifique o webhook: `https://api.telegram.org/bot<SEU_TOKEN>/getWebhookInfo`
- Se `url` vier vazio, confira `PUBLIC_URL` e redeploy.
