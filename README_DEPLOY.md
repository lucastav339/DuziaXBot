# iColor — Bot de Cores (Telegram) com MODO GOD (Premium)

## Arquivos incluídos
- `main.py` — código completo (PTB 21.6), com:
  - MODO GOD (inverte recomendação agressiva)
  - Deleta webhook automaticamente quando em polling (evita `Conflict`)
  - Webhook quando `PORT` e `WEBHOOK_URL` estão setados
- `requirements.txt` — `python-telegram-bot==21.6` (compatível com Python 3.13)
- `Procfile` — exemplos para Worker (polling) e Web (webhook)
- `render-worker.yaml` — serviço Worker no Render (polling)
- `render-web.yaml` — serviço Web no Render (webhook)

## Variáveis de ambiente
- `BOT_TOKEN` (obrigatório)
- `WEBHOOK_URL` (apenas modo webhook) — ex.: `https://seu-servico.onrender.com`
- `WEBHOOK_PATH` (opcional) — ex.: `webhook`
- `PORT` — Render define automaticamente em Web

## Como escolher o modo
### A) Worker (Polling) — mais simples
- Suba `render-worker.yaml` ou crie um serviço **Worker** no Render.
- Start Command: `python main.py`
- Somente `BOT_TOKEN` é necessário.
- Não rode outra instância em paralelo.

### B) Web (Webhook) — precisa de URL pública
- Suba `render-web.yaml` ou crie um serviço **Web** no Render.
- Defina `BOT_TOKEN` e `WEBHOOK_URL` (e opcional `WEBHOOK_PATH`).
- O app ligará o webhook automaticamente.

## Dica de logs
Para confirmar a versão:
- No build: deve aparecer `python-telegram-bot-21.6`
- No runtime Web (webhook): Render mostrará que uma porta está aberta (sem "No open ports").
