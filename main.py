import os
import re
import asyncio
import datetime  # manter este import (não use "from datetime import ...")
import logging
from html import escape as esc

from aiohttp import web
import redis.asyncio as redis

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

# =========================
# CONFIG / ENV
# =========================
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
PUBLIC_URL = (os.getenv("PUBLIC_URL") or "").strip().rstrip("/")
TG_PATH = (os.getenv("TG_PATH", "tg") or "tg").strip()
REDIS_URL = (os.getenv("REDIS_URL") or "").strip()

TRIAL_MAX_HITS = int((os.getenv("TRIAL_MAX_HITS") or "10").strip())
SUB_DAYS = int((os.getenv("SUB_DAYS") or "7").strip())
PAYWALL_OFF = ((os.getenv("PAYWALL_OFF") or "0").strip() == "1")

# Estratégia conservadora (parâmetros)
CONFIRM_REC = int((os.getenv("CONFIRM_REC") or "6").strip())          # janela curtíssima
REQUIRE_STREAK1 = int((os.getenv("REQUIRE_STREAK1") or "2").strip())  # ocorrências mínimas na curtíssima
MIN_GAP1 = int((os.getenv("MIN_GAP1") or "2").strip())                # gap mínimo (líder - 2ª) p/ 1 dúzia
MIN_GAP2 = int((os.getenv("MIN_GAP2") or "1").strip())                # gap mínimo (2ª - 3ª) p/ 2 dúzias
COOLDOWN_MISSES = int((os.getenv("COOLDOWN_MISSES") or "2").strip())  # freio após erros seguidos
GAP_BONUS_ON_COOLDOWN = int((os.getenv("GAP_BONUS_ON_COOLDOWN") or "1").strip())

PAYMENT_LINK = (os.getenv("PAYMENT_LINK") or "https://mpago.li/1cHXVHc").strip()

# Limites de entrada/antiflood
MAX_NUMS_PER_MSG = int((os.getenv("MAX_NUMS_PER_MSG") or "40").strip())   # corta exageros por mensagem
CHUNK = int((os.getenv("CHUNK") or "12").strip())                          # processa em blocos para não travar
MIN_GAP_SECONDS = float((os.getenv("MIN_GAP_SECONDS") or "0.35").strip())  # antiflood por usuário

APP_VERSION = "unificado-v2.2-IA-estrategica"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
log = logging.getLogger("main")

# =========================
# REDIS
# =========================
rds = None
if REDIS_URL:
    try:
        rds = redis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
            socket_keepalive=True
        )
    except Exception as e:
        log.error(f"[BOOT] Falha ao inicializar Redis: {e}")

async def _safe_redis(coro, default=None, note=""):
    try:
        return await coro
    except Exception as e:
        log.error(f"[REDIS-FAIL] {note}: {e}")
        return default

# =========================
# ESTADO LOCAL
# =========================
STATE = {}  # uid -> {modo,K,N,hist,pred_queue,stats,last_touch}
def ensure_user(uid: int):
    if uid not in STATE:
        STATE[uid] = {
            "modo": 2,  # padrão: 2 dúzias (conservador)
            "K": 5,
            "N": 80,
            "hist": [],
            "pred_queue": [],
            "stats": {"hits": 0, "misses": 0, "streak_hit": 0, "streak_miss": 0},
            "last_touch": None,
        }

# =========================
# UTIL + ENVIO
# =========================
TELEGRAM_LIMIT = 4096
def fit_telegram(html: str) -> str:
    return html if len(html) <= TELEGRAM_LIMIT else html[:TELEGRAM_LIMIT-1] + "…"

