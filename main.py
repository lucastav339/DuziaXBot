# main.py — Webhook PTB 21.6 + aiohttp (Render)
# LÓGICA PREMIUM: Sinaliza a próxima COR só quando há evidência de viés (qui-quadrado).
# UI PREMIUM: Mensagens formatadas, status claro, cooldown após avaliar um sinal.
#
# Requisitos:
#   python-telegram-bot==21.6
#   aiohttp==3.10.5
#
# ENV:
#   BOT_TOKEN, WEBHOOK_URL, (opcional) WEBHOOK_PATH, TG_SECRET_TOKEN, PORT

import os
import sys
import json
import math
import asyncio
import logging
import signal
from typing import Dict, Any, List, Optional

from aiohttp import web
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, filters
)

# =========================
# Config & ENV
# =========================
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger("duziaxbot")

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")                    # ex: https://duziaxbot.onrender.com
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "webhook")       # personalize (ex: webhook-a7d1c3)
SECRET_TOKEN = os.getenv("TG_SECRET_TOKEN")               # recomendado
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Defina BOT_TOKEN")
if not WEBHOOK_URL:
    raise RuntimeError("Defina WEBHOOK_URL (https://...onrender.com)")

try:
    import telegram
    log.info(f"python-telegram-bot: {telegram.__version__}")
except Exception:
    pass
log.info(f"Python: {sys.version}")
log.info(f"Webhook: {WEBHOOK_URL.rstrip('/')}/{WEBHOOK_PATH}")

# =========================
# Parâmetros da Estratégia
# =========================
WINDOW = int(os.getenv("WINDOW_SIZE", "60"))          # tamanho da janela
ALPHA = 0.01                                          # nível de significância
CHI2_CRIT_DF1 = 6.635                                 # crítico 1% df=1
GAP_MIN = int(os.getenv("GAP_MIN", "5"))              # diferença mínima V - P
COOLDOWN_AFTER_EVAL = int(os.getenv("COOLDOWN", "5")) # giros após avaliar um sinal

# =========================
# Estado por usuário
# =========================
# Campos:
# - jogadas, acertos, erros
# - history: List[str] com valores em {"R","B","Z"}
# - pending_signal: Optional[str] em {"R","B"} aguardando avaliação no próximo giro
# - cooldown_left: int (giros a aguardar antes de novo sinal)
STATE: Dict[int, Dict[str, Any]] = {}

def get_state(user_id: int) -> Dict[str, Any]:
    if user_id not in STATE:
        STATE[user_id] = {
            "jogadas": 0,
            "acertos": 0,
            "erros": 0,
            "history": [],              # sequência compacta: R/B/Z
            "pending_signal": None,     # cor sinalizada a ser avaliada no próximo giro
            "cooldown_left": 0,         # aguarda X giros após avaliação
        }
    return STATE[user_id]

# =========================
# UI (teclado e helpers)
# =========================
CHOICES = ["🔴 Vermelho", "⚫ Preto", "🟢 Zero"]
KB = ReplyKeyboardMarkup([CHOICES, ["/status", "/reset", "/estrategia"]], resize_keyboard=True)

def pretty_status(st: Dict[str, Any]) -> str:
    j, a, e = st["jogadas"], st["acertos"], st["erros"]
    taxa = (a / j * 100.0) if j > 0 else 0.0
    pend = st["pending_signal"]
    cool = st["cooldown_left"]
    label_pend = "—" if pend is None else ("🔴" if pend=="R" else "⚫")
    return (
        "🏷️ <b>Status</b>\n"
        f"• 🎯 <b>Jogadas:</b> {j}\n"
        f"• ✅ <b>Acertos:</b> {a}\n"
        f"• ❌ <b>Erros:</b> {e}\n"
        f"• 📈 <b>Taxa:</b> {taxa:.2f}%\n"
        f"• ⏱️ <b>Cooldown:</b> {cool}\n"
        f"• 🧠 <b>Sinal pendente:</b> {label_pend}"
    )

def as_symbol(c: str) -> str:
    return "🔴" if c == "R" else ("⚫" if c == "B" else "🟢")

