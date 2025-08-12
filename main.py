# main.py
import os
import re
import logging
import secrets
from html import escape as esc
from typing import Dict, Any, List, Tuple, Optional

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import PlainTextResponse

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

# -----------------------------------------------------------------------------
# LOGGING + BUILD TAG
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("roulette-bot")
BUILD_TAG = os.getenv("RENDER_GIT_COMMIT") or "local"
log.info("Starting build: %s", BUILD_TAG)

# -----------------------------------------------------------------------------
# CRIA O FASTAPI APP DE IMEDIATO (para o Uvicorn encontrar main:app)
# -----------------------------------------------------------------------------
app = FastAPI(title="Roulette Double Dozens Bot — Book Style + Gale1", version="1.0.0")

# -----------------------------------------------------------------------------
# CONFIG / CONSTANTES
# -----------------------------------------------------------------------------
SHOW_NOENTRY_RECOMMENDATION = False  # não mostrar recomendação quando decisão for "sem entrada"

MIN_SPINS = 800         # amostra mínima para testar viés (estilo livros)
P_THRESHOLD = 0.01      # nível de significância aproximado (qui-quadrado, gl=36)

# Ordem física (roda europeia, sentido horário)
WHEEL_ORDER = [0,32,15,19,4,21,2,25,17,34,6,27,13,36,11,30,8,23,10,5,24,16,33,1,20,14,31,9,22,18,29,7,28,12,35,3,26]

# Webhook path pode vir de env; se não vier, geramos um aleatório já no import
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH") or secrets.token_urlsafe(32)

# -----------------------------------------------------------------------------
# ESTADO (por chat)
# -----------------------------------------------------------------------------
def make_default_state() -> Dict[str, Any]:
    return {
        "history": [],        # sequência de números informados
        "wins": 0,
        "losses": 0,
        "events": [],         # {number, dz, outcome, d1, d2, excl, reason, gale_*}
        "counts": {i: 0 for i in range(37)},  # contagem por número
        "total_spins": 0,     # total de giros acumulados (para viés)

        # --- Gale controlado (máx. 1 passo) ---
        "gale_active": False,   # True => próxima rodada é Gale 1
        "gale_step": 0,         # 0 (desativado) | 1 (execução do Gale 1 nesta rodada)
        "gale_d1": None,
        "gale_d2": None,
        "gale_excl": None,
    }

STATE: Dict[int, Dict[str, Any]] = {}
def get_state(chat_id: int) -> Dict[str, Any]:
    if chat_id not in STATE:
        STATE[chat_id] = make_default_state()
    return STATE[chat_id]

# -----------------------------------------------------------------------------
# FUNÇÕES BÁSICAS
# -----------------------------------------------------------------------------
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

def bet_header(d1: str, d2: str, excl: str) -> str:
    return (
        f"🎯 <b>Recomendação</b>: {esc(d1)} + {esc(d2)}  |  🚫 <b>Excluída</b>: {esc(excl)}\n"
        f"📚 Estratégia: viés de roda + setor contíguo (estilo livros) | Gale: máx. 1 passo"
    )

def status_text(s: Dict[str, Any]) -> str:
    total_entries = s["wins"] + s["losses"]
    total_spins = len(s["history"])
    hit = (s["wins"] / total_entries * 100) if total_entries > 0 else 0.0
    gale_flag = (
        "Executando Gale 1" if s["gale_active"] and s["gale_step"] == 1
        else ("Sim (próx.=Gale 1)" if s["gale_active"] else "Não")
    )
    return (
        "📊 <b>Status</b>\n"
        f"• Entradas: {total_entries} (✅ {s['wins']} / ❌ {s['losses']})  |  Taxa de acerto: {hit:.1f}%\n"
        f"• Giros lidos: {total_spins}  |  Sem entrada: {total_spins - total_entries}\n"
        f"• Amostra p/viés: {s['total_spins']} números acumulados\n"
        f"• Gale: {gale_flag}\n"
        "• Janela de setor: 8–12 pockets contíguos (ordem física da roda)"
    )

