import os
import re
import asyncio
import datetime  # manter este import (não use "from datetime import ...")
import logging
from html import escape as esc

from aiohttp import web
import redis.asyncio as redis
import sqlite3

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

# ===== Parâmetros da ESTRATÉGIA ULTRA-CONSERVADORA =====
# (você pode sobrescrever via ENV, se quiser)
W = int((os.getenv("STRAT_W") or "36").strip())                 # janela de análise (reinicia no zero)
Z_ALPHA = float((os.getenv("STRAT_Z_ALPHA") or "1.96").strip())  # z mínimo
PVAL_MAX = float((os.getenv("STRAT_PVAL_MAX") or "0.05").strip())# p-valor máx no qui-quadrado (gl=2)
CUSUM_H = float((os.getenv("STRAT_CUSUM_H") or "4").strip())     # limiar de disparo do CUSUM
P1 = float((os.getenv("STRAT_P1") or "0.433").strip())           # hipótese de viés p1 (p0+Δ)
P0 = 1.0/3.0

# UI / limites
MAX_NUMS_PER_MSG = int((os.getenv("MAX_NUMS_PER_MSG") or "40").strip())
CHUNK = int((os.getenv("CHUNK") or "12").strip())
MIN_GAP_SECONDS = float((os.getenv("MIN_GAP_SECONDS") or "0.35").strip())

APP_VERSION = "unificado-v3.0-IA-ultra"

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
# ===== SQLite Logs =====
LOG_DB_PATH = os.getenv("LOG_DB_PATH", "logs.db").strip()

def _db_conn():
    conn = sqlite3.connect(LOG_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            kind TEXT NOT NULL,          -- 'signal' | 'settle' | 'error'
            number INTEGER,
            outcome_duzia TEXT,
            recommended TEXT,
            hit INTEGER,
            bankroll REAL,
            stake REAL,
            meta TEXT
        );
        """
    )
    return conn

def log_event(chat_id:int, kind:str, number:int|None=None, outcome_duzia:str|None=None,
              recommended:str|None=None, hit:int|None=None, bankroll:float|None=None,
              stake:float|None=None, meta:str|None=None):
    try:
        conn = _db_conn()
        conn.execute(
            "INSERT INTO logs (ts,chat_id,kind,number,outcome_duzia,recommended,hit,bankroll,stake,meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (datetime.datetime.utcnow().isoformat(), chat_id, kind, number, outcome_duzia, recommended, hit,
             bankroll, stake, meta)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error(f"[LOG] Falha ao gravar log: {e}")

# STATE mantém histórico em DÚZIAS ("D1","D2","D3").
STATE = {}  # uid -> {hist, pred_queue, stats, last_touch, cusum, bankroll, stake}

def ensure_user(uid: int):
    if uid not in STATE:
        STATE[uid] = {
            "hist": [],                 # sequência de 'D1'/'D2'/'D3' (zera ao sair 0)
            "pred_queue": [],          # previsões pendentes (para apuração de acerto)
            "stats": {"hits": 0, "misses": 0, "streak_hit": 0, "streak_miss": 0},
            "last_touch": None,
            "cusum": {"D1": 0.0, "D2": 0.0, "D3": 0.0},
            "bankroll": float(os.getenv("BANKROLL_INIT", "50") or 50),
            "stake": float(os.getenv("STAKE", "1") or 1),
        }

# =========================
# UTIL + ENVIO
# =========================
TELEGRAM_LIMIT = 4096

def fit_telegram(html: str) -> str:
    return html if len(html) <= TELEGRAM_LIMIT else html[:TELEGRAM_LIMIT-1] + "…"

async def send_html(update: Update, html: str):
    await asyncio.sleep(0.05)
    try:
        await update.message.reply_text(fit_telegram(html), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except BadRequest as e:
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

# =========================
# MAPEAMENTO / HISTÓRICO
# =========================

def num_to_duzia(n: int):
    if n == 0:
        return None
    if 1 <= n <= 12:
        return "D1"
    if 13 <= n <= 24:
        return "D2"
    if 25 <= n <= 36:
        return "D3"
    return None

# aceita itens já em 'D1'/'D2'/'D3' ou números crus

def _contagens_duzias(seq):
    c = {"D1": 0, "D2": 0, "D3": 0}
    for x in seq:
        d = x if isinstance(x, str) and x.startswith("D") else num_to_duzia(int(x))
        if d:
            c[d] += 1
    return c

# =========================
# FORMATAÇÃO
# =========================

def fmt_start(uid: int, hits_left: int, trial_max: int) -> str:
    return f"""
