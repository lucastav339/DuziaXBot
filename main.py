import os
import logging
from html import escape
from typing import Any, Dict

from fastapi import FastAPI, Request, Response, status
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# =========================
# CONFIGURAÇÃO DE LOG
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
log = logging.getLogger(__name__)

# =========================
# VARIÁVEIS DE AMBIENTE
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL")  # sem barra no final
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "webhook")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

if not BOT_TOKEN or not PUBLIC_URL:
    raise RuntimeError("Defina BOT_TOKEN e PUBLIC_URL nas variáveis de ambiente.")

# =========================
# ESTADO DO JOGO
# =========================
STATE: Dict[str, Any] = {
    "history": [],
    "wins": 0,
    "losses": 0
}

EV_POR_STAKE = 0.027  # exemplo

# =========================
# FUNÇÕES AUXILIARES
# =========================
def esc(x) -> str:
    return escape(str(x))

def get_dz(number: int) -> str:
    if number == 0:
        return "zero"
    elif 1 <= number <= 12:
        return "D1"
    elif 13 <= number <= 24:
        return "D2"
    elif 25 <= number <= 36:
        return "D3"
    return "?"

def status_text(s: Dict[str, Any]) -> str:
    total = s["wins"] + s["losses"]
    hit = (s["wins"] / total * 100) if total > 0 else 0.0
    return (
        "📊 <b>Status</b>\n"
        f"• Acertos: {s['wins']}  |  Erros: {s['losses']}  |  Taxa de acerto: {hit:.1f}%\n"
        f"• Giros lidos (com entrada): {total}\n"
        "• Janela de tendência: últimos 12 giros"
    )

def bet_header(d1: str, d2: str, excl: str) -> str:
    ev_pct = -EV_POR_STAKE * 100.0
    return (
        f"🎯 <b>Recomendação</b>: {esc(d1)} + {esc(d2)}  |  🚫 <b>Excluída</b>: {esc(excl)}\n"
        f"📈 Prob. teórica: ~64,86%  |  🧮 EV teórico: ~{ev_pct:.2f}% contra o apostador"
    )

# =========================
# LÓGICA DE APOSTA
# =========================
def apply_spin(number: int) -> str:
    dz = get_dz(number)
    STATE["history"].append(number)

    # exemplo: lógica simplificada
    if len(STATE["history"]) >= 3:
        # se as 2 últimas foram iguais → aposta
        if get_dz(STATE["history"][-1]) == get_dz(STATE["history"][-2]):
            if dz == get_dz(STATE["history"][-1]):
                STATE["wins"] += 1
                return f"✅ <b>Vitória</b> — saiu {number} ({dz}).\n{status_text(STATE)}"
            else:
                STATE["losses"] += 1
                return f"❌ <b>Derrota</b> — saiu {number} ({dz}).\n{status_text(STATE)}"

    return f"🎲 Resultado informado: <b>{number}</b> ({dz}).\n{status_text(STATE)}"

def apply_undo() -> str:
    if not STATE["history"]:
        return "⚠️ Nenhum número para desfazer."
    last_num = STATE["history"].pop()
    dz = get_dz(last_num)
    return (
        "↩️ <b>Undo feito</b>\n"
        f"• Removido: {last_num} ({dz})\n"
        f"{status_text(STATE)}"
    )

# =========================
# HANDLERS DO BOT
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Bot de roleta iniciado. Envie um número (0-36) ou /undo para desfazer.",
        parse_mode=ParseMode.HTML
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(status_text(STATE), parse_mode=ParseMode.HTML)

async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(apply_undo(), parse_mode=ParseMode.HTML)

async def on_number_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        num = int(update.message.text.strip())
        if 0 <= num <= 36:
            await update.message.reply_text(apply_spin(num), parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("⚠️ Número inválido. Digite entre 0 e 36.")
    except ValueError:
        await update.message.reply_text("⚠️ Envie apenas números entre 0 e 36.")

# =========================
# HANDLER GLOBAL DE ERROS
# =========================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Erro no bot: %s", context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Ocorreu um erro interno. Tente novamente."
            )
    except Exception:
        pass

# =========================
# FASTAPI + WEBHOOK
# =========================
app = FastAPI()
application = Application.builder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("status", status_cmd))
application.add_handler(CommandHandler("undo", undo_cmd))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_number_message))
application.add_error_handler(on_error)

@app.on_event("startup")
async def on_startup():
    webhook_url = f"{PUBLIC_URL}/{WEBHOOK_PATH}"
    await application.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET)
    log.info(f"Webhook registrado: {webhook_url}")
    await application.start()
    await application.updater.start_webhook(listen="0.0.0.0", port=10000)

@app.on_event("shutdown")
async def on_shutdown():
    # Não deleta o webhook para manter ativo entre reinícios
    await application.stop()
    await application.shutdown()
    log.info("Application stopped.")

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post(f"/{WEBHOOK_PATH}")
async def telegram_webhook(request: Request):
    try:
        if WEBHOOK_SECRET:
            secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if secret_header != WEBHOOK_SECRET:
                return Response(status_code=status.HTTP_401_UNAUTHORIZED)

        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as e:
        log.exception("Erro no webhook: %s", e)
        return Response(status_code=status.HTTP_200_OK)  # evita retry agressivo

# =========================
# RODAR LOCALMENTE
# =========================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=10000)