# =========================
# Estatística da janela
# =========================
def decide_signal(history: List[str]) -> Optional[str]:
    """
    Retorna 'R' ou 'B' quando há evidência de viés forte; None caso contrário.
    - Usa only R/B (ignora Z) para teste chi-quadrado df=1.
    - Requer gap mínimo e valor de qui-quadrado acima do crítico em 1%.
    """
    # pega última janela
    window = history[-WINDOW:] if len(history) > WINDOW else history[:]
    rb = [h for h in window if h in ("R", "B")]
    n = len(rb)
    if n < 20:  # amostra mínima razoável
        return None

    r = rb.count("R")
    b = n - r
    # expectativa sob H0 (justa, ignorando zeros): 50/50
    exp = n / 2.0
    chi2 = 0.0
    if exp > 0:
        chi2 = ((r - exp) ** 2) / exp + ((b - exp) ** 2) / exp

    gap = abs(r - b)
    if chi2 >= CHI2_CRIT_DF1 and gap >= GAP_MIN:
        # Direção do viés
        return "R" if r > b else "B"
    return None

# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE[uid] = {
        "jogadas": 0, "acertos": 0, "erros": 0,
        "history": [], "pending_signal": None, "cooldown_left": 0
    }
    await update.message.reply_html(
        "🤖 <b>iDozen Premium — Análise de Cores</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Envie o <b>resultado</b> usando os botões abaixo.\n"
        "O bot só recomenda se houver <b>vantagem estatística</b>.\n\n"
        "Comandos: <b>/status</b> • <b>/reset</b> • <b>/estrategia</b>",
        reply_markup=KB,
    )

async def estrategia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "📚 <b>Metodologia Premium (resumo)</b>\n"
        "• Janela deslizante das últimas <b>{}</b> jogadas.\n"
        "• Teste <b>Qui-Quadrado</b> (df=1, α=1%) em <b>Vermelho vs Preto</b> (ignora zeros).\n"
        "• Sinal apenas se <b>evidência forte</b> (χ² ≥ 6,635) e <b>gap</b> mínimo entre cores.\n"
        "• Após avaliar um sinal, aguarda <b>{}</b> giros antes de emitir outro.\n"
        "• Saídas “Sem vantagem” protegem seu bankroll de entradas -EV.\n\n"
        "💡 Zero (0) reinicia parcialmente a leitura (não influencia no teste de cor).".format(WINDOW, COOLDOWN_AFTER_EVAL),
        reply_markup=KB,
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_user.id)
    await update.message.reply_html(pretty_status(st), reply_markup=KB)

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE[uid] = {
        "jogadas": 0, "acertos": 0, "erros": 0,
        "history": [], "pending_signal": None, "cooldown_left": 0
    }
    await update.message.reply_html("♻️ <b>Histórico e placar resetados.</b>", reply_markup=KB)