async def send_html(update: Update, html: str):
    await asyncio.sleep(0.05)  # pequeno debounce anti-flood
    try:
        await update.message.reply_text(fit_telegram(html), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except BadRequest as e:
        # Se alguma tag quebrou ou estourou, manda texto puro
        log.error(f"[SEND_HTML] BadRequest: {e}. Enviando versão 'plain'.")
        try:
            plain = re.sub(r"<[^>]*>", "", html)
            await update.message.reply_text(fit_telegram(plain))
        except Exception as e2:
            log.error(f"[SEND_HTML] Fallback falhou: {e2}")

def today() -> datetime.date:
    return datetime.date.today()

def pct(h, m) -> float:
    t = h + m
    return round((h * 100 / t), 1) if t else 0.0

def get_duzia(n: int):
    if 1 <= n <= 12: return "D1"
    if 13 <= n <= 24: return "D2"
    if 25 <= n <= 36: return "D3"
    return None

def _contagens_duzias(nums):
    c = {"D1": 0, "D2": 0, "D3": 0}
    for n in nums:
        d = get_duzia(n)
        if d: c[d] += 1
    return c

# =========================
# FORMATAÇÃO "IA ESTRATÉGICA"
# =========================
def fmt_start(uid: int, hits_left: int, trial_max: int) -> str:
    return f"""
🤖 <b>IA Estratégica — Analista de Dúzias</b>
━━━━━━━━━━━━━━━━━━
🆔 <b>ID:</b> <code>{esc(str(uid))}</code>
🆓 <b>Teste:</b> {hits_left} / {trial_max} acertos restantes
━━━━━━━━━━━━━━━━━━
📌 <b>Comandos</b>:
<code>/mode 1</code> — 1 dúzia   •   <code>/mode 2</code> — 2 dúzias
<code>/k 5</code> — janela recente (K)   •   <code>/n 80</code> — histórico (N)
<code>/stats</code> — estatísticas   •   <code>/reset</code> — limpar histórico
<code>/assinar</code> — pagar   •   <code>/status</code> — validade
━━━━━━━━━━━━━━━━━━
💡 <i>Dica:</i> Envie um número por mensagem para apuração de acertos precisa.
""".strip()

def fmt_paywall(link: str, days: int) -> str:
    return f"""
💳 <b>Seu teste grátis terminou</b>
━━━━━━━━━━━━━━━━━━
Para continuar usando o <b>Analista de Dúzias</b> por <b>{days} dias</b>:
✅ Acesso ilimitado
✅ Estratégia conservadora validada
✅ Justificativas técnicas detalhadas

➡️ <a href="{esc(link)}">Clique aqui para pagar</a>
""".strip()

def fmt_recommendation(duzias: list[str], excl: str | None, justificativa: str,
                       pendentes: int, hits_left: int, trial_max: int) -> str:
    dz = " + ".join(f"<b>{d}</b>" for d in duzias)
    excl_txt = f"   → 🚫 Exclusão estratégica: <b>{excl}</b>\n" if excl else ""
    return f"""
🤖 <b>IA Estratégica — Análise Concluída</b>
━━━━━━━━━━━━━━━━━━
📡 <b>Varredura do histórico concluída</b>
📊 <b>Sugestão de entrada</b>:
   → 🎯 Recomendação: {dz}
{excl_txt}
🧠 <b>Raciocínio da IA</b>:
{esc(justificativa)}

📌 <b>Status da execução</b>: {pendentes} jogada(s) pendente(s)
🆓 <b>Modo teste</b>: {hits_left}/{trial_max} acertos restantes
━━━━━━━━━━━━━━━━━━
""".strip()

def fmt_no_recommendation(motivo: str, justificativa: str,
                          hits_left: int, trial_max: int) -> str:
    return f"""
🤖 <b>IA Estratégica — Monitoramento Ativo</b>
━━━━━━━━━━━━━━━━━━
📡 <b>Varredura do histórico concluída</b>
⚠️ <b>Nenhuma configuração de vantagem detectada</b>

📎 <b>Motivo técnico</b>: {esc(motivo)}
🧠 <b>Raciocínio da IA</b>:
{esc(justificativa)}

🆓 <b>Modo teste</b>: {hits_left}/{trial_max} acertos restantes
━━━━━━━━━━━━━━━━━━
""".strip()

# =========================
# TRIAL / PAYWALL
# =========================
async def get_active_until(user_id: int):
    if not rds: return None
    try:
        v = await rds.get(f"sub:{user_id}")
        return datetime.date.fromisoformat(v) if v else None
    except Exception:
        return None

async def set_active_days(user_id: int, days: int = SUB_DAYS):
    if not rds: return None
    dt = today() + datetime.timedelta(days=days)
    await _safe_redis(rds.set(f"sub:{user_id}", dt.isoformat()), note="set_active_days")
    return dt

async def get_trial_hits(user_id: int) -> int:
    if not rds: return 0
    v = await _safe_redis(rds.get(f"trial:hits:{user_id}"), default="0", note="get_trial_hits")
    try:
        return int(v or 0)
    except:  # noqa
        return 0

async def incr_trial_hits(user_id: int, inc: int = 1) -> int:
    if not rds: return 0
    return await _safe_redis(rds.incrby(f"trial:hits:{user_id}", inc), default=0, note="incr_trial_hits")

async def require_active_or_trial(update: Update) -> bool:
    if PAYWALL_OFF:
        return True
    uid = update.effective_user.id
    paid = await _safe_redis(get_active_until(uid), default=None, note="get_active_until@require")
    if rds and paid is None:
        # Redis caiu — não travar a conversa
        log.warning("[PAYWALL-SOFT] Redis indisponível; liberando esta mensagem.")
        return True
    if paid and paid >= today():
        return True
    hits = await get_trial_hits(uid)
    if hits < TRIAL_MAX_HITS:
        return True
    # bloqueia
    await send_html(update, fmt_paywall(PAYMENT_LINK, SUB_DAYS))
    return False

# =========================
# ESTRATÉGIA CONSERVADORA
# =========================
def escolher_1_duzia(hist, K, stats):
    if not hist:
        return (False, None, {}, "Histórico insuficiente")
    rec = hist[-K:]  # últimos K
    c_rec = _contagens_duzias(rec)
    c_glb = _contagens_duzias(hist)

    ordem = sorted(c_rec.items(), key=lambda x: (-x[1], -c_glb[x[0]]))
    d_top, v_top = ordem[0]
    d_second, v_second = ordem[1]
    gap = v_top - v_second

    short = hist[-CONFIRM_REC:] if len(hist) >= CONFIRM_REC else hist[:]
    c_short = _contagens_duzias(short)
    confirm_ok = c_short[d_top] >= REQUIRE_STREAK1
    min_gap = MIN_GAP1 + (GAP_BONUS_ON_COOLDOWN if stats["streak_miss"] >= COOLDOWN_MISSES else 0)

    dbg = {"rec": c_rec, "glb": c_glb, "short": c_short, "top": d_top, "second": d_second, "gap": gap, "min_gap": min_gap, "confirm_ok": confirm_ok}

    if not confirm_ok:
        return (False, None, dbg, f"Confirmação curta insuficiente ({c_short[d_top]}/{REQUIRE_STREAK1})")
    if gap < min_gap:
        return (False, None, dbg, f"Gap insuficiente (gap={gap} < {min_gap})")
    return (True, d_top, dbg, "Apto")

def escolher_2_duzias(hist, K, stats):
    if not hist:
        return (False, [], None, {}, "Histórico insuficiente")
    rec = hist[-K:]
    c_rec = _contagens_duzias(rec)
    c_glb = _contagens_duzias(hist)

    ordem = sorted(c_rec.items(), key=lambda x: (-x[1], -c_glb[x[0]]))
    d1, v1 = ordem[0]
    d2, v2 = ordem[1]
    d3, v3 = ordem[2]
    excl = d3
    gap23 = v2 - v3

    short = hist[-CONFIRM_REC:] if len(hist) >= CONFIRM_REC else hist[:]
    c_short = _contagens_duzias(short)
    confirm_ok = (c_short[d1] >= 1) or (c_short[d2] >= 1)
    min_gap2 = MIN_GAP2 + (GAP_BONUS_ON_COOLDOWN if stats["streak_miss"] >= COOLDOWN_MISSES else 0)

    dbg = {"rec": c_rec, "glb": c_glb, "short": c_short, "top2": [d1, d2], "excl": excl, "gap23": gap23, "min_gap2": min_gap2, "confirm_ok": confirm_ok}

    if not confirm_ok:
        return (False, [], excl, dbg, "Sem presença mínima na janela curtíssima")
    if gap23 < min_gap2:
        return (False, [], excl, dbg, f"Gap23 insuficiente (gap23={gap23} < {min_gap2})")
    return (True, [d1, d2], excl, dbg, "Apto")

# =========================
# JUSTIFICATIVAS (FORMAL/TÉCNICO)
# =========================
def just_apostar_1(dbg):
    gap = dbg.get("gap", "?"); min_gap = dbg.get("min_gap", "?"); c_short = dbg.get("short", {})
    d = dbg.get("top", "")
    return (
        f"Análise de força relativa indica dominância da {d} na janela recente, "
        f"com separação estatística adequada (gap={gap} ≥ {min_gap}) e confirmação na janela curtíssima "
        f"(ocorrências recentes: {c_short.get(d, 0)}). Essa configuração reduz dispersão e sustenta a tomada de posição."
    )

def just_apostar_2(dbg):
    d1, d2 = dbg.get("top2", ["D?", "D?"])
    gap23 = dbg.get("gap23", "?"); min_gap2 = dbg.get("min_gap2", "?")
    excl = dbg.get("excl", "D?")
    return (
        f"A combinação {d1}+{d2} apresenta dominância frente à excluída ({excl}), "
        f"com vantagem mínima entre 2ª e 3ª atendida (gap23={gap23} ≥ {min_gap2}). "
        f"A presença recente em ao menos uma das selecionadas valida a entrada sob critério conservador."
    )

def just_aguardar_1(dbg, motivo):
    gap = dbg.get("gap", "?"); min_gap = dbg.get("min_gap", "?"); d = dbg.get("top", "")
    base = "A recomendação foi postergada por insuficiência de evidência robusta no curto prazo. "
    if "Confirmação" in motivo or "curta" in motivo:
        return base + (
            f"A {d} não alcançou o mínimo de ocorrências exigido na janela curtíssima, "
            f"impedindo caracterização de tendência confiável no momento."
        )
    if "Gap" in motivo or "gap" in motivo:
        return base + (
            f"A separação entre a líder e a segunda colocada é inferior ao limiar (gap={gap} < {min_gap}), "
            f"indicando equilíbrio técnico e risco de reversão."
        )
    return base + "O cenário permanece difuso entre as dúzias, recomendando observação adicional."

def just_aguardar_2(dbg, motivo):
    gap23 = dbg.get("gap23", "?"); min_gap2 = dbg.get("min_gap2", "?"); excl = dbg.get("excl", "D?")
    base = "Sinal contido por ausência de dominância estatística suficiente entre as três dúzias. "
    if "presença" in motivo or "mínima" in motivo:
        return base + (
            "A janela curtíssima não registrou presença suficiente nas candidatas, "
            "reduzindo a confiabilidade de continuidade no próximo giro."
        )
    if "gap23" in motivo or "Gap23" in motivo or "Gap" in motivo:
        return base + (
            f"A diferença entre a 2ª e a 3ª não atingiu o limiar (gap23={gap23} < {min_gap2}), "
            f"sugerindo instabilidade de padrão."
        )
    return base + f"No momento, a dúzia excluída ({excl}) não se distancia o suficiente das selecionadas."

# =========================
# SCORE / PREVISÕES
# =========================
async def score_predictions(uid: int, nums: list[int]) -> bool:
    """Apura acertos/erros consumindo a fila de previsões. Retorna True se o trial estourou aqui."""
    st = STATE[uid]; q = st["pred_queue"]; s = st["stats"]
    paid = await _safe_redis(get_active_until(uid), default=None, note="get_active_until@score")
    on_trial = (not PAYWALL_OFF) and (not paid) and rds
    hit_limit_now = False

    # Consome os números na ordem de chegada
    for n in nums:
        d = get_duzia(n)
        if not d: continue
        if not q: break
        pred = q.pop(0)  # próxima previsão pendente
        hit = d in pred["duzias"]
        if hit:
            s["hits"] += 1; s["streak_hit"] += 1; s["streak_miss"] = 0
            if on_trial and TRIAL_MAX_HITS > 0:
                new_hits = await _safe_redis(incr_trial_hits(uid, 1), default=0, note="incr_trial_hits")
                if new_hits >= TRIAL_MAX_HITS:
                    hit_limit_now = True
        else:
            s["misses"] += 1; s["streak_miss"] += 1; s["streak_hit"] = 0
    return hit_limit_now

# =========================
# HANDLERS — COMANDOS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    uid = update.effective_user.id
    hits = await get_trial_hits(uid) if rds else 0
    hits_left = max(TRIAL_MAX_HITS - hits, 0)
    await send_html(update, fmt_start(uid, hits_left, TRIAL_MAX_HITS))

async def cmd_assinar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(update, fmt_paywall(PAYMENT_LINK, SUB_DAYS))

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    paid = await _safe_redis(get_active_until(uid), default=None, note="get_active_until@status")
    if paid and paid >= today():
        html = f"""
🤖 <b>IA Estratégica — Status</b>
━━━━━━━━━━━━━━━━━━
🟢 <b>Acesso ativo</b> até <b>{paid.strftime('%d/%m/%Y')}</b>.
""".strip()
        await send_html(update, html)
        return
    hits = await get_trial_hits(uid)
    hits_left = max(TRIAL_MAX_HITS - (hits or 0), 0)
    html = f"""
🤖 <b>IA Estratégica — Status</b>
━━━━━━━━━━━━━━━━━━
🆓 <b>Em teste</b> — {hits_left} / {TRIAL_MAX_HITS} acertos restantes.
""".strip()
    await send_html(update, html)

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active_or_trial(update): return
    ensure_user(update.effective_user.id)
    arg = (context.args[0] if context.args else "2").strip()
    STATE[update.effective_user.id]["modo"] = 1 if arg == "1" else 2
    await send_html(update, f"🎛️ Modo: <b>{'1 dúzia' if arg=='1' else '2 dúzias'}</b>.")

async def cmd_k(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active_or_trial(update): return
    ensure_user(update.effective_user.id)
    if context.args and context.args[0].isdigit():
        STATE[update.effective_user.id]["K"] = max(1, min(50, int(context.args[0])))
    await send_html(update, f"⚙️ K=<b>{STATE[update.effective_user.id]['K']}</b>.")

async def cmd_n(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active_or_trial(update): return
    ensure_user(update.effective_user.id)
    if context.args and context.args[0].isdigit():
        N = max(10, min(1000, int(context.args[0])))
        STATE[update.effective_user.id]["N"] = N
        STATE[update.effective_user.id]["hist"] = STATE[update.effective_user.id]["hist"][-N:]
    await send_html(update, f"⚙️ N=<b>{STATE[update.effective_user.id]['N']}</b>.")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active_or_trial(update): return
    ensure_user(update.effective_user.id)
    STATE[update.effective_user.id]["hist"] = []
    STATE[update.effective_user.id]["pred_queue"] = []
    await send_html(update, "🧹 Histórico limpo.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active_or_trial(update): return
    ensure_user(update.effective_user.id)
    s = STATE[update.effective_user.id]["stats"]
    p = len(STATE[update.effective_user.id]["pred_queue"])
    html = (
        "📈 <b>IA Estratégica — Resultados</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"• ✅ Acertos: <b>{s['hits']}</b>\n"
        f"• ❌ Erros: <b>{s['misses']}</b>\n"
        f"• 🎯 Taxa: <b>{pct(s['hits'], s['misses'])}%</b>\n"
        f"• 🔁 Pendentes: <b>{p}</b>"
    )
    await send_html(update, html)

# =========================
# HANDLER PRINCIPAL (TEXTO)
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    uid = update.effective_user.id

    # Anti-flood por usuário (usa datetime.datetime.utcnow())
    now = datetime.datetime.utcnow()
    last = STATE[uid]["last_touch"]
    if last and (now - last).total_seconds() < MIN_GAP_SECONDS:
        return
    STATE[uid]["last_touch"] = now

    txt = (update.message.text or "").strip()
    nums = [int(x) for x in re.findall(r"\d+", txt)]
    if not nums:
        await send_html(update, "Envie números (ex.: <code>32 19 33 12 8</code>).")
        return

    # 1) Apura previsões pendentes em blocos (evita travar)
    nums = nums[:MAX_NUMS_PER_MSG]
    hit_limit_now = False
    for i in range(0, len(nums), CHUNK):
        bloc = nums[i:i+CHUNK]
        if await score_predictions(uid, bloc):
            hit_limit_now = True
        await asyncio.sleep(0.05)
    if hit_limit_now and not PAYWALL_OFF:
        await send_html(update, fmt_paywall(PAYMENT_LINK, SUB_DAYS))
        return

    # 2) Paywall/trial
    if not await require_active_or_trial(update):
        return

    # 3) Atualiza histórico global do usuário
    st = STATE[uid]
    st["hist"].extend(nums)
    st["hist"] = st["hist"][-st["N"]:]
    K = st["K"]
    s = st["stats"]

    # 4) Executa estratégia (modo conservador)
    if st["modo"] == 1:
        ok, duzia, dbg, motivo = escolher_1_duzia(st["hist"], K, s)
        hits = await get_trial_hits(uid) if rds else 0
        hits_left = max(TRIAL_MAX_HITS - hits, 0)

        if not ok:
            jus = just_aguardar_1(dbg, motivo)
            html = fmt_no_recommendation(motivo, jus, hits_left, TRIAL_MAX_HITS)
            await send_html(update, html)
            return

        # Registrar previsão pendente
        st["pred_queue"].append({"modo": 1, "duzias": [duzia]})
        pend = len(st["pred_queue"])
        jus = just_apostar_1(dbg)
        html = fmt_recommendation([duzia], None, jus, pend, hits_left, TRIAL_MAX_HITS)
        await send_html(update, html)

    else:
        ok, duzias, excl, dbg, motivo = escolher_2_duzias(st["hist"], K, s)
        hits = await get_trial_hits(uid) if rds else 0
        hits_left = max(TRIAL_MAX_HITS - hits, 0)

        if not ok:
            jus = just_aguardar_2(dbg, motivo)
            html = fmt_no_recommendation(motivo, jus, hits_left, TRIAL_MAX_HITS)
            await send_html(update, html)
            return

        st["pred_queue"].append({"modo": 2, "duzias": duzias})
        pend = len(st["pred_queue"])
        jus = just_apostar_2(dbg)
        html = fmt_recommendation(duzias, excl, jus, pend, hits_left, TRIAL_MAX_HITS)
        await send_html(update, html)

# =========================
# AIOHTTP + TELEGRAM (WEBHOOK)
# =========================
application = Application.builder().token(TELEGRAM_TOKEN).build()

# Comandos
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("assinar", cmd_assinar))
application.add_handler(CommandHandler("status", cmd_status))
application.add_handler(CommandHandler("mode", cmd_mode))
application.add_handler(CommandHandler("k", cmd_k))
application.add_handler(CommandHandler("n", cmd_n))
application.add_handler(CommandHandler("reset", cmd_reset))
application.add_handler(CommandHandler("stats", cmd_stats))

# Texto
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# Web app
aio = web.Application()

async def tg_handler(request: web.Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response(text="ok")

async def payments_handler(request: web.Request):
    # Webhook opcional de pagamento: espera JSON {"status":"paid","user_id":123}
    data = await request.json()
    if data.get("status") == "paid" and "user_id" in data:
        uid = int(data["user_id"])
        dt = await set_active_days(uid, SUB_DAYS)
        return web.json_response({"ok": True, "active_until": dt.isoformat() if dt else None})
    return web.json_response({"ok": False})

async def health_handler(request: web.Request):
    return web.Response(text="ok")

async def root_handler(request: web.Request):
    return web.Response(text="ok")

aio.router.add_post(f"/{TG_PATH}", tg_handler)
aio.router.add_post("/payments/webhook", payments_handler)
aio.router.add_get("/health", health_handler)
aio.router.add_get("/", root_handler)

async def on_startup(app: web.Application):
    if (not TELEGRAM_TOKEN) or ("\n" in TELEGRAM_TOKEN) or (" " in TELEGRAM_TOKEN):
        raise RuntimeError("TELEGRAM_TOKEN inválido (vazio, com espaço ou quebra de linha). Corrija nas Environment Variables.")
    print(f"🚀 {APP_VERSION} | PUBLIC_URL={PUBLIC_URL} | TG_PATH=/{TG_PATH} | TRIAL_MAX_HITS={TRIAL_MAX_HITS}")
    await application.initialize()
    await application.start()
    if PUBLIC_URL:
        hook_url = f"{PUBLIC_URL}/{TG_PATH}"
        await application.bot.set_webhook(url=hook_url, drop_pending_updates=True)
        print(f"✅ Webhook setado em {hook_url}")
    else:
        print("⚠️ Defina PUBLIC_URL para habilitar o webhook.")

async def on_cleanup(app: web.Application):
    try:
        await application.bot.delete_webhook()
    except Exception:
        pass
    await application.stop()
    await application.shutdown()

aio.on_startup.append(on_startup)
aio.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    web.run_app(aio, host="0.0.0.0", port=port)
