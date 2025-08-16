# -*- coding: utf-8 -*-
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
# Utilitário para evitar UnicodeEncodeError (surrogates)
# =========================
def de_surrogate(text: str) -> str:
    """
    Converte sequências \\uDxxx (surrogates) em caracteres reais.
    Mantém o texto igual se não houver surrogates.
    """
    if not isinstance(text, str):
        text = str(text)
    try:
        # Converte preservando pares surrogate e decodifica para chars reais
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except Exception:
        return text  # fallback: devolve como veio

async def safe_reply(message, text: str, **kwargs):
    clean = de_surrogate(text)
    await message.reply_text(clean, **kwargs)

# =========================
# Handlers de comandos
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_reply(
        update.message,
        "Envie números (0-36) para análise ou /help para comandos. Jogue com responsabilidade."
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_reply(
        update.message,
        "Comandos: /start, /reset, /explicar, /janela N, /status, /modo <tipo>, "
        "/banca on/off valor, /progressao martingale|dalembert, /corrigir X"
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    state.reset_history()
    await safe_reply(update.message, "Histórico zerado.")

async def explicar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    state.explain_next = True
    await safe_reply(update.message, "Próxima resposta terá justificativa detalhada.")

async def janela(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if context.args:
        try:
            n = int(context.args[0])
        except ValueError:
            await safe_reply(update.message, "Valor inválido.")
            return
        if 8 <= n <= 18:
            state.window = n
            await safe_reply(update.message, f"Janela ajustada para {n} giros.")
        else:
            await safe_reply(update.message, "Valor deve estar entre 8 e 18.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    _ = analyze(state)
    msg = (
        f"Modo: {state.mode}\n"
        f"Janela: {state.window}\n"
        f"Histórico: {list(state.history)[-12:]}\n"
        f"Frequências: (ver análise interna)"
    )
    await safe_reply(update.message, msg)

async def modo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if context.args and context.args[0] in {"conservador", "agressivo", "neutro"}:
        state.mode = context.args[0]
        await safe_reply(update.message, f"Modo ajustado para {state.mode}.")
    else:
        await safe_reply(update.message, "Modos: conservador, agressivo, neutro.")

async def banca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if not context.args:
        await safe_reply(update.message, "Uso: /banca on <valor> | /banca off")
        return
    if context.args[0] == "on" and len(context.args) > 1:
        try:
            state.stake_value = float(context.args[1])
            state.stake_on = True
            await safe_reply(update.message, f"Stake ativada em {state.stake_value:.2f}.")
        except ValueError:
            await safe_reply(update.message, "Valor inválido.")
    elif context.args[0] == "off":
        state.stake_on = False
        await safe_reply(update.message, "Stake desativada.")
    else:
        await safe_reply(update.message, "Uso: /banca on <valor> | /banca off")

async def progressao(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if context.args and context.args[0] in {"martingale", "dalembert"}:
        state.progression = context.args[0]
        await safe_reply(update.message, f"Progressão {state.progression} configurada.")
    else:
        state.progression = None
        await safe_reply(update.message, "Progressão desativada.")

async def corrigir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if not context.args:
        await safe_reply(update.message, "Informe o número para correção. Ex: /corrigir 17")
        return
    ok, num = validate_number(context.args[0])
    if not ok or num is None:
        await safe_reply(update.message, "Número inválido.")
        return
    if state.correct_last(num):
        analysis = analyze(state)
        msg = RESP_CORRECT.format(num=num) + "\n" + format_response(state, analysis)
        await safe_reply(update.message, msg)
    else:
        await safe_reply(update.message, "Sem histórico para corrigir.")

# =========================
# Handler de números
# =========================
async def handle_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    ok, num = validate_number(text)
    if not ok or num is None:
        await safe_reply(update.message, "Entrada inválida. Envie apenas números de 0 a 36.")
        return

    state = get_state(update.effective_chat.id)

    if num == 0:
        state.reset_history()
        await safe_reply(update.message, RESP_ZERO)
        return

    state.add_number(num)
    analysis = analyze(state)
    msg = format_response(state, analysis)
    await safe_reply(update.message, msg)

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
