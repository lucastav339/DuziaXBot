
import os, re, datetime, logging
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import redis.asyncio as redis
from aiohttp import web

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
TG_PATH = os.getenv("TG_PATH", "tg")
SUB_DAYS = int(os.getenv("SUB_DAYS", "7"))
REDIS_URL = os.getenv("REDIS_URL", "")

TRIAL_MAX_HITS = int(os.getenv("TRIAL_MAX_HITS", "10"))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "0"))
TRIAL_CAP = int(os.getenv("TRIAL_CAP", "0"))
PAYWALL_OFF = os.getenv("PAYWALL_OFF", "0") == "1"

W_RECENT = int(os.getenv("W_RECENT", "3"))
W_GLOBAL = int(os.getenv("W_GLOBAL", "1"))
CONFIRM_REC = int(os.getenv("CONFIRM_REC", "6"))
REQUIRE_STREAK1 = int(os.getenv("REQUIRE_STREAK1", "2"))
MIN_GAP1 = int(os.getenv("MIN_GAP1", "2"))
MIN_GAP2 = int(os.getenv("MIN_GAP2", "1"))
COOLDOWN_MISSES = int(os.getenv("COOLDOWN_MISSES", "2"))
GAP_BONUS_ON_COOLDOWN = int(os.getenv("GAP_BONUS_ON_COOLDOWN", "1"))

APP_VERSION = "unificado-v1.3-trial-hits-conservador"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")

def today():
    return datetime.date.today()

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
    except Exception:
        logging.exception("Falha ao inicializar Redis")
        rds = None

STATE = {}

def ensure_user(uid: int):
    if uid not in STATE:
        STATE[uid] = {"modo":2,"K":5,"N":80,"hist":[], "pred_queue":[], "stats":{"hits":0,"misses":0,"streak_hit":0,"streak_miss":0}}

def get_duzia(n: int):
    if 1 <= n <= 12: return "D1"
    if 13 <= n <= 24: return "D2"
    if 25 <= n <= 36: return "D3"
    return None

def _contagens_duzias(nums):
    c = {"D1":0, "D2":0, "D3":0}
    for n in nums:
        d = get_duzia(n)
        if d: c[d]+=1
    return c

def escolher_1_duzia_conservador(hist, K, stats):
    if not hist:
        return (False, None, {}, "Histórico vazio")
    recent = hist[:K]
    c_rec = _contagens_duzias(recent)
    c_glb = _contagens_duzias(hist)
    ordem = sorted(c_rec.items(), key=lambda x: (-x[1], -c_glb[x[0]]))
    d_top, v_top = ordem[0]
    d_second, v_second = ordem[1]
    gap = v_top - v_second
    short = hist[:CONFIRM_REC]
    c_short = _contagens_duzias(short)
    confirm_ok = c_short[d_top] >= REQUIRE_STREAK1
    min_gap = MIN_GAP1 + (GAP_BONUS_ON_COOLDOWN if stats["streak_miss"] >= COOLDOWN_MISSES else 0)
    debug = {"rec":c_rec,"glb":c_glb,"short":c_short,"top":d_top,"second":d_second,"gap":gap,
             "require_streak":REQUIRE_STREAK1,"confirm_ok":confirm_ok,"min_gap":min_gap}
    if not confirm_ok:
        return (False, None, debug, f"Sem confirmação recente ({c_short[d_top]}/{REQUIRE_STREAK1})")
    if gap < min_gap:
        return (False, None, debug, f"Vantagem insuficiente (gap={gap} < {min_gap})")
    return (True, d_top, debug, "Sinal confirmado")

