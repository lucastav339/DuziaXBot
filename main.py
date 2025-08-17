
import os
import math
import asyncio
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
)

# Para health server (quando estiver em serviço Web sem WEBHOOK_URL)
try:
    from aiohttp import web
except Exception:
    web = None

# =========================
# ------- ESTADO ----------
# =========================

@dataclass
class UserState:
    history: List[str] = field(default_factory=list)     # 'R','B','0'
    hits: int = 0
    misses: int = 0
    mode: str = "Conservador"  # "Conservador" | "Agressivo"
    last_reco: Optional[str] = None  # 'R'/'B'
    god_mode: bool = False           # MODO GOD

STATE: Dict[int, UserState] = {}

# =========================
# ---- COPY "PREMIUM" -----
# =========================

BRAND = "iColor — Edição Premium"
EMO = {
    "brand": "🎛️",
    "spark": "✨",
    "warn": "⚠️",
    "info": "ℹ️",
    "ok": "✅",
    "rec": "🎯",
    "why": "📖",
    "hist": "📋",
    "status": "📊",
    "mode": "⚙️",
    "god": "🧠",
    "wait": "⏳",
    "reset": "♻️",
    "undo": "↩️",
    "r": "🔴",
    "b": "⚫",
    "z": "🟢",
}

WELCOME = (
    f"{EMO['spark']} <b>{BRAND}</b>\n"
    f"{EMO['brand']} <i>Especialista em análise de cores (Vermelho/Preto) com heurísticas de runs e desvio R/B.</i>\n\n"
    "Como usar:\n"
    f"1) Toque em <b>{EMO['r']} Vermelho</b> ou <b>{EMO['b']} Preto</b> (ou {EMO['z']} Zero) conforme o último giro.\n"
    "2) Eu registro o histórico e mostro a <b>próxima recomendação</b> com justificativa.\n"
    "3) Use <b>Status</b> para ver métricas e <b>Modo</b> para alternar o perfil de risco.\n\n"
    f"{EMO['god']} <b>MODO GOD</b>: força análise agressiva e <b>inverte</b> a recomendação final.\n"
    f"{EMO['warn']} <i>Heurísticas recreativas. Não há vantagem garantida em RNG.</i>"
)

RESP_WAIT = (
    f"{EMO['wait']} <b>Aguardando mais dados…</b>\n"
    "<i>Coletando padrão estatístico. Envie os próximos resultados para liberar nova recomendação.</i>"
)

def fmt_color(c: str) -> str:
    return f"{EMO['r']} Vermelho" if c == 'R' else f"{EMO['b']} Preto"

def fmt_hist(seq):
    s = ''.join(seq[-12:]) or '—'
    s = s.replace('R', EMO['r']).replace('B', EMO['b']).replace('0', EMO['z'])
    return s

# =========================
# ---- BOTÕES / UI --------
# =========================

def main_keyboard(state: UserState) -> InlineKeyboardMarkup:
    row1 = [
        InlineKeyboardButton(f"{EMO['r']} Vermelho", callback_data="add_R"),
        InlineKeyboardButton(f"{EMO['b']} Preto", callback_data="add_B"),
        InlineKeyboardButton(f"{EMO['z']} Zero", callback_data="add_0"),
    ]
    row2 = [
        InlineKeyboardButton(f"{EMO['undo']} Corrigir último", callback_data="undo"),
        InlineKeyboardButton(f"{EMO['status']} Status", callback_data="status"),
    ]
    row3 = [
        InlineKeyboardButton(f"{EMO['reset']} Resetar", callback_data="reset"),
        InlineKeyboardButton(f"{EMO['mode']} Modo: {state.mode}", callback_data="toggle_mode"),
    ]
    row4 = [
        InlineKeyboardButton(
            f"{EMO['god']} MODO GOD: {'ON' if state.god_mode else 'OFF'}",
            callback_data="toggle_god"
        )
    ]
    return InlineKeyboardMarkup([row1, row2, row3, row4])

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
    sub = [s for s in recent(seq, window) if s in ('R','B')]
    n = len(sub)
    if n == 0:
        return 0.0, 1.0
    r, b = count_R_B(sub)
    expected = n / 2.0
    chi2 = 0.0
    if expected > 0:
        chi2 = ((r - expected) ** 2) / expected + ((b - expected) ** 2) / expected
    p_approx = math.exp(-chi2 / 2) * (1 + chi2) ** 0.5
    return chi2, p_approx

def run_trigger(seq: List[str]) -> Optional[str]:
    sub = [s for s in recent(seq, 4) if s in ('R','B')]
    if len(sub) < 3:
        return None
    r, b = count_R_B(sub)
    if r >= 3:
        return 'R'
    if b >= 3:
        return 'B'
    return None