# -----------------------------------------------------------------------------
# VIÉS + MAPEAMENTO PARA DUAS DÚZIAS
# -----------------------------------------------------------------------------
def update_counts(s: Dict[str, Any], n: int) -> None:
    if 0 <= n <= 36:
        s["counts"][n] += 1
        s["total_spins"] += 1

def chi_square_bias(counts: Dict[int,int], total: int) -> Tuple[float, float]:
    """Qui-quadrado simples vs. uniforme (1/37). Retorna (chi2, p_approx)."""
    if total == 0:
        return (0.0, 1.0)
    expected = total / 37.0
    chi2 = 0.0
    for i in range(37):
        o = counts.get(i, 0)
        diff = o - expected
        chi2 += (diff * diff) / expected
    # p-value approx (sem SciPy), gl=36
    import math
    lam = 0.5 * chi2
    ssum, fact, powt = 0.0, 1.0, 1.0
    for k in range(19):
        if k > 0:
            fact *= k
            powt *= lam
        ssum += powt / fact
    p_approx = math.exp(-lam) * ssum
    return (chi2, min(max(p_approx, 0.0), 1.0))

def find_hottest_sector(counts: Dict[int,int], window_len: int = 12) -> List[int]:
    """Varre janelas contíguas na ordem da roda e retorna o setor com maior excesso sobre o esperado."""
    total = sum(counts.values())
    if total == 0:
        return []
    exp_per_num = total / 37.0
    best_sector: List[int] = []
    best_excess = float("-inf")
    n = len(WHEEL_ORDER)
    for L in range(8, window_len+1):  # sectores de 8 a 12 pockets
        for start in range(n):
            sector = [WHEEL_ORDER[(start + k) % n] for k in range(L)]
            obs = sum(counts[i] for i in sector)
            exp = exp_per_num * L
            excess = obs - exp
            if excess > best_excess:
                best_excess = excess
                best_sector = sector
    return best_sector

def sector_to_two_dozens(sector: List[int]) -> Tuple[str,str,str]:
    """Escolhe as 2 dúzias que mais cobrem o setor quente."""
    if not sector:
        return ("D1","D2","D3")
    cover = {"D1":0,"D2":0,"D3":0}
    for x in sector:
        dz = dozen_of(x)
        if dz in cover:
            cover[dz] += 1
    ordered = sorted(cover.items(), key=lambda kv:(-kv[1], kv[0]))
    d1, d2 = ordered[0][0], ordered[1][0]
    excl = {"D1","D2","D3"}.difference({d1,d2}).pop()
    return (d1,d2,excl)

def should_enter_book_style(s: Dict[str, Any]) -> Tuple[bool, str, Tuple[str,str,str]]:
    """
    Estilo livros:
      - amostra grande (MIN_SPINS)
      - testa viés global (qui² vs uniforme)
      - encontra setor contíguo mais quente
      - mapeia setor -> duas dúzias
    """
    total = s.get("total_spins", 0)
    if total < MIN_SPINS:
        return (False, f"amostra insuficiente ({total}/{MIN_SPINS})", ("D1","D2","D3"))
    chi2, p = chi_square_bias(s["counts"], total)
    if p > P_THRESHOLD:
        return (False, f"sem viés detectável (p≈{p:.3f})", ("D1","D2","D3"))
    sector = find_hottest_sector(s["counts"], window_len=12)
    d1, d2, excl = sector_to_two_dozens(sector)
    return (True, "viés detectado", (d1,d2,excl))

# -----------------------------------------------------------------------------
# GALE HELPERS
# -----------------------------------------------------------------------------
def snapshot_gale(s: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "gale_active": s["gale_active"],
        "gale_step": s["gale_step"],
        "gale_d1": s["gale_d1"],
        "gale_d2": s["gale_d2"],
        "gale_excl": s["gale_excl"],
    }

def restore_gale(s: Dict[str, Any], snap: Dict[str, Any]) -> None:
    s["gale_active"] = snap.get("gale_active", False)
    s["gale_step"] = snap.get("gale_step", 0)
    s["gale_d1"] = snap.get("gale_d1")
    s["gale_d2"] = snap.get("gale_d2")
    s["gale_excl"] = snap.get("gale_excl")

