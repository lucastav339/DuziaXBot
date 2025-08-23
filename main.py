import os
from contextlib import asynccontextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from playwright.async_api import async_playwright

TARGET_URL = "https://gamblingcounting.com/pt/pragmatic-brazilian-roulette"

# ====== Servidor de saúde (Render exige porta aberta) ======
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"🌐 Health server rodando em http://0.0.0.0:{port}/health")
    return server

# ====== Navegador headless compartilhado (abre uma vez e reutiliza) ======
_browser = {"playwright": None, "browser": None, "context": None, "page": None}

@asynccontextmanager
async def browser_context():
    if _browser["browser"] is None:
        pw = await async_playwright().start()
        _browser["playwright"] = pw
        browser = await pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"
        ])
        _browser["browser"] = browser
        _browser["context"] = await browser.new_context(viewport={"width": 1280, "height": 900})
        _browser["page"] = await _browser["context"].new_page()
    try:
        yield _browser["page"]
    finally:
        pass

async def extract_last_spin(page):
    await page.goto(TARGET_URL, wait_until="domcontentloaded")
    await page.wait_for_timeout(1500)

    data = await page.evaluate("""
    () => {
      const textMatches = (el, needle) =>
        el && el.textContent && el.textContent.toLowerCase().includes(needle);

      const all = Array.from(document.querySelectorAll("h2, h3, h4, section, div"));
      const anchor = all.find(el => textMatches(el, "história dos rounds") || textMatches(el, "history of rounds"));
      if (!anchor) return null;

      const candidates = [];
      const scope = anchor.parentElement || document.body;
      const walker = document.createTreeWalker(scope, NodeFilter.SHOW_ELEMENT);
      while (walker.nextNode()) {
        const el = walker.currentNode;
        const html = el.outerHTML || "";
        const childCount = el.children ? el.children.length : 0;
        const looksLikeList = childCount >= 10 && childCount <= 300;
        const hasData = /\bdata-/.test(html) || /\baria-/.test(html) || /class=/.test(html);
        if (looksLikeList && hasData) candidates.push(el);
      }

      candidates.sort((a,b) => (b.children.length||0) - (a.children.length||0));
      const list = candidates[0];
      if (!list) return null;

      const first = list.children[0] || list.querySelector(":scope > *");
      if (!first) return null;

      const numAttr = first.getAttribute("data-number") || first.getAttribute("data-num") || "";
      let numero = numAttr ? parseInt(numAttr, 10) : NaN;

      if (Number.isNaN(numero)) {
        const t = (first.textContent || "").match(/\b([0-9]|[12][0-9]|3[0-6])\b/);
        if (t) numero = parseInt(t[1], 10);
      }

      const cls = (first.getAttribute("class") || "").toLowerCase();
      let cor = "";
      if (/red/.test(cls) || /vermelh/.test(cls)) cor = "Vermelho";
      else if (/black/.test(cls) || /pret/.test(cls)) cor = "Preto";
      else if (/green|zero/.test(cls) || numero === 0) cor = "Verde";

      if (!cor) {
        const style = (first.getAttribute("style") || "").toLowerCase();
        if (/red/.test(style)) cor = "Vermelho";
        else if (/black/.test(style)) cor = "Preto";
        else if (/green/.test(style)) cor = "Verde";
      }

      if (!cor) {
        const inner = first.querySelector("*");
        if (inner) {
          const icls = (inner.getAttribute("class") || "").toLowerCase();
          if (/red/.test(icls) || /vermelh/.test(icls)) cor = "Vermelho";
          else if (/black/.test(icls) || /pret/.test(icls)) cor = "Preto";
          else if (/green|zero/.test(icls) || numero === 0) cor = "Verde";
        }
      }

      if (Number.isNaN(numero)) return null;
      return { numero, cor: cor || (numero === 0 ? "Verde" : "") };
    }
    """)

    if not data:
        raise RuntimeError("Não consegui localizar a lista da História dos rounds. O layout pode ter mudado.")

    numero = data.get("numero")
    cor = data.get("cor") or ("Verde" if numero == 0 else "Vermelho/Preto (indefinido no HTML)")
    return numero, cor

# ========= Handlers do Telegram =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        async with browser_context() as page:
            numero, cor = await extract_last_spin(page)
        msg = (
            "🔎 Último resultado na Brazilian Roulette (Pragmatic)\n\n"
            f"🎯 Número: <b>{numero}</b>\n"
            f"🎨 Cor: <b>{cor}</b>\n\n"
            "Fonte: GamblingCounting — História dos rounds"
        )
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Não consegui buscar o último resultado.\nDetalhes: {e}")

async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("OK")

async def on_shutdown(app: Application):
    try:
        if _browser["context"]:
            await _browser["context"].close()
        if _browser["browser"]:
            await _browser["browser"].close()
        if _browser["playwright"]:
            await _browser["playwright"].stop()
    except Exception:
        pass

def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Defina a variável de ambiente BOT_TOKEN com o token do seu bot.")

    # 🔹 inicia servidor de saúde para o Render
    start_health_server()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("health", health))
    app.post_shutdown = on_shutdown

    app.run_polling()

if __name__ == "__main__":
    main()