def escolher_2_duzias_conservador(hist, K, stats):
    if not hist:
        return (False, [], None, {}, "Histórico vazio")
    c_glb = _contagens_duzias(hist)
    c_rec = _contagens_duzias(hist[:K])
    ordem = sorted(c_rec.items(), key=lambda x: (-x[1], -c_glb[x[0]]))
    d1, v1 = ordem[0]
    d2, v2 = ordem[1]
    d3, v3 = ordem[2]
    excl = d3
    gap23 = v2 - v3
    short = hist[:CONFIRM_REC]
    c_short = _contagens_duzias(short)
    confirm_ok = (c_short[d1] >= 1) or (c_short[d2] >= 1)
    min_gap2 = MIN_GAP2 + (GAP_BONUS_ON_COOLDOWN if stats["streak_miss"] >= COOLDOWN_MISSES else 0)
    debug = {"rec":c_rec,"glb":c_glb,"short":c_short,"top2":[d1,d2],"excl":excl,"gap23":gap23,"min_gap2":min_gap2,"confirm_ok":confirm_ok}
    if not confirm_ok:
        return (False, [], excl, debug, "Sem confirmação curta nas escolhidas")
    if gap23 < min_gap2:
        return (False, [], excl, debug, f"Vantagem insuficiente (gap23={gap23} < {min_gap2})")
    return (True, [d1, d2], excl, debug, "Sinal confirmado")

def pct(h, m):
    t = h + m
    return round((h*100/t), 1) if t else 0.0