# -----------------------------------------------------------------------------
# APLICAÇÃO DA RODADA (com Gale 1) — REC SÓ QUANDO ENTRA
# -----------------------------------------------------------------------------
def apply_spin(s: Dict[str, Any], number: int) -> str:
    """
    1) Se gale está ativo, executa Gale 1 com as mesmas dúzias (mesmo sem edge).
    2) Senão, decide por edge (viés/sector/qui²).
    3) Compara com a recomendação pré-giro, registra e só então atualiza contagens.
    4) Em 'noentry', NÃO exibe recomendação (a menos que SHOW_NOENTRY_RECOMMENDATION=True).
    """
    dz = dozen_of(number)
    gale_prev = snapshot_gale(s)  # para undo fiel

    # --- Execução de Gale 1 ---
    if s["gale_active"] and s["gale_step"] == 1:
        d1, d2, excl = s["gale_d1"], s["gale_d2"], s["gale_excl"]
        recomendacao_txt = f"🎯 Recomendado antes do giro (Gale 1): {esc(d1)} + {esc(d2)}  |  🚫 Excluída: {esc(excl)}"

        if dz in {d1, d2}:
            s["wins"] += 1
            outcome = "win_gale1"
            line = f"✅ <b>Vitória na Gale 1</b> — saiu {number} ({dz})."
        else:
            s["losses"] += 1
            outcome = "loss_gale1"
            line = f"❌ <b>Derrota na Gale 1</b> — saiu {number} ({'zero' if number == 0 else dz})."

        s["history"].append(number)
        update_counts(s, number)

        # encerra gale
        s["gale_active"] = False
        s["gale_step"] = 0
        s["gale_d1"] = s["gale_d2"] = s["gale_excl"] = None

        header = bet_header(d1, d2, excl)
        s["events"].append({
            "number": number, "dz": dz, "blocked": False, "outcome": outcome,
            "d1": d1, "d2": d2, "excl": excl, "reason": "gale",
            "gale_prev": gale_prev
        })
        return (
            f"{header}\n"
            f"{recomendacao_txt}\n"
            "— — —\n"
            f"🎲 Resultado: <b>{number}</b>  |  {line}\n"
            f"{status_text(s)}"
        )

    # --- Decisão por edge (estilo livros) ---
    enter, reason, rec = should_enter_book_style(s)
    d1, d2, excl = rec
    recomendacao_txt = f"🎯 Recomendado antes do giro: {esc(d1)} + {esc(d2)}  |  🚫 Excluída: {esc(excl)}"

    if not enter:
        # Sem entrada: só registra; por padrão não mostra rec.
        s["history"].append(number)
        update_counts(s, number)
        s["events"].append({
            "number": number, "dz": dz, "blocked": False, "outcome": "noentry",
            "d1": d1, "d2": d2, "excl": excl, "reason": reason,
            "gale_prev": gale_prev
        })

        if SHOW_NOENTRY_RECOMMENDATION:
            header = bet_header(d1, d2, excl)
            return (
                f"{header}\n"
                f"{recomendacao_txt}\n"
                "— — —\n"
                f"🧪 Critérios (livro) não atendidos: <i>{esc(reason)}</i>. <b>Sem entrada.</b>\n"
                f"🎲 Resultado: <b>{number}</b> ({dz})\n"
                f"{status_text(s)}"
            )
        else:
            return (
                "⏭️ <b>Sem entrada</b>\n"
                f"Motivo: <i>{esc(reason)}</i>\n"
                f"🎲 Resultado: <b>{number}</b> ({dz})\n"
                f"{status_text(s)}"
            )

    # --- Entrou (edge presente) ---
    if dz in {d1, d2}:
        s["wins"] += 1
        outcome = "win"
        line = f"✅ <b>Vitória</b> — saiu {number} ({dz})."
        # Gale não é ativado
    else:
        s["losses"] += 1
        outcome = "loss"
        line = f"❌ <b>Derrota</b> — saiu {number} ({'zero' if number == 0 else dz})."
        # Ativar Gale 1 para a próxima
        s["gale_active"] = True
        s["gale_step"] = 1
        s["gale_d1"], s["gale_d2"], s["gale_excl"] = d1, d2, excl

    s["history"].append(number)
    update_counts(s, number)
    s["events"].append({
        "number": number, "dz": dz, "blocked": False, "outcome": outcome,
        "d1": d1, "d2": d2, "excl": excl, "reason": "edge",
        "gale_prev": gale_prev
    })

    header = bet_header(d1, d2, excl)
    gale_note = "\n🔁 Próxima rodada: <b>Gale 1</b> com a mesma recomendação." if outcome == "loss" else ""
    return (
        f"{header}\n"
        f"{recomendacao_txt}\n"
        "— — —\n"
        f"🎲 Resultado: <b>{number}</b>  |  {line}{gale_note}\n"
        f"{status_text(s)}"
    )

