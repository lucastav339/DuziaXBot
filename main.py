# main.py — Webhook PTB 21.6 + aiohttp (Render)
# MODOS:
#  • Premium estatístico (padrão): chi-quadrado + burst
#  • Tendência curta (/tendencia): 2 seguidas repete; 4+ inverte; com Gale 1x
#
# UI: Histórico em grade fixa de bolinhas (não “anda”).
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
# Parâmetros — Modo Premium (rápido)
# =========================
WINDOW = int(os.getenv("WINDOW_SIZE", "30"))          # janela p/ chi-quadrado
CHI2_CRIT_DF1 = 3.841                                 # 5% df=1
GAP_MIN = int(os.getenv("GAP_MIN", "3"))              # gap mínimo
COOLDOWN_AFTER_EVAL = int(os.getenv("COOLDOWN", "3")) # cooldown curto

# ======= Visual do histórico em grade fixa =======
HISTORY_COLS = 30
MAX_HISTORY_ROWS = 8
HISTORY_PLACEHOLDER = "◻️"

# =========================
# Estado por usuário
# =========================
# Campos:
# - jogadas, acertos, erros
# - history: List[str] com {"R","B","Z"} — ilimitado
# - mode: "premium" | "tendencia"
# - pending_signal: Optional["R"|"B"]
# - pending_stage: None | "base" | "gale"   (apenas no modo tendencia)
# - cooldown_left: int (usado no premium)
STATE: Dict[int, Dict[str, Any]] = {}

def get_state(user_id: int) -> Dict[str, Any]:
    if user_id not in STATE:
        STATE[user_id] = {
            "jogadas": 0,
            "acertos": 0,
            "erros": 0,
            "history": [],
            "mode": "premium",
            "pending_signal": None,
            "pending_stage": None,
            "cooldown_left": 0,
        }
    return STATE[user_id]

# =========================
# UI (teclado e helpers)
# =========================
CHOICES = ["🔴 Vermelho", "⚫ Preto", "🟢 Zero"]
# Substitui /status por /tendencia no teclado
KB = ReplyKeyboardMarkup([CHOICES, ["/tendencia", "/reset", "/estrategia"]], resize_keyboard=True)

def as_symbol(c: str) -> str:
    return "🔴" if c == "R" else ("⚫" if c == "B" else "🟢")

def render_history_grid(history: List[str]) -> str:
    syms = [as_symbol(c) for c in history]
    rows: List[List[str]] = []
    for i in range(0, len(syms), HISTORY_COLS):
        rows.append(syms[i:i + HISTORY_COLS])
    if not rows:
        return HISTORY_PLACEHOLDER * HISTORY_COLS
    last = rows[-1]
    if len(last) < HISTORY_COLS:
        last = last + [HISTORY_PLACEHOLDER] * (HISTORY_COLS - len(last))
        rows[-1] = last
    rows_to_show = rows[-MAX_HISTORY_ROWS:]
    rendered_lines: List[str] = []
    total_rows = len(rows)
    start_row_index = total_rows - len(rows_to_show) + 1
    for idx, row in enumerate(rows_to_show, start=start_row_index):
        prefix = f"L{idx:02d}: "
        rendered_lines.append(prefix + "".join(row))
    return "\n".join(rendered_lines)

def pretty_status(st: Dict[str, Any]) -> str:
    j, a, e = st["jogadas"], st["acertos"], st["erros"]
    taxa = (a / j * 100.0) if j > 0 else 0.0
    pend = st["pending_signal"]
    pend_stage = st.get("pending_stage")
    cool = st["cooldown_left"]
    label_pend = "—" if pend is None else ("🔴" if pend=="R" else "⚫")
    stage = "—"
    if pend_stage in ("base","gale"):
        stage = "BASE" if pend_stage=="base" else "GALE"
    return (
        "🏷️ <b>Status</b>\n"
        f"• 🎯 <b>Jogadas:</b> {j}\n"
        f"• ✅ <b>Acertos:</b> {a}\n"
        f"• ❌ <b>Erros:</b> {e}\n"
        f"• 📈 <b>Taxa:</b> {taxa:.2f}%\n"
        f"• 🧭 <b>Modo:</b> {st['mode']}\n"
        f"• 🧠 <b>Sinal pendente:</b> {label_pend} ({stage})\n"
        f"• ⏱️ <b>Cooldown:</b> {cool}"
    )

# =========================
# Lógica — Premium (burst + chi-quadrado)
# =========================
def fast_burst_trigger(history: List[str]) -> Optional[str]:
    rb = [h for h in history if h in ("R","B")]
    if len(rb) < 12:
        return None
    last = rb[-12:]
    r = last.count("R")
    b = 12 - r
    if max(r,b) >= 9:
        return "R" if r > b else "B"
    return None

