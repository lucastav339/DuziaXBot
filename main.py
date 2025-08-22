# main.py — Webhook PTB 21.6 + aiohttp (Render)
# MODOS:
#  • Premium estatístico (padrão): chi-quadrado + burst
#  • Tendência curta (/tendencia): 2 seguidas repete; 4+ inverte; com Gale 1x
#  • Faixa (/faixa): após 4 giros consecutivos Altos/Baixos da MESMA COR, sugere 9 números (faixa+cor)
#
# UI: Histórico em grade fixa de bolinhas (não “anda”). Aceita botões e entrada numérica (0–36).
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

# ======= Pós-acerto =======
POSTWIN_SPINS = int(os.getenv("POSTWIN_SPINS", "5"))

# =========================
# Mapeamento cor dos números (Roleta Europeia)
# =========================
RED_SET = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
BLACK_SET = {2,4,6,8,10,11,13,15,17,20,22,24,26,28,29,31,33,35}

HIGH_SET = set(range(19, 37))   # 19-36
LOW_SET  = set(range(1, 19))    # 1-18

HIGH_RED  = sorted(list(HIGH_SET & RED_SET))   # 9 números
HIGH_BLACK= sorted(list(HIGH_SET & BLACK_SET)) # 9 números
LOW_RED   = sorted(list(LOW_SET  & RED_SET))   # 9 números
LOW_BLACK = sorted(list(LOW_SET  & BLACK_SET)) # 9 números

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
# - history: List[str] com {"R","B","Z"} — ilimitado
# - numbers: List[Optional[int]] — mantém números quando informados; None quando input for botão
# - mode: "premium" | "tendencia"
# - pending_signal: Optional["R"|"B"] (premium/tendência)
# - pending_stage: None | "base" | "gale"   (apenas no modo tendencia)
# - cooldown_left: int (premium)
# - postwin_wait_left: int  — contador pós-acerto (global)
# - faixa_enabled: bool — estratégia de faixa ativa
# - pending_bucket: Optional[Tuple["H"|"L","R"|"B"]] — pendência da estratégia /faixa
STATE: Dict[int, Dict[str, Any]] = {}

def _fresh_state() -> Dict[str, Any]:
    return {
        "jogadas": 0,
        "acertos": 0,
        "erros": 0,
        "history": [],
        "numbers": [],
        "mode": "premium",
        "pending_signal": None,
        "pending_stage": None,
        "cooldown_left": 0,
        "postwin_wait_left": 0,
        "faixa_enabled": False,
        "pending_bucket": None,
    }

def get_state(user_id: int) -> Dict[str, Any]:
    if user_id not in STATE:
        STATE[user_id] = _fresh_state()
    return STATE[user_id]

# =========================
# UI (teclado e helpers)
# =========================
CHOICES = ["🔴 Vermelho", "⚫ Preto", "🟢 Zero"]
KB = ReplyKeyboardMarkup([CHOICES, ["/tendencia", "/faixa", "/reset", "/estrategia"]], resize_keyboard=True)

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
    post = st.get("postwin_wait_left", 0)
    faixa_on = "Ativa" if st.get("faixa_enabled") else "Desativada"
    bucket = st.get("pending_bucket")
    bucket_txt = "—"
    if bucket:
        hilo, col = bucket
        cor = "🔴" if col == "R" else "⚫"
        faixa_name = "Altos" if hilo == "H" else "Baixos"
        bucket_txt = f"{faixa_name} {cor}"
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
        f"• 🧠 <b>Sinal pendente (cores):</b> {label_pend} ({stage})\n"
        f"• 🎯 <b>/faixa:</b> {faixa_on} • <b>Pendente:</b> {bucket_txt}\n"
        f"• ⏱️ <b>Cooldown:</b> {cool}\n"
        f"• ⌛ <b>Pós-acerto:</b> {post}/{POSTWIN_SPINS if post>0 else 0}"
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
def last_streak_color(history: List[str]) -> Optional[Tuple[str,int]]:
    i = len(history) - 1
    while i >= 0 and history[i] not in ("R","B"):
        i -= 1
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
# Lógica — /faixa (Altos/Baixos + mesma COR)
# =========================
def last_k_nonzero(numbers: List[Optional[int]], k: int) -> Optional[List[int]]:
    buf: List[int] = []
    for n in reversed(numbers):
        if n is None:  # entrada por botão não traz número → rompe
            break
        if n == 0:     # zero rompe a sequência
            break
        buf.append(n)
        if len(buf) == k:
            break
    if len(buf) < k:
        return None
    return list(reversed(buf))

