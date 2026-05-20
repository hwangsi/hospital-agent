"""
PubMed E-utilities API — 의사별 H-index 산출
- esearch: 논문 검색 (저자명 + 소속기관)
- iCite: NIH 인용수 조회 (citation_count, 키 불필요)

주의: httpx.AsyncClient가 Python 3.14 + anyio 환경에서 ConnectError를 일으키는
      호환성 문제가 있어 aiohttp로 대체함.
"""
import asyncio
import json
import ssl
from typing import Optional
from urllib.parse import urlencode

import aiohttp

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import NCBI_API_KEY, REQUEST_TIMEOUT

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
ICITE_URL   = "https://icite.od.nih.gov/api/pubs"

# 병원 영문명 (PubMed affiliation 검색용)
HOSPITAL_AFFILIATIONS = {
    "서울대학교병원":   ["Seoul National University Hospital", "SNUH"],
    "서울아산병원":     ["Asan Medical Center", "AMC Seoul"],
    "삼성서울병원":     ["Samsung Medical Center", "SMC Seoul"],
    "세브란스병원":     ["Severance Hospital", "Yonsei University"],
    "분당서울대병원":   ["Seoul National University Bundang Hospital", "SNUBH"],
}

def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    return ctx


class PubMedClient:
    """PubMed E-utilities + NIH iCite 기반 H-index 산출"""

    def __init__(self):
        self._cache: dict = {}

    async def get_h_index(self, doctor_name: str, hospital_name: str) -> dict:
        """
        의사 이름 + 병원명으로 PubMed 검색 → H-index 산출.

        H-index 계산법:
        - 논문을 인용수 내림차순 정렬
        - i번째(1-indexed) 논문의 인용수가 i 이상인 최대 i값

        Returns: {"h_index": int, "papers": int, "citations": int}
        """
        cache_key = f"{doctor_name}:{hospital_name}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        affiliations = HOSPITAL_AFFILIATIONS.get(hospital_name, [hospital_name])

        try:
            pmids = await self._search_papers(doctor_name, affiliations)
            if not pmids:
                result = {"h_index": 0, "papers": 0, "citations": 0}
                self._cache[cache_key] = result
                return result

            citation_counts = await self._get_citations(pmids)
            citation_counts.sort(reverse=True)
            h_index = sum(1 for i, c in enumerate(citation_counts) if c >= i + 1)

            result = {
                "h_index": h_index,
                "papers": len(pmids),
                "citations": sum(citation_counts),
            }
            self._cache[cache_key] = result
            return result

        except Exception as e:
            print(f"[PubMed] Error for {doctor_name}: {e}")
            return {"h_index": 0, "papers": 0, "citations": 0}

    async def _search_papers(self, author_name: str, affiliations: list[str]) -> list[str]:
        """PubMed esearch — 저자+소속으로 PMID 목록 반환"""
        affil_query = " OR ".join(f'"{a}"[Affiliation]' for a in affiliations)
        query = f'"{author_name}"[Author] AND ({affil_query})'

        params: dict = {
            "db":      "pubmed",
            "term":    query,
            "retmax":  "200",
            "retmode": "json",
        }
        if NCBI_API_KEY:
            params["api_key"] = NCBI_API_KEY

        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_ssl_ctx())) as session:
            async with session.get(ESEARCH_URL, params=params, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json(content_type=None)
                return data.get("esearchresult", {}).get("idlist", [])

    async def _get_citations(self, pmids: list[str]) -> list[int]:
        """
        NIH iCite API로 인용수 일괄 조회.
        citation_count 필드 직접 제공, API 키 불필요, 100개/배치.
        https://icite.od.nih.gov/api
        """
        if not pmids:
            return []

        counts_map: dict[str, int] = {}
        batch_size = 100

        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=_ssl_ctx())) as session:
            for i in range(0, len(pmids), batch_size):
                batch = pmids[i:i + batch_size]
                try:
                    async with session.get(
                        ICITE_URL,
                        params={"pmids": ",".join(batch)},
                        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            for pub in data.get("data", []):
                                pmid = str(pub.get("pmid", ""))
                                counts_map[pmid] = int(pub.get("citation_count", 0))
                except Exception as e:
                    print(f"[iCite] batch {i//batch_size+1} error: {e}")

                if not NCBI_API_KEY:
                    await asyncio.sleep(0.2)

        return [counts_map.get(pid, 0) for pid in pmids]
