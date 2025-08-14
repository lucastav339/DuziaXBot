# main.py
import os
import asyncio
import logging
from typing import Any, Dict, Tuple, List, Optional
from collections import deque

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, CallbackQueryHandler, filters
)

# =========================
# Config & Logging
# =========================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("roulette-bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "changeme")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN or not PUBLIC_URL or not WEBHOOK_SECRET:
    log.warning("Ambiente incompleto: BOT_TOKEN, PUBLIC_URL e WEBHOOK_SECRET são obrigatórios.")

# =========================
# FastAPI app
# =========================
app = FastAPI(title="Roulette Signals Bot", version="1.2.0")
ptb_app: Optional[Application] = None

# =========================
# Regras/Utilidades
# =========================
DOZENS = {
    "D1": set(range(1, 13)),
    "D2": set(range(13, 25)),
    "D3": set(range(25, 37)),
}

def dozen_of(n: int) -> Optional[str]:
    if 1 <= n <= 12:
        return "D1"
    if 13 <= n <= 24:
        return "D2"
    if 25 <= n <= 36:
        return "D3"
    return None  # zero ou inválido

def make_default_state(window_max: int = 150) -> Dict[str, Any]:
    return {
        "history": deque(maxlen=window_max),
        "counts": [0]*37,
        "total_spins": 0,
        "gale_active": False,
        "gale_level": 0,
        "last_recommendation": ("D1","D2","D3"),
        "last_reason": "",
        "window_max": window_max,
        # Estatísticas
        "bets": 0,
        "wins": 0,
        "losses": 0,
        "win_streak": 0,
        "pending_bet": None,         # {"d1": "D1", "d2": "D2"} aguardando próximo giro
        # Correção
        "awaiting_correction": False,
        "last_input": None,          # último número recebido
        "last_closure": {            # snapshot do fechamento que ocorreu no último giro
            "had": False,            # True se houve fechamento de aposta
            "was_win": False,        # True se foi acerto
            "prev_pending": None,    # pending_bet antes de fechar
            "prev_streak": 0,        # streak antes do fechamento
        },
    }

def recompute_counts_from_history(state: Dict[str, Any]) -> None:
    counts = [0]*37
    total = 0
    for x in state["history"]:
        if 0 <= x <= 36:
            counts[x] += 1
            total += 1
    state["counts"] = counts
    state["total_spins"] = total

def update_counts(state: Dict[str, Any], _new_number: int) -> None:
    # Recalcula da janela (robusto e simples)
    recompute_counts_from_history(state)

def chi_square_bias(counts: List[int], total: int) -> Tuple[float, float]:
    if total <= 0:
        return 0.0, 1.0
    exp = total / 37.0
    chi2 = 0.0
    for c in counts:
        chi2 += (c - exp) ** 2 / exp
    import math
    df = 36.0
    if chi2 <= 0:
        return 0.0, 1.0
    z = ((chi2/df)**(1.0/3.0) - (1 - 2/(9*df))) / math.sqrt(2/(9*df))
    p = 1 - 0.5*(1 + math.erf(z / math.sqrt(2)))
    p = max(0.0, min(1.0, p))
    return chi2, p

def find_hottest_sector(counts: List[int], window_len: int = 12) -> List[int]:
    n = 37
    if window_len >= n:
        return list(range(n))
    extended = counts + counts[:window_len-1]
    max_sum = sum(extended[:window_len])
    best_start = 0
    cur_sum = max_sum
    for i in range(1, n):
        cur_sum += extended[i+window_len-1] - extended[i-1]
        if cur_sum > max_sum:
            max_sum = cur_sum
            best_start = i
    return [(best_start + j) % n for j in range(window_len)]

def sector_to_two_dozens(sector: List[int]) -> Tuple[str, str, str]:
    hits = {"D1":0,"D2":0,"D3":0}
    for p in sector:
        d = dozen_of(p)
        if d:
            hits[d] += 1
    ordered = sorted(hits.items(), key=lambda kv:(-kv[1], kv[0]))
    d1, d2 = ordered[0][0], ordered[1][0]
    excl = ({'D1','D2','D3'} - {d1,d2}).pop()
    return d1, d2, excl

def last_k_dozens(state: Dict[str, Any], k: int) -> List[str]:
    seq = list(state["history"])[-k:]
    return [dozen_of(x) for x in seq if 0 <= x <= 36 and dozen_of(x) is not None]

