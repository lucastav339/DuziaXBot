# main.py — Web Service (Render) com WEBHOOK + /health e placar (jogadas/acertos/erros)
# ✔️ PTB 21.6 + aiohttp (sem run_webhook)
# Endpoints:
#   GET  /health   → 200 OK (Render health check)
#   GET  /<WEBHOOK_PATH> → 200 (útil p/ ver se a rota existe)
#   POST /<WEBHOOK_PATH> → recebe updates do Telegram e entrega ao PTB (não bloqueante)
#
# Requisitos (requirements.txt):
#   python-telegram-bot==21.6
#   aiohttp==3.10.5
#
# ENV obrigatórias:
#   BOT_TOKEN        = <seu token do BotFather>
#   WEBHOOK_URL      = https://duziaxbot.onrender.com   (sem /final)
#   WEBHOOK_PATH     = webhook-<string-aleatoria>       (ex.: webhook-7f2a9d)  [opcional, default: "webhook"]
#   TG_SECRET_TOKEN  = <string forte para validar header> [fortemente recomendado]
#   PORT             = fornecido pelo Render (não defina manualmente)
#
# Start command no Render:  python main.py

import os
import sys
import json
import asyncio
import logging
import signal
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")                  # ex: https://duziaxbot.onrender.com
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "webhook")     # ex: webhook-7f2a9d (use algo "secreto")
PORT = int(os.getenv("PORT", "10000"))
SECRET_TOKEN = os.getenv("TG_SECRET_TOKEN")             # recomenda-se definir

if not BOT_TOKEN:
    raise RuntimeError("Defina BOT_TOKEN no ambiente.")
if not WEBHOOK_URL:
    raise RuntimeError("Defina WEBHOOK_URL no ambiente (ex.: https://...onrender.com).")

# Log de versões para diagnóstico
try:
    import telegram
    log.info(f"python-telegram-bot: {telegram.__version__}")
except Exception:
    log.info("python-telegram-bot: (não foi possível obter versão)")
log.info(f"Python: {sys.version}")
log.info(f"Webhook público esperado: {WEBHOOK_URL.rstrip('/')}/{WEBHOOK_PATH}")

# =========================
# Estado por usuário (em memória)
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
        # (Opcional) valida token secreto no header do Telegram
        if SECRET_TOKEN:
            recv = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if recv != SECRET_TOKEN:
                log.warning("Webhook com secret inválido/ausente.")
                return web.Response(text="Forbidden", status=403)

        try:
            data = await request.json()
        except Exception:
            data = json.loads(await request.text())  # fallback

        update = Update.de_json(data, tg_app.bot)

        # ✅ não bloquear a resposta HTTP: enfileira e responde 200
        try:
            tg_app.update_queue.put_nowait(update)
        except Exception:
            # Fallback síncrono se a fila estiver cheia/indisponível
            asyncio.create_task(tg_app.process_update(update))

        return web.Response(text="OK", status=200)

    app.router.add_get("/health", health)
    app.router.add_get(f"/{WEBHOOK_PATH}", health)            # útil p/ checar rota
    app.router.add_post(f"/{WEBHOOK_PATH}", telegram_webhook) # endpoint real do webhook
    app.router.add_get("/", health)                           # raiz também 200

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
    webhook_full = WEBHOOK_URL.rstrip("/") + f"/{WEBHOOK_PATH}"
    ok = await tg_app.bot.set_webhook(
        url=webhook_full,
        drop_pending_updates=True,    # 🔒 evita lixo/duplicidades
        allowed_updates=None,         # ou liste se quiser filtrar
        secret_token=SECRET_TOKEN     # 🔒 valida via header no handler
    )
    log.info(f"setWebhook({webhook_full}) → {ok}")

    # Sobe servidor aiohttp com /health e /WEBHOOK_PATH
    web_app = build_web_app(tg_app)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info(f"Servidor aiohttp ouvindo em 0.0.0.0:{PORT} (health + webhook).")

    # Graceful shutdown em SIGTERM/SIGINT
    stop_event = asyncio.Event()

    def _handle_signal():
        log.info("Sinal recebido, finalizando...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(s, _handle_signal)
        except NotImplementedError:
            pass  # Windows, etc.

    try:
        await stop_event.wait()
    finally:
        await tg_app.stop()
        await tg_app.shutdown()
        await runner.cleanup()
        log.info("Encerrado com sucesso.")

def main():
    asyncio.run(amain())

if __name__ == "__main__":
    main()
