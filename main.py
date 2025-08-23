import os
import asyncio
from contextlib import asynccontextmanager

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Playwright (headless browser)
from playwright.async_api import async_playwright

TARGET_URL = "https://gamblingcounting.com/pt/pragmatic-brazilian-roulette"

# ====== Navegador headless compartilhado (abre uma vez e reutiliza) ======
_browser = {"playwright": None, "browser": None, "context": None, "page": None}

@asynccontextmanager
async def browser_context():
    # Reutiliza entre chamadas para ficar rápido
    if _browser["browser"] is None:
        pw = await async_playwright().start()
        _browser["playwright"] = pw
        # No Render/Ubuntu, use chromium com flags seguras
        browser = await pw.chromium.launch(headless=True, args=[
            "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"
        ])
        _browser["browser"] = browser
        _browser["context"] = await browser.new_context(viewport={"width": 1280, "height": 900})
        _browser["page"] = await _browser["context"].new_page()
    try:
        yield _browser["page"]
    finally:
        # mantemos aberto para reutilização; feche no shutdown se quiser
        pass

async def extract_last_spin(page):
    """
    Abre a página e tenta identificar o primeiro item da 'História dos rounds'.
    Faz uma busca tolerante a mudanças de layout, procurando pelo heading e,
    a partir dele, o contêiner com as bolinhas/itens do histórico.
    Retorna (numero:int, cor:str) onde cor ∈ {'Vermelho','Preto','Verde'}.
    """
    await page.goto(TARGET_URL, wait_until="domcontentloaded")
    # Aguarda JS hidratar a seção do histórico (as 'bolinhas' aparecem dinamicamente)
    # Seletores tolerantes: tenta achar pelo texto do bloco e depois pelos itens.
    # Muitas páginas usam <li> ou <div> com data-number ou classes de cor.
    # Abaixo, varremos alguns padrões comuns e pegamos o primeiro item.
    await page.wait_for_timeout(1500)  # pequeno tempo para carregar widgets

    # Tenta localizar o contêiner do histórico próximo ao heading com “História dos rounds”
    container = await page.evaluate("""
    () => {
      // 1) ache um heading que contenha 'História dos rounds' (PT) ou 'History of rounds' (fallback)
      const textMatches = (el, needle) =>
        el && el.textContent && el.textContent.toLowerCase().includes(needle);

      const all = Array.from(document.querySelectorAll("h2, h3, h4, section, div"));
      const anchor = all.find(el => textMatches(el, "história dos rounds") || textMatches(el, "history of rounds"));
      if (!anchor) return null;

      // Procura próximo do anchor por um contêiner que liste rodadas (muitas bolinhas/itens)
      // Heurística: elementos com muitos filhos pequenos e com números 0–36 no texto alt/title/aria ou data-*
      const candidates = [];
      const scope = anchor.parentElement || document.body;
      const walker = document.createTreeWalker(scope, NodeFilter.SHOW_ELEMENT);
      while (walker.nextNode()) {
        const el = walker.currentNode;
        const html = el.outerHTML || "";
        const txt = (el.textContent || "").trim();
        const childCount = el.children ? el.children.length : 0;

        // Sinais de contêiner do histórico
        const looksLikeList = childCount >= 10 && childCount <= 300;
        const hasData = /\bdata-/.test(html) || /\baria-/.test(html) || /class=/.test(html);

        if (looksLikeList && hasData) candidates.push(el);
      }

      // Escolhe o candidato com mais filhos (provável lista das 200 rodadas)
      candidates.sort((a,b) => (b.children.length||0) - (a.children.length||0));
      const list = candidates[0];
      if (!list) return null;

      // Agora, pegue o primeiro item (última rodada). Em alguns sites, a ordem é da esquerda p/ direita.
      // Vamos testar várias formas de extrair número e cor.
      const first = list.children[0] || list.querySelector(":scope > *");
      if (!first) return null;

      // Extrai número
      const numAttr = first.getAttribute("data-number") || first.getAttribute("data-num") || "";
      let numero = numAttr ? parseInt(numAttr, 10) : NaN;

      if (Number.isNaN(numero)) {
        // tenta de texto interno (ex.: <span>12</span>)
        const t = (first.textContent || "").match(/\b([0-9]|[12][0-9]|3[0-6])\b/);
        if (t) numero = parseInt(t[1], 10);
      }

      // Extrai cor por classe
      const cls = (first.getAttribute("class") || "").toLowerCase();
      let cor = "";
      if (/red/.test(cls) || /vermelh/.test(cls)) cor = "Vermelho";
      else if (/black/.test(cls) || /pret/.test(cls)) cor = "Preto";
      else if (/green|zero/.test(cls) || numero === 0) cor = "Verde";

      // Tenta por style/background, se necessário
      if (!cor) {
        const style = (first.getAttribute("style") || "").toLowerCase();
        if (/red/.test(style)) cor = "Vermelho";
        else if (/black/.test(style)) cor = "Preto";
        else if (/green/.test(style)) cor = "Verde";
      }

      // Se ainda faltar cor, tenta olhar filho (alguns sites colocam a classe no inner)
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

    if not container:
        raise RuntimeError("Não consegui localizar a lista da História dos rounds. O layout pode ter mudado.")

    numero = container.get("numero")
    cor = container.get("cor") or ("Verde" if numero == 0 else "")
    if not cor:
        # fallback: define pela paridade se o site omitiu a classe (NÃO é perfeito, mas evita falhar)
        # (na roleta real, vermelho/preto seguem a roda; aqui só para não quebrar)
        cor = "Vermelho/Preto (indefinido no HTML)"

    return numero, cor

# ========= Handlers do Telegram =========

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        async with browser_context() as page:
            numero, cor = await extract_last_spin(page)
        msg = f"🔎 Último resultado na Brazilian Roulette (Pragmatic)\n\n🎯 Número: <b>{numero}</b>\n🎨 Cor: <b>{cor}</b>\n\nFonte: GamblingCounting — História dos rounds"
        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
    except Exception as e:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ Não consegui buscar o último resultado.\nDetalhes: {e}")

async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("OK")

async def on_shutdown(app: Application):
    # Fecha Playwright no desligamento
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

    app = Application.builder().token(token).post_init(None).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("health", health))
    app.post_shutdown = on_shutdown

    # Para início rápido, use polling (evita complexidade de webhook)
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    asyncio.run(asyncio.to_thread(main))
