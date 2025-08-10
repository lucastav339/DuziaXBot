
import os, re, datetime, asyncio
from dotenv import load_dotenv

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import redis.asyncio as redis
from aiohttp import web

# =========================
# Config & Globals
# =========================
load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
TG_PATH = os.getenv("TG_PATH", "tg")
SUB_DAYS = int(os.getenv("SUB_DAYS", "30"))
REDIS_URL = os.getenv("REDIS_URL", "")

APP_VERSION = "unificado-v1.0"

# Redis
rds = redis.from_url(REDIS_URL) if REDIS_URL else None

# Estado por usuário (em memória) — para histórico/estatística
STATE = {}  # { uid: {"modo":2,"K":5,"N":80,"hist":[], "pred_queue":[], "stats":{...}} }

def today():
    return datetime.date.today()

def ensure_user(uid: int):
    if uid not in STATE:
        STATE[uid] = {
            "modo": 2, "K": 5, "N": 80, "hist": [],
            "pred_queue": [],
            "stats": {"hits":0,"misses":0,"streak_hit":0,"streak_miss":0}
        }

def get_duzia(n: int):
    if 1 <= n <= 12: return "D1"
    if 13 <= n <= 24: return "D2"
    if 25 <= n <= 36: return "D3"
    return None  # ignora 0 e fora 1..36

def escolher_1_duzia(hist, K=5):
    rec = hist[:K]
    c = {"D1":0,"D2":0,"D3":0}
    for n in rec:
        d = get_duzia(n)
        if d: c[d]+=1
    cg = {"D1":0,"D2":0,"D3":0}
    for n in hist:
        d = get_duzia(n)
        if d: cg[d]+=1
    d_rec = sorted(c.items(), key=lambda x:(-x[1], -cg[x[0]]))[0][0]
    return d_rec, c, cg

def escolher_2_duzias(hist, K=5):
    cg = {"D1":0,"D2":0,"D3":0}
    for n in hist:
        d = get_duzia(n)
        if d: cg[d]+=1
    top2 = [x for x,_ in sorted(cg.items(), key=lambda x:x[1], reverse=True)[:2]]
    excl = {"D1","D2","D3"}.difference(top2).pop()
    rec = hist[:K]
    cr = {"D1":0,"D2":0,"D3":0}
    for n in rec:
        d = get_duzia(n)
        if d: cr[d]+=1
    return top2, excl, cr, cg

def pct(h, m):
    t = h + m
    return round((h*100/t), 1) if t else 0.0

