from __future__ import annotations

import os
from typing import Dict

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from roulette_bot.state import UserState
from roulette_bot.analysis import analyze, validate_number
from roulette_bot.formatting import format_response, RESP_ZERO, RESP_CORRECT

# User states stored in-memory per chat id
USER_STATES: Dict[int, UserState] = {}


def get_state(chat_id: int) -> UserState:
    if chat_id not in USER_STATES:
        USER_STATES[chat_id] = UserState()
    return USER_STATES[chat_id]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Envie números (0-36) para análise ou /help para comandos. Jogue com responsabilidade."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Comandos: /start, /reset, /explicar, /janela N, /status, /modo <tipo>, /banca on/off valor, "
        "/progressao martingale|dalembert, /corrigir X"
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    state.reset_history()
    await update.message.reply_text("Histórico zerado.")


async def explicar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    state.explain_next = True
    await update.message.reply_text("Próxima resposta terá justificativa detalhada.")


async def janela(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if context.args:
        try:
            n = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Valor inválido.")
            return
        if 8 <= n <= 18:
            state.window = n
            await update.message.reply_text(f"Janela ajustada para {n} giros.")
        else:
            await update.message.reply_text("Valor deve estar entre 8 e 18.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    analysis = analyze(state)
    msg = (
        f"Modo: {state.mode}\nJanela: {state.window}\nHistórico: {list(state.history)[-12:]}\nFrequências: "
    )
    await update.message.reply_text(msg)


async def modo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if context.args and context.args[0] in {"conservador", "agressivo", "neutro"}:
        state.mode = context.args[0]
        await update.message.reply_text(f"Modo ajustado para {state.mode}.")
    else:
        await update.message.reply_text("Modos: conservador, agressivo, neutro.")


async def banca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if not context.args:
        return
    if context.args[0] == "on" and len(context.args) > 1:
        try:
            state.stake_value = float(context.args[1])
            state.stake_on = True
            await update.message.reply_text("Stake ativada.")
        except ValueError:
            await update.message.reply_text("Valor inválido.")
    elif context.args[0] == "off":
        state.stake_on = False
        await update.message.reply_text("Stake desativada.")


async def progressao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if context.args and context.args[0] in {"martingale", "dalembert"}:
        state.progression = context.args[0]
        await update.message.reply_text(f"Progressão {state.progression} configurada.")
    else:
        state.progression = None
        await update.message.reply_text("Progressão desativada.")


async def corrigir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if not context.args:
        await update.message.reply_text("Informe o número para correção.")
        return
    ok, num = validate_number(context.args[0])
    if not ok or num is None:
        await update.message.reply_text("Número inválido.")
        return
    if state.correct_last(num):
        analysis = analyze(state)
        msg = RESP_CORRECT.format(num=num) + "\n" + format_response(state, analysis)
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text("Sem histórico para corrigir.")


async def handle_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    ok, num = validate_number(text)
    if not ok or num is None:
        await update.message.reply_text("Entrada inválida.")
        return
    state = get_state(update.effective_chat.id)
    if num == 0:
        state.reset_history()
        await update.message.reply_text(RESP_ZERO)
        return
    state.add_number(num)
    analysis = analyze(state)
    msg = format_response(state, analysis)
    await update.message.reply_text(msg)


async def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN não definido")

    app = (
        Application.builder().token(token).build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("explicar", explicar))
    app.add_handler(CommandHandler("janela", janela))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("modo", modo))
    app.add_handler(CommandHandler("banca", banca))
    app.add_handler(CommandHandler("progressao", progressao))
    app.add_handler(CommandHandler("corrigir", corrigir))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_number))
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    await app.updater.wait()  # substitui o antigo app.idle()
    await app.shutdown()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