🤖 <b>IA Estratégica — Análise de Dúzias (Ultra)</b>
━━━━━━━━━━━━━━━━━━
🆔 <b>ID:</b> <code>{esc(str(uid))}</code>
🆓 <b>Teste:</b> {hits_left} / {trial_max} acertos restantes
━━━━━━━━━━━━━━━━━━
📌 <b>Comandos</b>:
<code>/status</code> — status | <code>/reset</code> — limpar histórico
<code>/stake 1.00</code> — stake | <code>/bank 50</code> — banca
<code>/ultra</code> — aplica thresholds ultra (padrão)
━━━━━━━━━━━━━━━━━━
💡 <i>Dica:</i> Envie números na ordem (ex.: 7 28 25 14). Zero (0) reinicia.
""".strip()


def fmt_paywall(link: str, days: int) -> str:
    return f"""
💳 <b>Seu teste grátis terminou</b>
━━━━━━━━━━━━━━━━━━
Para continuar usando o <b>Analista de Dúzias</b> por <b>{days} dias</b>:
✅ Acesso ilimitado
✅ Estratégia ultra-conservadora com filtros estatísticos
✅ Justificativas técnicas

➡️ <a href="{esc(link)}">Clique aqui para pagar</a>
""".strip()


def fmt_recommendation(duzias, justificativa, pendentes, hits_left, trial_max, banca, stake):
    dz = " + ".join(f"<b>{d}</b>" for d in duzias)
    return f"""
🤖 <b>IA Estratégica — Sinal de Entrada</b>
━━━━━━━━━━━━━━━━━━
🎯 <b>Recomendação:</b> {dz}
🧠 <b>Justificativa:</b>
{esc(justificativa)}

💰 <b>Stake:</b> R$ {stake:.2f} | <b>Banca:</b> R$ {banca:.2f}
🔁 <b>Pendentes:</b> {pendentes}
🆓 <b>Teste:</b> {hits_left}/{trial_max} acertos restantes
━━━━━━━━━━━━━━━━━━
""".strip()


def fmt_no_recommendation(motivo: str, justificativa: str, hits_left: int, trial_max: int, banca: float):
    return f"""
🤖 <b>IA Estratégica — Monitoramento</b>
━━━━━━━━━━━━━━━━━━
⚠️ <b>Sem vantagem confirmada</b>
📎 <b>Motivo técnico:</b> {esc(motivo)}
🧠 <b>Raciocínio:</b>
{esc(justificativa)}