def decide_signal_premium(history: List[str]) -> Optional[str]:
    burst = fast_burst_trigger(history)
    if burst is not None:
        return burst
    window = history[-WINDOW:] if len(history) > WINDOW else history[:]
    rb = [h for h in window if h in ("R","B")]
    n = len(rb)
    if n < 14:
        return None
    r = rb.count("R")
    b = n - r
    exp = n/2.0
    chi2 = 0.0 if exp==0 else ((r-exp)**2)/exp + ((b-exp)**2)/exp
    gap = abs(r-b)
    if chi2 >= CHI2_CRIT_DF1 and gap >= GAP_MIN:
        return "R" if r>b else "B"
    return None

# =========================
# Lógica — Tendência curta + Gale 1x
# =========================
def last_streak_color(history: List[str]) -> Optional[str]:
    """
    Retorna ('R'|'B', tamanho_da_streak) considerando que 'Z' quebra a sequência.
    Ignora rótulos fora de R/B/Z.
    """
    # percorre do fim até encontrar primeira R/B
    i = len(history) - 1
    while i >= 0 and history[i] not in ("R","B"):
        # Z ou outra marca quebra a sequência
        i -= 1
        # se for zero logo no final, não existe sequência contínua R/B
        if i < 0:
            return None
        if history[i] not in ("R","B"):
            return None
    if i < 0:
        return None
    color = history[i]
    streak = 1
    j = i - 1
    while j >= 0 and history[j] == color:
        streak += 1
        j -= 1
    return (color, streak)

def decide_signal_trend(history: List[str]) -> Optional[str]:
    """
    Regras:
      • 2 seguidas → apostar que repete (mesma cor).
      • 4+ seguidas → apostar na cor oposta.
    """
    res = last_streak_color(history)
    if not res:
        return None
    color, streak = res
    if streak >= 4:
        return "B" if color == "R" else "R"
    if streak >= 2:
        return color
    return None

# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE[uid] = {
        "jogadas": 0, "acertos": 0, "erros": 0,
        "history": [], "mode": "premium",
        "pending_signal": None, "pending_stage": None,
        "cooldown_left": 0
    }
    await update.message.reply_html(
        "🤖 <b>iDozen Premium — Análise de Cores</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Envie o <b>resultado</b> usando os botões abaixo.\n"
        "Modos: <b>premium</b> (estatística) ou <b>tendencia</b> (curta com Gale 1x via /tendencia).\n\n"
        "Comandos: <b>/tendencia</b> • <b>/reset</b> • <b>/estrategia</b>",
        reply_markup=KB,
    )

async def estrategia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_user.id)
    if st["mode"] == "tendencia":
        await update.message.reply_html(
            "🧭 <b>Modo: Tendência Curta</b>\n"
            "• Se uma cor saiu <b>2x seguidas</b>, apostar que <b>repete</b>.\n"
            "• Se saiu <b>4x seguidas</b> ou mais, apostar na <b>oposta</b>.\n"
            "• <b>Gale 1x</b>: se errar a base, repete a aposta 1x; se acertar no gale, conta <b>acerto</b> e <b>não</b> conta erro.\n"
            "• Zeros (🟢) quebram a sequência.\n",
            reply_markup=KB,
        )
    else:
        await update.message.reply_html(
            "📚 <b>Modo: Premium (rápido)</b>\n"
            f"• Burst: últimas 12 (ignora 🟢); se ≥9 da mesma cor → sinal.\n"
            f"• Janela: últimas {WINDOW} (R/B), χ² ≥ {CHI2_CRIT_DF1} + gap ≥ {GAP_MIN}.\n"
            f"• Cooldown: {COOLDOWN_AFTER_EVAL} giros após avaliar.\n"
            "• Zero (🟢) não entra no teste de cor, mas é registrado.\n",
            reply_markup=KB,
        )

