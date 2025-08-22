# main.py — PTB 21.6 + aiohttp (Render)
# Estratégia ÚNICA: /faixa (Altos/Baixos + mesma cor) com Gale 1x
#
# UI: Teclado INLINE com botões 0–36 (cada um já com cor: 🔴/⚫/🟢). Clique = registra número.
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
from typing import Dict, Any, List, Optional, Tuple

from aiohttp import web
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
    MessageHandler, CallbackQueryHandler, filters
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

# ======= Parâmetros =======
HISTORY_COLS = 30
MAX_HISTORY_ROWS = 8
HISTORY_PLACEHOLDER = "◻️"

POSTWIN_SPINS = int(os.getenv("POSTWIN_SPINS", "5"))  # coletas pós-acerto

# ======= Mapeamento cor (Roleta Europeia) =======
RED_SET = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
BLACK_SET = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}

HIGH_SET = set(range(19, 37))   # 19-36
LOW_SET  = set(range(1, 19))    # 1-18

HIGH_RED   = sorted(list(HIGH_SET & RED_SET))    # 9 números
HIGH_BLACK = sorted(list(HIGH_SET & BLACK_SET))  # 9 números
LOW_RED    = sorted(list(LOW_SET  & RED_SET))    # 9 números
LOW_BLACK  = sorted(list(LOW_SET  & BLACK_SET))  # 9 números

def color_of(n: int) -> Optional[str]:
    if n == 0: return None
    if n in RED_SET: return "R"
    if n in BLACK_SET: return "B"
    return None

def hilo_of(n: int) -> Optional[str]:
    if n == 0: return None
    return "H" if n in HIGH_SET else "L"

def bucket_numbers(hilo: str, color: str) -> List[int]:
    if hilo == "H" and color == "R": return HIGH_RED
    if hilo == "H" and color == "B": return HIGH_BLACK
    if hilo == "L" and color == "R": return LOW_RED
    if hilo == "L" and color == "B": return LOW_BLACK
    return []

# =========================
# Estado por usuário
# =========================
# Campos:
# - jogadas, acertos, erros
# - history: List[str] com {"R","B","Z"}
# - numbers: List[Optional[int]] (número clicado; None não ocorre aqui)
# - postwin_wait_left: int  — contador pós-acerto
# - pending_bucket: Optional[Tuple["H"|"L","R"|"B"]] — pendência da estratégia /faixa
# - pending_bucket_stage: None | "base" | "gale" — estágio do Gale na /faixa
STATE: Dict[int, Dict[str, Any]] = {}

def _fresh_state() -> Dict[str, Any]:
    return {
        "jogadas": 0,
        "acertos": 0,
        "erros": 0,
        "history": [],
        "numbers": [],
        "postwin_wait_left": 0,
        "pending_bucket": None,
        "pending_bucket_stage": None,
    }

def get_state(user_id: int) -> Dict[str, Any]:
    if user_id not in STATE:
        STATE[user_id] = _fresh_state()
    return STATE[user_id]

# =========================
# UI — Teclado Inline 0–36
# =========================
def label_for_number(n: int) -> str:
    if n == 0:
        return "🟢 0"
    return f"{'🔴' if n in RED_SET else '⚫'} {n}"

def build_numeric_keyboard() -> InlineKeyboardMarkup:
    """
    Distribuição em linhas:
    0 sozinho
    1–9, 10–18, 19–27, 28–36 (9 por linha)
    """
    rows: List[List[InlineKeyboardButton]] = []

    # zero
    rows.append([InlineKeyboardButton(text=label_for_number(0), callback_data="num:0")])

    # faixas de 1 a 36
    for start in (1, 10, 19, 28):
        row: List[InlineKeyboardButton] = []
        for n in range(start, min(start + 9, 37)):
            row.append(InlineKeyboardButton(text=label_for_number(n), callback_data=f"num:{n}"))
        rows.append(row)

    return InlineKeyboardMarkup(rows)

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
    post = st.get("postwin_wait_left", 0)
    bucket = st.get("pending_bucket")
    bucket_stage = st.get("pending_bucket_stage")
    bucket_txt = "—"
    if bucket:
        hilo, col = bucket
        cor = "🔴" if col == "R" else "⚫"
        faixa_name = "Altos" if hilo == "H" else "Baixos"
        stage = "BASE" if bucket_stage == "base" else ("GALE" if bucket_stage == "gale" else "—")
        bucket_txt = f"{faixa_name} {cor} ({stage})"
    return (
        "🏷️ <b>Status</b>\n"
        f"• 🎯 <b>Jogadas:</b> {j}\n"
        f"• ✅ <b>Acertos:</b> {a}\n"
        f"• ❌ <b>Erros:</b> {e}\n"
        f"• 📈 <b>Taxa:</b> {taxa:.2f}%\n"
        f"• 🎯 <b>Sinal pendente:</b> {bucket_txt}\n"
        f"• ⌛ <b>Pós-acerto:</b> {post}/{POSTWIN_SPINS if post>0 else 0}"
    )