async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = get_state(uid)
    text = (update.message.text or "").strip()

    if text not in CHOICES:
        await update.message.reply_html("Use os botões abaixo para registrar o resultado.", reply_markup=KB)
        return

    # mapeia entrada → símbolo compacto
    if text.startswith("🔴"):
        obs = "R"
    elif text.startswith("⚫"):
        obs = "B"
    else:
        obs = "Z"

    # 1) Atualiza histórico
    st["history"].append(obs)
    if len(st["history"]) > 500:
        st["history"] = st["history"][-500:]  # teto de memória

    # 2) Se havia sinal pendente, avalia neste giro
    outcome_msg = ""
    if st["pending_signal"] in ("R", "B"):
        st["jogadas"] += 1
        if obs == st["pending_signal"]:
            st["acertos"] += 1
            outcome_msg = "🏆 <b>Resultado:</b> ✅ Acerto no sinal anterior."
        elif obs in ("R", "B"):
            st["erros"] += 1
            outcome_msg = "🏆 <b>Resultado:</b> ❌ Erro no sinal anterior."
        else:
            outcome_msg = "🏆 <b>Resultado:</b> 🟢 Zero — sinal não contabilizado."

        # aplica cooldown e limpa pendência
        st["pending_signal"] = None
        st["cooldown_left"] = COOLDOWN_AFTER_EVAL

    # 3) Se cooldown > 0, apenas consome uma unidade
    if st["cooldown_left"] > 0:
        st["cooldown_left"] -= 1
        await update.message.reply_html(
            "{}\n\n⏳ <b>Coletando dados (cooldown).</b>\n{}".format(
                outcome_msg or "📥 Resultado registrado.",
                pretty_status(st)
            ),
            reply_markup=KB,
        )
        return

    # 4) Se não há cooldown, tenta decidir um novo sinal
    signal = decide_signal(st["history"])

    if signal is None:
        # Sem evidência forte
        await update.message.reply_html(
            "{}\n\n🧪 <b>Sem vantagem estatística.</b> Continuando a coleta…\n{}".format(
                outcome_msg or "📥 Resultado registrado.",
                pretty_status(st)
            ),
            reply_markup=KB,
        )
        return

    # 5) Emite sinal (a ser avaliado no próximo giro)
    st["pending_signal"] = signal
    cor_txt = "🔴 Vermelho" if signal == "R" else "⚫ Preto"
    await update.message.reply_html(
        "{}\n\n🎯 <b>Recomendação Premium</b>\n"
        "• Apostar em: <b>{}</b>\n"
        "• Motivo: <i>viés de cor detectado</i> (χ² ≥ 6,635 e gap ≥ {}).\n\n"
        "👉 <b>Agora envie o próximo resultado</b> para avaliarmos o sinal."
        "\n\n{}".format(
            outcome_msg or "📥 Resultado registrado.",
            cor_txt, GAP_MIN, pretty_status(st)
        ),
        reply_markup=KB,
    )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Erro no handler:", exc_info=context.error)

# =========================
# Servidor aiohttp (Webhook + Health)
# =========================
def build_web_app(tg_app: Application) -> web.Application:
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="OK", status=200)

    async def telegram_webhook(request: web.Request) -> web.Response:
        if SECRET_TOKEN:
            recv = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if recv != SECRET_TOKEN:
                return web.Response(text="Forbidden", status=403)
        try:
            data = await request.json()
        except Exception:
            data = json.loads(await request.text())
        update = Update.de_json(data, tg_app.bot)
        try:
            tg_app.update_queue.put_nowait(update)
        except Exception:
            asyncio.create_task(tg_app.process_update(update))
        return web.Response(text="OK", status=200)

    app.router.add_get("/health", health)
    app.router.add_get(f"/{WEBHOOK_PATH}", health)
    app.router.add_post(f"/{WEBHOOK_PATH}", telegram_webhook)
    app.router.add_get("/", health)
    return app

# =========================
# Boot
# =========================
async def amain():
    tg_app = ApplicationBuilder().token(BOT_TOKEN).build()
    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("status", status_cmd))
    tg_app.add_handler(CommandHandler("reset", reset_cmd))
    tg_app.add_handler(CommandHandler("estrategia", estrategia))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_choice))
    tg_app.add_error_handler(error_handler)

    await tg_app.initialize()
    await tg_app.start()
    log.info("PTB Application started (custom webhook server).")

    webhook_full = WEBHOOK_URL.rstrip("/") + f"/{WEBHOOK_PATH}"
    ok = await tg_app.bot.set_webhook(
        url=webhook_full,
        drop_pending_updates=True,
        allowed_updates=None,
        secret_token=SECRET_TOKEN
    )
    log.info(f"setWebhook({webhook_full}) → {ok}")

    web_app = build_web_app(tg_app)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=PORT)
    await site.start()
    log.info(f"Servidor aiohttp ouvindo em 0.0.0.0:{PORT}")

    stop_event = asyncio.Event()
    def _sig(): stop_event.set()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        try: loop.add_signal_handler(s, _sig)
        except NotImplementedError: pass

    try:
        await stop_event.wait()
    finally:
        await tg_app.stop()
        await tg_app.shutdown()
        await runner.cleanup()
        log.info("Encerrado.")

def main():
    asyncio.run(amain())

if __name__ == "__main__":
    main()
