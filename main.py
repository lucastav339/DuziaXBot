import os
import logging
from typing import Dict, Any
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import Conflict

# =========================
# Configuração básica
# =========================
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # ex: https://duziaxbot.onrender.com
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "")  # se vazio, definimos um padrão seguro
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # recomendado no webhook
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("Defina BOT_TOKEN no ambiente.")

# =========================
# Estado por usuário
# =========================
STATE: Dict[int, Dict[str, Any]] = {}


def get_state(user_id: int) -> Dict[str, Any]:
    if user_id not in STATE:
        STATE[user_id] = {
            "jogadas": 0,
            "acertos": 0,
            "erros": 0,
            "ultimo_palpite": None,  # texto do último botão registrado
        }
    return STATE[user_id]


# =========================
# UI
# =========================
CHOICES = ["🔴 Vermelho", "⚫ Preto", "🟢 Zero"]
KB = ReplyKeyboardMarkup([CHOICES, ["/status", "/reset"]], resize_keyboard=True)


# =========================
# Handlers
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE[uid] = {"jogadas": 0, "acertos": 0, "erros": 0, "ultimo_palpite": None}
    if update.message:
        await update.message.reply_text(
            "🎲 Bem-vindo!\n"
            "Use os botões para registrar as jogadas.\n"
            "Comandos: /status /reset",
            reply_markup=KB,
        )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st = get_state(update.effective_user.id)
    j, a, e = st["jogadas"], st["acertos"], st["erros"]
    taxa = (a / j * 100.0) if j > 0 else 0.0
    if update.message:
        await update.message.reply_text(
            f"📊 Status\n"
            f"➡️ Jogadas: {j}\n"
            f"✅ Acertos: {a}\n"
            f"❌ Erros: {e}\n"
            f"📈 Taxa: {taxa:.2f}%"
        )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    STATE[uid] = {"jogadas": 0, "acertos": 0, "erros": 0, "ultimo_palpite": None}
    if update.message:
        await update.message.reply_text("♻️ Histórico e placar resetados!", reply_markup=KB)


async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    st = get_state(uid)
    jogada = update.message.text.strip()

    if jogada not in CHOICES:
        # Ignora textos aleatórios; reenvia teclado
        await update.message.reply_text("Use os botões abaixo para registrar.", reply_markup=KB)
        return

    palpite = st["ultimo_palpite"]
    if palpite is not None:
        st["jogadas"] += 1
        if jogada == palpite:
            st["acertos"] += 1
            resultado = "✅ Acerto!"
        else:
            st["erros"] += 1
            resultado = "❌ Erro!"
    else:
        resultado = "⚡ Primeira jogada registrada (sem comparação)."

    # Atualiza "palpite" (neste modelo simplificado, igual à jogada atual)
    st["ultimo_palpite"] = jogada

    taxa = (st["acertos"] / st["jogadas"] * 100.0) if st["jogadas"] > 0 else 0.0
    await update.message.reply_text(
        f"{resultado}\n\n"
        f"📊 Placar:\n"
        f"➡️ Jogadas: {st['jogadas']}\n"
        f"✅ Acertos: {st['acertos']}\n"
        f"❌ Erros: {st['erros']}\n"
        f"📈 Taxa: {taxa:.2f}%",
        reply_markup=KB,
    )


# =========================
# Hooks & Inicialização
# =========================
async def _post_init(app):
    """
    Executa após a Application ser inicializada.
    Se for rodar em POLLING, removemos qualquer webhook antigo para evitar 409/Conflict.
    (Se WEBHOOK_URL estiver setado, o run_webhook vai setar o webhook depois.)
    """
    if not WEBHOOK_URL:
        try:
            await app.bot.delete_webhook(drop_pending_updates=True)
            log.info("Webhook removido (modo polling).")
        except Exception as e:
            log.warning(f"Falha ao remover webhook: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Erro no handler:", exc_info=context.error)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_choice))
    app.add_error_handler(error_handler)

    if WEBHOOK_URL:
        # ── WEBHOOK (produção/Render) ─────────────────────────────────────
        # Definimos um path seguro: se não vier do env, usa WEBHOOK_SECRET ou BOT_TOKEN
        path = (WEBHOOK_PATH or WEBHOOK_SECRET or BOT_TOKEN).strip("/")
        webhook_full = WEBHOOK_URL.rstrip("/") + f"/{path}"
        log.info(f"🌐 Iniciando em WEBHOOK: {webhook_full} (porta {PORT})")

        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=path,                 # rota interna do servidor embutido (aiohttp)
            webhook_url=webhook_full,      # URL pública no Telegram
            secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=True,     # evita backlog antigo
            allowed_updates=Update.ALL_TYPES,
        )
    else:
        # ── POLLING (local/worker único) ──────────────────────────────────
        log.info("🤖 Iniciando em POLLING.")
        try:
            app.run_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )
        except Conflict:
            # Outro processo está chamando getUpdates com o MESMO token
            log.error("409 Conflict: outra instância está em polling. Encerre as duplicadas.")
            raise SystemExit(1)


if __name__ == "__main__":
    main()