def apply_undo(s: Dict[str, Any]) -> str:
    """
    Desfaz o último giro, ajusta estatísticas, contagens e estado de gale.
    Exibe rec. pré-giro desfeita e rec. atual após undo.
    """
    if not s["history"]:
        return "Nada para desfazer."

    last_num = s["history"].pop()
    last_event = s["events"].pop() if s["events"] else None
    last_dz = dozen_of(last_num)

    # Reverte contagens globais
    if 0 <= last_num <= 36 and s["counts"].get(last_num, 0) > 0:
        s["counts"][last_num] -= 1
        s["total_spins"] = max(0, s["total_spins"] - 1)

    # Reverte estatísticas se contou
    if last_event and last_event.get("outcome") in ("win", "loss", "win_gale1", "loss_gale1"):
        if last_event["outcome"] in ("win", "win_gale1"):
            s["wins"] = max(0, s["wins"] - 1)
        else:
            s["losses"] = max(0, s["losses"] - 1)

    # Restaura o estado de gale anterior ao evento
    gale_prev = last_event.get("gale_prev") if last_event else None
    if gale_prev:
        restore_gale(s, gale_prev)

    # Recomendação pré-giro do lance desfeito
    prev_d1 = last_event.get("d1") if last_event else None
    prev_d2 = last_event.get("d2") if last_event else None
    prev_excl = last_event.get("excl") if last_event else None
    prev_rec_txt = (
        f"🎯 <b>Rec. pré-giro desfeito</b>: {esc(prev_d1)} + {esc(prev_d2)}  |  🚫 {esc(prev_excl)}"
        if (prev_d1 and prev_d2 and prev_excl) else
        "🎯 <b>Rec. pré-giro desfeito</b>: (indisponível)"
    )

    # Recomendação atual (histórico já corrigido)
    enter_now, _, (cur_d1, cur_d2, cur_excl) = should_enter_book_style(s)
    cur_label = "pronta p/ entrada" if enter_now else "sem entrada"
    cur_rec_txt = f"🧭 <b>Rec. atual</b>: {esc(cur_d1)} + {esc(cur_d2)}  |  🚫 {esc(cur_excl)} ({cur_label})"

    return (
        "↩️ <b>Undo feito</b>\n"
        f"• Removido: {last_num} ({'zero' if last_num == 0 else last_dz})\n"
        f"{prev_rec_txt}\n"
        f"{cur_rec_txt}\n"
        f"{status_text(s)}"
    )

# -----------------------------------------------------------------------------
# HANDLERS DO BOT (definições)
# -----------------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    get_state(update.effective_chat.id)
    text = (
        "🤖 <b>Bot de Roleta — Duas Dúzias</b> (Webhook/FastAPI)\n"
        "• Envie o número que saiu (0–36). O bot só “entra” quando há <b>viés detectado</b> (estilo livros).\n"
        "• Sem edge ⇒ sem entrada. Se perder uma entrada, executa <b>Gale 1</b> na próxima com a mesma recomendação.\n\n"
        "<b>Comandos:</b>\n"
        "/status — mostra acertos/erros e progresso de amostra\n"
        "/reset — zera histórico\n"
        "/undo — desfaz o último giro"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_state(update.effective_chat.id)
    await update.message.reply_text(status_text(s), parse_mode=ParseMode.HTML)

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    STATE[chat_id] = make_default_state()
    await update.message.reply_text("🔄 Histórico, contagens, estatísticas e gale resetados.")

async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = get_state(update.effective_chat.id)
    resp = apply_undo(s)
    await update.message.reply_text(resp, parse_mode=ParseMode.HTML)

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
    resp = apply_spin(s, n)
    await update.message.reply_text(resp, parse_mode=ParseMode.HTML)

# -----------------------------------------------------------------------------
# PTB Application (será criado no startup)
# -----------------------------------------------------------------------------
application: Optional[Application] = None

def ensure_ptb_application(bot_token: str) -> Application:
    """Cria e configura a Application do PTB apenas uma vez."""
    global application
    if application is not None:
        return application
    application = Application.builder().token(bot_token).build()
    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("status", status_cmd))
    application.add_handler(CommandHandler("reset", reset_cmd))
    application.add_handler(CommandHandler("undo", undo_cmd))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_number_message))
    return application

