"""
네이버 뉴스 검색 API — 의사별 언론노출 건수
https://developers.naver.com/docs/serviceapi/search/news/news.md
"""
import ssl
import aiohttp

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, NAVER_SEARCH_URL, REQUEST_TIMEOUT

SHORT_NAMES = {
    "서울대학교병원": "서울대병원",
    "서울아산병원":   "아산병원",
    "삼성서울병원":   "삼성병원",
    "세브란스병원":   "세브란스",
    "분당서울대병원": "분당서울대",
}


class NaverNewsClient:

    def __init__(self):
        self._cache: dict = {}

    def _headers(self) -> dict:
        return {
            "X-Naver-Client-Id":     NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        }

    async def get_news_count(self, doctor_name: str, hospital_name: str) -> int:
        """
        의사명 + 병원명 네이버 뉴스 검색 → 기사 수 반환.
        Returns: int
        """
        cache_key = f"{doctor_name}:{hospital_name}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        short = SHORT_NAMES.get(hospital_name, hospital_name)
        query = f'"{doctor_name}" "{short}"'

        try:
            ssl_ctx = ssl.create_default_context()
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
                async with session.get(
                    NAVER_SEARCH_URL,
                    params={"query": query, "display": 1, "sort": "date"},
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        count = data.get("total", 0)
                        self._cache[cache_key] = count
                        return count
                    print(f"[Naver] Status {resp.status} for {query}")
        except Exception as e:
            print(f"[Naver] Error: {e}")
        return 0

    async def get_news_articles(self, doctor_name: str, hospital_name: str, count: int = 10) -> list[dict]:
        """뉴스 기사 목록 조회 (제목, 링크, 날짜)"""
        short = SHORT_NAMES.get(hospital_name, hospital_name)
        query = f'"{doctor_name}" "{short}"'

        try:
            ssl_ctx = ssl.create_default_context()
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
                async with session.get(
                    NAVER_SEARCH_URL,
                    params={"query": query, "display": count, "sort": "date"},
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        return [
                            {
                                "title": item.get("title", "").replace("<b>", "").replace("</b>", ""),
                                "link":  item.get("originallink", item.get("link", "")),
                                "date":  item.get("pubDate", ""),
                            }
                            for item in data.get("items", [])
                        ]
        except Exception as e:
            print(f"[Naver] Articles error: {e}")
        return []
