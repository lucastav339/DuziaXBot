# main.py
import os
import asyncio
import logging
import random
from typing import Any, Dict, Tuple, List, Optional
from collections import deque

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
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
app = FastAPI(title="Roulette Signals Bot", version="1.9.2-ia")
ptb_app: Optional[Application] = None

# =========================
# Simulação de IA (somente efeito 'digitando…', sem moldura)
# =========================
IA_MODE = True          # liga/desliga a simulação de IA
IA_ADD_HEADER = False   # mantido por compatibilidade; permanece False (sem moldura)

async def ia_typing(update: Update, context: ContextTypes.DEFAULT_TYPE, min_delay=0.35, max_delay=0.8):
    """Simula IA 'pensando' antes de responder (modo discreto, sem alterar visual)."""
    if not IA_MODE:
        return
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
        await asyncio.sleep(random.uniform(min_delay, max_delay))
    except Exception:
        pass

def ia_wrap(text: str) -> str:
    """Sem moldura: retorna o texto intacto."""
    return text

async def ia_send(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, **kwargs):
    """Substituto de reply_text com 'digitando…' (sem moldura)."""
    await ia_typing(update, context)
    return await update.message.reply_text(ia_wrap(text), **kwargs)

# =========================
# “Justificativas” aleatórias (entrada/erro) sem repetição consecutiva
# =========================
JUSTIFICATIVAS_ENTRADA = [
    "Padrão de repetição detectado nas últimas ocorrências, favorecendo essa combinação.",
    "Tendência estatística reforçada pela dominância recente das dúzias selecionadas.",
    "Correlação positiva identificada entre as últimas rodadas e a recomendação atual.",
    "Sequência anterior indica probabilidade elevada de ocorrência desta configuração.",
    "Análise de frequência sugere vantagem temporária para as dúzias indicadas.",
    "Histórico recente mostra viés consistente a favor dessa seleção.",
    "O modelo detectou um alinhamento favorável no padrão das últimas jogadas.",
    "Alta taxa de convergência nas últimas ocorrências reforça a entrada sugerida.",
    "A leitura de tendência estável aumenta a confiança nesta recomendação.",
    "O desvio padrão recente indica consistência na repetição dessa configuração."
]

JUSTIFICATIVAS_ERRO = [
    "A variação inesperada na distribuição quebrou o padrão observado nas últimas rodadas.",
    "O desvio foi atípico e rompeu a correlação estatística das dúzias.",
    "O ruído aleatório aumentou devido a uma sequência improvável de resultados.",
    "Houve uma anomalia estatística fora do intervalo de confiança.",
    "O padrão estava correto, mas ocorreu um evento de baixa probabilidade.",
    "A tendência detectada foi interrompida por um número isolado fora da sequência.",
    "O modelo indicou viés, porém o giro resultou em um outlier.",
    "A previsão foi afetada por um pico de variabilidade momentânea.",
    "O comportamento da mesa mudou abruptamente, quebrando a sequência monitorada.",
    "O cálculo foi consistente, mas o resultado destoou do esperado."
]

def pick_no_repeat(pool: List[str], last_idx: int) -> Tuple[str, int]:
    if not pool:
        return "", -1
    if len(pool) == 1:
        return pool[0], 0
    idx = last_idx
    while idx == last_idx:
        idx = random.randrange(len(pool))
    return pool[idx], idx

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
            "had": False,
            "was_win": False,
            "prev_pending": None,
            "prev_streak": 0,
        },
        # Base da última ENTRADA (para justificar GALE no erro)
        "last_entry_basis": {"kind": None},  # "book" | "quick" | None
        # Anti-repetição de justificativas
        "last_just_entry_idx": -1,
        "last_just_error_idx": -1,
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
) -> Tuple[bool, Tuple[str,str,str], str, Dict[str,int]]:
    dzs = last_k_dozens(state, k)
    if len(dzs) < k:
        return (False, ("D1","D2","D3"), "curto-prazo: janela insuficiente", {"D1":0,"D2":0,"D3":0})
    c = {"D1":0, "D2":0, "D3":0}
    for d in dzs:
        c[d] += 1
    ordered = sorted(c.items(), key=lambda kv:(-kv[1], kv[0]))
    d1, d2 = ordered[0][0], ordered[1][0]
    excl = ({'D1','D2','D3'} - {d1,d2}).pop()
    if c[d1] + c[d2] >= need:
        return (True, (d1,d2,excl), f"curto-prazo: {c[d1]}+{c[d2]} em {k}", c)
    return (False, ("D1","D2","D3"), f"curto-prazo insuficiente: {c[d1]}+{c[d2]}<{need}", c)

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

