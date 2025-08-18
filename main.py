# main.py
import os
import logging
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ───────────────────────
# Config
# ───────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("roulette-bot")

BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN")  # obrigatória
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")
# Defina WEBHOOK_URL = https://<seu-servico>.onrender.com/webhook
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

if not WEBHOOK_URL:
    # fallback (Render costuma expor essa env automaticamente)
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if base:
        WEBHOOK_URL = f"{base}/webhook"
    else:
        logger.warning("WEBHOOK_URL não definida. Defina nas variáveis de ambiente!")

if not BOT_TOKEN:
    raise RuntimeError("A variável de ambiente TELEGRAM_TOKEN não está definida.")

# ───────────────────────
# FastAPI app
# ───────────────────────
app = FastAPI(title="Telegram Bot (Webhook)")

# Constrói a Application do PTB uma única vez (sem polling!)
application = Application.builder().token(BOT_TOKEN).build()

# ───────────────────────
# Handlers do bot
# ───────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(
        "<b>Bot iniciado em modo Webhook</b> ✅\n"
        "Sem polling, sem conflitos. Pode mandar comandos!"
    )

# Registra handlers
application.add_handler(CommandHandler("start", cmd_start))

# ───────────────────────
# Ciclo de vida do servidor
# ───────────────────────
@app.on_event("startup")
async def on_startup() -> None:
    logger.info("Inicializando PTB Application...")
    # Inicializa e inicia a engine interna do PTB
    await application.initialize()
    await application.start()

    # Define (ou redefine) o webhook do Telegram para este serviço
    # drop_pending_updates=True evita processar backlog antigo
    await application.bot.set_webhook(
        url=WEBHOOK_URL,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True,
    )
    logger.info(f"Webhook configurado em: {WEBHOOK_URL}")

@app.on_event("shutdown")
async def on_shutdown() -> None:
    logger.info("Parando PTB Application...")
    await application.stop()
    await application.shutdown()
    logger.info("PTB finalizado.")

# ───────────────────────
# Rotas HTTP
# ───────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}

@app.get("/")
async def root():
    return {"service": "Telegram Bot via Webhook", "ok": True}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    # Valida o segredo enviado pelo Telegram (protege de chamadas externas)
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    data = await request.json()
    # Converte payload em Update e entrega ao PTB (sem getUpdates!)
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return PlainTextResponse("OK")
