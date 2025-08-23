import os
import time
from contextlib import asynccontextmanager
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
from typing import Optional, Tuple, Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from playwright.async_api import async_playwright

# URLs possíveis (alguns ambientes usam /pt-BR, outros /pt)
TARGET_URLS = [
    "https://gamblingcounting.com/pt-BR/pragmatic-brazilian-roulette",
    "https://gamblingcounting.com/pt/pragmatic-brazilian-roulette",
]

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
    print(f"🌐 Health server em http://0.0.0.0:{port}/health")
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
        _browser["context"] = await browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        _browser["page"] = await _browser["context"].new_page()
        _browser["page"].set_default_timeout(20000)  # 20s
    try:
        yield _browser["page"]
    finally:
        pass

# ====== Utilitários JS (varredura incluindo Shadow DOM) ======
PIERCE_DOM_JS = """
() => {
  const out = [];
  const seen = new Set();
  function visit(node) {
    if (!node || seen.has(node)) return;
    seen.add(node);
    if (node.nodeType === Node.ELEMENT_NODE) {
      out.push(node);
      // Shadow root
      const sr = node.shadowRoot;
      if (sr) {
        Array.from(sr.children).forEach(visit);
      }
      // Children
      Array.from(node.children || []).forEach(visit);
    }
  }
  visit(document.documentElement);
  return out;
}
"""

EXTRACT_LAST_SPIN_JS = """
(anchorHintText) => {
  const els = (() => {
    const out = [];
    const seen = new Set();
    function visit(node) {
      if (!node || seen.has(node)) return;
      seen.add(node);
      if (node.nodeType === Node.ELEMENT_NODE) {
        out.push(node);
        if (node.shadowRoot) Array.from(node.shadowRoot.children).forEach(visit);
        Array.from(node.children || []).forEach(visit);
      }
    }
    visit(document.documentElement);
    return out;
  })();

  const lc = (s) => (s || "").toLowerCase();
  const hasHist = (el) => lc(el.textContent || "").includes(anchorHintText);

  // 1) Tenta achar um bloco que pareça com "História dos rounds"
  let anchor = els.find(el => hasHist(el));
  if (!anchor) {
    // fallback: tenta "historia", "history", "últimas 200 rodadas"
    anchor = els.find(el => {
      const t = lc(el.textContent || "");
      return t.includes("hist") || t.includes("últimas 200") || t.includes("history");
    });
  }

  // Candidatos a lista grande
  const candidates = [];
  (anchor ? [anchor, anchor.parentElement, document.body] : [document.body]).forEach(root => {
    if (!root) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    while (walker.nextNode()) {
      const el = walker.currentNode;
      const childCount = el.children ? el.children.length : 0;
      const html = el.outerHTML || "";
      const looksLikeList = childCount >= 10 && childCount <= 400;
      const hasData = /data-|aria-|class=|role=|list|grid|row|cell/i.test(html);
      if (looksLikeList && hasData) candidates.push(el);
    }
  });

  // Ordena por qtde de filhos (maior primeiro)
  candidates.sort((a,b) => (b.children.length||0) - (a.children.length||0));

  function numFromEl(el) {
    const n1 = el.getAttribute?.("data-number") || el.getAttribute?.("data-num") || el.getAttribute?.("data-value") || "";
    let numero = n1 && /^\d+$/.test(n1) ? parseInt(n1, 10) : NaN;
    if (Number.isNaN(numero)) {
      const aria = el.getAttribute?.("aria-label") || "";
      const alt = el.getAttribute?.("alt") || "";
      const txt = el.textContent || "";
      const m = (aria + " " + alt + " " + txt).match(/\b([0-9]|[12][0-9]|3[0-6])\b/);
      if (m) numero = parseInt(m[1], 10);
    }
    return numero;
  }

  function corFromEl(el, numero) {
    const cls = lc(el.getAttribute?.("class") || "");
    const style = lc(el.getAttribute?.("style") || "");
    const aria = lc(el.getAttribute?.("aria-label") || "");
    const alt  = lc(el.getAttribute?.("alt") || "");
    const txt  = lc(el.textContent || "");

    const pool = cls + " " + style + " " + aria + " " + alt + " " + txt;

    if (/green|verde|zero/.test(pool) || numero === 0) return "Verde";
    if (/black|preto/.test(pool)) return "Preto";
    if (/red|vermelh/.test(pool)) return "Vermelho";

    // tenta no primeiro filho
    const inner = el.querySelector?.("*");
    if (inner) {
      const ics = lc(inner.getAttribute?.("class") || "") + " " + lc(inner.getAttribute?.("style") || "") + " " + lc(inner.textContent || "");
      if (/green|verde|zero/.test(ics) || numero === 0) return "Verde";
      if (/black|preto/.test(ics)) return "Preto";
      if (/red|vermelh/.test(ics)) return "Vermelho";
    }
    return "";
  }

  for (const list of candidates.slice(0, 6)) {
    const items = Array.from(list.children || []);
    if (!items.length) continue;

    // Tenta pegar primeiro (último round) e se falhar, o último
    const probes = [items[0], items[items.length - 1]];
    for (const el of probes) {
      const numero = numFromEl(el);
      if (!Number.isNaN(numero)) {
        let cor = corFromEl(el, numero);
        if (!cor) cor = numero === 0 ? "Verde" : "";
        return { numero, cor };
      }
      // tenta um filho “bola”
      const child = el.querySelector?.("*");
      if (child) {
        const numero2 = numFromEl(child);
        if (!Number.isNaN(numero2)) {
          let cor = corFromEl(child, numero2);
          if (!cor) cor = numero2 === 0 ? "Verde" : "";
          return { numero: numero2, cor };
        }
      }
    }
  }
  return null;
}
"""

async def try_extract_on_url(page, url: str) -> Optional[Tuple[int, str]]:
    await page.goto(url, wait_until="networkidle")
    # rolar um pouco (às vezes conteúdo aparece após scroll)
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight * 0.3)")
    await page.wait_for_timeout(2000)

    # Pista textual para o bloco do histórico
    hint = "história dos rounds"
    data: Optional[dict[str, Any]] = await page.evaluate(EXTRACT_LAST_SPIN_JS, hint)
    if data and "numero" in data:
        numero = int(data.get("numero"))
        cor = data.get("cor") or ("Verde" if numero == 0 else "Vermelho/Preto (indefinido)")
        return numero, cor
    return None

async def extract_last_spin(page) -> Tuple[int, str]:
    # até 3 tentativas, alternando URLs
    last_err = None
    for attempt in range(3):
        for url in TARGET_URLS:
            try:
                res = await try_extract_on_url(page, url)
                if res:
                    return res
            except Exception as e:
                last_err = e
        # pequena espera entre tentativas
        await page.wait_for_timeout(1500)
    raise RuntimeError(f"Não consegui localizar o último resultado. {last_err or ''}")

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
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ Não consegui buscar o último resultado agora.\n"
                 f"Tente novamente em alguns segundos.\n\nDetalhes: {e}"
        )

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

    start_health_server()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("health", health))
    app.post_shutdown = on_shutdown

    app.run_polling()

if __name__ == "__main__":
    main()