async def send_html(update: Update, html: str):
    await update.message.reply_text(html, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

# ---------- Redis helpers (assinatura) ----------
async def get_active_until(user_id: int):
    if not rds: return None
    v = await rds.get(f"sub:{user_id}")
    if not v: return None
    try:
        s = v.decode() if isinstance(v, bytes) else v
        return datetime.date.fromisoformat(s)
    except:
        return None

async def set_active_days(user_id: int, days: int = SUB_DAYS):
    if not rds: return None
    dt = today() + datetime.timedelta(days=days)
    await rds.set(f"sub:{user_id}", dt.isoformat())
    return dt

# ---------- Scoring de previsões ----------
def score_predictions(uid: int, nums: list[int]):
    st = STATE[uid]
    q = st["pred_queue"]
    stats = st["stats"]
    changed = False

    for n in reversed(nums):  # último da lista é o mais antigo
        d = get_duzia(n)
        if d is None:
            continue  # 0 não consome previsão
        if not q:
            break
        pred = q.pop(0)
        hit = d in pred["duzias"]
        if hit:
            stats["hits"] += 1
            stats["streak_hit"] += 1
            stats["streak_miss"] = 0
        else:
            stats["misses"] += 1
            stats["streak_miss"] += 1
            stats["streak_hit"] = 0
        changed = True
    return changed

# =========================
# Handlers do Telegram
# =========================
async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_html(update, f"🧩 <b>Versão</b>: <code>unificado-v1.0</code>")

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ensure_user(update.effective_user.id)
    uid = update.effective_user.id
    html = (
        "🤖 <b>Analista de Dúzias</b>\n"
        f"Seu ID: <code>{uid}</code>\n\n"
        "Envie números (ex.: <code>32 19 33 12 8</code>). Padrão: <b>2 dúzias</b>.\n\n"
        "Comandos:\n"
        "• <code>/mode 1</code> — 1 dúzia | <code>/mode 2</code> — 2 dúzias\n"
        "• <code>/k 5</code> — janela recente (K)\n"
        "• <code>/n 80</code> — tamanho do histórico (N)\n"
        "• <code>/stats</code> — seus acertos | <code>/resetstats</code> — zerar\n"
        "• <code>/assinar</code> — pagar | <code>/status</code> — validade\n"
        "• <code>/reset</code> — limpar histórico\n\n"
        "💡 Conteúdo informativo/educativo (18+). Para acurácia, envie <b>um número por mensagem</b>."
    )
    await send_html(update, html)

async def cmd_assinar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = "https://seu-psp.com/pagar/SEU_PRODUTO"  # TROCAR PELO SEU LINK
    await send_html(update,
        "💳 <b>Assinatura</b>\n"
        f"Acesso por {SUB_DAYS} dias.\n\n"
        f"➡️ Pague aqui: <a href=\"{link}\">Finalizar pagamento</a>\n"
        "Assim que aprovado, liberamos automaticamente. Use /status para conferir."
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    dt = await get_active_until(uid)
    if dt and dt >= today():
        await send_html(update, f"🟢 <b>Ativo</b> até <b>{dt.strftime('%d/%m/%Y')}</b>.")
    else:
        await send_html(update, "🔴 <b>Inativo</b>. Use /assinar para liberar seu acesso.")

async def require_active(update: Update) -> bool:
    uid = update.effective_user.id
    dt = await get_active_until(uid)
    if dt and dt >= today():
        return True
    await send_html(update, "🔒 <b>Acesso restrito</b>. Use /assinar para liberar seu acesso.")
    return False

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active(update): return
    ensure_user(update.effective_user.id)
    if context.args and context.args[0] == "1":
        STATE[update.effective_user.id]["modo"] = 1
        await send_html(update, "🎛️ Modo: <b>1 dúzia</b>.")
    else:
        STATE[update.effective_user.id]["modo"] = 2
        await send_html(update, "🎛️ Modo: <b>2 dúzias</b>.")

async def cmd_k(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active(update): return
    ensure_user(update.effective_user.id)
    if context.args and context.args[0].isdigit():
        STATE[update.effective_user.id]["K"] = max(1, min(50, int(context.args[0])))
    await send_html(update, f"⚙️ K=<b>{STATE[update.effective_user.id]['K']}</b>.")

async def cmd_n(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active(update): return
    ensure_user(update.effective_user.id)
    if context.args and context.args[0].isdigit():
        N = max(10, min(1000, int(context.args[0])))
        STATE[update.effective_user.id]["N"] = N
        STATE[update.effective_user.id]["hist"] = STATE[update.effective_user.id]["hist"][:N]
    await send_html(update, f"⚙️ N=<b>{STATE[update.effective_user.id]['N']}</b>.")

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active(update): return
    ensure_user(update.effective_user.id)
    STATE[update.effective_user.id]["hist"] = []
    STATE[update.effective_user.id]["pred_queue"] = []
    await send_html(update, "🧹 Histórico limpo.")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active(update): return
    ensure_user(update.effective_user.id)
    s = STATE[update.effective_user.id]["stats"]
    p = len(STATE[update.effective_user.id]["pred_queue"])
    await send_html(update,
        f"📈 <b>Resultados</b>\n"
        f"• ✅ Acertos: <b>{s['hits']}</b>\n"
        f"• ❌ Erros: <b>{s['misses']}</b>\n"
        f"• 🎯 Taxa: <b>{pct(s['hits'], s['misses'])}%</b>\n"
        f"• 🔁 Pendentes: <b>{p}</b>\n"
        f"• 🔥 Streak: <b>{s['streak_hit']}✔️</b> | <b>{s['streak_miss']}❌</b>\n"
        f"<i>(0 é ignorado e não consome previsão)</i>"
    )

async def cmd_resetstats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_active(update): return
    ensure_user(update.effective_user.id)
    STATE[update.effective_user.id]["stats"] = {"hits":0,"misses":0,"streak_hit":0,"streak_miss":0}
    STATE[update.effective_user.id]["pred_queue"] = []
    await send_html(update, "🔄 Contadores zerados.")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Comandos públicos sempre permitidos
    txt = (update.message.text or "").strip().lower()
    if txt in ["/start","/assinar","/status","/version"]:
        return  # já tratados nos handlers específicos

    if not await require_active(update): return

    ensure_user(update.effective_user.id)
    uid = update.effective_user.id
    text = update.message.text or ""
    nums = [int(x) for x in re.findall(r"\d+", text)]
    if not nums:
        await send_html(update, "Envie números (ex.: <code>32 19 33 12 8</code>) ou <code>/start</code>.")
        return

    # 1) pontuar previsões antigas com novos resultados
    score_predictions(uid, nums)

    # 2) atualizar histórico
    st = STATE[uid]
    st["hist"] = (nums + st["hist"])[:st["N"]]
    K = st["K"]
    N = len(st["hist"])

    # 3) gerar nova previsão (entra na fila)
    if st["modo"] == 1:
        d, rec, ger = escolher_1_duzia(st["hist"], K)
        st["pred_queue"].append({"modo":1, "duzias":[d]})
        s = st["stats"]; pend = len(st["pred_queue"])
        html = (
            f"🎯 <b>Dúzia</b>: <b>{d}</b>\n"
            f"🪄 <b>Recentes</b> (K={K}): D1=<b>{rec['D1']}</b> • D2=<b>{rec['D2']}</b> • D3=<b>{rec['D3']}</b>\n"
            f"📊 <b>Geral</b> (N={N}): D1=<b>{ger['D1']}</b> • D2=<b>{ger['D2']}</b> • D3=<b>{ger['D3']}</b>\n"
            f"—\n"
            f"✅ <b>Acertos</b>: <b>{s['hits']}</b> / <b>{s['hits']+s['misses']}</b> "
            f"(<b>{pct(s['hits'], s['misses'])}%</b>)  |  🔁 Pendentes: <b>{pend}</b>\n"
            f"🔥 <b>Streak</b>: <b>{s['streak_hit']}✔️</b> | <b>{s['streak_miss']}❌</b>\n"
            f"<i>(0 é ignorado e não consome previsão)</i>"
        )
    else:
        top2, excl, cr, cg = escolher_2_duzias(st["hist"], K)
        st["pred_queue"].append({"modo":2, "duzias":top2})
        s = st["stats"]; pend = len(st["pred_queue"])
        html = (
            f"🎯 <b>Dúzias</b>: <b>{top2[0]}</b> + <b>{top2[1]}</b>  |  🚫 Excluída: <b>{excl}</b>\n"
            f"🪄 <b>Recentes</b> (K={K}): D1=<b>{cr['D1']}</b> • D2=<b>{cr['D2']}</b> • D3=<b>{cr['D3']}</b>\n"
            f"📊 <b>Geral</b> (N={N}): D1=<b>{cg['D1']}</b> • D2=<b>{cg['D2']}</b> • D3=<b>{cg['D3']}</b>\n"
            f"—\n"
            f"✅ <b>Acertos</b>: <b>{s['hits']}</b> / <b>{s['hits']+s['misses']}</b> "
            f"(<b>{pct(s['hits'], s['misses'])}%</b>)  |  🔁 Pendentes: <b>{pend}</b>\n"
            f"🔥 <b>Streak</b>: <b>{s['streak_hit']}✔️</b> | <b>{s['streak_miss']}❌</b>\n"
            f"<i>(0 é ignorado e não consome previsão)</i>"
        )
    await send_html(update, html)

# =========================
# AIOHTTP app (Telegram + Payments + Health)
# =========================
application = Application.builder().token(TOKEN).build()

application.add_handler(CommandHandler("version", cmd_version))
application.add_handler(CommandHandler("start", cmd_start))
application.add_handler(CommandHandler("assinar", cmd_assinar))
application.add_handler(CommandHandler("status", cmd_status))

application.add_handler(CommandHandler("mode", cmd_mode))
application.add_handler(CommandHandler("k", cmd_k))
application.add_handler(CommandHandler("n", cmd_n))
application.add_handler(CommandHandler("reset", cmd_reset))
application.add_handler(CommandHandler("stats", cmd_stats))
application.add_handler(CommandHandler("resetstats", cmd_resetstats))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

# AIOHTTP server
aio = web.Application()

async def tg_handler(request: web.Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response(text="ok")

async def payments_handler(request: web.Request):
    data = await request.json()
    # Exemplo esperado do PSP: {"status":"paid","user_id":123456789}
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
    print(f"🚀 unificado-v1.0 | PUBLIC_URL={PUBLIC_URL} | TG_PATH=/{TG_PATH}")
    if not TOKEN:
        raise RuntimeError("Defina TELEGRAM_TOKEN")
    # inicia o PTB
    await application.initialize()
    await application.start()
    # configura webhook do Telegram
    if PUBLIC_URL:
        await application.bot.set_webhook(
            url=f"{PUBLIC_URL}/{TG_PATH}",
            drop_pending_updates=True
        )
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