# =========================
# Estratégia /faixa (gatilho + avaliação com Gale 1x)
# =========================
def last_k_nonzero(numbers: List[int], k: int) -> Optional[List[int]]:
    buf: List[int] = []
    for n in reversed(numbers):
        if n == 0:   # zero quebra a sequência
            break
        buf.append(n)
        if len(buf) == k:
            break
    if len(buf) < k:
        return None
    return list(reversed(buf))

def faixa_trigger(numbers: List[int]) -> Optional[Tuple[str,str]]:
    """
    Se os últimos 4 números consecutivos não-zeros existirem e forem:
      - todos 'H' (19-36) OU todos 'L' (1-18)
      - e todos da MESMA cor (R/B)
    então retorna (hilo, color) → ("H"/"L", "R"/"B")
    """
    seq = last_k_nonzero(numbers, 4)
    if not seq:
        return None
    colors = [color_of(n) for n in seq]
    hilos  = [hilo_of(n)  for n in seq]
    if None in colors or None in hilos:
        return None
    if len(set(colors)) == 1 and len(set(hilos)) == 1:
        return (hilos[0], colors[0])
    return None

def evaluate_faixa_on_spin(st: Dict[str, Any], num: int) -> str:
    """
    Avalia pendência da /faixa com Gale 1x:
      - BASE: se num ∈ conjunto → ACERTO; se num==0 → aguarda; se fora → entra GALE (sem contar erro)
      - GALE: se num ∈ conjunto → ACERTO (sem contar erro da base); se fora → ERRO
    """
    msg = ""
    bucket = st.get("pending_bucket")
    stage  = st.get("pending_bucket_stage")
    if not bucket:
        return msg

    if num == 0:
        return "🏆 <b>/faixa:</b> 🟢 Zero — aguardando avaliação."

    hilo, col = bucket
    allowed = set(bucket_numbers(hilo, col))

    if stage == "base":
        if num in allowed:
            st["jogadas"] += 1
            st["acertos"] += 1
            msg = "🏆 <b>/faixa:</b> ✅ Acerto na BASE."
            st["pending_bucket"] = None
            st["pending_bucket_stage"] = None
            _arm_postwin(st)
        else:
            st["pending_bucket_stage"] = "gale"
            msg = "🔁 <b>/faixa Gale 1x:</b> repetir a mesma faixa+cor no próximo giro."
    elif stage == "gale":
        st["jogadas"] += 1
        if num in allowed:
            st["acertos"] += 1
            msg = "🏆 <b>/faixa:</b> ✅ Acerto no GALE (sem erro contabilizado)."
            st["pending_bucket"] = None
            st["pending_bucket_stage"] = None
            _arm_postwin(st)
        else:
            st["erros"] += 1
            msg = "🏆 <b>/faixa:</b> ❌ Erro no GALE."
            st["pending_bucket"] = None
            st["pending_bucket_stage"] = None
    return msg

# --------- Pós-acerto Helpers ----------
def _arm_postwin(st: Dict[str, Any]) -> None:
    st["pending_bucket"] = None
    st["pending_bucket_stage"] = None
    st["postwin_wait_left"] = POSTWIN_SPINS

def _tick_postwin_and_maybe_reset(st: Dict[str, Any]) -> Optional[str]:
    if st.get("postwin_wait_left", 0) <= 0:
        return None
    st["postwin_wait_left"] -= 1
    remaining = st["postwin_wait_left"]
    if remaining > 0:
        return f"⏳ <b>Coleta pós-acerto:</b> {POSTWIN_SPINS-remaining}/{POSTWIN_SPINS}. Sem novos sinais."
    st["history"] = []
    st["numbers"] = []
    return "♻️ <b>Coleta concluída.</b> Histórico zerado. Reiniciando análise."

# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE[uid] = _fresh_state()
    kb = build_numeric_keyboard()
    await update.message.reply_html(
        "🎯 <b>iDozen — Estratégia Faixa c/ Gale 1x</b>\n"
        "Envie o resultado clicando no <b>número</b> abaixo.\n"
        "• Gatilho: 4 números seguidos (sem zero) todos <b>Altos</b> ou todos <b>Baixos</b> e da <b>mesma cor</b>.\n"
        "• Sinal: aposta nos <b>9 números</b> da faixa+cor.\n"
        "• Gale 1x: se base errar (≠0), repete 1x.\n"
        "• Zero (🟢) não avalia.\n"
        "• Pós-acerto: coleta 5 giros e zera apenas o histórico.",
        reply_markup=kb,
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_user.id)
    hist_grid = render_history_grid(st["history"])
    kb = build_numeric_keyboard()
    await update.message.reply_html(
        "📊 <b>Status</b>\n"
        f"{pretty_status(st)}\n\n"
        "🧩 <b>Histórico (grade fixa):</b>\n"
        f"{hist_grid}",
        reply_markup=kb,
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE[uid] = _fresh_state()
    kb = build_numeric_keyboard()
    await update.message.reply_html("♻️ <b>Histórico e placar resetados.</b>", reply_markup=kb)

def _append_spin(st: Dict[str, Any], n: int) -> None:
    # Atualiza históricos
    st["numbers"].append(n)
    if n == 0:
        st["history"].append("Z")
    else:
        st["history"].append(color_of(n) or "Z")

async def _handle_spin_and_respond(message_fn, st: Dict[str, Any], n: int):
    # 1) Atualiza histórico
    _append_spin(st, n)

    # 2) Avalia pendência /faixa (com Gale)
    msgs: List[str] = []
    if st.get("pending_bucket"):
        m = evaluate_faixa_on_spin(st, n)
        if m:
            msgs.append(m)

    # 3) Pós-acerto ativo?
    post_msg = _tick_postwin_and_maybe_reset(st)
    if post_msg:
        msgs.append(post_msg)
        kb = build_numeric_keyboard()
        await message_fn(
            "\n".join(msgs) + "\n\n" +
            "🧩 <b>Histórico (grade fixa):</b>\n" + render_history_grid(st["history"]) + "\n\n" +
            pretty_status(st),
            reply_markup=kb
        )
        return

    # 4) Se não há pendência e não está em pós-acerto, tentar gerar novo sinal
    if st.get("pending_bucket") is None and st.get("postwin_wait_left", 0) == 0 and n != 0:
        trig = faixa_trigger(st["numbers"])
        if trig:
            hilo, col = trig
            st["pending_bucket"] = (hilo, col)
            st["pending_bucket_stage"] = "base"
            faixa_name = "Altos" if hilo == "H" else "Baixos"
            cor_txt = "🔴 Vermelho" if col == "R" else "⚫ Preto"
            nums = bucket_numbers(hilo, col)
            msgs.append(
                "🎯 <b>Sinal — Faixa</b>\n"
                f"• Faixa: <b>{faixa_name}</b> • Cor: <b>{cor_txt}</b>\n"
                f"• Números: <code>{', '.join(map(str, nums))}</code>\n"
                "• <b>Gale 1x</b> habilitado (se base errar).\n"
                "👉 Clique no próximo número que sair."
            )

    # 5) Responder com status e teclado
    base_msg = "\n".join(msgs) if msgs else "📥 Resultado registrado."
    kb = build_numeric_keyboard()
    await message_fn(
        base_msg + "\n\n" +
        "🧩 <b>Histórico (grade fixa):</b>\n" + render_history_grid(st["history"]) + "\n\n" +
        pretty_status(st),
        reply_markup=kb
    )

# Callback de clique nos botões numéricos
async def on_number_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    st = get_state(uid)

    data = query.data or ""
    if not data.startswith("num:"):
        await query.answer()
        return
    try:
        n = int(data.split(":")[1])
    except Exception:
        await query.answer()
        return

    await query.answer(text=f"Registrado: {label_for_number(n)}", show_alert=False)

    async def reply_fn(text: str, reply_markup: InlineKeyboardMarkup):
        # Edita a mensagem do callback se possível; caso contrário, envia nova
        try:
            await query.message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_html(text, reply_markup=reply_markup)

    await _handle_spin_and_respond(reply_fn, st, n)

# (Opcional) aceitar dígitos digitados
async def handle_digit_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = (update.message.text or "").strip()
    if not t.isdigit():
        return
    n = int(t)
    if not (0 <= n <= 36):
        return
    uid = update.effective_user.id
    st = get_state(uid)

    async def reply_fn(text: str, reply_markup: InlineKeyboardMarkup):
        await update.message.reply_html(text, reply_markup=reply_markup)

    await _handle_spin_and_respond(reply_fn, st, n)

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

    tg_app.add_handler(CallbackQueryHandler(on_number_click))
    tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_digit_text))

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
