import asyncio
import re
from playwright.async_api import async_playwright

TIPMINER_URL = "https://www.tipminer.com/br/historico/evolution/speed-roulette"

# Seletores mais focados em "bolinhas" e células com o número
CANDIDATE_SELECTORS = [
    # tabelas/listas mais comuns
    "table tbody tr:first-child td",
    ".history .number, .history .ball, .history .result, .history-item .number",
    ".results .result-number, .results .number, .result .number",
    "ul[class*='history'] li, div[class*='history'] div[class*='number']",
    "[class*='roulette'] [class*='history'] *",
]

NUM_RE = re.compile(r"^\s*(\d{1,2})\s*$")  # 1–2 dígitos isolados (0–36)

def pick_number_from_text(txt: str):
    if not txt:
        return None
    txt = txt.strip()
    m = NUM_RE.match(txt)
    if not m:
        return None
    n = int(m.group(1))
    return n if 0 <= n <= 36 else None

async def fetch_latest_result(timeout_ms: int = 25000) -> int | None:
    """
    Abre a página, espera o conteúdo dinâmico, tenta ler o número mais recente do histórico.
    Retorna 0..36 ou None.
    """
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent=ua,
            locale="pt-BR",
            color_scheme="dark",
            viewport={"width": 1366, "height": 768},
        )
        page = await context.new_page()

        # carrega e espera rede parada (JS finalizando)
        await page.goto(TIPMINER_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            await page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except:
            pass
        # pequeno delay pra frameworks que montam DOM após networkidle
        await page.wait_for_timeout(1200)

        # tenta achar um container de histórico mais provável
        probable_containers = [
            "table tbody",
            ".history", ".results", ".roulette-history", "[class*='history']",
        ]
        for pc in probable_containers:
            try:
                await page.wait_for_selector(pc, timeout=2000)
                break
            except:
                continue

        # 1) varre seletores específicos
        for sel in CANDIDATE_SELECTORS:
            try:
                loc = page.locator(sel)
                count = await loc.count()
                if count == 0:
                    continue

                # pegamos no máximo os 12 primeiros nós
                limit = min(count, 12)
                for i in range(limit):
                    txt = (await loc.nth(i).inner_text()).strip()
                    # ignorar textos longos (ex.: "Resultado", "Histórico", etc.)
                    if len(txt) > 2:
                        continue
                    n = pick_number_from_text(txt)
                    if n is not None:
                        await browser.close()
                        return n
            except:
                continue

        # 2) fallback: pega só nós com texto curtíssimo (<=2 chars) no body
        try:
            tiny_nodes = await page.locator("body *:not(script):not(style)").all()
            for el in tiny_nodes[:200]:
                try:
                    t = (await el.inner_text()).strip()
                    if 1 <= len(t) <= 2:
                        n = pick_number_from_text(t)
                        if n is not None:
                            await browser.close()
                            return n
                except:
                    continue
        except:
            pass

        # 3) último fallback: varre o HTML mas só aceita tokens isolados (1–2 chars)
        html = await page.content()
        # pega apenas números soltos (1–2 dígitos) e devolve o primeiro válido
        for token in re.findall(r">\s*([0-9]{1,2})\s*<", html):
            n = int(token)
            if 0 <= n <= 36:
                await browser.close()
                return n

        await browser.close()
        return None

if __name__ == "__main__":
    n = asyncio.run(fetch_latest_result())
    print(n)
