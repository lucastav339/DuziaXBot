import logging
from typing import Optional, List, Dict, Literal

from aiohttp import ClientSession, ClientTimeout, ClientResponseError
from bs4 import BeautifulSoup, Tag

# Fallback headless browser
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

log = logging.getLogger("roulette-scraper")

Color = Literal["red", "black", "green"]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

class RouletteScraper:
    """
    Estratégia:
      1) Tentar HTTP simples (aiohttp) -> rápido
      2) Se 403/sem itens -> fallback Playwright (Chromium headless)
    """

    def __init__(self, url: str):
        self.url = url
        self._timeout = ClientTimeout(total=15)
        self._headers = {
            "User-Agent": UA,
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://gamblingcounting.com/",
        }

    # ---------- AIOHTTP PATH ----------
    async def _fetch_html_aiohttp(self) -> str:
        async with ClientSession(timeout=self._timeout, headers=self._headers) as s:
            async with s.get(self.url, allow_redirects=True) as r:
                r.raise_for_status()
                return await r.text()

    @staticmethod
    def _color_from_classes(tag: Tag) -> Color:
        cls = set(tag.get("class", []))
        if "roulette-number--red" in cls:
            return "red"
        if "roulette-number--black" in cls:
            return "black"
        return "green"

    @staticmethod
    def _parse_item(tag: Tag) -> Optional[Dict]:
        txt = (tag.get_text(strip=True) or "").split()[0]
        if not txt.isdigit():
            return None
        n = int(txt)
        if not (0 <= n <= 36):
            return None
        return {"number": n, "color": RouletteScraper._color_from_classes(tag)}

    def _extract_items_bs(self, html: str) -> List[Dict]:
        soup = BeautifulSoup(html, "html.parser")

        # Seletor preciso do bloco de histórico
        container = soup.select_one(
            'section[aria-labelledby="live-game-result-label"] '
            '.live-game-page__block__results.live-game-page__block__results--roulette'
        )

        nodes: List[Tag] = []
        if container:
            nodes = list(container.select(".roulette-number"))
        if not nodes:
            # fallback mais amplo
            nodes = list(soup.select(".roulette-number"))

        items: List[Dict] = []
        for div in nodes:
            parsed = self._parse_item(div)
            if parsed:
                items.append(parsed)
        return items

    # ---------- PLAYWRIGHT PATH ----------
    async def _extract_items_playwright(self) -> List[Dict]:
        """
        Abre Chromium headless, navega até a página e extrai via DOM renderizado.
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=UA,
                locale="pt-BR",
                viewport={"width": 1280, "height": 900},
            )
            page = await context.new_page()
            try:
                await page.goto(self.url, wait_until="domcontentloaded", timeout=20000)
                # Espera por qualquer '.roulette-number' (até 10s). Se necessário, aumente.
                await page.wait_for_selector(".roulette-number", timeout=10000)

                # Extrai diretamente do DOM os números + cores
                data = await page.evaluate("""
                    () => {
                      const sel = 'section[aria-labelledby="live-game-result-label"] .live-game-page__block__results--roulette .roulette-number';
                      let nodes = Array.from(document.querySelectorAll(sel));
                      if (!nodes.length) nodes = Array.from(document.querySelectorAll('.roulette-number'));
                      const items = [];
                      for (const el of nodes) {
                        const txt = (el.textContent || '').trim().split(/\s+/)[0];
                        const n = parseInt(txt, 10);
                        if (!Number.isInteger(n) || n < 0 || n > 36) continue;
                        const cls = el.className || '';
                        let color = 'green';
                        if (cls.includes('roulette-number--red')) color = 'red';
                        else if (cls.includes('roulette-number--black')) color = 'black';
                        items.push({ number: n, color });
                      }
                      return items;
                    }
                """)
                return data or []
            finally:
                await context.close()
                await browser.close()

    # ---------- API pública ----------
    async def _get_items(self) -> List[Dict]:
        """
        Retorna a lista de itens (mais recente -> mais antigo).
        Tenta aiohttp; se 403 ou vazio, usa Playwright.
        """
        # Primeiro: HTTP simples
        try:
            html = await self._fetch_html_aiohttp()
            items = self._extract_items_bs(html)
            if items:
                return items
            log.info("[scraper] AIOHTTP retornou 0 itens; tentando Playwright.")
        except ClientResponseError as e:
            log.warning(f"[scraper] AIOHTTP falhou: {e.status} {e.message}; tentando Playwright.")
        except Exception as e:
            log.warning(f"[scraper] AIOHTTP exceção: {e}; tentando Playwright.")

        # Fallback: Chromium headless
        try:
            items = await self._extract_items_playwright()
            return items
        except PWTimeoutError:
            log.error("[scraper] Playwright timeout ao aguardar elementos.")
            return []
        except Exception as e:
            log.exception(f"[scraper] Playwright falhou: {e}")
            return []

    async def fetch_latest_entry(self) -> Optional[Dict]:
        items = await self._get_items()
        return items[0] if items else None

    async def fetch_history(self, limit: int = 15) -> List[Dict]:
        items = await self._get_items()
        return items[:limit]