def faixa_trigger(numbers: List[Optional[int]]) -> Optional[Tuple[str,str]]:
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

def evaluate_faixa_on_spin(st: Dict[str, Any], num: Optional[int]) -> str:
    """
    Avalia pendência da /faixa:
      - num ∈ conjunto sugerido → ACERTO (+ pós-acerto)
      - num == 0 → aguarda
      - num ∉ conjunto e num != 0 → ERRO
      - num == None (entrada via botão) → não avalia
    """
    msg = ""
    bucket = st.get("pending_bucket")
    if not bucket:
        return msg
    if num is None:
        return "📥 Número não informado (apenas cor). Aguardando número para avaliar /faixa."
    if num == 0:
        return "🏆 <b>/faixa:</b> 🟢 Zero — aguardando avaliação."
    hilo, col = bucket
    allowed = set(bucket_numbers(hilo, col))
    st["jogadas"] += 1
    if num in allowed:
        st["acertos"] += 1
        msg = "🏆 <b>/faixa:</b> ✅ Acerto no conjunto sugerido."
        st["pending_bucket"] = None
        # ativa pós-acerto global
        _arm_postwin(st)
    else:
        st["erros"] += 1
        msg = "🏆 <b>/faixa:</b> ❌ Erro no conjunto sugerido."
        st["pending_bucket"] = None
    return msg

# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE[uid] = _fresh_state()
    await update.message.reply_html(
        "🤖 <b>iDozen Premium — Análise de Cores</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Envie o <b>resultado</b> pelos botões ou digite o <b>número (0–36)</b>.\n"
        "Modos: <b>premium</b> (estatística), <b>tendencia</b> (curta c/ Gale 1x) e <b>/faixa</b> (Altos/Baixos + Cor).\n\n"
        "Comandos: <b>/tendencia</b> • <b>/faixa</b> • <b>/reset</b> • <b>/estrategia</b>",
        reply_markup=KB,
    )

async def estrategia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_user.id)
    faixa_on = "Ativa" if st.get("faixa_enabled") else "Desativada"
    if st["mode"] == "tendencia":
        mode_txt = (
            "🧭 <b>Modo: Tendência Curta</b>\n"
            "• 2 seguidas → apostar que repete.\n"
            "• 4+ seguidas → apostar na oposta.\n"
            "• <b>Gale 1x</b> (acerto no gale não conta erro da base).\n"
            "• 🟢 Zero quebra sequência.\n"
        )
    else:
        mode_txt = (
            "📚 <b>Modo: Premium (rápido)</b>\n"
            f"• Burst 12 últimas (ignora 🟢); se ≥9 mesma cor → sinal.\n"
            f"• Janela {WINDOW} (χ² ≥ {CHI2_CRIT_DF1} + gap ≥ {GAP_MIN}).\n"
            f"• Cooldown: {COOLDOWN_AFTER_EVAL} giros.\n"
        )
    faixa_txt = (
        f"🎯 <b>/faixa:</b> {faixa_on}\n"
        "• Gatilho: <b>4 números consecutivos</b> (sem zero), todos <b>Altos (19–36)</b> ou todos <b>Baixos (1–18)</b> e da <b>mesma cor</b>.\n"
        "• Sinal: apostar nos <b>9 números</b> da faixa+cor (ex.: Altos 🔴 → 19,21,23,25,27,30,32,34,36).\n"
        "• Avaliação: acerta se próximo número cair no conjunto; 🟢 zero não avalia.\n"
    )
    post_txt = "• Pós-acerto: coleta 5 giros e zera apenas o histórico.\n"
    await update.message.reply_html(mode_txt + "\n" + faixa_txt + post_txt, reply_markup=KB)

async def toggle_tendencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_user.id)
    if st["mode"] == "tendencia":
        st["mode"] = "premium"
        st["pending_signal"] = None
        st["pending_stage"] = None
        await update.message.reply_html("🧭 Modo alterado para <b>premium</b> (estatística).", reply_markup=KB)
    else:
        st["mode"] = "tendencia"
        st["pending_signal"] = None
        st["pending_stage"] = None
        await update.message.reply_html("🧭 Modo alterado para <b>tendência curta</b> (com Gale 1x).", reply_markup=KB)

