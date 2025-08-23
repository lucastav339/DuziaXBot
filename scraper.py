import logging
from typing import Optional, List, Dict, Literal

from aiohttp import ClientSession, ClientTimeout
from bs4 import BeautifulSoup, Tag

log = logging.getLogger("roulette-scraper")

Color = Literal["red", "black", "green"]

class RouletteScraper:
    def __init__(self, url: str):
        self.url = url
        self._timeout = ClientTimeout(total=15)
        self._headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
        }

    async def _fetch_html(self) -> str:
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
        color = RouletteScraper._color_from_classes(tag)
        return {"number": n, "color": color}

    def _extract_items(self, soup: BeautifulSoup) -> List[Dict]:
        section = soup.select_one('section[aria-labelledby="live-game-result-label"]')
        if not section:
            return []
        container = section.select_one(".live-game-page__block__results--roulette")
        if not container:
            return []
        items: List[Dict] = []
        for div in container.select(".roulette-number"):
            parsed = self._parse_item(div)
            if parsed:
                items.append(parsed)
        return items

    async def fetch_latest_entry(self) -> Optional[Dict]:
        html = await self._fetch_html()
        soup = BeautifulSoup(html, "html.parser")
        items = self._extract_items(soup)
        return items[0] if items else None

    async def fetch_history(self, limit: int = 15) -> List[Dict]:
        html = await self._fetch_html()
        soup = BeautifulSoup(html, "html.parser")
        items = self._extract_items(soup)
        return items[:limit]