def invert_color(c: Optional[str]) -> Optional[str]:
    if c is None:
        return None
    return 'B' if c == 'R' else 'R'

def recommend_core(hist: List[str], mode: str) -> Tuple[Optional[str], str]:
    if hist and hist[-1] == '0':
        return None, f"{EMO['z']} Zero recente — reiniciando leitura curta. Aguarde mais dados."

    run_side = run_trigger(hist)
    if run_side:
        if mode == "Conservador":
            side = 'B' if run_side == 'R' else 'R'
            txt = f"Run {run_side} detectada (≥3/4). Conservador → reversão em {fmt_color(side)}."
        else:
            side = run_side
            txt = f"Run {run_side} detectada (≥3/4). Agressivo → continuidade em {fmt_color(side)}."
        return side, txt

    chi2, p = chi_square_rb(hist, window=36)
    if p < 0.05:
        r, b = count_R_B([s for s in recent(hist, 36) if s in ('R','B')])
        majority = 'R' if r > b else 'B'
        minority = 'B' if majority == 'R' else 'R'
        if mode == "Conservador":
            side = minority
            txt = f"Desvio R/B χ²={chi2:.2f} p≈{p:.3f}. Conservador → reversão em {fmt_color(side)}."
        else:
            side = majority
            txt = f"Desvio R/B χ²={chi2:.2f} p≈{p:.3f}. Agressivo → continuidade em {fmt_color(side)}."
        return side, txt

    return None, RESP_WAIT

def recommend_with_god(state: UserState) -> Tuple[Optional[str], str]:
    hist = state.history[:]
    if state.god_mode:
        reco, why = recommend_core(hist, mode="Agressivo")
        inv = invert_color(reco)
        if inv is None:
            return None, f"{why} (MODO GOD ativo: aguardando para inverter)"
        return inv, f"{why} → {EMO['god']} MODO GOD: invertido em {fmt_color(inv)}."
    else:
        return recommend_core(hist, mode=state.mode)

# =========================
# ---- HANDLERS / BOT -----
# =========================

async def ensure_state(user_id: int) -> UserState:
    if user_id not in STATE:
        STATE[user_id] = UserState()
    return STATE[user_id]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = await ensure_state(update.effective_user.id)
    await update.message.reply_html(WELCOME, reply_markup=main_keyboard(st))