async def toggle_tendencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_user.id)
    if st["mode"] == "tendencia":
        st["mode"] = "premium"
        st["pending_signal"] = None
        st["pending_stage"] = None
        await update.message.reply_html(
            "🧭 Modo alterado para <b>premium</b> (estatística).", reply_markup=KB
        )
    else:
        st["mode"] = "tendencia"
        st["pending_signal"] = None
        st["pending_stage"] = None
        await update.message.reply_html(
            "🧭 Modo alterado para <b>tendência curta</b> (com Gale 1x).", reply_markup=KB
        )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Continua disponível (não está no teclado, mas o comando existe)
    st = get_state(update.effective_user.id)
    hist_grid = render_history_grid(st["history"])
    await update.message.reply_html(
        "📊 <b>Status</b>\n"
        f"{pretty_status(st)}\n\n"
        "🧩 <b>Histórico (grade fixa):</b>\n"
        f"{hist_grid}",
        reply_markup=KB,
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE[uid] = {
        "jogadas": 0, "acertos": 0, "erros": 0,
        "history": [], "mode": "premium",
        "pending_signal": None, "pending_stage": None,
        "cooldown_left": 0
    }
    await update.message.reply_html("♻️ <b>Histórico e placar resetados.</b>", reply_markup=KB)

# --------- Avaliação de sinais (com/sem gale) ----------
def evaluate_premium_on_spin(st: Dict[str, Any], obs: str) -> str:
    """Avalia pendência no modo premium (sem gale)."""
    outcome_msg = ""
    if st["pending_signal"] in ("R","B"):
        if obs == st["pending_signal"]:
            st["jogadas"] += 1
            st["acertos"] += 1
            outcome_msg = "🏆 <b>Resultado:</b> ✅ Acerto no sinal anterior."
        elif obs in ("R","B"):
            st["jogadas"] += 1
            st["erros"] += 1
            outcome_msg = "🏆 <b>Resultado:</b> ❌ Erro no sinal anterior."
        else:
            outcome_msg = "🏆 <b>Resultado:</b> 🟢 Zero — sinal não contabilizado."
        st["pending_signal"] = None
        st["cooldown_left"] = COOLDOWN_AFTER_EVAL
    return outcome_msg

def evaluate_trend_on_spin(st: Dict[str, Any], obs: str) -> str:
    """
    Avalia pendência no modo tendência com Gale 1x:
    - Se base erra e obs é R/B → não conta ainda; entra 'gale'
    - Se gale acerta → conta 1 acerto e não conta erro
    - Se gale erra → conta 1 erro
    - Zero não avalia; mantém pendência
    """
    msg = ""
    sig = st["pending_signal"]
    stage = st["pending_stage"]
    if sig not in ("R","B"):
        return msg

    if obs == "Z":
        return "🏆 <b>Resultado:</b> 🟢 Zero — aguardando avaliação."

    if stage == "base":
        if obs == sig:
            st["jogadas"] += 1
            st["acertos"] += 1
            msg = "🏆 <b>Resultado:</b> ✅ Acerto na BASE."
            st["pending_signal"] = None
            st["pending_stage"] = None
        else:
            # errou base → entra Gale 1x (sem contar erro agora)
            st["pending_stage"] = "gale"
            msg = "🔁 <b>Gale 1x:</b> repetir a mesma cor no próximo giro."
    elif stage == "gale":
        st["jogadas"] += 1
        if obs == sig:
            # acerto no gale → conta ACERTO e NÃO conta o erro da base
            st["acertos"] += 1
            msg = "🏆 <b>Resultado:</b> ✅ Acerto no GALE (sem erro contabilizado)."
        else:
            st["erros"] += 1
            msg = "🏆 <b>Resultado:</b> ❌ Erro no GALE."
        st["pending_signal"] = None
        st["pending_stage"] = None

    return msg

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

    # 1) Atualiza histórico (ilimitado)
    st["history"].append(obs)

    # 2) Avaliação de pendências conforme modo
    hist_grid = render_history_grid(st["history"])
    outcome_msg = ""

    if st["mode"] == "tendencia":
        outcome_msg = evaluate_trend_on_spin(st, obs)
        # no modo tendência, sem cooldown
    else:
        # premium
        if st["cooldown_left"] > 0:
            st["cooldown_left"] -= 1
        outcome_msg = evaluate_premium_on_spin(st, obs)

    # 3) Se ainda não há pendência ativa (ou ela foi concluída), tentar novo sinal
    recommend_msg = ""
    if st["pending_signal"] is None and obs in ("R","B"):  # só decide após registrar algo útil
        if st["mode"] == "tendencia":
            sig = decide_signal_trend(st["history"])
            if sig:
                st["pending_signal"] = sig
                st["pending_stage"] = "base"
                cor_txt = "🔴 Vermelho" if sig == "R" else "⚫ Preto"
                recommend_msg = (
                    "🎯 <b>Sinal — Tendência Curta</b>\n"
                    f"• Apostar em: <b>{cor_txt}</b>\n"
                    "• Regras: 2 seguidas repete; 4+ inverte.\n"
                    "• <b>Gale 1x</b> habilitado (se base errar).\n"
                    "👉 Envie o próximo resultado para avaliar."
                )
        else:
            # premium: só se cooldown zerado
            if st["cooldown_left"] <= 0:
                sig = decide_signal_premium(st["history"])
                if sig:
                    st["pending_signal"] = sig
                    cor_txt = "🔴 Vermelho" if sig == "R" else "⚫ Preto"
                    recommend_msg = (
                        "🎯 <b>Recomendação Premium</b>\n"
                        f"• Apostar em: <b>{cor_txt}</b>\n"
                        f"• Motivo: <i>viés recente (burst) ou χ² ≥ {CHI2_CRIT_DF1} + gap ≥ {GAP_MIN}</i>.\n"
                        "👉 Envie o próximo resultado para avaliar."
                    )

    # 4) Resposta consolidada
    base_msg = outcome_msg or "📥 Resultado registrado."
    extra = recommend_msg if recommend_msg else "🧩 <b>Histórico (grade fixa):</b>\n" + hist_grid
    await update.message.reply_html(
        f"{base_msg}\n\n{extra}\n\n{pretty_status(st)}",
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
    tg_app.add_handler(CommandHandler("tendencia", toggle_tendencia))  # << novo comando
    tg_app.add_handler(CommandHandler("status", status_cmd))           # ainda existe como comando opcional
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
