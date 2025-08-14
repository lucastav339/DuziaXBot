# main.py
import os
import asyncio
import logging
from typing import Any, Dict, Tuple, List, Optional
from collections import deque

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, ContextTypes,
    CommandHandler, MessageHandler, filters
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
app = FastAPI(title="Roulette Signals Bot", version="1.0.0")

# PTB application (criada no startup)
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
        "history": deque(maxlen=window_max),   # últimos N giros (janela deslizante)
        "counts": [0]*37,                      # contagem por número (0..36)
        "total_spins": 0,                      # dentro da janela
        "gale_active": False,
        "gale_level": 0,                       # 0 (sem gale) ou 1 (Gale 1)
        "last_recommendation": ("D1","D2","D3"),
        "last_reason": "",
        "window_max": window_max,
    }

def update_counts(state: Dict[str, Any], new_number: int) -> None:
    """
    Atualiza contagens respeitando a janela deslizante.
    Precisa ser chamada logo após empurrar para history (deque).
    """
    # Se a deque estava cheia, o elemento mais antigo foi descartado agora
    # Removemos sua contribuição:
    # Observação: deque descarta automaticamente no append; então precisamos saber
    # qual foi descartado. Truque: antes do append, checar o 0º; porém como já
    # adicionamos, faremos assim: se lotada antes, old era o que estava no índice 0
    # Para simplificar, faremos um ajuste: calcularemos a contagem a partir do histórico quando necessário.
    # Para robustez e simplicidade, recalcular:
    counts = [0]*37
    total = 0
    for x in state["history"]:
        if 0 <= x <= 36:
            counts[x] += 1
            total += 1
    state["counts"] = counts
    state["total_spins"] = total

def chi_square_bias(counts: List[int], total: int) -> Tuple[float, float]:
    """
    Qui-quadrado simples contra distribuição uniforme (37 pockets).
    Retorna (chi2, p_aproximado). p é uma aproximação via Wilson-Hilferty / normal,
    suficiente para gating básico sem SciPy.
    """
    if total <= 0:
        return 0.0, 1.0
    exp = total / 37.0
    chi2 = 0.0
    for c in counts:
        chi2 += (c - exp) ** 2 / exp

    # Aproximação de p-valor:
    # Graus de liberdade df = 36. Converter qui-quadrado -> normal aprox.
    df = 36.0
    # Wilson-Hilferty: Z ≈ ( (X/df)^(1/3) - (1 - 2/(9df)) ) / sqrt(2/(9df))
    import math
    if chi2 <= 0:
        return 0.0, 1.0
    z = ((chi2/df)**(1.0/3.0) - (1 - 2/(9*df))) / math.sqrt(2/(9*df))
    # p ~ 1 - Phi(z)
    # Phi(z) ~ 0.5*(1+erf(z/sqrt(2)))
    p = 1 - 0.5*(1 + math.erf(z / math.sqrt(2)))
    p = max(0.0, min(1.0, p))
    return chi2, p

def find_hottest_sector(counts: List[int], window_len: int = 12) -> List[int]:
    """
    Acha o setor circular de comprimento 'window_len' com maior soma (0..36).
    Retorna a lista de pockets (índices) do setor (comprimento window_len).
    """
    n = 37
    if window_len >= n:
        return list(range(n))
    extended = counts + counts[:window_len-1]
    max_sum = -1
    best_start = 0
    cur_sum = sum(extended[:window_len])
    max_sum = cur_sum
    for i in range(1, n):
        cur_sum += extended[i+window_len-1] - extended[i-1]
        if cur_sum > max_sum:
            max_sum = cur_sum
            best_start = i
    sector = [(best_start + j) % n for j in range(window_len)]
    return sector

def sector_to_two_dozens(sector: List[int]) -> Tuple[str, str, str]:
    """
    Converte setor (lista de pockets 0..36) nas duas dúzias que melhor o cobrem.
    Heurística: conta cobertura por D1, D2, D3 (ignorando zeros) e escolhe as top-2.
    """
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
    """
    OR de curto prazo: se, nos últimos k giros, duas dúzias somam >= need ocorrências,
    recomenda essas duas e exclui a restante.
    """
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
    chi2, p = chi_square_bias(state["counts"], total)
    if p > p_threshold:
        return (False, f"sem viés detectável (p≈{p:.3f})", ("D1","D2","D3"))
    sector = find_hottest_sector(state["counts"], window_len=12)
    d1, d2, excl = sector_to_two_dozens(sector)
    return (True, "viés detectado", (d1,d2,excl))

def format_reco_text(d1: str, d2: str, excl: str, reason: str, mode: str, params: Dict[str, Any]) -> str:
    return (
        f"🎯 Recomendação: **{d1} + {d2}**  |  🚫 Excluída: {excl}\n"
        f"📖 Critério: {reason}\n"
        f"⚙️ Modo: {mode}  | p≤{params['P_THRESHOLD']}  | K={params['K']} NEED={params['NEED']}  | janela={params['WINDOW']}\n"
        f"ℹ️ Envie números (0–36)."
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
    # Apenas agressivo e conservador visíveis
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
    msg = format_reco_text(d1, d2, excl, reason, mode, params)
    await update.message.reply_text(msg)

async def number_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Recebe números 0–36 e decide se recomenda entrada em duas dúzias.
    """
    await ensure_chat_state(update, context)
    s = context.chat_data["state"]

    text = (update.message.text or "").strip()
    if not text.isdigit():
        return
    n = int(text)
    if n < 0 or n > 36:
        await update.message.reply_text("Envie números entre 0 e 36.")
        return

    # 1) Empilha no histórico (janela deslizante)
    s["history"].append(n)
    # 2) Recalcula contagens dentro da janela
    update_counts(s, n)

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
        await update.message.reply_text(
            "🎬 **ENTRAR**\n" + format_reco_text(d1, d2, excl, reason, mode, params),
            parse_mode="Markdown"
        )
        # Gale 1 opcional (sinalizar estado)
        s["gale_active"] = True
        s["gale_level"] = 0
    else:
        await update.message.reply_text(
            "⏳ **Aguardar**\n" + format_reco_text(d1, d2, excl, reason, mode, params),
            parse_mode="Markdown"
        )

async def ensure_chat_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Garante state por chat e aplica janela configurada no modo atual.
    """
    if "state" not in context.chat_data:
        # usa janela do modo atual (se já definida em bot_data) ou 150
        win = context.bot_data.get("WINDOW", 150)
        context.chat_data["state"] = make_default_state(window_max=win)
    else:
        # se o modo mudou a janela, atualiza
        s = context.chat_data["state"]
        win = context.bot_data.get("WINDOW", s.get("window_max", 150))
        if s.get("window_max") != win:
            old_hist = list(s["history"])
            s["history"] = deque(old_hist, maxlen=win)
            s["window_max"] = win
            update_counts(s, -1)  # força recálculo

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
        ptb_app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), number_message))

        # Define webhook
        webhook_url = f"{PUBLIC_URL}/telegram/webhook"
        await ptb_app.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES
        )
        # Inicia PTB (rodará em background, FastAPI atende HTTP)
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

# Para rodar localmente:
# uvicorn main:app --host 0.0.0.0 --port 10000
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