async def send_html(update: Update, html: str):
    await update.message.reply_text(html, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def get_active_until(user_id: int):
    if not rds: return None
    try:
        v = await rds.get(f"sub:{user_id}")
        if not v: return None
        return datetime.date.fromisoformat(v)
    except Exception:
        logging.exception("Redis GET falhou em get_active_until")
        return None

async def set_active_days(user_id: int, days: int = SUB_DAYS):
    if not rds: return None
    try:
        dt = today() + datetime.timedelta(days=days)
        await rds.set(f"sub:{user_id}", dt.isoformat())
        return dt
    except Exception:
        logging.exception("Redis SET falhou em set_active_days")
        return None

async def set_trial_start_if_absent(user_id: int):
    if not rds: return None
    try:
        key = f"trial:start:{user_id}"
        exists = await rds.exists(key)
        if not exists:
            await rds.set(key, today().isoformat())
    except Exception:
        logging.exception("Redis falhou em set_trial_start_if_absent")

async def get_trial_hits(user_id: int) -> int:
    if not rds: return 0
    try:
        v = await rds.get(f"trial:hits:{user_id}")
        return int(v) if v is not None else 0
    except Exception:
        logging.exception("Redis GET falhou em get_trial_hits")
        return 0

async def incr_trial_hits(user_id: int, inc: int = 1) -> int:
    if not rds: return 0
    try:
        return await rds.incrby(f"trial:hits:{user_id}", inc)
    except Exception:
        logging.exception("Redis INCR falhou em incr_trial_hits")
        return 0

async def get_trial_used(user_id: int) -> int:
    if not rds: return 0
    try:
        v = await rds.get(f"trial:used:{user_id}")
        return int(v) if v is not None else 0
    except Exception:
        logging.exception("Redis GET falhou em get_trial_used")
        return 0

async def incr_trial_used(user_id: int, inc: int = 1) -> int:
    if not rds: return 0
    try:
        return await rds.incrby(f"trial:used:{user_id}", inc)
    except Exception:
        logging.exception("Redis INCR falhou em incr_trial_used")
        return 0

async def get_trial_info(user_id: int):
    enabled = (TRIAL_MAX_HITS > 0) or (TRIAL_DAYS > 0) or (TRIAL_CAP > 0)
    if not enabled or not rds:
        return {"enabled": False, "hits": 0, "hits_left": None, "start": None, "until": None, "used": 0, "days_left": None, "uses_left": None}
    await set_trial_start_if_absent(user_id)
    hits = await get_trial_hits(user_id)
    hits_left = (TRIAL_MAX_HITS - hits) if (TRIAL_MAX_HITS > 0) else None
    try:
        start = await rds.get(f"trial:start:{user_id}")
        start = (datetime.date.fromisoformat(start) if start else None)
    except Exception:
        logging.exception("Redis GET falhou em trial:start")
        start = None
    until = (start + datetime.timedelta(days=TRIAL_DAYS)) if (start and TRIAL_DAYS > 0) else None
    days_left = (until - today()).days if until else None
    used = await get_trial_used(user_id)
    uses_left = (TRIAL_CAP - used) if (TRIAL_CAP > 0) else None
    return {"enabled": True, "hits": hits, "hits_left": hits_left, "start": start, "until": until, "used": used, "days_left": days_left, "uses_left": uses_left}

def trial_allows(trial: dict) -> bool:
    if not trial.get("enabled"):
        return False
    if trial.get("hits_left") is not None and trial["hits_left"] <= 0:
        return False
    if trial.get("until") is not None and today() > trial["until"]:
        return False
    if trial.get("uses_left") is not None and trial["uses_left"] <= 0:
        return False
    return True

async def score_predictions(uid: int, nums: list[int]) -> bool:
    st = STATE[uid]
    q = st["pred_queue"]
    stats = st["stats"]
    paid = await get_active_until(uid)
    on_trial = (not PAYWALL_OFF) and (not paid) and rds
    hit_limit_now = False

    for n in reversed(nums):
        d = get_duzia(n)
        if d is None:
            continue
        if not q:
            break
        pred = q.pop(0)
        hit = d in pred["duzias"]
        if hit:
            stats["hits"] += 1
            stats["streak_hit"] += 1
            stats["streak_miss"] = 0
            if on_trial and TRIAL_MAX_HITS > 0:
                new_hits = await incr_trial_hits(uid, 1)
                if new_hits >= TRIAL_MAX_HITS:
                    hit_limit_now = True
        else:
            stats["misses"] += 1
            stats["streak_miss"] += 1
            stats["streak_hit"] = 0
    return hit_limit_now

async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(update, f"🧩 <b>Versão</b>: <code>{APP_VERSION}</code>")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    uid = update.effective_user.id
    if not PAYWALL_OFF and rds:
        paid = await get_active_until(uid)
        if not paid:
            await set_trial_start_if_absent(uid)
    trial = await get_trial_info(uid)
    trial_line = ""
    if trial["enabled"]:
        if trial_allows(trial):
            parts = []
            if trial["hits_left"] is not None: parts.append(f"{trial['hits_left']} acerto(s)")
            if trial["days_left"] is not None: parts.append(f"{trial['days_left']} dia(s)")
            if trial["uses_left"] is not None: parts.append(f"{trial['uses_left']} análise(s)")
            saldo = " • ".join(parts) if parts else "ativo"
            trial_line = f"\\n🆓 <b>Teste</b>: {saldo} restante(s)."
        else:
            trial_line = "\\n🆓 <b>Teste</b>: encerrado. Use /assinar para continuar."
    html = (
        "🤖 <b>Analista de Dúzias</b>\\n"
        f"Seu ID: <code>{uid}</code>\\n"
        f"{trial_line}\\n\\n"
        "Envie números (ex.: <code>32 19 33 12 8</code>). Padrão: <b>2 dúzias</b>.\\n\\n"
        "Comandos:\\n"
        "• <code>/mode 1</code> — 1 dúzia | <code>/mode 2</code> — 2 dúzias\\n"
        "• <code>/k 5</code> — janela recente (K)\\n"
        "• <code>/n 80</code> — tamanho do histórico (N)\\n"
        "• <code>/stats</code> — seus acertos | <code>/resetstats</code> — zerar\\n"
        "• <code>/assinar</code> — pagar | <code>/status</code> — validade\\n"
        "• <code>/reset</code> — limpar histórico\\n\\n"
        "💡 Para melhor apuração de acertos, envie <b>um número por mensagem</b>."
    )
    await send_html(update, html)

async def cmd_assinar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = "https://mpago.li/1cHXVHc"
    await send_html(update, "💳 <b>Assinatura</b>\\n"
                            f"Acesso por {SUB_DAYS} dias.\\n\\n"
                            f"➡️ Pague aqui: <a href=\\"{link}\\">Finalizar pagamento</a>\\n"
                            "Assim que aprovado, liberamos automaticamente. Use /status para conferir.")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    paid = await get_active_until(uid)
    if paid and paid >= today():
        await send_html(update, f"🟢 <b>Ativo</b> até <b>{paid.strftime('%d/%m/%Y')}</b>.")
        return
    trial = await get_trial_info(uid)
    if trial["enabled"]:
        if trial_allows(trial):
            parts = []
            if trial["hits_left"] is not None: parts.append(f"{trial['hits_left']} acerto(s)")
            if trial["days_left"] is not None: parts.append(f"{trial['days_left']} dia(s)")
            if trial["uses_left"] is not None: parts.append(f"{trial['uses_left']} análise(s)")
            saldo = " • ".join(parts) if parts else "ativo"
            await send_html(update, f"🆓 <b>Em teste</b> — {saldo} restante(s).")
        else:
            await send_html(update, "🔴 <b>Inativo</b>. Seu período de teste encerrou. Use /assinar para continuar.")
    else:
        await send_html(update, "🔴 <b>Inativo</b>. Use /assinar para liberar seu acesso.")

async def require_active_or_trial(update: Update) -> bool:
    if PAYWALL_OFF:
        return True
    uid = update.effective_user.id
    paid = await get_active_until(uid)
    if paid and paid >= today():
        return True
    trial = await get_trial_info(uid)
    if trial_allows(trial):
        return True
    link = "https://seu-psp.com/pagar/SEU_PRODUTO"
    await send_html(update, "🔒 <b>Seu teste terminou</b>.\\n"
                            f"Para continuar por {SUB_DAYS} dias: <a href=\\"{link}\\">assine aqui</a>.")
    return False

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active_or_trial(update): return
    ensure_user(update.effective_user.id)
    if context.args and context.args[0] == "1":
        STATE[update.effective_user.id]["modo"] = 1
        await send_html(update, "🎛️ Modo: <b>1 dúzia</b>.")
    else:
        STATE[update.effective_user.id]["modo"] = 2
        await send_html(update, "🎛️ Modo: <b>2 dúzias</b>.")

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
        STATE[update.effective_user.id]["hist"] = STATE[update.effective_user.id]["hist"][:N]
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
    trial = await get_trial_info(update.effective_user.id)
    tline = ""
    if trial["enabled"]:
        if trial_allows(trial):
            rem = []
            if trial["hits_left"] is not None: rem.append(f"{trial['hits_left']} acerto(s)")
            if trial["days_left"] is not None: rem.append(f"{trial['days_left']} dia(s)")
            if trial["uses_left"] is not None: rem.append(f"{trial['uses_left']} análise(s)")
            tline = "\\n🆓 Teste: " + (" • ".join(rem) if rem else "ativo")
        else:
            tline = "\\n🆓 Teste: encerrado"
    await send_html(update,
        f"📈 <b>Resultados</b>\\n"
        f"• ✅ Acertos: <b>{s['hits']}</b>\\n"
        f"• ❌ Erros: <b>{s['misses']}</b>\\n"
        f"• 🎯 Taxa: <b>{pct(s['hits'], s['misses'])}%</b>\\n"
        f"• 🔁 Pendentes: <b>{p}</b>\\n"
        f"• 🔥 Streak: <b>{s['streak_hit']}✔️</b> | <b>{s['streak_miss']}❌</b>"
        f"{tline}"
    )

async def debug_tap(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if update and update.message:
            logging.info(f"[DBG] Chat {update.effective_chat.id} -> {update.message.text!r}")
    except Exception:
        logging.exception("Erro no debug_tap")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    import traceback
    tb = "".join(traceback.format_exception(None, context.error, context.error.__traceback__))
    logging.error("PTB ERROR:\\n%s", tb)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ Ocorreu um erro interno. Já registrei nos logs."
            )
    except Exception:
        pass

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (update.message.text or "").strip().lower()
    if txt in ["/start","/assinar","/status","/version"]:
        return
    ensure_user(update.effective_user.id)
    uid = update.effective_user.id

    nums_now = [int(x) for x in re.findall(r"\\d+", update.message.text or "")]
    hit_limit_now = await score_predictions(uid, nums_now)
    if hit_limit_now and not PAYWALL_OFF:
        link = "https://seu-psp.com/pagar/SEU_PRODUTO"
        await send_html(update, "🆓 <b>Período de teste encerrado</b> — você atingiu o limite de "
                                f"<b>{TRIAL_MAX_HITS} acertos</b>.\\n"
                                f"Para continuar por {SUB_DAYS} dias: <a href=\\"{link}\\">assine aqui</a>.")
        return
    if not await require_active_or_trial(update):
        return
    if not nums_now:
        await send_html(update, "Envie números (ex.: <code>32 19 33 12 8</code>) ou <code>/start</code>.")
        return

    st = STATE[uid]
    st["hist"] = (nums_now + st["hist"])[:st["N"]]
    K = st["K"]
    N = len(st["hist"])

    s = STATE[uid]["stats"]
    if st["modo"] == 1:
        ok, duzia, dbg, motivo = escolher_1_duzia_conservador(st["hist"], K, s)
        if not ok:
            html = ("⏸️ <b>Sem aposta agora</b>\\n"
                    f"Motivo: {motivo}\\n"
                    f"🪄 Recentes (K={K}): D1=<b>{dbg['rec']['D1']}</b> • D2=<b>{dbg['rec']['D2']}</b> • D3=<b>{dbg['rec']['D3']}</b>\\n"
                    f"📊 Geral (N={N}): D1=<b>{dbg['glb']['D1']}</b> • D2=<b>{dbg['glb']['D2']}</b> • D3=<b>{dbg['glb']['D3']}</b>\\n"
                    f"🔥 Streak: <b>{s['streak_hit']}✔️</b> | <b>{s['streak_miss']}❌</b>")
            await send_html(update, html)
            return
        st["pred_queue"].append({"modo":1, "duzias":[duzia]})
        pend = len(st["pred_queue"])
        html = (f"🎯 <b>Dúzia</b>: <b>{duzia}</b>  •  ✅ Confirmação OK\\n"
                f"🪄 Recentes (K={K}): D1=<b>{dbg['rec']['D1']}</b> • D2=<b>{dbg['rec']['D2']}</b> • D3=<b>{dbg['rec']['D3']}</b>\\n"
                f"📊 Geral (N={N}): D1=<b>{dbg['glb']['D1']}</b> • D2=<b>{dbg['glb']['D2']}</b> • D3=<b>{dbg['glb']['D3']}</b>\\n"
                f"—\\n"
                f"✅ <b>Acertos</b>: <b>{s['hits']}</b> / <b>{s['hits']+s['misses']}</b> (<b>{pct(s['hits'], s['misses'])}%</b>)  |  🔁 Pendentes: <b>{pend}</b>\\n"
                f"🔥 <b>Streak</b>: <b>{s['streak_hit']}✔️</b> | <b>{s['streak_miss']}❌</b>")
        await send_html(update, html)
    else:
        ok, duzias, excl, dbg, motivo = escolher_2_duzias_conservador(st["hist"], K, s)
        if not ok:
            html = ("⏸️ <b>Sem aposta agora</b>\\n"
                    f"Motivo: {motivo}\\n"
                    f"🪄 Recentes (K={K}): D1=<b>{dbg['rec']['D1']}</b> • D2=<b>{dbg['rec']['D2']}</b> • D3=<b>{dbg['rec']['D3']}</b>\\n"
                    f"📊 Geral (N={N}): D1=<b>{dbg['glb']['D1']}</b> • D2=<b>{dbg['glb']['D2']}</b> • D3=<b>{dbg['glb']['D3']}</b>\\n"
                    f"🔥 Streak: <b>{s['streak_hit']}✔️</b> | <b>{s['streak_miss']}❌</b>")
            await send_html(update, html)
            return
        st["pred_queue"].append({"modo":2, "duzias":duzias})
        pend = len(st["pred_queue"])
        html = (f"🎯 <b>Dúzias</b>: <b>{duzias[0]}</b> + <b>{duzias[1]}</b>  |  🚫 Excluída: <b>{excl}</b>  •  ✅ Confirmação OK\\n"
                f"🪄 Recentes (K={K}): D1=<b>{dbg['rec']['D1']}</b> • D2=<b>{dbg['rec']['D2']}</b> • D3=<b>{dbg['rec']['D3']}</b>\\n"
                f"📊 Geral (N={N}): D1=<b>{dbg['glb']['D1']}</b> • D2=<b>{dbg['glb']['D2']}</b> • D3=<b>{dbg['glb']['D3']}</b>\\n"
                f"—\\n"
                f"✅ <b>Acertos</b>: <b>{s['hits']}</b> / <b>{s['hits']+s['misses']}</b> (<b>{pct(s['hits'], s['misses'])}%</b>)  |  🔁 Pendentes: <b>{pend}</b>\\n"
                f"🔥 <b>Streak</b>: <b>{s['streak_hit']}✔️</b> | <b>{s['streak_miss']}❌</b>")
        await send_html(update, html)

application = Application.builder().token(TOKEN).build()

application.add_handler(MessageHandler(filters.ALL, debug_tap), group=-1)
application.add_error_handler(on_error)

application.add_handler(CommandHandler("version", cmd_version))
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("assinar", cmd_assinar))
application.add_handler(CommandHandler("status", cmd_status))
application.add_handler(CommandHandler("mode", cmd_mode))
application.add_handler(CommandHandler("k", cmd_k))
application.add_handler(CommandHandler("n", cmd_n))
application.add_handler(CommandHandler("reset", cmd_reset))
application.add_handler(CommandHandler("stats", cmd_stats))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

