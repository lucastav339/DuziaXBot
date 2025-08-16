from __future__ import annotations

import os
import asyncio
import signal
from typing import Dict

from aiohttp import web
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from roulette_bot.state import UserState
from roulette_bot.analysis import analyze, validate_number
from roulette_bot.formatting import format_response, RESP_ZERO, RESP_CORRECT

# =========================
# Estado por usuário
# =========================
USER_STATES: Dict[int, UserState] = {}


def get_state(chat_id: int) -> UserState:
    if chat_id not in USER_STATES:
        USER_STATES[chat_id] = UserState()
    return USER_STATES[chat_id]


# =========================
# Handlers de comandos
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Envie números (0-36) para análise ou /help para comandos. Jogue com responsabilidade."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Comandos: /start, /reset, /explicar, /janela N, /status, /modo <tipo>, "
        "/banca on/off valor, /progressao martingale|dalembert, /corrigir X"
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
    _ = analyze(state)  # se quiser usar algo da análise depois
    msg = (
        f"Modo: {state.mode}\n"
        f"Janela: {state.window}\n"
        f"Histórico: {list(state.history)[-12:]}\n"
        f"Frequências: (ver análise interna)"
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
        await update.message.reply_text(
            "Uso: /banca on <valor> | /banca off"
        )
        return
    if context.args[0] == "on" and len(context.args) > 1:
        try:
            state.stake_value = float(context.args[1])
            state.stake_on = True
            await update.message.reply_text(f"Stake ativada em {state.stake_value:.2f}.")
        except ValueError:
            await update.message.reply_text("Valor inválido.")
    elif context.args[0] == "off":
        state.stake_on = False
        await update.message.reply_text("Stake desativada.")
    else:
        await update.message.reply_text("Uso: /banca on <valor> | /banca off")


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
        await update.message.reply_text("Informe o número para correção. Ex: /corrigir 17")
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


# =========================
# Handler de números
# =========================
async def handle_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    ok, num = validate_number(text)
    if not ok or num is None:
        await update.message.reply_text("Entrada inválida. Envie apenas números de 0 a 36.")
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


# =========================
# Health server (aiohttp)
# =========================
async def _health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def _start_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8000"))  # Render injeta $PORT
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner


# =========================
# Main assíncrono
# =========================
async def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN não definido")

    # Telegram Application
    tg_app = Application.builder().token(token).build()

    # Handlers
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("help", help_cmd))
    tg_app.add_handler(CommandHandler("reset", reset))
    tg_app.add_handler(CommandHandler("explicar", explicar))
    tg_app.add_handler(CommandHandler("janela", janela))
    tg_app.add_handler(CommandHandler("status", status))
    tg_app.add_handler(CommandHandler("modo", modo))
    tg_app.add_handler(CommandHandler("banca", banca))
    tg_app.add_handler(CommandHandler("progressao", progressao))
    tg_app.add_handler(CommandHandler("corrigir", corrigir))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_number))

    # Inicia health server e bot em paralelo
    health_runner = await _start_health_server()

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)

    # Espera sinais para shutdown gracioso
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Em plataformas sem suporte a signals (ex.: Windows), ignorar
            pass

    try:
        await stop_event.wait()
    finally:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        await health_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