# ---- Mensagens / Teclados ----
def format_reco_text(d1: str, d2: str, mode: str) -> str:
    return (
        f"🎬 **ENTRAR**\n"
        f"🎯 Recomendação: **{d1} + {d2}**\n"
        f"🧩 Modo Ativado: {mode}\n"
        f"ℹ️ Envie o próximo número."
    )

def format_wait_text(mode: str) -> str:
    return (
        f"⏳ **Aguardar**\n"
        f"🧩 Modo Ativado: {mode}\n"
        f"ℹ️ Envie o próximo número."
    )

def gale_justification_text(state: Dict[str, Any], d1: str, d2: str) -> str:
    basis = state.get("last_entry_basis", {"kind": None})
    kind = basis.get("kind")
    if kind == "quick":
        k = basis.get("k", 12)
        need = basis.get("need", 7)
        counts = state.get("last_entry_basis", {}).get("counts", {"D1":0, "D2":0, "D3":0})
        c1 = counts.get(d1, 0); c2 = counts.get(d2, 0)
        return (
            f"🛠️ **GALE Nível 1 sugerido** nas mesmas dúzias **{d1} + {d2}**.\n"
            f"🧾 Justificativa: no curto prazo, essas duas dúzias somam {c1+c2} ocorrências nos últimos {k} giros "
            f"(limiar {need}). O erro pode ser variância; repetir **uma vez** é coerente."
        )
    elif kind == "book":
        return (
            f"🛠️ **GALE Nível 1 sugerido** nas mesmas dúzias **{d1} + {d2}**.\n"
            f"🧾 Justificativa: o padrão de setor ainda é dominante na janela recente; "
            f"um erro isolado não invalida o viés. Repetir **uma vez** mantém a coerência do modelo."
        )
    else:
        return (
            f"🛠️ **GALE Nível 1 sugerido** nas mesmas dúzias **{d1} + {d2}**.\n"
            f"🧾 Justificativa: vantagem local recente; repetir **uma vez** reduz impacto da variância."
        )

def entry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✏️ Corrigir último", callback_data="fix_last"),
                InlineKeyboardButton("🗑️ Reset histórico", callback_data="reset_hist"),
            ],
            [
                InlineKeyboardButton("🎯 Modo agressivo", callback_data="set_agressivo"),
                InlineKeyboardButton("🛡️ Modo conservador", callback_data="set_conservador"),
            ]
        ]
    )

def mode_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🎯 Modo agressivo", callback_data="set_agressivo"),
            InlineKeyboardButton("🛡️ Modo conservador", callback_data="set_conservador"),
        ]]
    )

def prompt_next_number_text() -> str:
    return (
        "👉 Agora me diga o **número que acabou de sair** na roleta (0–36).\n"
        "Ex.: 17\n"
        "Dica: se enviar errado, quando aparecer uma **ENTRADA** você poderá tocar em **✏️ Corrigir último**."
    )