async def add_color(update: Update, context: ContextTypes.DEFAULT_TYPE, color: str):
    user_id = update.effective_user.id
    st = await ensure_state(user_id)
    st.history.append(color)

    reco, why = recommend_with_god(st)
    st.last_reco = reco

    await update.callback_query.answer()

    icon = EMO['r'] if color == 'R' else EMO['b'] if color == 'B' else EMO['z']
    body = (
        f"{EMO['ok']} <b>Registrado:</b> {icon}\n\n"
        f"{EMO['hist']} <b>Histórico (últimos 12):</b> {fmt_hist(st.history)}\n\n"
    )

    if reco:
        body += (
            f"{EMO['rec']} <b>Recomendação:</b> {fmt_color(reco)}\n"
            f"{EMO['why']} <b>Motivo:</b> {why}\n"
            f"{EMO['mode']} <b>Perfil:</b> {st.mode}  |  {EMO['god']} <b>MODO GOD:</b> {'ON' if st.god_mode else 'OFF'}"
        )
    else:
        body += RESP_WAIT

    await update.callback_query.edit_message_text(
        body, reply_markup=main_keyboard(st), parse_mode='HTML'
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
            removed_fmt = fmt_color('R' if removed == 'R' else 'B') if removed in ('R', 'B') else f"{EMO['z']} Zero"
            txt = f"{EMO['undo']} <b>Removido o último:</b> {removed_fmt}"
        else:
            txt = "Nada para desfazer."
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            f"{txt}\n\n{EMO['hist']} <b>Histórico (últimos 12):</b> {fmt_hist(st.history)}",
            reply_markup=main_keyboard(st), parse_mode='HTML'
        )
        return

    if data == "reset":
        god = st.god_mode
        mode = st.mode
        STATE[user_id] = UserState(mode=mode, god_mode=god)
        await update.callback_query.answer("Histórico limpo.")
        await update.callback_query.edit_message_text(
            f"{EMO['reset']} <b>Histórico resetado.</b>\n\nEnvie a próxima cor com os botões.",
            reply_markup=main_keyboard(await ensure_state(user_id)), parse_mode='HTML'
        )
        return

    if data == "status":
        r, b = count_R_B([s for s in st.history if s in ('R','B')])
        zeros = sum(1 for s in st.history if s == '0')
        chi2, p = chi_square_rb(st.history, 36)
        last = fmt_hist(st.history[-10:]) if st.history else '—'
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            f"{EMO['status']} <b>Status Geral</b>\n"
            f"• <b>Entradas:</b> {len(st.history)}  (R:{r}  |  B:{b}  |  0:{zeros})\n"
            f"• <b>χ² (janela 36):</b> {chi2:.2f}  (p≈{p:.3f})\n"
            f"• <b>Modo:</b> {st.mode}  |  {EMO['god']} <b>MODO GOD:</b> {'ON' if st.god_mode else 'OFF'}\n"
            f"• <b>Últimos:</b> {last}",
            reply_markup=main_keyboard(st),
            parse_mode='HTML'
        )
        return

    if data == "toggle_mode":
        st.mode = "Agressivo" if st.mode == "Conservador" else "Conservador"
        await update.callback_query.answer(f"Modo alterado para {st.mode}.")
        reco, why = recommend_with_god(st)
        st.last_reco = reco
        msg = f"{EMO['mode']} <b>Modo atual:</b> {st.mode}\n\n"
        if reco:
            msg += f"{EMO['rec']} <b>Recomendação:</b> {fmt_color(reco)}\n{EMO['why']} <b>Motivo:</b> {why}"
        else:
            msg += RESP_WAIT
        await update.callback_query.edit_message_text(
            msg, reply_markup=main_keyboard(st), parse_mode='HTML'
        )
        return

    if data == "toggle_god":
        st.god_mode = not st.god_mode
        await update.callback_query.answer(f"MODO GOD {'ativado' if st.god_mode else 'desativado'}.")
        reco, why = recommend_with_god(st)
        st.last_reco = reco
        msg = f"{EMO['god']} <b>MODO GOD:</b> {'ON' if st.god_mode else 'OFF'}\n\n"
        if reco:
            msg += f"{EMO['rec']} <b>Recomendação:</b> {fmt_color(reco)}\n{EMO['why']} <b>Motivo:</b> {why}"
        else:
            msg += RESP_WAIT
        await update.callback_query.edit_message_text(
            msg, reply_markup=main_keyboard(st), parse_mode='HTML'
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
# ------ SERVIDORES -------
# =========================

async def on_startup(app):
    # Se NÃO for webhook, remova webhook (evita conflito com polling)
    port = os.environ.get("PORT")
    url = os.environ.get("WEBHOOK_URL")
    if not (port and url):
        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
            print("Webhook removido (modo polling).")
        except Exception as e:
            print("Aviso: não consegui remover webhook:", e)
    else:
        print("Modo webhook detectado (PORT e WEBHOOK_URL presentes).")

async def run_health_server(port: int):
    if web is None:
        raise RuntimeError("aiohttp não instalado. Adicione 'aiohttp' no requirements.txt.")
    app = web.Application()
    async def health(_):
        return web.Response(text="OK")
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Health server ouvindo na porta {port}")

async def run_polling_with_optional_health(app, port: Optional[int] = None):
    # Sobe health server se estivermos num serviço Web sem WEBHOOK_URL (port existe)
    if port:
        await run_health_server(port)

    # Start do Telegram em polling
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    print("Polling iniciado.")
    await app.updater.wait()

def run():
    token = os.environ.get("BOT_TOKEN")
    if not token:
        raise RuntimeError("Defina BOT_TOKEN no ambiente.")

    tg_app = ApplicationBuilder().token(token).post_init(on_startup).build()

    tg_app.add_handler(CommandHandler("start", start))
    tg_app.add_handler(CommandHandler("help", help_cmd))
    tg_app.add_handler(CallbackQueryHandler(cb_handler))

    port = os.environ.get("PORT")
    url = os.environ.get("WEBHOOK_URL")

    if port and url:
        # WEBHOOK (serviço Web)
        tg_app.run_webhook(
            listen="0.0.0.0",
            port=int(port),
            url_path=os.environ.get("WEBHOOK_PATH", ""),
            webhook_url=url.rstrip("/") + "/" + os.environ.get("WEBHOOK_PATH","")
        )
    else:
        # POLLING (serviço Worker) OU Web sem WEBHOOK_URL (abrimos health server)
        # Se PORT existir mas sem WEBHOOK_URL, abrimos health server na mesma porta.
        if port and not url:
            asyncio.run(run_polling_with_optional_health(tg_app, int(port)))
        else:
            tg_app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    run()
