import os
import asyncio
import logging
from typing import Dict, Optional

from fastapi import FastAPI
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes
)

from runtime import get_mode_and_url, build_webhook_app
from scraper import RouletteScraper

# =========================
# Configuração de LOG
# =========================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
log = logging.getLogger("roulette-bot")

# =========================
# ENV / Constantes
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Defina a variável de ambiente BOT_TOKEN com o token do bot.")

SOURCE_URL = os.getenv(
    "SOURCE_URL",
    "https://gamblingcounting.com/pt-BR/pragmatic-brazilian-roulette"
).strip()

DEFAULT_INTERVAL_SEC = int(os.getenv("INTERVAL_SEC", "10"))  # frequência de checagem (segundos)

# =========================
# Estado por chat
# =========================
class ChatState:
    def __init__(self, interval_sec: int):
        self.interval_sec = interval_sec
        self.task: Optional[asyncio.Task] = None
        self.last_sent: Optional[str] = None  # guarda "numero/cor" para deduplicar

# chat_id -> ChatState
CHAT_STATES: Dict[int, ChatState] = {}

scraper = RouletteScraper(SOURCE_URL)

# =========================
# Loop de envio
# =========================
async def sender_loop(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    log.info(f"[{chat_id}] Loop de envio iniciado.")
    try:
        while True:
            state = CHAT_STATES.get(chat_id)
            if state is None:
                return

            try:
                latest = await scraper.fetch_latest_entry()
                if latest is not None:
                    payload = f"{latest['number']}/{latest['color']}"
                    if payload != state.last_sent:
                        state.last_sent = payload
                        await context.bot.send_message(
                            chat_id=chat_id,
                            text=f"🎰 Último resultado: <b>{latest['number']}</b> — {latest['color']}",
                            parse_mode="HTML"
                        )
                        log.info(f"[{chat_id}] Enviado novo resultado: {payload}")
            except Exception as e:
                log.exception(f"[{chat_id}] Erro durante scraping: {e}")

            await asyncio.sleep(state.interval_sec)

    except asyncio.CancelledError:
        log.info(f"[{chat_id}] Loop cancelado.")
    finally:
        log.info(f"[{chat_id}] Loop de envio finalizado.")

async def ensure_task_running(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    state = CHAT_STATES.get(chat_id)
    if state is None:
        state = ChatState(DEFAULT_INTERVAL_SEC)
        CHAT_STATES[chat_id] = state

    if state.task is None or state.task.done():
        state.task = asyncio.create_task(sender_loop(chat_id, context))

async def stop_task(chat_id: int):
    state = CHAT_STATES.get(chat_id)
    if state and state.task and not state.task.done():
        state.task.cancel()
        try:
            await state.task
        except asyncio.CancelledError:
            pass
        state.task = None

# =========================
# Handlers de comando
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🤖 Bot de Resultados — <b>Pragmatic Brazilian Roulette</b>\n\n"
            "Assim que houver um novo giro, eu te envio aqui.\n\n"
            "Comandos:\n"
            "• /interval &lt;segundos&gt; — muda a frequência (ex.: /interval 10)\n"
            "• /status — mostra o status atual\n"
            "• /history — últimos 15 resultados\n"
            "• /stop — para o envio\n"
        ),
        parse_mode="HTML"
    )
    await ensure_task_running(chat_id, context)

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await stop_task(chat_id)
    await context.bot.send_message(chat_id=chat_id, text="🛑 Envio pausado. Use /start para retomar.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = CHAT_STATES.get(chat_id)
    if state and state.task and not state.task.done():
        last = state.last_sent or "—"
        await context.bot.send_message(
            chat_id=chat_id,
            text=(f"✅ Status: Ativo\n⏱️ Intervalo: {state.interval_sec}s\n🧠 Último enviado: {last}")
        )
    else:
        await context.bot.send_message(chat_id=chat_id, text="⏸️ Status: Inativo (use /start).")

async def set_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Use: /interval 5  (por exemplo)")
        return
    try:
        sec = max(2, int(context.args[0]))
    except ValueError:
        await update.message.reply_text("Valor inválido. Ex.: /interval 10")
        return

    chat_id = update.effective_chat.id
    state = CHAT_STATES.get(chat_id)
    if not state:
        state = ChatState(sec)
        CHAT_STATES[chat_id] = state
    else:
        state.interval_sec = sec

    await update.message.reply_text(f"Intervalo atualizado para {sec}s.")

async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    hist = await scraper.fetch_history(limit=15)
    if not hist:
        await update.message.reply_text("Sem histórico disponível.")
        return
    def mark(c): return {"red":"R","black":"B","green":"G"}[c]
    line = ", ".join(f"{x['number']}({mark(x['color'])})" for x in hist)
    await update.message.reply_text(f"🧾 Últimos 15: {line}")

# =========================
# Construção da aplicação
# =========================
def build_application() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("interval", set_interval))
    app.add_handler(CommandHandler("history", history))
    return app

# Cria app FastAPI para webhook
_mode, _public_url = get_mode_and_url()
_application = build_application()
app: Optional[FastAPI] = None
if _mode == "webhook":
    app = build_webhook_app(_application, _public_url)

if __name__ == "__main__":
    if _mode == "webhook":
        import uvicorn
        port = int(os.getenv("PORT", "10000"))
        uvicorn.run(app, host="0.0.0.0", port=port)  # type: ignore
    else:
        _application.run_polling(close_loop=False)
