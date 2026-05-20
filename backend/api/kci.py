"""
KCI(한국학술지인용색인) API — 국내 논문 검색
https://open.kci.go.kr
"""
import ssl
import aiohttp
from xml.etree import ElementTree as ET

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import KCI_API_KEY, KCI_BASE_URL, REQUEST_TIMEOUT


class KCIClient:

    async def search_papers(self, author_name: str, affiliation: str = "") -> dict:
        """
        KCI 논문 검색.
        GET https://open.kci.go.kr/po/openapi/openApiSearch.kci
            ?apiCode=articleSearch&key={KEY}&author={저자명}&displayCount=100
        응답: XML
        """
        try:
            ssl_ctx = ssl.create_default_context()
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
                async with session.get(
                    KCI_BASE_URL,
                    params={
                        "apiCode":      "articleSearch",
                        "key":          KCI_API_KEY,
                        "author":       author_name,
                        "displayCount": 100,
                    },
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        root = ET.fromstring(text)
                        records = root.findall(".//record")
                        return {
                            "total": len(records),
                            "papers": [
                                {
                                    "title":     r.findtext("title", ""),
                                    "journal":   r.findtext("journalName", ""),
                                    "year":      r.findtext("pubYear", ""),
                                    "citations": int(r.findtext("citedCount", "0")),
                                }
                                for r in records
                            ],
                        }
        except Exception as e:
            print(f"[KCI] Error: {e}")
        return {"total": 0, "papers": []}
