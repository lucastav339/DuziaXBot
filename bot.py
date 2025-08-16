# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import asyncio
import signal
import logging
import threading
import http.server
import socketserver
from typing import Dict

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from roulette_bot.state import UserState
from roulette_bot.analysis import analyze, validate_number
from roulette_bot.formatting import format_response, RESP_ZERO, RESP_CORRECT

# =========================
# Logging
# =========================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("roulette-bot")

# =========================
# Estado por usuário
# =========================
USER_STATES: Dict[int, UserState] = {}

def get_state(chat_id: int) -> UserState:
    if chat_id not in USER_STATES:
        USER_STATES[chat_id] = UserState()
    return USER_STATES[chat_id]

# =========================
# Utilitário contra UnicodeEncodeError (surrogates)
# =========================
def de_surrogate(text: str) -> str:
    if not isinstance(text, str):
        text = str(text)
    try:
        return text.encode("utf-16", "surrogatepass").decode("utf-16")
    except Exception:
        return text

async def safe_reply(message, text: str, **kwargs):
    clean = de_surrogate(text)
    await message.reply_text(clean, **kwargs)

# =========================
# Handlers
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
# Health server (stdlib)
# =========================
def start_health_server():
    port = int(os.environ.get("PORT", "8000"))

    class HealthHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
            else:
                self.send_response(404)
                self.end_headers()

        # Evita logs gigantes no Render
        def log_message(self, format, *args):
            logging.getLogger("health").info("%s - %s", self.address_string(), format % args)

    def serve():
        with socketserver.TCPServer(("0.0.0.0", port), HealthHandler) as httpd:
            log.info("Health server listening on 0.0.0.0:%s", port)
            httpd.serve_forever()

    t = threading.Thread(target=serve, daemon=True)
    t.start()

# =========================
# Main assíncrono
# =========================
async def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        log.error("BOT_TOKEN não definido como variável de ambiente. Defina BOT_TOKEN no Render.")
        # Força exit “limpo” para aparecer o log e não reiniciar em loop
        raise RuntimeError("BOT_TOKEN não definido")

    # Inicia /health para satisfazer Web Service (porta $PORT)
    start_health_server()

    tg_app = Application.builder().token(token).build()

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

    # Evita conflitos com webhooks antigos e esvazia updates pendentes
    await tg_app.bot.delete_webhook(drop_pending_updates=True)

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)

    log.info("Bot iniciado. Polling ativo. Serviço pronto.")

    # Espera por SIGINT/SIGTERM (Render envia)
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
        log.info("Encerrando…")
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()
        log.info("Finalizado.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        log.exception("Falha ao iniciar a aplicação: %s", e)
        # Relevanta para ver o erro no Render; re-raise para sinalizar falha
        raise
