import asyncio
import re
from playwright.async_api import async_playwright

TIPMINER_URL = "https://www.tipminer.com/br/historico/evolution/speed-roulette"

CANDIDATE_SELECTORS = [
    "table tbody tr:first-child td, table tr:first-child td",
    ".history, .result, .card, .grid, .list, .item"
]

NUM_RE = re.compile(r"\b([0-2]?\d|3[0-6]|0)\b")

def extract_first_number(text: str) -> int | None:
    if not text:
        return None
    text = " ".join(text.split())
    m = NUM_RE.search(text)
    if m:
        try:
            n = int(m.group(1))
            if 0 <= n <= 36:
                return n
        except:
            return None
    return None

async def fetch_latest_result(timeout_ms: int = 15000) -> int | None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(TIPMINER_URL, wait_until="domcontentloaded", timeout=timeout_ms)

        found_content = False
        for sel in CANDIDATE_SELECTORS:
            try:
                await page.wait_for_selector(sel, timeout=timeout_ms)
                found_content = True
                break
            except:
                continue

        if not found_content:
            content = await page.content()
            num = extract_first_number(content)
            await browser.close()
            return num

        for sel in CANDIDATE_SELECTORS:
            try:
                elements = await page.query_selector_all(sel)
                for el in elements[:10]:
                    txt = (await el.inner_text()).strip()
                    n = extract_first_number(txt)
                    if n is not None:
                        await browser.close()
                        return n
            except:
                continue

        html = await page.content()
        await browser.close()
        return extract_first_number(html)

if __name__ == "__main__":
    n = asyncio.run(fetch_latest_result())
    print(n)
