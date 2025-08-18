import os
import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackContext,
    filters,
)

# 🔹 Configuração de logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Defina BOT_TOKEN no ambiente.")

# Estado por usuário
user_state = {}  # user_id -> dict

def get_user_state(user_id: int):
    if user_id not in user_state:
        user_state[user_id] = {
            "jogadas": 0,
            "acertos": 0,
            "erros": 0,
            "ultimo_palpite": None,
        }
    return user_state[user_id]

# ---------- Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"jogadas": 0, "acertos": 0, "erros": 0, "ultimo_palpite": None}

    keyboard = [["🔴 Vermelho", "⚫ Preto", "🟢 Zero"], ["/status", "/reset"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "🎲 Bem-vindo ao Bot de Roleta!\n\n"
        "Use os botões para registrar as jogadas.\n"
        "Digite /status para ver estatísticas ou /reset para zerar.",
        reply_markup=reply_markup,
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_user_state(update.effective_user.id)
    jogadas, acertos, erros = st["jogadas"], st["acertos"], st["erros"]
    taxa = (acertos / jogadas * 100.0) if jogadas > 0 else 0.0

    await update.message.reply_text(
        f"📊 Status atual:\n"
        f"➡️ Jogadas: {jogadas}\n"
        f"✅ Acertos: {acertos}\n"
        f"❌ Erros: {erros}\n"
        f"📈 Taxa de acerto: {taxa:.2f}%"
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    user_state[uid] = {"jogadas": 0, "acertos": 0, "erros": 0, "ultimo_palpite": None}
    await update.message.reply_text("♻️ Histórico e placar resetados!")

async def handle_jogada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = get_user_state(uid)
    jogada = update.message.text  # "🔴 Vermelho", "⚫ Preto", "🟢 Zero"

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

    # Exemplo simples: atualiza "palpite" como a própria jogada feita
    st["ultimo_palpite"] = jogada

    taxa = (st["acertos"] / st["jogadas"] * 100) if st["jogadas"] > 0 else 0.0
    await update.message.reply_text(
        f"{resultado}\n\n"
        f"📊 Placar:\n"
        f"➡️ Jogadas: {st['jogadas']}\n"
        f"✅ Acertos: {st['acertos']}\n"
        f"❌ Erros: {st['erros']}\n"
        f"📈 Taxa: {taxa:.2f}%"
    )

# ---------- Startup hook: apaga webhook antes de pollar ----------
async def on_startup(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook removido com sucesso (modo polling).")
    except Exception as e:
        logger.warning(f"Não consegui remover webhook: {e}")

def main():
    app = ApplicationBuilder().token(TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_jogada))

    logger.info("🤖 Bot iniciado em polling.")
    # drop_pending_updates=True evita processar fila pendente antiga
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
