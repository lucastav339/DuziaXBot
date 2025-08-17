import os
import logging
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# 🔹 Configuração de logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# 🔹 Variáveis globais
TOKEN = os.getenv("BOT_TOKEN")
PORT = int(os.environ.get("PORT", "10000"))

user_state = {}  # guarda placar e histórico por usuário


def get_user_state(user_id: int):
    if user_id not in user_state:
        user_state[user_id] = {
            "jogadas": 0,
            "acertos": 0,
            "erros": 0,
            "ultimo_palpite": None,
        }
    return user_state[user_id]


# 🔹 Funções do bot
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_state[user_id] = {
        "jogadas": 0,
        "acertos": 0,
        "erros": 0,
        "ultimo_palpite": None,
    }

    keyboard = [["🔴 Vermelho", "⚫ Preto", "🟢 Zero"], ["/status", "/reset"]]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "🎲 Bem-vindo ao Bot de Roleta!\n\n"
        "Use os botões para registrar as jogadas.\n"
        "Digite /status para ver estatísticas ou /reset para zerar.",
        reply_markup=reply_markup,
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = get_user_state(update.effective_user.id)
    jogadas = state["jogadas"]
    acertos = state["acertos"]
    erros = state["erros"]
    taxa = (acertos / jogadas * 100) if jogadas > 0 else 0.0

    await update.message.reply_text(
        f"📊 Status atual:\n"
        f"➡️ Jogadas: {jogadas}\n"
        f"✅ Acertos: {acertos}\n"
        f"❌ Erros: {erros}\n"
        f"📈 Taxa de acerto: {taxa:.2f}%"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_state[user_id] = {
        "jogadas": 0,
        "acertos": 0,
        "erros": 0,
        "ultimo_palpite": None,
    }
    await update.message.reply_text("♻️ Histórico e placar resetados!")


async def handle_jogada(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    state = get_user_state(user_id)
    jogada = update.message.text

    # Simulação: palpite anterior salvo
    palpite = state["ultimo_palpite"]

    # Comparar resultado atual com palpite
    if palpite is not None:
        state["jogadas"] += 1
        if jogada == palpite:
            state["acertos"] += 1
            resultado = "✅ Acerto!"
        else:
            state["erros"] += 1
            resultado = "❌ Erro!"
    else:
        resultado = "⚡ Primeira jogada registrada (sem comparação)."

    # Atualiza último palpite (aqui simplificado: sempre igual à jogada feita)
    state["ultimo_palpite"] = jogada

    taxa = (state["acertos"] / state["jogadas"] * 100) if state["jogadas"] > 0 else 0.0

    await update.message.reply_text(
        f"{resultado}\n\n"
        f"📊 Placar:\n"
        f"➡️ Jogadas: {state['jogadas']}\n"
        f"✅ Acertos: {state['acertos']}\n"
        f"❌ Erros: {state['erros']}\n"
        f"📈 Taxa: {taxa:.2f}%"
    )


# 🔹 Execução
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_jogada))

    logger.info(f"🤖 Bot iniciado. Health check na porta {PORT}")

    app.run_polling()


if __name__ == "__main__":
    main()
