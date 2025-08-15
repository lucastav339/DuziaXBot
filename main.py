import asyncio
import random
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

TOKEN = "SEU_TOKEN_AQUI"

# Efeito IA digitando
async def ia_typing(update, context, min_delay=0.35, max_delay=0.8):
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        await asyncio.sleep(random.uniform(min_delay, max_delay))
    except Exception:
        pass

# Teclado de modos
def mode_keyboard():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎯 Modo Agressivo", callback_data="set_agressivo"),
             InlineKeyboardButton("🛡️ Modo Conservador", callback_data="set_conservador")]
        ]
    )

# /start
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode_raw = context.bot_data.get("MODE", "conservador")
    mode = "Agressivo" if mode_raw.lower().startswith("agress") else "Conservador"

    texto = (
        "🤖 **iDozen — Assistente Inteligente de Duas Dúzias**\n\n"
        "🧠 _Sistema ativo_. Pronto para analisar padrões e sugerir entradas com foco em **duas dúzias**.\n\n"
        f"🎛️ **Modo Ativado:** _{mode}_\n\n"
        "🚀 **Como começar:**\n"
        "1️⃣ **Escolha o modo de operação**: _Agressivo_ 🎯 para mais frequência de sinais ou _Conservador_ 🛡️ para maior segurança.\n"
        "2️⃣ **Informe o último número** que saiu na roleta (0–36).\n"
        "3️⃣ **Aguarde minha análise** e receba a recomendação assim que surgir uma oportunidade estatística favorável.\n\n"
        "💡 *Dica:* Se enviar um número incorreto, quando surgir uma **ENTRADA** você poderá usar **✏️ Corrigir último**.\n\n"
        "▶️ **Pronto?** Escolha o modo abaixo e informe o número que acabou de sair."
    )

    await ia_typing(update, context)
    await update.message.reply_text(texto, reply_markup=mode_keyboard(), parse_mode="Markdown")

# Callback para troca de modos
async def mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "set_agressivo":
        context.bot_data["MODE"] = "agressivo"
        msg = "🎯 **Modo Agressivo ativado.**\nInforme o último número que saiu na roleta."
    elif query.data == "set_conservador":
        context.bot_data["MODE"] = "conservador"
        msg = "🛡️ **Modo Conservador ativado.**\nInforme o último número que saiu na roleta."
    else:
        return

    await ia_typing(update, context)
    await query.edit_message_text(msg, parse_mode="Markdown")

# Handler para mensagens de números
async def number_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        num = int(update.message.text)
    except ValueError:
        await update.message.reply_text("⚠️ Envie apenas números de 0 a 36.")
        return

    if not 0 <= num <= 36:
        await update.message.reply_text("⚠️ Envie apenas números de 0 a 36.")
        return

    await ia_typing(update, context)
    await update.message.reply_text(f"📡 Número registrado: **{num}**\n🔍 Analisando padrões...", parse_mode="Markdown")

# Inicialização do bot
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(mode_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, number_handler))

    print("Bot iDozen iniciado...")
    app.run_polling()

if __name__ == "__main__":
    main()