def quick_edge_two_dozens(
    state: Dict[str, Any],
    k: int = 12,
    need: int = 7
) -> Tuple[bool, Tuple[str,str,str], str]:
    dzs = last_k_dozens(state, k)
    if len(dzs) < k:
        return (False, ("D1","D2","D3"), "curto-prazo: janela insuficiente")
    c = {"D1":0, "D2":0, "D3":0}
    for d in dzs:
        c[d] += 1
    ordered = sorted(c.items(), key=lambda kv:(-kv[1], kv[0]))
    d1, d2 = ordered[0][0], ordered[1][0]
    excl = ({'D1','D2','D3'} - {d1,d2}).pop()
    if c[d1] + c[d2] >= need:
        return (True, (d1,d2,excl), f"curto-prazo: {c[d1]}+{c[d2]} em {k}")
    return (False, ("D1","D2","D3"), f"curto-prazo insuficiente: {c[d1]}+{c[d2]}<{need}")

def should_enter_book_style(
    state: Dict[str, Any],
    min_spins: int,
    p_threshold: float
) -> Tuple[bool, str, Tuple[str,str,str]]:
    total = state.get("total_spins", 0)
    if total < min_spins:
        return (False, f"amostra insuficiente ({total}/{min_spins})", ("D1","D2","D3"))
    _, p = chi_square_bias(state["counts"], total)
    if p > p_threshold:
        return (False, f"sem viés detectável (p≈{p:.3f})", ("D1","D2","D3"))
    sector = find_hottest_sector(state["counts"], window_len=12)
    d1, d2, excl = sector_to_two_dozens(sector)
    return (True, "viés detectado", (d1,d2,excl))

def stats_text(state: Dict[str, Any]) -> str:
    b = state.get("bets", 0)
    w = state.get("wins", 0)
    l = state.get("losses", 0)
    rate = (w / b * 100) if b else 0.0
    streak = state.get("win_streak", 0)
    return (
        f"📊 Estatísticas\n"
        f"✅ Acertos: {w}\n"
        f"❌ Erros: {l}\n"
        f"📈 Taxa: {rate:.1f}%  (em {b} apostas)\n"
        f"🔥 Sequência de vitórias: {streak}"
    )

def format_reco_text(d1: str, d2: str, excl: str, reason: str, mode: str, params: Dict[str, Any]) -> str:
    return (
        f"🎬 **ENTRAR**\n"
        f"🎯 Recomendação: **{d1} + {d2}**  |  🚫 Excluída: {excl}\n"
        f"📖 Critério: {reason}\n"
        f"⚙️ Modo: {mode}  | p≤{params['P_THRESHOLD']}  | K={params['K']} NEED={params['NEED']}  | janela={params['WINDOW']}\n"
        f"ℹ️ Envie o próximo número."
    )

def format_wait_text(reason: str, mode: str, params: Dict[str, Any]) -> str:
    return (
        f"⏳ **Aguardar**\n"
        f"📖 Critério: {reason}\n"
        f"⚙️ Modo: {mode}  | p≤{params['P_THRESHOLD']}  | K={params['K']} NEED={params['NEED']}  | janela={params['WINDOW']}\n"
        f"ℹ️ Envie o próximo número."
    )

def entry_keyboard() -> InlineKeyboardMarkup:
    # Botões somente quando HÁ ENTRADA
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✏️ Corrigir último", callback_data="fix_last"),
            InlineKeyboardButton("🗑️ Reset histórico", callback_data="reset_hist"),
        ]]
    )

# =========================
# Handlers
# =========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_chat_state(update, context)
    mode = context.bot_data.get("MODE", "conservador")
    text = (
        "🤖 Bot de sinais para **duas dúzias** (roleta europeia).\n"
        "Use /modo agressivo ou /modo conservador.\n"
        "Envie números (0–36) a cada giro."
    )
    await update.message.reply_text(text + f"\nModo atual: {mode}")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Comandos:\n"
        "/start – iniciar\n"
        "/modo agressivo|conservador – perfil de entradas\n"
        "/status – ver parâmetros e última recomendação\n"
        "Envie números (0–36) como mensagens."
    )