# -----------------------------------------------------------------------------
# VARIÁVEIS DE AMBIENTE (lidas/validadas no startup)
# -----------------------------------------------------------------------------
BOT_TOKEN: Optional[str] = None
BASE_URL: Optional[str] = None
WEBHOOK_SECRET: Optional[str] = None

# -----------------------------------------------------------------------------
# FASTAPI LIFECYCLE
# -----------------------------------------------------------------------------
@app.on_event("startup")
async def on_startup():
    global BOT_TOKEN, BASE_URL, WEBHOOK_PATH, WEBHOOK_SECRET, application

    # Ler envs aqui (para não quebrar o import do módulo)
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    BASE_URL = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
    # Se o usuário fornecer WEBHOOK_PATH agora, honre; senão, mantenha o que já foi definido
    env_wh = os.getenv("WEBHOOK_PATH")
    if env_wh:
        WEBHOOK_PATH = env_wh  # type: ignore

    if not BOT_TOKEN:
        log.critical("BOT_TOKEN ausente. Defina a env var no Render.")
        raise RuntimeError("BOT_TOKEN ausente.")
    if not BASE_URL:
        log.critical("PUBLIC_URL/RENDER_EXTERNAL_URL ausente. Defina PUBLIC_URL no Render.")
        raise RuntimeError("PUBLIC_URL ausente.")

    # Garantir PTB Application configurada
    application = ensure_ptb_application(BOT_TOKEN)

    # Registrar webhook
    webhook_url = f"{BASE_URL.rstrip('/')}/{WEBHOOK_PATH}"
    await application.bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET,
        drop_pending_updates=True
    )
    info = await application.bot.get_webhook_info()
    log.info("Webhook info: url=%s, has_custom_cert=%s, pending_update_count=%s",
             info.url, info.has_custom_certificate, info.pending_update_count)
    if info.last_error_message:
        log.warning("Último erro do Telegram: %s (há %ss)", info.last_error_message, info.last_error_date)

    await application.initialize()
    await application.start()
    log.info("Application started (PTB + FastAPI). Path=/%s  Build=%s", WEBHOOK_PATH, BUILD_TAG)

@app.on_event("shutdown")
async def on_shutdown():
    # NÃO deletar o webhook — mantém ativo entre reinícios rápidos do Render
    if application is not None:
        await application.stop()
        await application.shutdown()
    log.info("Application stopped. Build=%s", BUILD_TAG)

# -----------------------------------------------------------------------------
# ROTAS FASTAPI
# -----------------------------------------------------------------------------
@app.get("/health", response_class=PlainTextResponse)
async def health():
    return "ok"

# Importante: o caminho do webhook é definido em tempo de import via WEBHOOK_PATH.
# Se mudar WEBHOOK_PATH nas envs após o primeiro import, faça redeploy.
@app.post(f"/{WEBHOOK_PATH}")
async def telegram_webhook(request: Request):
    try:
        # Validação opcional do secret
        if WEBHOOK_SECRET:
            secret_header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if secret_header != WEBHOOK_SECRET:
                return Response(status_code=status.HTTP_401_UNAUTHORIZED)

        data = await request.json()
        if application is None:
            log.error("PTB application não inicializada.")
            return Response(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)

        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return Response(status_code=status.HTTP_200_OK)
    except Exception as e:
        log.exception("Webhook handler exception: %s", e)
        # 200 evita retry agressivo do Telegram e não derruba o servidor
        return Response(status_code=status.HTTP_200_OK)
