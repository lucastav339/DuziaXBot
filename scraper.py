import logging
from typing import Optional, List, Dict, Literal

from aiohttp import ClientSession, ClientTimeout
from bs4 import BeautifulSoup, Tag

log = logging.getLogger("roulette-scraper")

Color = Literal["red", "black", "green"]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)

class RouletteScraper:
    """
    Scraper robusto:
    - Tenta seletores específicos do bloco 'História das rodadas'
    - Fallback: qualquer '.roulette-number' no documento
    - Logs de diagnóstico para /debug
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
        self._last_fetch_ok = None  # para debug
        self._last_items_count = 0
        self._last_sample_classes: List[str] = []
        self._last_sample_numbers: List[Dict] = []

    async def _fetch_html(self) -> str:
        async with ClientSession(timeout=self._timeout, headers=self._headers) as s:
            async with s.get(self.url, allow_redirects=True) as r:
                self._last_fetch_ok = (r.status, str(r.url))
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
        color = RouletteScraper._color_from_classes(tag)
        return {"number": n, "color": color}

    def _extract_items(self, soup: BeautifulSoup) -> List[Dict]:
        # 1) seletor do bloco “História das rodadas”
        container = soup.select_one(
            'section[aria-labelledby="live-game-result-label"] '
            '.live-game-page__block__results.live-game-page__block__results--roulette'
        )
        nodes: List[Tag] = []
        if container:
            nodes = list(container.select(".roulette-number"))

        # 2) fallback global
        if not nodes:
            nodes = list(soup.select(".roulette-number"))

        items: List[Dict] = []
        sample_classes: List[str] = []
        for div in nodes:
            parsed = self._parse_item(div)
            if parsed:
                items.append(parsed)
            if len(sample_classes) < 10:
                sample_classes.append(" ".join(div.get("class", [])))

        # salvar amostras para /debug
        self._last_items_count = len(items)
        self._last_sample_classes = sample_classes
        self._last_sample_numbers = items[:5]

        return items

    async def fetch_latest_entry(self) -> Optional[Dict]:
        """
        Retorna o item mais recente: {"number": int, "color": "red|black|green"}
        Assume que o DOM vem do mais recente -> mais antigo; se inverter, troque para items[-1].
        """
        html = await self._fetch_html()
        soup = BeautifulSoup(html, "html.parser")
        items = self._extract_items(soup)
        if not items:
            log.warning(f"[scraper] Nenhum item encontrado. fetch={self._last_fetch_ok}")
            return None
        return items[0]

    async def fetch_history(self, limit: int = 15) -> List[Dict]:
        html = await self._fetch_html()
        soup = BeautifulSoup(html, "html.parser")
        items = self._extract_items(soup)
        return items[:limit]

    # -------- utilitário de debug --------
    async def debug_snapshot(self) -> Dict:
        try:
            html = await self._fetch_html()
            soup = BeautifulSoup(html, "html.parser")
            _ = self._extract_items(soup)
        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
                "last_fetch": self._last_fetch_ok,
            }
        return {
            "ok": True,
            "fetch": self._last_fetch_ok,                # (status, url_resolvida)
            "items_found": self._last_items_count,       # quantos números parseados
            "sample_classes": self._last_sample_classes, # até 10 classes encontradas
            "sample_numbers": self._last_sample_numbers, # até 5 itens parseados
        }