💰 <b>Banca:</b> R$ {banca:.2f}
🆓 <b>Teste:</b> {hits_left}/{trial_max}
━━━━━━━━━━━━━━━━━━
""".strip()

# =========================
# TRIAL / PAYWALL
# =========================
PAYMENT_LINK = (os.getenv("PAYMENT_LINK") or "https://mpago.li/1cHXVHc").strip()

async def get_active_until(user_id: int):
    if not rds:
        return None
    try:
        v = await rds.get(f"sub:{user_id}")
        return datetime.date.fromisoformat(v) if v else None
    except Exception:
        return None

async def set_active_days(user_id: int, days: int = SUB_DAYS):
    if not rds:
        return None
    dt = today() + datetime.timedelta(days=days)
    await _safe_redis(rds.set(f"sub:{user_id}", dt.isoformat()), note="set_active_days")
    return dt

async def get_trial_hits(user_id: int) -> int:
    if not rds:
        return 0
    v = await _safe_redis(rds.get(f"trial:hits:{user_id}"), default="0", note="get_trial_hits")
    try:
        return int(v or 0)
    except:  # noqa
        return 0

async def incr_trial_hits(user_id: int, inc: int = 1) -> int:
    if not rds:
        return 0
    return await _safe_redis(rds.incrby(f"trial:hits:{user_id}", inc), default=0, note="incr_trial_hits")

async def require_active_or_trial(update: Update) -> bool:
    if PAYWALL_OFF:
        return True
    uid = update.effective_user.id
    paid = await _safe_redis(get_active_until(uid), default=None, note="get_active_until@require")
    if rds and paid is None:
        log.warning("[PAYWALL-SOFT] Redis indisponível; liberando esta mensagem.")
        return True
    if paid and paid >= today():
        return True
    hits = await get_trial_hits(uid)
    if hits < TRIAL_MAX_HITS:
        return True
    await send_html(update, fmt_paywall(PAYMENT_LINK, SUB_DAYS))
    return False

# =========================
# ESTATÍSTICA — z, χ² (gl=2), Wilson, CUSUM
# =========================

def _wilson_lower(phat: float, w: int, z: float) -> float:
    if w <= 0:
        return 0.0
    denom = 1.0 + (z*z)/w
    center = phat + (z*z)/(2.0*w)
    rad = z * ((phat*(1.0-phat)/w + (z*z)/(4.0*w*w)) ** 0.5)
    return (center - rad) / denom


def _chi2_p_gl2(chi2: float) -> float:
    # gl=2 => p = exp(-chi2/2)
    try:
        import math
        return math.exp(-chi2/2.0)
    except Exception:
        return 1.0


def _window_counts(hist: list[str], w: int):
    window = hist[-w:]
    c = _contagens_duzias(window)
    weff = len(window)
    return window, c, weff


def _z_scores(c: dict, w_eff: int):
    import math
    if w_eff <= 0:
        return {"D1": 0.0, "D2": 0.0, "D3": 0.0}
    denom = (P0*(1.0-P0)/w_eff) ** 0.5
    return {d: (c[d]/w_eff - P0)/denom for d in ("D1","D2","D3")}


def _has_seq_or_3in4(hist: list[str], target: str) -> bool:
    if len(hist) >= 3 and all(d == target for d in hist[-3:]):
        return True
    if len(hist) >= 4 and sum(1 for d in hist[-4:] if d == target) >= 3:
        return True
    return False


def _update_cusum(cusum: dict, outcome_duzia: str | None, p1: float = P1) -> dict:
    import math
    out = {}
    for d in ("D1","D2","D3"):
        X = 1 if (outcome_duzia == d) else 0
        inc = (math.log(p1/P0) if X else math.log((1.0-p1)/(1.0-P0)))
        out[d] = max(0.0, cusum.get(d, 0.0) + inc)
    return out

# =========================
# NÚCLEO DA ESTRATÉGIA (Modo 1 dúzia / Modo 2 dúzias)
# =========================

def decidir_1_duzia(state: dict):
    """Retorna (ok, duzia, dbg, motivo). Usa filtros: z/Wilson + χ², sequência e CUSUM."""
    hist = state["hist"]
    if not hist:
        return False, None, {}, "Histórico insuficiente"

    window, C, weff = _window_counts(hist, W)
    Z = _z_scores(C, weff)
    # Qui-quadrado
    exp = weff/3.0 if weff>0 else 0.0
    chi2 = sum(((C[d] - exp)**2)/exp for d in ("D1","D2","D3")) if weff>0 else 0.0
    pval = _chi2_p_gl2(chi2)

    # D* = maior z
    d_star = max(("D1","D2","D3"), key=lambda d: Z[d])
    phat_star = (C[d_star]/weff) if weff>0 else 0.0
    wilsonL = _wilson_lower(phat_star, weff, Z_ALPHA)

    stat_ok = ((Z[d_star] >= Z_ALPHA and pval < PVAL_MAX) or (wilsonL > P0))
    seq_ok = _has_seq_or_3in4(hist, d_star)
    cusum_ok = (state["cusum"].get(d_star, 0.0) > CUSUM_H)

    dbg = {
        "weff": weff, "C": C, "Z": {k: round(v,3) for k,v in Z.items()},
        "chi2": round(chi2,3), "p": round(pval,4), "d*": d_star,
        "wilsonL": round(wilsonL,4), "cusum": {k: round(v,3) for k,v in state["cusum"].items()},
        "seq": seq_ok, "stat": stat_ok, "cusum_ok": cusum_ok
    }

    if stat_ok and seq_ok and cusum_ok:
        return True, d_star, dbg, "Apto"

    # Motivo detalhado
    if not stat_ok:
        return False, None, dbg, "Sem confirmação estatística (z/Wilson + χ²)"
    if not seq_ok:
        return False, None, dbg, "Sem sequência (3 seguidas ou 3 em 4)"
    return False, None, dbg, "CUSUM abaixo do limiar"


def decidir_2_duzias(state: dict):
    """Retorna (ok, duzias_list, dbg, motivo). Critério: χ² significativo + (seq OU cusum) em pelo menos 1 das duas com maior z."""
    hist = state["hist"]
    if not hist:
        return False, [], {}, "Histórico insuficiente"

    window, C, weff = _window_counts(hist, W)
    Z = _z_scores(C, weff)
    exp = weff/3.0 if weff>0 else 0.0
    chi2 = sum(((C[d] - exp)**2)/exp for d in ("D1","D2","D3")) if weff>0 else 0.0
    pval = _chi2_p_gl2(chi2)

    orden = sorted(("D1","D2","D3"), key=lambda d: Z[d], reverse=True)
    d1, d2, d3 = orden[0], orden[1], orden[2]

    # Pelo menos uma das duas deve cumprir (seq OU cusum)
    cond_d1 = _has_seq_or_3in4(hist, d1) or (state["cusum"].get(d1,0.0) > CUSUM_H)
    cond_d2 = _has_seq_or_3in4(hist, d2) or (state["cusum"].get(d2,0.0) > CUSUM_H)

    stat_ok = (pval < PVAL_MAX)  # χ² confirma desequilíbrio global
    any_confirm = (cond_d1 or cond_d2)

    dbg = {
        "weff": weff, "C": C, "Z": {k: round(v,3) for k,v in Z.items()},
        "chi2": round(chi2,3), "p": round(pval,4), "top2": [d1, d2], "excl": d3,
        "cusum": {k: round(v,3) for k,v in state["cusum"].items()},
        "d1_seq/cusum": cond_d1, "d2_seq/cusum": cond_d2
    }

    if stat_ok and any_confirm:
        return True, [d1, d2], dbg, "Apto"

    if not stat_ok:
        return False, [], dbg, "χ² não significativo (p ≥ limiar)"
    return False, [], dbg, "Sem sequência/CUSUM nas candidatas"

# =========================
# SCORE / PREVISÕES
# =========================
async def score_predictions(uid: int, nums: list[int]) -> bool:
    """Apura acertos/erros consumindo a fila; retorna True se o trial estourou aqui."""
    st = STATE[uid]
    q = st["pred_queue"]
    s = st["stats"]

    paid = await _safe_redis(get_active_until(uid), default=None, note="get_active_until@score")
    on_trial = (not PAYWALL_OFF) and (not paid) and rds
    hit_limit_now = False

    for n in nums:
        d = num_to_duzia(n)
        if d is None:
            # zero: não apura; apenas continua
            continue
        if not q:
            # nada a apurar
            continue
        pred = q.pop(0)
        hit = (d in pred.get("duzias", []))
        if hit:
            s["hits"] += 1; s["streak_hit"] += 1; s["streak_miss"] = 0
            log_event(uid, kind="settle", number=n, outcome_duzia=d,
                      recommended=",".join(pred.get("duzias", [])), hit=1,
                      bankroll=STATE[uid]["bankroll"], stake=STATE[uid]["stake"])
            if on_trial and TRIAL_MAX_HITS > 0:
                new_hits = await _safe_redis(incr_trial_hits(uid, 1), default=0, note="incr_trial_hits")
                if new_hits >= TRIAL_MAX_HITS:
                    hit_limit_now = True
        else:
            s["misses"] += 1; s["streak_miss"] += 1; s["streak_hit"] = 0
            log_event(uid, kind="settle", number=n, outcome_duzia=d,
                      recommended=",".join(pred.get("duzias", [])), hit=0,
                      bankroll=STATE[uid]["bankroll"], stake=STATE[uid]["stake"])
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

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active_or_trial(update):
        return
    ensure_user(update.effective_user.id)
    st = STATE[update.effective_user.id]
    st["hist"] = []
    st["pred_queue"] = []
    st["cusum"] = {"D1":0.0, "D2":0.0, "D3":0.0}
    await send_html(update, "🧹 Histórico e CUSUM zerados.")

async def cmd_stake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active_or_trial(update):
        return
    ensure_user(update.effective_user.id)
    st = STATE[update.effective_user.id]
    try:
        value = float(context.args[0])
        st["stake"] = max(0.01, value)
        await send_html(update, f"Stake ajustada para R$ {st['stake']:.2f}.")
    except Exception:
        await send_html(update, "Uso: /stake 1.00")

async def cmd_bank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active_or_trial(update):
        return
    ensure_user(update.effective_user.id)
    st = STATE[update.effective_user.id]
    try:
        value = float(context.args[0])
        st["bankroll"] = max(0.0, value)
        await send_html(update, f"Banca ajustada para R$ {st['bankroll']:.2f}.")
    except Exception:
        await send_html(update, "Uso: /bank 50")

async def cmd_ultra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active_or_trial(update):
        return
    await send_html(update, f"Modo <b>ULTRA</b> ativo: W={W}, z≥{Z_ALPHA}, p<{PVAL_MAX}, CUSUM h={CUSUM_H}, p1={P1}.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active_or_trial(update):
        return
    ensure_user(update.effective_user.id)
    s = STATE[update.effective_user.id]["stats"]
    p = len(STATE[update.effective_user.id]["pred_queue"])
    await send_html(update, (
        "📈 <b>IA Estratégica — Resultados</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"• ✅ Acertos: <b>{s['hits']}</b>\n"
        f"• ❌ Erros: <b>{s['misses']}</b>\n"
        f"• 🎯 Taxa: <b>{pct(s['hits'], s['misses'])}%</b>\n"
        f"• 🔁 Pendentes: <b>{p}</b>"
    ))

# =========================
# HANDLER PRINCIPAL (TEXTO)
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    uid = update.effective_user.id

    # Anti-flood por usuário
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

    # 1) Apura previsões pendentes
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

    # 3) Atualiza histórico (em dúzias), reseta no zero e atualiza CUSUM
    st = STATE[uid]
    for n in nums:
        d = num_to_duzia(n)
        if d is None:  # zero
            st["hist"] = []
            st["cusum"] = {"D1":0.0, "D2":0.0, "D3":0.0}
        else:
            st["hist"].append(d)
            st["hist"] = st["hist"][-max(W*5, 200):]  # histórico longo suficiente (capado)
            st["cusum"] = _update_cusum(st["cusum"], d, P1)

    # 4) Rodar decisão conforme modo do usuário
    mode = st.get("mode", "1d")
    if mode == "2d":
        ok, duzias, dbg, motivo = decidir_2_duzias(st)
    else:
        ok, d1, dbg, motivo = decidir_1_duzia(st)
        duzias = [d1] if d1 else []
        dbg = dbg

    hits = await get_trial_hits(uid) if rds else 0
    hits_left = max(TRIAL_MAX_HITS - hits, 0)

    if not ok:
        jus = (
            f"Janela efetiva={dbg.get('weff',0)}; Contagens={dbg.get('C')}; z={dbg.get('Z')}
"
            f"χ²={dbg.get('chi2')} (p={dbg.get('p')}); WilsonL={dbg.get('wilsonL', '—')}; "
            f"CUSUM={dbg.get('cusum')}; Extra={dbg.get('top2', '') or dbg.get('d*', '')}"
        )
        await send_html(update, fmt_no_recommendation(motivo, jus, hits_left, TRIAL_MAX_HITS, st["bankroll"]))
        return

    # Registrar previsão pendente
    st["pred_queue"].append({"duzias": duzias})
    pend = len(st["pred_queue"])
    # log do sinal
    try:
        log_event(uid, kind="signal", number=None, outcome_duzia=None,
                  recommended=",".join(duzias), hit=None,
                  bankroll=st["bankroll"], stake=st["stake"], meta=str(dbg))
    except Exception:
        pass

    if mode == "2d":
        jus = (
            f"Top2={dbg['top2']} com χ²={dbg['chi2']} (p={dbg['p']}). "
            f"CUSUM={dbg['cusum']} | Cond1={dbg['d1_seq/cusum']} Cond2={dbg['d2_seq/cusum']}"
        )
    else:
        d1 = duzias[0]
        jus = (
            f"D*={d1} com z={dbg['Z'][d1]:.3f}; χ²={dbg['chi2']} (p={dbg['p']}). "
            f"WilsonL={dbg['wilsonL']}; Seq={dbg['seq']}; CUSUM={dbg['cusum'][d1]:.3f}"
        )

    await send_html(update, fmt_recommendation(duzias, jus, pend, hits_left, TRIAL_MAX_HITS, st["bankroll"], st["stake"]))

# =========================
# AIOHTTP + TELEGRAM (WEBHOOK)
# =========================
application = Application.builder().token(TELEGRAM_TOKEN).build()

# --- Error handler global (evita que o bot 'morra' silenciosamente) ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("[ERROR] Exception no handler", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Ocorreu um erro interno. Tentando recuperar…")
    except Exception:
        pass

application.add_error_handler(error_handler)

# Comandos
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("assinar", cmd_assinar))
application.add_handler(CommandHandler("status", cmd_status))
application.add_handler(CommandHandler("reset", cmd_reset))
application.add_handler(CommandHandler("stats", cmd_stats))
application.add_handler(CommandHandler("stake", cmd_stake))
application.add_handler(CommandHandler("bank", cmd_bank))
application.add_handler(CommandHandler("ultra", cmd_ultra))

# Debug/saúde
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_whinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = await application.bot.get_webhook_info()
    await update.message.reply_text(
        f"Webhook: {info.url or '-'}
Pendentes: {info.pending_update_count}
Erro último: {info.last_error_message or '-'}"
    )

application.add_handler(CommandHandler("ping", cmd_ping))
application.add_handler(CommandHandler("whinfo", cmd_whinfo))

# /health: checa Telegram e Redis em tempo real
async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = await application.bot.get_webhook_info()
    ok_redis = True
    if rds:
        try:
            pong = await rds.ping()
            ok_redis = bool(pong)
        except Exception:
            ok_redis = False
    status = (
        f"🩺 <b>Health</b>
"
        f"Webhook: {info.url or '-'}
"
        f"Pendentes: {info.pending_update_count}
"
        f"Último erro: {info.last_error_message or '-'}
"
        f"Redis: {'ok' if ok_redis else 'falhou'}"
    )
    await send_html(update, status)

application.add_handler(CommandHandler("health", cmd_health))

# Texto
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# Web app
aio = web.Application()

async def tg_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.Response(text="bad json", status=400)
    try:
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
    except Exception as e:
        log.exception(f"[TG_HANDLER] Falha ao processar update: {e}")
        return web.Response(text="error", status=500)
    return web.Response(text="ok")

async def payments_handler(request: web.Request):
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "err": "bad json"}, status=400)
    if data.get("status") == "paid" and "user_id" in data:
        uid = int(data["user_id"])
        dt = await set_active_days(uid, SUB_DAYS)
        return web.json_response({"ok": True, "active_until": dt.isoformat() if dt else None})
    return web.json_response({"ok": False})

async def health_handler(request: web.Request):
    return web.Response(text="ok")

async def root_handler(request: web.Request):
    return web.Response(text="ok")

io.router.add_post(f"/{TG_PATH}", tg_handler)
io.router.add_post("/payments/webhook", payments_handler)
io.router.add_get("/health", health_handler)
io.router.add_get("/", root_handler)

async def on_startup(app: web.Application):
    if (not TELEGRAM_TOKEN) or ("
" in TELEGRAM_TOKEN) or (" " in TELEGRAM_TOKEN):
        raise RuntimeError("TELEGRAM_TOKEN inválido (vazio, com espaço ou quebra de linha). Corrija nas Environment Variables.")
    print(f"🚀 {APP_VERSION} | PUBLIC_URL={PUBLIC_URL} | TG_PATH=/{TG_PATH} | TRIAL_MAX_HITS={TRIAL_MAX_HITS}")
    await application.initialize()
    await application.start()
    # Garante que só recebemos tipos de atualização que tratamos
    await application.bot.set_my_commands([
        ("start", "iniciar"), ("status", "ver status"), ("reset", "zerar histórico"),
        ("stats", "resultados"), ("stake", "stake"), ("bank", "banca"), ("ultra", "mostrar thresholds"),
        ("ping", "teste"), ("whinfo", "webhook info"), ("health", "checar saúde")
    ])
    if PUBLIC_URL:
        hook_url = f"{PUBLIC_URL}/{TG_PATH}"
        await application.bot.set_webhook(url=hook_url, drop_pending_updates=True, allowed_updates=["message"])
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

io.on_startup.append(on_startup)
io.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    web.run_app(aio, host="0.0.0.0", port=port)
