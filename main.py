# main.py
import os
import re
import logging
import secrets
from typing import Dict, Any, List, Tuple
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("roulette-bot")

# ──────────────────────────────────────────────────────────────────────────────
# ESTADO EM MEMÓRIA
# ──────────────────────────────────────────────────────────────────────────────
STATE: Dict[int, Dict[str, Any]] = {}
DEFAULTS = {
    "history": [],   # sequência de números informados
    "wins": 0,
    "losses": 0,
    "events": [],    # log por giro: dict(number, dz, blocked, outcome: 'win'|'loss'|'skip')
}

# Probabilidades (roleta europeia 37 números): referência matemática
P_VITORIA = 24 / 37
P_DERROTA = 1 - P_VITORIA
# EV teórico por rodada (duas dúzias) em "unidades de stake" (apenas referência informativa)
EV_POR_STAKE = P_VITORIA * (+1) + P_DERROTA * (-2)  # ≈ -0.02703 (−2.703%)

# ──────────────────────────────────────────────────────────────────────────────
# FUNÇÕES DE NEGÓCIO
# ──────────────────────────────────────────────────────────────────────────────
def get_state(chat_id: int) -> Dict[str, Any]:
    if chat_id not in STATE:
        STATE[chat_id] = {k: (v.copy() if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    return STATE[chat_id]

def dozen_of(n: int) -> str:
    if n == 0:
        return "Z"
    if 1 <= n <= 12:
        return "D1"
    if 13 <= n <= 24:
        return "D2"
    if 25 <= n <= 36:
        return "D3"
    return "?"

def pick_two_dozens_auto(history: List[int]) -> Tuple[str, str, str, bool]:
    """
    Escolhe as 2 dúzias mais frequentes nos últimos 12 giros.
    Se zero ocorreu nos últimos 2 giros, sinaliza 'bloquear entrada'.
    Retorna (d1, d2, excluida, bloquear_por_zero_recente).
    """
    if not history:
        return ("D1", "D2", "D3", False)

    tail = history[-2:] if len(history) >= 2 else history[-1:]
    bloquear = any(x == 0 for x in tail)

    window = history[-12:]
    counts = {"D1": 0, "D2": 0, "D3": 0}
    for x in window:
        dz = dozen_of(x)
        if dz in counts:
            counts[dz] += 1

    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    d1, d2 = ordered[0][0], ordered[1][0]
    excl = {"D1", "D2", "D3"}.difference({d1, d2}).pop()
    return (d1, d2, excl, bloquear)

def bet_header(d1: str, d2: str, excl: str) -> str:
    ev_pct = -EV_POR_STAKE * 100.0  # valor positivo para exibição (~2,70%)
    return (
        f"🎯 *Recomendação*: {d1} + {d2}  |  🚫 *Excluída*: {excl}\n"
        f"📈 Prob. teórica: ~64,86%  |  🧮 EV teórico: ~{ev_pct:.2f}% contra o apostador"
    )

def status_text(s: Dict[str, Any]) -> str:
    total = s["wins"] + s["losses"]
    hit = (s["wins"] / total * 100) if total > 0 else 0.0
    return (
        "📊 *Status*\n"
        f"• Acertos: {s['wins']}  |  Erros: {s['losses']}  |  Taxa de acerto: {hit:.1f}%\n"
        f"• Giros lidos (com entrada): {total}\n"
        "• Janela de tendência: últimos 12 giros"
    )

def apply_spin(s: Dict[str, Any], number: int) -> str:
    s["history"].append(number)
    d1, d2, excl, bloquear = pick_two_dozens_auto(s["history"])
    dz = dozen_of(number)

    if bloquear:
        # Não conta vitória/derrota — outcome = skip
        s["events"].append({
            "number": number, "dz": dz, "blocked": True, "outcome": "skip",
            "d1": d1, "d2": d2, "excl": excl
        })
        header = bet_header(d1, d2, excl)
        return (
            f"{header}\n"
            "— — —\n"
            f"🛑 Zero recente detectado. *Evite entrada nesta rodada.*\n"
            f"🎲 Resultado informado: *{number}* ({'zero' if number == 0 else dz})\n"
            f"{status_text(s)}"
        )

    # Resultado da rodada com base nas duas dúzias recomendadas
    if dz in {d1, d2}:
        s["wins"] += 1
        outcome = "win"
        line = f"✅ *Vitória* — saiu {number} ({dz})."
    else:
        s["losses"] += 1
        outcome = "loss"
        line = f"❌ *Derrota* — saiu {number} ({'zero' if number == 0 else dz})."

    s["events"].append({
        "number": number, "dz": dz, "blocked": False, "outcome": outcome,
        "d1": d1, "d2": d2, "excl": excl
    })

    header = bet_header(d1, d2, excl)
    return (
        f"{header}\n"
        "— — —\n"
        f"🎲 Resultado: *{number}*  |  {line}\n"
        f"{status_text(s)}"
    )

def apply_undo(s: Dict[str, Any]) -> str:
    if not s["history"]:
        return "Nada para desfazer."
    last_num = s["history"].pop()
    last_event = s["events"].pop() if s["events"] else None

    # Reverte estatísticas se necessário
    if last_event and not last_event.get("blocked", False):
        if last_event.get("outcome") == "win":
            s["wins"] = max(0, s["wins"] - 1)
        elif last_event.get("outcome") == "loss":
            s["losses"] = max(0, s["losses"] - 1)

    dz = dozen_of(last_num)
    return (
        f"↩️ *Undo feito*\n"
        f"• Removido: {last_num} ({'zero' if last_num == 0 else dz})\n"
        f"{status_text(s)}"
    )

# ──────────────────────────────────────────────────────────────────────────────
# HANDLERS
# ──────────────────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_state(update.effective_chat.id)
    text = (
        "🤖 *Bot de Roleta — Duas Dúzias* (Webhook)\n"
        "• Envie o número que saiu (0–36) e eu recomendo as duas dúzias.\n"
        "• Evito entrada quando o zero apareceu nos últimos 2 giros.\n\n"
        "*Comandos:*\n"
        "/status — mostra acertos/erros\n"
        "/reset — zera histórico\n"
        "/undo — desfaz o último giro"
    )
    await update.message.reply_markdown_v2(text)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_state(update.effective_chat.id)
    await update.message.reply_markdown_v2(status_text(s))

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_state(update.effective_chat.id)
    s["history"].clear()
    s["wins"] = 0
    s["losses"] = 0
    s["events"].clear()
    await update.message.reply_text("🔄 Histórico e estatísticas zerados.")

async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_state(update.effective_chat.id)
    resp = apply_undo(s).replace("-", r"\-")
    await update.message.reply_markdown_v2(resp)

async def on_number_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_state(update.effective_chat.id)
    text = (update.message.text or "").strip()
    m = re.search(r"(?<!\d)(\d{1,2})(?!\d)", text)
    if not m:
        await update.message.reply_text("Envie um número entre 0 e 36. Use /undo para desfazer o último giro.")
        return
    n = int(m.group(1))
    if not (0 <= n <= 36):
        await update.message.reply_text("Número fora do intervalo. Use 0 a 36.")
        return
    resp = apply_spin(s, n).replace("-", r"\-")
    await update.message.reply_markdown_v2(resp)

# ──────────────────────────────────────────────────────────────────────────────
# MAIN (WEBHOOK)
# ──────────────────────────────────────────────────────────────────────────────
def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Defina a variável de ambiente BOT_TOKEN com o token do BotFather.")

    base_url = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if not base_url:
        raise RuntimeError("Defina PUBLIC_URL (ou deixe o Render expor RENDER_EXTERNAL_URL).")

    port = int(os.getenv("PORT", "10000"))
    webhook_path = os.getenv("WEBHOOK_PATH") or secrets.token_urlsafe(32)
    secret_token = os.getenv("WEBHOOK_SECRET")  # opcional, mas recomendado

    log.info("Iniciando bot (PTB webhook)…")
    log.info("PTB versão: tentando importar…")
    try:
        import telegram
        log.info("python-telegram-bot: %s", getattr(telegram, "__version__", "desconhecida"))
    except Exception as e:
        log.warning("Falha ao obter versão do PTB: %s", e)

    log.info("Config webhook:")
    log.info("  URL base: %s", base_url)
    log.info("  Porta: %s", port)
    log.info("  Path: /%s", webhook_path)
    log.info("  Secret token definido? %s", "sim" if secret_token else "não")

    application = Application.builder().token(token).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("undo", undo_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_number_message))

    # Sobe servidor e registra webhook
    application.run_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=f"{base_url.rstrip('/')}/{webhook_path}",
        secret_token=secret_token,
        drop_pending_updates=True,
    )

if __name__ == "__main__":
    main()
