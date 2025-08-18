# main.py — Web Service (Render) com WEBHOOK + /health e placar (jogadas/acertos/erros)
# ✔️ Não usa run_webhook; servimos nosso próprio servidor aiohttp:
#    - GET  /health  → 200 OK (para o health check do Render)
#    - POST /webhook → recebe updates do Telegram e entrega ao PTB
# 📦 requirements.txt:  python-telegram-bot==21.6  aiohttp==3.10.5

import os
import sys
import json
import asyncio
import logging
from typing import Dict, Any

from aiohttp import web
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# Config & ENV
# =========================
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger("duziaxbot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")            # ex: https://duziaxbot.onrender.com
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "webhook")  # caminho público/privado (use "webhook")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Defina BOT_TOKEN no ambiente.")
if not WEBHOOK_URL:
    raise RuntimeError("Defina WEBHOOK_URL (modo Webhook/Web escolhido).")

# Log de versões para diagnóstico
try:
    import telegram
    log.info(f"python-telegram-bot: {telegram.__version__}")
except Exception:
    log.info("python-telegram-bot: (não foi possível obter versão)")
log.info(f"Python: {sys.version}")
log.info(f"Webhook público esperado: {WEBHOOK_URL.rstrip('/')}/{WEBHOOK_PATH}")

# =========================
# Estado por usuário
# =========================
STATE: Dict[int, Dict[str, Any]] = {}

def get_state(user_id: int) -> Dict[str, Any]:
    if user_id not in STATE:
        STATE[user_id] = {
            "jogadas": 0,
            "acertos": 0,
            "erros": 0,
            "ultimo_palpite": None,
        }
    return STATE[user_id]

# =========================
# UI
# =========================
CHOICES = ["🔴 Vermelho", "⚫ Preto", "🟢 Zero"]
KB = ReplyKeyboardMarkup([CHOICES, ["/status", "/reset"]], resize_keyboard=True)

# =========================
# Handlers do bot
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE[uid] = {"jogadas": 0, "acertos": 0, "erros": 0, "ultimo_palpite": None}
    await update.message.reply_text(
        "🎲 Bem-vindo!\n"
        "Use os botões para registrar as jogadas.\n"
        "Comandos: /status /reset",
        reply_markup=KB,
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_user.id)
    j, a, e = st["jogadas"], st["acertos"], st["erros"]
    taxa = (a / j * 100.0) if j > 0 else 0.0
    await update.message.reply_text(
        f"📊 Status\n"
        f"➡️ Jogadas: {j}\n"
        f"✅ Acertos: {a}\n"
        f"❌ Erros: {e}\n"
        f"📈 Taxa: {taxa:.2f}%"
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE[uid] = {"jogadas": 0, "acertos": 0, "erros": 0, "ultimo_palpite": None}
    await update.message.reply_text("♻️ Histórico e placar resetados!", reply_markup=KB)

async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = get_state(uid)
    jogada = (update.message.text or "").strip()

    if jogada not in CHOICES:
        await update.message.reply_text("Use os botões abaixo para registrar.", reply_markup=KB)
        return

    palpite = st["ultimo_palpite"]
    if palpite is not None:
        st["jogadas"] += 1
        if jogada == palpite:
            st["acertos"] += 1
            resultado = "✅ Acerto!"
        else:
            st["erros"] += 1
            resultado = "❌ Erro!"
    else:
        resultado = "⚡ Primeira jogada registrada (sem comparação)."

    # Neste modelo simples, o "palpite" passa a ser a jogada atual
    st["ultimo_palpite"] = jogada

    taxa = (st["acertos"] / st["jogadas"] * 100.0) if st["jogadas"] > 0 else 0.0
    await update.message.reply_text(
        f"{resultado}\n\n"
        f"📊 Placar:\n"
        f"➡️ Jogadas: {st['jogadas']}\n"
        f"✅ Acertos: {st['acertos']}\n"
        f"❌ Erros: {st['erros']}\n"
        f"📈 Taxa: {taxa:.2f}%",
        reply_markup=KB,
    )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Erro no handler:", exc_info=context.error)

# =========================
# Servidor aiohttp (Webhook + Health)
# =========================
def build_web_app(tg_app: Application) -> web.Application:
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="OK", status=200)

    async def telegram_webhook(request: web.Request) -> web.Response:
        # Telegram envia POST JSON aqui; repassamos o update para o PTB
        try:
            data = await request.json()
        except Exception:
            data = json.loads(await request.text())  # fallback
        update = Update.de_json(data, tg_app.bot)
        await tg_app.process_update(update)  # entrega o update ao PTB
        return web.Response(text="OK", status=200)

    app.router.add_get("/health", health)
    # aceite também GET no webhook para o Render poder validar se configurado assim
    app.router.add_get(f"/{WEBHOOK_PATH}", health)
    app.router.add_post(f"/{WEBHOOK_PATH}", telegram_webhook)
    # opcional: raiz responde 200
    app.router.add_get("/", health)

    return app

# =========================
# Boot: inicia PTB + aiohttp
# =========================
async def amain():
    # Telegram Application
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("status", status_cmd))
    tg_app.add_handler(CommandHandler("reset", reset_cmd))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_choice))
    tg_app.add_error_handler(error_handler)

    # Inicializa PTB (sem polling e sem run_webhook)
    await tg_app.initialize()
    await tg_app.start()
    log.info("PTB Application started (custom webhook server).")

    # Configura webhook no Telegram apontando para /WEBHOOK_PATH
    # (Mesmo que não use run_webhook, precisamos dizer ao Telegram o endpoint público.)
    webhook_full = WEBHOOK_URL.rstrip("/") + f"/{WEBHOOK_PATH}"
    ok = await tg_app.bot.set_webhook(webhook_full)
    log.info(f"setWebhook({webhook_full}) → {ok}")

    # Sobe servidor aiohttp com /health e /WEBHOOK_PATH
    web_app = build_web_app(tg_app)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info(f"Servidor aiohttp ouvindo em 0.0.0.0:{PORT} (health + webhook).")

    # Aguarda para sempre
    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        # shutdown ordenado (Render envia SIGTERM)
        await tg_app.stop()
        await tg_app.shutdown()
        await runner.cleanup()

def main():
    asyncio.run(amain())

if __name__ == "__main__":
    main()
