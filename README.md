# Bot do Telegram — Trial por acertos (encerra em 10) + Webhook + Pagamentos

Variáveis de ambiente no Render → Settings → Environment:
- TELEGRAM_TOKEN
- PUBLIC_URL (sem / no fim)
- REDIS_URL (rediss:// externo)
- SUB_DAYS=7
- TG_PATH=tg
- TRIAL_MAX_HITS=10
- TRIAL_DAYS=0
- TRIAL_CAP=0
- PAYWALL_OFF=0

Build: pip install -r requirements.txt
Start: python main.py