async def toggle_faixa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_user.id)
    st["faixa_enabled"] = not st.get("faixa_enabled", False)
    if not st["faixa_enabled"]:
        st["pending_bucket"] = None
    state_txt = "Ativada ✅" if st["faixa_enabled"] else "Desativada ⛔"
    await update.message.reply_html(
        f"🎯 Estratégia <b>/faixa</b> {state_txt}.\n"
        "Regras: 4 números consecutivos Altos/Baixos da MESMA cor → aposta nos 9 números da faixa+cor.",
        reply_markup=KB
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    STATE[uid] = _fresh_state()
    await update.message.reply_html("♻️ <b>Histórico e placar resetados.</b>", reply_markup=KB)

# --------- Pós-acerto Helpers ----------
def _arm_postwin(st: Dict[str, Any]) -> None:
    st["pending_signal"] = None
    st["pending_stage"] = None
    st["pending_bucket"] = None
    st["cooldown_left"] = 0
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

# --------- Avaliação de sinais (cores) ----------
def evaluate_premium_on_spin(st: Dict[str, Any], obs: str) -> str:
    outcome_msg = ""
    if st["pending_signal"] in ("R","B"):
        if obs == st["pending_signal"]:
            st["jogadas"] += 1
            st["acertos"] += 1
            outcome_msg = "🏆 <b>Resultado:</b> ✅ Acerto no sinal anterior."
            _arm_postwin(st)
        elif obs in ("R","B"):
            st["jogadas"] += 1
            st["erros"] += 1
            outcome_msg = "🏆 <b>Resultado:</b> ❌ Erro no sinal anterior."
            st["pending_signal"] = None
            st["cooldown_left"] = COOLDOWN_AFTER_EVAL
        else:
            outcome_msg = "🏆 <b>Resultado:</b> 🟢 Zero — sinal não contabilizado."
            st["pending_signal"] = None
            st["cooldown_left"] = COOLDOWN_AFTER_EVAL
    return outcome_msg

def evaluate_trend_on_spin(st: Dict[str, Any], obs: str) -> str:
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
            _arm_postwin(st)
        else:
            st["pending_stage"] = "gale"
            msg = "🔁 <b>Gale 1x:</b> repetir a mesma cor no próximo giro."
    elif stage == "gale":
        st["jogadas"] += 1
        if obs == sig:
            st["acertos"] += 1
            msg = "🏆 <b>Resultado:</b> ✅ Acerto no GALE (sem erro contabilizado)."
            _arm_postwin(st)
        else:
            st["erros"] += 1
            msg = "🏆 <b>Resultado:</b> ❌ Erro no GALE."
            st["pending_signal"] = None
            st["pending_stage"] = None
    return msg

# --------- Entrada e Roteamento ----------
def parse_input_to_num_and_color(text: str) -> Tuple[Optional[int], Optional[str]]:
    """
    Retorna (numero, cor_abrev) onde:
      - numero ∈ [0..36] ou None (se entrada por botão)
      - cor_abrev ∈ {"R","B","Z"} (Z para zero) ou None se não identificado
    """
    t = text.strip()
    if t.isdigit():
        n = int(t)
        if 0 <= n <= 36:
            if n == 0:
                return (0, "Z")
            c = color_of(n)
            return (n, c if c else None)
    if t.startswith("🔴"):
        return (None, "R")
    if t.startswith("⚫"):
        return (None, "B")
    if t.startswith("🟢"):
        return (None, "Z")
    return (None, None)

async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = get_state(uid)
    text = (update.message.text or "").strip()

    # Aceita número 0–36 OU botões
    num, cor = parse_input_to_num_and_color(text)
    if num is None and text not in CHOICES:
        await update.message.reply_html(
            "Use os botões ou digite um <b>número de 0 a 36</b>.",
            reply_markup=KB
        )
        return

    # Mapear observação (obs) para histórico de cor
    if cor == "R":
        obs = "R"
    elif cor == "B":
        obs = "B"
    elif cor == "Z":
        obs = "Z"
    else:
        # caso tenha sido número válido mas não achou cor (não deve ocorrer), ignora
        obs = "Z" if num == 0 else None

    # 1) Atualiza históricos
    if obs is not None:
        st["history"].append(obs)
    st["numbers"].append(num)  # pode ser None quando for botão

    # 2) Avaliação de pendências: ordem de prioridade
    hist_grid = render_history_grid(st["history"])
    outcome_msgs: List[str] = []

    # 2a) /faixa pendente?
    if st.get("pending_bucket"):
        msg = evaluate_faixa_on_spin(st, num)
        if msg:
            outcome_msgs.append(msg)

    # 2b) Modos de cor
    if st["mode"] == "tendencia":
        m = evaluate_trend_on_spin(st, obs if obs else "Z")
        if m:
            outcome_msgs.append(m)
    else:
        if st["cooldown_left"] > 0:
            st["cooldown_left"] -= 1
        m = evaluate_premium_on_spin(st, obs if obs else "Z")
        if m:
            outcome_msgs.append(m)

    # 2c) Pós-acerto ativo?
    post_msg = _tick_postwin_and_maybe_reset(st)
    if post_msg:
        base_msg = ("\n".join(outcome_msgs)) if outcome_msgs else "📥 Resultado registrado."
        extra = post_msg + "\n\n" + "🧩 <b>Histórico (grade fixa):</b>\n" + render_history_grid(st["history"])
        await update.message.reply_html(
            f"{base_msg}\n\n{extra}\n\n{pretty_status(st)}",
            reply_markup=KB,
        )
        return

    # 3) Geração de novos sinais (somente se não houver pendências e não estiver em pós-acerto)
    recommend_blocks: List[str] = []
    any_pending = (st.get("pending_signal") in ("R","B")) or (st.get("pending_stage") in ("base","gale")) or (st.get("pending_bucket") is not None)
    if not any_pending and st.get("postwin_wait_left", 0) == 0:
        # 3a) /faixa (se ativa e há número válido novo)
        if st.get("faixa_enabled") and num is not None:
            trig = faixa_trigger(st["numbers"])
            if trig:
                hilo, col = trig
                st["pending_bucket"] = (hilo, col)
                faixa_name = "Altos" if hilo == "H" else "Baixos"
                cor_txt = "🔴 Vermelho" if col == "R" else "⚫ Preto"
                nums = bucket_numbers(hilo, col)
                recommend_blocks.append(
                    "🎯 <b>Sinal — /faixa</b>\n"
                    f"• Faixa: <b>{faixa_name}</b> • Cor: <b>{cor_txt}</b>\n"
                    f"• Números: <code>{', '.join(map(str, nums))}</code>\n"
                    "👉 Envie o próximo resultado para avaliar."
                )

        # 3b) Cores (somente se /faixa não gerou algo agora)
        if not recommend_blocks and obs in ("R","B"):
            if st["mode"] == "tendencia":
                sig = decide_signal_trend(st["history"])
                if sig:
                    st["pending_signal"] = sig
                    st["pending_stage"] = "base"
                    cor_txt = "🔴 Vermelho" if sig == "R" else "⚫ Preto"
                    recommend_blocks.append(
                        "🎯 <b>Sinal — Tendência Curta</b>\n"
                        f"• Apostar em: <b>{cor_txt}</b>\n"
                        "• Regras: 2 seguidas repete; 4+ inverte.\n"
                        "• <b>Gale 1x</b> habilitado (se base errar).\n"
                        "👉 Envie o próximo resultado para avaliar."
                    )
            else:
                if st["cooldown_left"] <= 0:
                    sig = decide_signal_premium(st["history"])
                    if sig:
                        st["pending_signal"] = sig
                        cor_txt = "🔴 Vermelho" if sig == "R" else "⚫ Preto"
                        recommend_blocks.append(
                            "🎯 <b>Recomendação Premium</b>\n"
                            f"• Apostar em: <b>{cor_txt}</b>\n"
                            f"• Motivo: <i>viés recente (burst) ou χ² ≥ {CHI2_CRIT_DF1} + gap ≥ {GAP_MIN}</i>.\n"
                            "👉 Envie o próximo resultado para avaliar."
                        )

    # 4) Resposta
    base_msg = ("\n".join(outcome_msgs)) if outcome_msgs else "📥 Resultado registrado."
    extra = "\n\n".join(recommend_blocks) if recommend_blocks else "🧩 <b>Histórico (grade fixa):</b>\n" + hist_grid
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
    tg_app.add_handler(CommandHandler("tendencia", toggle_tendencia))
    tg_app.add_handler(CommandHandler("faixa", toggle_faixa))          # NOVO
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
