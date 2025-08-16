# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import asyncio
import signal
import logging
import threading
import http.server
import socketserver
from typing import Dict, Set

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.error import Conflict, NetworkError

from roulette_bot.state import UserState
from roulette_bot.analysis import analyze, validate_number, number_to_dozen
from roulette_bot.formatting import format_response, RESP_ZERO, RESP_CORRECT

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("roulette-bot")

USER_STATES: Dict[int, UserState] = {}

def get_state(chat_id: int) -> UserState:
    if chat_id not in USER_STATES:
        USER_STATES[chat_id] = UserState()
    return USER_STATES[chat_id]

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
        "🎲✨ <b>iDozen</b> — estratégia single com gatilho 3 em 4 + 1 gale.\n"
        "Envie o último número (0–36) para iniciar.",
        parse_mode="HTML"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await safe_reply(
        update.message,
        "Comandos: /start, /reset, /janela N, /status, /banca on/off valor, /corrigir X"
    )

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    state.reset_history()
    state.clear_recommendation()  # zera placar + gale + timers
    await safe_reply(update.message, "Histórico e placar zerados. Gale e timers desativados.")

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
    _ = analyze(state)  # só para compor info fresca se precisar
    gale_status = (
        f"ATIVO (1/1) na {state.gale_dozen}" if state.gale_left > 0 and state.gale_dozen
        else "pronto (1/1)"
    )
    msg = (
        f"Janela: {state.window}\n"
        f"Recomendação ativa: {sorted(state.current_rec) if state.current_rec else '—'}\n"
        f"Placar: Jogadas {state.rec_plays} | Acertos {state.rec_hits} | Erros {state.rec_misses}\n"
        f"Gale: {gale_status}\n"
        f"Refratário: {state.refractory_left}/{state.refractory_spins} | "
        f"Cooldown: {state.cooldown_left}"
    )
    await safe_reply(update.message, msg)

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

async def corrigir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = get_state(update.effective_chat.id)
    if not context.args:
        await safe_reply(update.message, "⚠️ Informe o número para correção. Ex: /corrigir 17")
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

    # =========================
    # 1) Placar da recomendação anterior — só se a ÚLTIMA resposta foi recomendação ativa
    # =========================
    if state.rec_active and state.current_rec and num != 0:
        dz = number_to_dozen(num)
        state.rec_plays += 1
        if dz in state.current_rec:
            # ACERTO
            state.rec_hits += 1
            # Se estava em gale e havia um erro “recuperável”, anula aquele erro
            if state.gale_left > 0 and state.gale_recover_miss and state.rec_misses > 0:
                state.rec_misses -= 1  # <-- ANULA O ERRO QUE DISPAROU O GALE
            # encerra gale e limpa flags
            state.gale_left = 0
            state.gale_dozen = None
            state.gale_recover_miss = False
        else:
            # ERRO
            state.rec_misses += 1
            # Se não havia gale pendente e gale está habilitado, arma 1 gale e marca erro “recuperável”
            if state.gale_enabled and state.gale_left == 0 and state.current_rec:
                state.gale_left = 1
                state.gale_dozen = list(state.current_rec)[0]
                state.gale_recover_miss = True
            else:
                # Se já estava em gale (errou o gale), encerra e limpa flags; pode ativar refratário
                if state.gale_left > 0:
                    state.gale_left = 0
                    state.gale_dozen = None
                    state.gale_recover_miss = False
                    if state.refractory_spins > 0:
                        state.refractory_left = state.refractory_spins

    # =========================
    # 2) Zero: limpa histórico e desativa rec_active/gale/timers
    # =========================
    if num == 0:
        state.reset_history()
        state.rec_active = False
        state.gale_left = 0
        state.gale_dozen = None
        state.gale_recover_miss = False
        state.refractory_left = 0
        await safe_reply(update.message, RESP_ZERO)
        return

    # =========================
    # 3) Adiciona número ao histórico
    # =========================
    state.add_number(num)

    # =========================
    # 4) Timers → WAIT e não pontuar próxima
    # =========================
    if state.refractory_left > 0:
        state.refractory_left -= 1
        state.rec_active = False
        await safe_reply(update.message, format_response(state, {"status": "wait"}))
        return
    if state.cooldown_left > 0:
        state.cooldown_left -= 1
        state.rec_active = False
        await safe_reply(update.message, format_response(state, {"status": "wait"}))
        return

    # =========================
    # 5) Análise (aplica gale se pendente, senão 3 em 4)
    # =========================
    analysis = analyze(state)

    # =========================
    # 6) Sincroniza recomendação/flag de atividade
    # =========================
    if analysis.get("status") == "ok":
        rec_text = analysis.get("recommendation", "").strip()  # "D1"/"D2"/"D3"
        state.set_recommendation(rec_text if rec_text else None)
        state.rec_active = True
    else:
        state.rec_active = False

    # =========================
    # 7) Responde
    # =========================
    msg = format_response(state, analysis)
    await safe_reply(update.message, msg)

# --- Health server (Render/healthcheck simples) ---
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

        def log_message(self, format, *args):
            logging.getLogger("health").info("%s - %s", self.address_string(), format % args)

    def serve():
        with socketserver.TCPServer(("0.0.0.0", port), HealthHandler) as httpd:
            log.info("Health server listening on 0.0.0.0:%s", port)
            httpd.serve_forever()

    t = threading.Thread(target=serve, daemon=True)
    t.start()

# --- Main assíncrono ---
async def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        log.error("BOT_TOKEN não definido como variável de ambiente.")
        raise RuntimeError("BOT_TOKEN não definido")

    tg_app = Application.builder().token(token).build()
    try:
        await tg_app.bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass

    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("help", help_cmd))
    tg_app.add_handler(CommandHandler("reset", reset))
    tg_app.add_handler(CommandHandler("janela", janela))
    tg_app.add_handler(CommandHandler("status", status))
    tg_app.add_handler(CommandHandler("banca", banca))
    tg_app.add_handler(CommandHandler("corrigir", corrigir))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_number))

    start_health_server()

    try:
        await tg_app.initialize()
        await tg_app.start()
        await tg_app.updater.start_polling(drop_pending_updates=True)
        log.info("Bot iniciado. Polling ativo.")
    except Conflict as e:
        log.error("CONFLICT: Outra instância usando o mesmo BOT_TOKEN. Detalhes: %s", e)
        raise SystemExit(1)
    except NetworkError as e:
        log.error("NetworkError ao iniciar polling: %s", e)
        raise
    except Exception as e:
        log.exception("Falha inesperada ao iniciar o bot: %s", e)
        raise

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

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        raise
    except Exception as e:
        log.exception("Falha ao iniciar a aplicação: %s", e)
        raise