aio = web.Application()

async def tg_handler(request: web.Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response(text="ok")

async def payments_handler(request: web.Request):
    data = await request.json()
    if data.get("status") == "paid" and "user_id" in data:
        uid = int(data["user_id"])
        dt = await set_active_days(uid, SUB_DAYS)
        return web.json_response({"ok": True, "active_until": dt.isoformat() if dt else None})
    return web.json_response({"ok": False})

async def health_handler(request: web.Request):
    return web.Response(text="ok")

aio.router.add_post(f"/{TG_PATH}", tg_handler)
aio.router.add_post("/payments/webhook", payments_handler)
aio.router.add_get("/health", health_handler)

async def on_startup(app: web.Application):
    print(f"🚀 {APP_VERSION} | PUBLIC_URL={PUBLIC_URL} | TG_PATH=/{TG_PATH} | TRIAL_MAX_HITS={TRIAL_MAX_HITS} | PAYWALL_OFF={PAYWALL_OFF}")
    if not TOKEN:
        raise RuntimeError("Defina TELEGRAM_TOKEN")
    await application.initialize()
    await application.start()
    if PUBLIC_URL:
        await application.bot.set_webhook(url=f"{PUBLIC_URL}/{TG_PATH}", drop_pending_updates=True)
        print("✅ Webhook setado.")
    else:
        print("⚠️ PUBLIC_URL vazio — defina para usar webhook.")

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
