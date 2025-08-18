import os
import math
import time
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes,
    CallbackQueryHandler
)

# =========================
# ------- ESTADO ----------
# =========================

@dataclass
class UserState:
    history: List[str] = field(default_factory=list)     # 'R','B','0'
    bets_made: int = 0
    hits: int = 0
    misses: int = 0
    mode: str = "Conservador"  # ou "Agressivo"
    last_reco: Optional[str] = None  # 'R'/'B'
    # Para não contar acerto/erro enquanto aguardava outro dado (caso ajuste)
    awaiting: bool = False

STATE: Dict[int, UserState] = {}  # simple in-memory; para produção multi-instância use Redis

# =========================
# ---- BOTÕES / UI --------
# =========================

def main_keyboard(state: UserState) -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton("🔴 Vermelho", callback_data="add_R"),
        InlineKeyboardButton("⚫ Preto", callback_data="add_B"),
        InlineKeyboardButton("🟢 Zero", callback_data="add_0"),
    ]
    row2 = [
        InlineKeyboardButton("↩️ Corrigir último", callback_data="undo"),
        InlineKeyboardButton("📊 Status", callback_data="status"),
    ]
    row3 = [
        InlineKeyboardButton("♻️ Resetar", callback_data="reset"),
        InlineKeyboardButton(f"⚙️ Modo: {state.mode}", callback_data="toggle_mode"),
    ]
    return InlineKeyboardMarkup([row1, row2, row3])

# =========================
# ---- ESTRATÉGIAS --------
# =========================

def recent(seq: List[str], n: int) -> List[str]:
    return seq[-n:] if len(seq) >= n else seq[:]

def count_R_B(seq: List[str]) -> Tuple[int, int]:
    r = sum(1 for s in seq if s == 'R')
    b = sum(1 for s in seq if s == 'B')
    return r, b

def chi_square_rb(seq: List[str], window: int = 36) -> Tuple[float, float]:
    """
    Chi-quadrado simples para diferença R vs B numa janela.
    Retorna (chi2, p_approx). p_approx é uma aproximação (df=1).
    Ignora '0'.
    """
    sub = [s for s in recent(seq, window) if s in ('R','B')]
    n = len(sub)
    if n == 0:
        return 0.0, 1.0
    r, b = count_R_B(sub)
    expected = n / 2.0
    chi2 = 0.0
    if expected > 0:
        chi2 = ((r - expected) ** 2) / expected + ((b - expected) ** 2) / expected
    # Aproximação p-value para df=1 usando Q ≈ exp(-chi2/2)*sqrt(1+chi2)
    # (não exato, mas suficiente como heurística recreativa)
    p_approx = math.exp(-chi2 / 2) * math.sqrt(1 + chi2)
    return chi2, p_approx

def run_trigger(seq: List[str]) -> Optional[str]:
    """
    Detecta runs na última janela de 4:
    Se 3+ da mesma cor nos últimos 4 -> retorna 'R' ou 'B' daquela run.
    Ignora '0'.
    """
    sub = [s for s in recent(seq, 4) if s in ('R','B')]
    if len(sub) < 3:  # precisa de base
        return None
    r, b = count_R_B(sub)
    if r >= 3:
        return 'R'
    if b >= 3:
        return 'B'
    return None

def recommend(state: UserState) -> Tuple[Optional[str], str]:
    """
    Gera recomendação 'R'/'B' e uma justificativa curta.
    Regras:
      1) Run trigger (peso alto)
      2) Chi-square (p<0,05) na janela 36
      3) Caso nada: "Aguardar"
    Modo Conservador = reverter; Agressivo = continuar.
    """
    hist = state.history[:]

    # Se último é zero, sugerir aguardar (reinicia leitura curta)
    if hist and hist[-1] == '0':
        return None, "🟢 Zero recente — reiniciando leitura curta. Aguarde mais dados."

    # 1) Runs
    run_side = run_trigger(hist)  # 'R' ou 'B' ou None
    if run_side:
        if state.mode == "Conservador":
            side = 'B' if run_side == 'R' else 'R'
            txt = f"Run {run_side} detectada (≥3/4). Modo Conservador → reversão em {fmt_color(side)}."
        else:
            side = run_side
            txt = f"Run {run_side} detectada (≥3/4). Modo Agressivo → continuidade em {fmt_color(side)}."
        return side, txt

    # 2) Desvio estatístico (janela 36)
    chi2, p = chi_square_rb(hist, window=36)
    if p < 0.05:
        r, b = count_R_B([s for s in recent(hist, 36) if s in ('R','B')])
        majority = 'R' if r > b else 'B'
        minority = 'B' if majority == 'R' else 'R'
        if state.mode == "Conservador":
            side = minority
            txt = (f"Desvio R/B (χ²={chi2:.2f}, p≈{p:.3f}) em 36. Modo Conservador → "
                   f"reversão no lado deficitário: {fmt_color(side)}.")
        else:
            side = majority
            txt = (f"Desvio R/B (χ²={chi2:.2f}, p≈{p:.3f}) em 36. Modo Agressivo → "
                   f"continuidade no lado dominante: {fmt_color(side)}.")
        return side, txt

    # 3) Nada robusto
    return None, "⏳ Aguardar mais dados… Sem padrões confiáveis no momento."