# =========================
# Handlers
# =========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mensagem de boas-vindas com identidade iDozen (IA) e mesmo design."""
    await ensure_chat_state(update, context)
    mode_raw = context.bot_data.get("MODE", "conservador")
    mode = "Agressivo" if mode_raw.lower().startswith("agress") else "Conservador"

    texto = (
        "🤖 **iDozen — Mestre das Dúzias**\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n"
    f"🎛️ **Modo Ativado:** _{mode}_\n"
    "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "📋 **Como começar:**\n"
    "1️⃣ **Escolha o modo de operação**\n"
    "  🎯 **Agressivo** ou 🛡️ **Conservador**\n"
    "2️⃣ **Envie o último número** que saiu na roleta (**0–36**)\n"
    "3️⃣ **Aguarde a análise** e receba a **recomendação** quando houver oportunidade\n\n"
    "💡 **Dica:** Se enviar um número incorreto, quando surgir uma **ENTRADA** use **✏️ Corrigir último** para ajustar.\n\n"
    "▶️ **Pronto?** Escolha o modo abaixo e informe o número que acabou de sair."
    )

    await ia_send(update, context, texto, reply_markup=mode_keyboard(), parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ia_send(
        update, context,
        "Comandos:\n"
        "/start – iniciar\n"
        "/modo agressivo|conservador – perfil de entradas\n"
        "/status – ver modo, última recomendação e estatísticas\n"
        "Envie números (0–36) como mensagens."
    )

async def modo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        cur = context.bot_data.get("MODE", "conservador")
        cur_pt = "Agressivo" if cur.lower().startswith("agress") else "Conservador"
        await ia_send(update, context, f"🧩 Modo Ativado: {cur_pt}\nUse: /modo agressivo  ou  /modo conservador")
        return

    arg = context.args[0].lower().strip()
    if arg in ("agressivo", "agro"):
        context.bot_data["MODE"] = "agressivo"
        context.bot_data["MIN_SPINS"] = 8
        context.bot_data["P_THRESHOLD"] = 0.15
        context.bot_data["WINDOW"] = 120
        context.bot_data["K"] = 10
        context.bot_data["NEED"] = 6
        msg = "✅ Modo agressivo ativado."
    elif arg in ("conservador", "safe"):
        context.bot_data["MODE"] = "conservador"
        context.bot_data["MIN_SPINS"] = 25
        context.bot_data["P_THRESHOLD"] = 0.05
        context.bot_data["WINDOW"] = 200
        context.bot_data["K"] = 14
        context.bot_data["NEED"] = 9
        msg = "✅ Modo conservador ativado."
    else:
        msg = "Use: /modo agressivo  ou  /modo conservador"
    await ia_send(update, context, msg)
    await ia_send(update, context, prompt_next_number_text())

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_chat_state(update, context)
    s = context.chat_data["state"]
    mode_raw = context.bot_data.get("MODE","conservador")
    mode = "Agressivo" if mode_raw.lower().startswith("agress") else "Conservador"
    d1, d2, _excl = s.get("last_recommendation", ("D1","D2","D3"))
    msg = (
        f"🧩 Modo Ativado: {mode}\n"
        f"Última recomendação: {d1} + {d2}\n"
        + stats_text(s)
    )
    await ia_send(update, context, msg, parse_mode="Markdown")

async def number_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_chat_state(update, context)
    s = context.chat_data["state"]

    text = (update.message.text or "").strip()
    if not text.isdigit():
        return
    n = int(text)
    if n < 0 or n > 36:
        await ia_send(update, context, "Envie números entre 0 e 36.")
        return

    # Correção: se aguardando número de correção, substitui o último
    if s.get("awaiting_correction", False):
        if len(s["history"]) == 0 or s["last_input"] is None:
            s["awaiting_correction"] = False
            await ia_send(update, context, "Nada para corrigir no momento.")
            return

        old = s["last_input"]
        if len(s["history"]) > 0:
            s["history"].pop()

        lc = s.get("last_closure", {"had": False})
        if lc.get("had", False):
            if s["bets"] > 0:
                s["bets"] -= 1
            if lc.get("was_win", False):
                if s["wins"] > 0:
                    s["wins"] -= 1
                s["win_streak"] = lc.get("prev_streak", 0)
            else:
                if s["losses"] > 0:
                    s["losses"] -= 1
                s["win_streak"] = lc.get("prev_streak", 0)

            prev_pending = lc.get("prev_pending")
            dz_new = dozen_of(n)
            s["bets"] += 1
            if prev_pending and dz_new in (prev_pending["d1"], prev_pending["d2"]):
                s["wins"] += 1
                s["win_streak"] = lc.get("prev_streak", 0) + 1
            else:
                s["losses"] += 1
                s["win_streak"] = 0

        s["history"].append(n)
        recompute_counts_from_history(s)
        s["last_input"] = n
        s["awaiting_correction"] = False

        await ia_send(update, context, f"✔️ Corrigido: {old} → {n}\n" + stats_text(s))
        return

    # --- FECHAMENTO DE APOSTA PENDENTE (resultado do giro anterior) ---
    prev_pending = s["pending_bet"]
    if s.get("pending_bet"):
        d1p = s["pending_bet"]["d1"]; d2p = s["pending_bet"]["d2"]
        dz = dozen_of(n)
        s["bets"] += 1
        was_win = (dz in (d1p, d2p))
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
            s["gale_active"] = False
            s["gale_level"] = 0
            await ia_send(update, context, result_text + "\n" + stats_text(s))
        else:
            s["losses"] += 1
            s["win_streak"] = 0
            result_text = f"❌ Errou ({n}{'' if dz is None else f' em {dz}'})."
            s["gale_active"] = True
            s["gale_level"] = 1
            await ia_send(update, context, result_text + "\n" + stats_text(s))
            # Desculpa técnica aleatória (sem repetir)
            txt, idx = pick_no_repeat(JUSTIFICATIVAS_ERRO, s.get("last_just_error_idx", -1))
            s["last_just_error_idx"] = idx
            await ia_send(update, context, f"📖 {txt}")
            # Orientação GALE com justificativa
            await ia_send(update, context, gale_justification_text(s, d1p, d2p))

        s["pending_bet"] = None
    else:
        s["last_closure"] = {"had": False, "was_win": False, "prev_pending": None, "prev_streak": s.get("win_streak", 0)}

    # 1) Empilha no histórico (janela deslizante)
    s["history"].append(n)
    # 2) Recalcula contagens
    update_counts(s, n)
    s["last_input"] = n

    # 3) Parâmetros de decisão
    MIN_SPINS = context.bot_data.get("MIN_SPINS", 15)
    P_THRESHOLD = context.bot_data.get("P_THRESHOLD", 0.10)
    K = context.bot_data.get("K", 12)
    NEED = context.bot_data.get("NEED", 7)

    # 4) Gate “estilo livros”
    enter, _reason, rec = should_enter_book_style(s, MIN_SPINS, P_THRESHOLD)
    entry_basis = {"kind": None}

    # 5) Fallback curto-prazo
    if not enter:
        q_ok, q_rec, _q_reason, counts = quick_edge_two_dozens(s, k=K, need=NEED)
        if q_ok:
            enter, rec = True, q_rec
            entry_basis = {"kind": "quick", "k": K, "need": NEED, "counts": counts}
    else:
        entry_basis = {"kind": "book"}

    d1, d2, _excl = rec
    s["last_recommendation"] = rec
    s["last_entry_basis"] = entry_basis

    mode_raw = context.bot_data.get("MODE","conservador")
    mode = "Agressivo" if mode_raw.lower().startswith("agress") else "Conservador"

    if enter:
        txt, idx = pick_no_repeat(JUSTIFICATIVAS_ENTRADA, s.get("last_just_entry_idx", -1))
        s["last_just_entry_idx"] = idx
        await ia_send(
            update, context,
            format_reco_text(d1, d2, mode) + f"\n📖 {txt}",
            parse_mode="Markdown",
            reply_markup=entry_keyboard()
        )
        s["pending_bet"] = {"d1": d1, "d2": d2}
        s["gale_active"] = True
        s["gale_level"] = 0
    else:
        await ia_send(update, context, format_wait_text(mode), parse_mode="Markdown")

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback dos botões inline (modos + corrigir/reset)."""
    await ensure_chat_state(update, context)
    s = context.chat_data["state"]
    query = update.callback_query
    data = query.data

    if data == "fix_last":
        if len(s["history"]) == 0 or s["last_input"] is None:
            await query.answer("Nada para corrigir.")
            await query.edit_message_reply_markup()
            await ia_typing(update, context, 0.25, 0.5)
            await query.message.reply_text("Nada para corrigir.")
            return
        s["awaiting_correction"] = True
        await query.answer()
        await query.edit_message_reply_markup()
        await ia_typing(update, context, 0.25, 0.5)
        await query.message.reply_text(f"✏️ Envie o número correto para substituir o último: {s['last_input']}")

    elif data == "reset_hist":
        win = context.bot_data.get("WINDOW", s.get("window_max", 150))
        context.chat_data["state"] = make_default_state(window_max=win)
        await query.answer("Histórico resetado.")
        await query.edit_message_reply_markup()
        await ia_typing(update, context, 0.25, 0.5)
        await query.message.reply_text("🗑️ Histórico e estatísticas foram resetados.")

    elif data == "set_agressivo":
        context.bot_data["MODE"] = "agressivo"
        context.bot_data["MIN_SPINS"] = 8
        context.bot_data["P_THRESHOLD"] = 0.15
        context.bot_data["WINDOW"] = 120
        context.bot_data["K"] = 10
        context.bot_data["NEED"] = 6
        await query.answer("Modo agressivo ativado.")
        await query.edit_message_reply_markup()
        await ia_typing(update, context, 0.3, 0.6)
        await query.message.reply_text("✅ Modo agressivo ativado.")
        await ia_typing(update, context, 0.2, 0.45)
        await query.message.reply_text(prompt_next_number_text())

    elif data == "set_conservador":
        context.bot_data["MODE"] = "conservador"
        context.bot_data["MIN_SPINS"] = 25
        context.bot_data["P_THRESHOLD"] = 0.05
        context.bot_data["WINDOW"] = 200
        context.bot_data["K"] = 14
        context.bot_data["NEED"] = 9
        await query.answer("Modo conservador ativado.")
        await query.edit_message_reply_markup()
        await ia_typing(update, context, 0.3, 0.6)
        await query.message.reply_text("✅ Modo conservador ativado.")
        await ia_typing(update, context, 0.2, 0.45)
        await query.message.reply_text(prompt_next_number_text())

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
# PTB + Webhook bootstrap (ordem correta: initialize → set_webhook → start)
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

        # Defaults: modo CONSERVADOR
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

        # ORDEM correta
        await ptb_app.initialize()
        webhook_url = f"{PUBLIC_URL}/telegram/webhook"
        await ptb_app.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES
        )
        await ptb_app.start()
        log.info("PTB inicializado e webhook configurado em %s", webhook_url)

def _log_routes():
    try:
        routes = [getattr(r, "path", str(r)) for r in app.routes]
        log.info("Rotas registradas: %s", routes)
    except Exception as e:
        log.warning("Não consegui listar rotas: %s", e)

@app.on_event("startup")
async def _startup():
    await on_startup()
    _log_routes()

@app.on_event("shutdown")
async def _shutdown():
    global ptb_app
    if ptb_app:
        try:
            await ptb_app.bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            pass
        await ptb_app.stop()
        await ptb_app.shutdown()
        log.info("PTB finalizado.")

# =========================
# Rotas HTTP (root/health/webhook)
# =========================
@app.get("/")
async def root():
    return {"ok": True, "service": "roulette-bot", "version": "1.9.2-ia"}

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
    log.info("Iniciando Uvicorn em 0.0.0.0:%s", PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