async def modo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        cur = context.bot_data.get("MODE", "conservador")
        await update.message.reply_text(
            f"Modo atual: {cur}\nUse: /modo agressivo  ou  /modo conservador"
        )
        return

    arg = context.args[0].lower().strip()
    if arg in ("agressivo", "agro"):
        context.bot_data["MODE"] = "agressivo"
        context.bot_data["MIN_SPINS"] = 8
        context.bot_data["P_THRESHOLD"] = 0.15
        context.bot_data["WINDOW"] = 120
        context.bot_data["K"] = 10
        context.bot_data["NEED"] = 6
        msg = "✅ Modo agressivo ativado: entradas mais frequentes."
    elif arg in ("conservador", "safe"):
        context.bot_data["MODE"] = "conservador"
        context.bot_data["MIN_SPINS"] = 25
        context.bot_data["P_THRESHOLD"] = 0.05
        context.bot_data["WINDOW"] = 200
        context.bot_data["K"] = 14
        context.bot_data["NEED"] = 9
        msg = "✅ Modo conservador ativado: entradas mais seletivas."
    else:
        msg = "Use: /modo agressivo  ou  /modo conservador"
    await update.message.reply_text(msg)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_chat_state(update, context)
    s = context.chat_data["state"]
    mode = context.bot_data.get("MODE","conservador")
    params = {
        "P_THRESHOLD": context.bot_data.get("P_THRESHOLD", 0.10),
        "K": context.bot_data.get("K", 12),
        "NEED": context.bot_data.get("NEED", 7),
        "WINDOW": s["window_max"]
    }
    d1, d2, excl = s.get("last_recommendation", ("D1","D2","D3"))
    reason = s.get("last_reason","—")
    msg = format_reco_text(d1, d2, excl, reason, mode, params) + "\n" + stats_text(s)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def number_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_chat_state(update, context)
    s = context.chat_data["state"]

    text = (update.message.text or "").strip()
    if not text.isdigit():
        return
    n = int(text)
    if n < 0 or n > 36:
        await update.message.reply_text("Envie números entre 0 e 36.")
        return

    # Correção: se aguardando número de correção, substitui o último
    if s.get("awaiting_correction", False):
        if len(s["history"]) == 0 or s["last_input"] is None:
            s["awaiting_correction"] = False
            await update.message.reply_text("Nada para corrigir no momento.")
            return

        old = s["last_input"]
        # Remove último número do histórico
        if len(s["history"]) > 0:
            s["history"].pop()

        # Reverte estatísticas do fechamento anterior e re-aplica sobre o novo valor
        lc = s.get("last_closure", {"had": False})
        if lc.get("had", False):
            # desfaz o fechamento anterior
            if s["bets"] > 0:
                s["bets"] -= 1
            if lc.get("was_win", False):
                if s["wins"] > 0:
                    s["wins"] -= 1
                # ao desfazer uma vitória, volta o streak ao valor anterior salvo
                s["win_streak"] = lc.get("prev_streak", 0)
            else:
                if s["losses"] > 0:
                    s["losses"] -= 1
                # ao desfazer uma derrota, o streak anterior era o salvo
                s["win_streak"] = lc.get("prev_streak", 0)

            # aplica o fechamento com o número corrigido
            prev_pending = lc.get("prev_pending")
            dz_new = dozen_of(n)
            s["bets"] += 1
            if prev_pending and dz_new in (prev_pending["d1"], prev_pending["d2"]):
                s["wins"] += 1
                s["win_streak"] = lc.get("prev_streak", 0) + 1
            else:
                s["losses"] += 1
                s["win_streak"] = 0

        # Adiciona o número corrigido e reconta
        s["history"].append(n)
        recompute_counts_from_history(s)
        s["last_input"] = n
        s["awaiting_correction"] = False

        await update.message.reply_text(
            f"✔️ Corrigido: {old} → {n}\n" + stats_text(s)
        )
        return

    # --- FECHAMENTO DE APOSTA PENDENTE (resultado do giro anterior) ---
    prev_pending = s["pending_bet"]
    if s.get("pending_bet"):
        d1 = s["pending_bet"]["d1"]; d2 = s["pending_bet"]["d2"]
        dz = dozen_of(n)
        s["bets"] += 1
        was_win = (dz in (d1, d2))
        # salva snapshot para possível correção
        s["last_closure"] = {
            "had": True,
            "was_win": was_win,
            "prev_pending": prev_pending,
            "prev_streak": s.get("win_streak", 0),
        }
        if was_win:
            s["wins"] += 1
            s["win_streak"] = s.get("win_streak", 0) + 1
            result_text = f"✅ Acertou ({n}{'' if dz is None else f' em {dz}'})."
        else:
            s["losses"] += 1
            s["win_streak"] = 0
            result_text = f"❌ Errou ({n}{'' if dz is None else f' em {dz}'})."
        s["pending_bet"] = None
        s["gale_active"] = False
        s["gale_level"] = 0
        await update.message.reply_text(result_text + "\n" + stats_text(s))
    else:
        # não houve fechamento nesse giro
        s["last_closure"] = {"had": False, "was_win": False, "prev_pending": None, "prev_streak": s.get("win_streak", 0)}

    # 1) Empilha no histórico (janela deslizante)
    s["history"].append(n)
    # 2) Recalcula contagens dentro da janela
    update_counts(s, n)
    s["last_input"] = n

    # 3) Parâmetros de decisão
    MIN_SPINS = context.bot_data.get("MIN_SPINS", 15)
    P_THRESHOLD = context.bot_data.get("P_THRESHOLD", 0.10)
    K = context.bot_data.get("K", 12)
    NEED = context.bot_data.get("NEED", 7)

    # 4) Gate “estilo livros” (qui-quadrado + setor quente)
    enter, reason, rec = should_enter_book_style(s, MIN_SPINS, P_THRESHOLD)

    # 5) Fallback curto-prazo (rápido) se livros não acionou
    if not enter:
        q_ok, q_rec, q_reason = quick_edge_two_dozens(s, k=K, need=NEED)
        if q_ok:
            enter, reason, rec = True, q_reason, q_rec

    d1, d2, excl = rec
    s["last_recommendation"] = rec
    s["last_reason"] = reason

    mode = context.bot_data.get("MODE","conservador")
    params = {"P_THRESHOLD": P_THRESHOLD, "K": K, "NEED": NEED, "WINDOW": s["window_max"]}

    if enter:
        # SOMENTE AQUI mostramos os botões
        await update.message.reply_text(
            format_reco_text(d1, d2, excl, reason, mode, params),
            parse_mode="Markdown",
            reply_markup=entry_keyboard()
        )
        s["pending_bet"] = {"d1": d1, "d2": d2}
        s["gale_active"] = True
        s["gale_level"] = 0
    else:
        await update.message.reply_text(
            format_wait_text(reason, mode, params),
            parse_mode="Markdown"
        )

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback dos botões inline (somente exibidos na mensagem de ENTRAR)."""
    await ensure_chat_state(update, context)
    s = context.chat_data["state"]
    query = update.callback_query
    data = query.data

    if data == "fix_last":
        if len(s["history"]) == 0 or s["last_input"] is None:
            await query.answer("Nada para corrigir.")
            await query.edit_message_reply_markup()  # remove teclados antigos
            return
        s["awaiting_correction"] = True
        await query.answer()
        await query.message.reply_text(
            f"✏️ Envie o número correto para substituir o último: {s['last_input']}"
        )
        await query.edit_message_reply_markup()  # remove teclados da msg antiga
    elif data == "reset_hist":
        win = context.bot_data.get("WINDOW", s.get("window_max", 150))
        context.chat_data["state"] = make_default_state(window_max=win)
        await query.answer("Histórico resetado.")
        await query.message.reply_text("🗑️ Histórico e estatísticas foram resetados.")
        await query.edit_message_reply_markup()  # remove teclados da msg antiga
    else:
        await query.answer()

async def ensure_chat_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "state" not in context.chat_data:
        win = context.bot_data.get("WINDOW", 150)
        context.chat_data["state"] = make_default_state(window_max=win)
    else:
        s = context.chat_data["state"]
        win = context.bot_data.get("WINDOW", s.get("window_max", 150))
        if s.get("window_max") != win:
            old_hist = list(s["history"])
            s["history"] = deque(old_hist, maxlen=win)
            s["window_max"] = win
            recompute_counts_from_history(s)

# =========================
# PTB + Webhook bootstrap
# =========================
async def on_startup() -> None:
    global ptb_app
    if ptb_app is None:
        ptb_app = (
            ApplicationBuilder()
            .token(BOT_TOKEN)
            .concurrent_updates(True)
            .build()
        )

        # Defaults: modo CONSERVADOR visível
        ptb_app.bot_data["MODE"] = "conservador"
        ptb_app.bot_data["MIN_SPINS"] = 25
        ptb_app.bot_data["P_THRESHOLD"] = 0.05
        ptb_app.bot_data["WINDOW"] = 200
        ptb_app.bot_data["K"] = 14
        ptb_app.bot_data["NEED"] = 9

        # Handlers
        ptb_app.add_handler(CommandHandler("start", start_cmd))
        ptb_app.add_handler(CommandHandler("help", help_cmd))
        ptb_app.add_handler(CommandHandler("modo", modo_cmd))
        ptb_app.add_handler(CommandHandler("status", status_cmd))
        ptb_app.add_handler(CallbackQueryHandler(cb_handler))
        ptb_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), number_message))

        # Define webhook
        webhook_url = f"{PUBLIC_URL}/telegram/webhook"
        await ptb_app.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES
        )
        asyncio.create_task(ptb_app.initialize())
        asyncio.create_task(ptb_app.start())
        log.info("PTB inicializado e webhook configurado em %s", webhook_url)

@app.on_event("startup")
async def _startup():
    await on_startup()

@app.on_event("shutdown")
async def _shutdown():
    global ptb_app
    if ptb_app:
        await ptb_app.stop()
        await ptb_app.shutdown()
        log.info("PTB finalizado.")

@app.get("/health")
async def health():
    return {"ok": True}

@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(None)
):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="invalid secret")
    data = await request.json()
    try:
        update = Update.de_json(data, ptb_app.bot)
        await ptb_app.process_update(update)
    except Exception as e:
        log.exception("Erro processando update: %s", e)
    return JSONResponse({"ok": True})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