def fmt_color(c: str) -> str:
    return "🔴 Vermelho" if c == 'R' else "⚫ Preto"

# =========================
# ---- HANDLERS / BOT -----
# =========================

WELCOME = (
    "🤖 **iColor — Analisador de Cores (Roleta)**\n\n"
    "Envie a cor que saiu usando os botões abaixo.\n"
    "Eu registro o histórico e sugiro a próxima entrada com base em **runs** e **desvios R/B (janela 36)**.\n\n"
    "⚠️ *Aviso*: Em roletas online/RNG, estas heurísticas **não** fornecem vantagem garantida.\n"
)

async def ensure_state(user_id: int) -> UserState:
    if user_id not in STATE:
        STATE[user_id] = UserState()
    return STATE[user_id]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    st = await ensure_state(user_id)
    await update.message.reply_html(
        WELCOME,
        reply_markup=main_keyboard(st)
    )

async def add_color(update: Update, context: ContextTypes.DEFAULT_TYPE, color: str):
    user_id = update.effective_user.id
    st = await ensure_state(user_id)
    st.history.append(color)
    # Após cada input, gerar recomendação
    reco, why = recommend(st)
    st.last_reco = reco
    await update.callback_query.answer()
    msg = (
        f"✅ Registrado: {'🔴' if color=='R' else '⚫' if color=='B' else '🟢'}\n\n"
        f"📋 Histórico (últimos 12): {''.join(st.history[-12:]) or '—'}\n\n"
    )
    if reco:
        msg += f"🎯 Recomendação: {fmt_color(reco)}\n📖 Motivo: {why}"
    else:
        msg += f"{why}"
    await update.callback_query.edit_message_text(
        msg, reply_markup=main_keyboard(st)
    )

async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = update.callback_query.data
    user_id = update.effective_user.id
    st = await ensure_state(user_id)

    if data == "add_R":
        return await add_color(update, context, 'R')
    if data == "add_B":
        return await add_color(update, context, 'B')
    if data == "add_0":
        return await add_color(update, context, '0')

    if data == "undo":
        if st.history:
            removed = st.history.pop()
            txt = f"↩️ Removido o último: {removed}"
        else:
            txt = "Nada para desfazer."
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            f"{txt}\n\n📋 Histórico (últimos 12): {''.join(st.history[-12:]) or '—'}",
            reply_markup=main_keyboard(st)
        )
        return

    if data == "reset":
        STATE[user_id] = UserState(mode=st.mode)  # mantém o modo
        await update.callback_query.answer("Histórico limpo.")
        await update.callback_query.edit_message_text(
            "♻️ Histórico resetado.\n\n"
            "Envie a próxima cor com os botões.",
            reply_markup=main_keyboard(await ensure_state(user_id))
        )
        return

    if data == "status":
        r, b = count_R_B([s for s in st.history if s in ('R','B')])
        zeros = sum(1 for s in st.history if s == '0')
        chi2, p = chi_square_rb(st.history, 36)
        last = st.history[-10:] if st.history else []
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            "📊 *Status*\n"
            f"• Total entradas: {len(st.history)} (R:{r}, B:{b}, 0:{zeros})\n"
            f"• χ² janela 36: {chi2:.2f} (p≈{p:.3f})\n"
            f"• Modo: {st.mode}\n"
            f"• Últimos: {''.join(last) or '—'}",
            reply_markup=main_keyboard(st),
            parse_mode='Markdown'
        )
        return

    if data == "toggle_mode":
        st.mode = "Agressivo" if st.mode == "Conservador" else "Conservador"
        await update.callback_query.answer(f"Modo alterado para {st.mode}.")
        # Atualiza recomendação com novo modo (opcional)
        reco, why = recommend(st)
        st.last_reco = reco
        msg = (
            f"⚙️ Modo atual: *{st.mode}*\n\n"
        )
        if reco:
            msg += f"🎯 Recomendação: {fmt_color(reco)}\n📖 Motivo: {why}"
        else:
            msg += f"{why}"
        await update.callback_query.edit_message_text(
            msg, reply_markup=main_keyboard(st), parse_mode='Markdown'
        )
        return

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Comandos:\n"
        "/start – iniciar\n"
        "/help – ajuda\n"
        "Use os botões para registrar as cores."
    )

# =========================
# ------ EXECUÇÃO ---------
# =========================

async def on_startup(app):
    print("iColor bot iniciado.")

def run():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Defina BOT_TOKEN no ambiente.")
    app = ApplicationBuilder().token(token).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(cb_handler))

    # Se estiver no Render/railway com PORT, pode optar por webhook.
    port = os.environ.get("PORT")
    url = os.environ.get("WEBHOOK_URL")  # ex: https://seu-dominio.com/webhook
    if port and url:
        # Webhook (evita conflito de múltiplas instâncias do getUpdates)
        app.run_webhook(
            listen="0.0.0.0",
            port=int(port),
            url_path=os.environ.get("WEBHOOK_PATH", ""),
            webhook_url=url.rstrip("/") + "/" + os.environ.get("WEBHOOK_PATH","")
        )
    else:
        # Polling simples (garanta 1 instância)
        app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    run()
