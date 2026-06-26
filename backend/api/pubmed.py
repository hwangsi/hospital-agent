"""
PubMed E-utilities API — 의사별 H-index 산출
- esearch: 논문 검색 (저자명 + 소속기관)
- OpenAlex: 인용수 조회 (cited_by_count, 키 불필요)
  ※ 과거 NIH iCite(/api/pubs)를 썼으나 2026년 현재 404(폐지)라 OpenAlex로 교체.
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
from backend.utils.name_converter import (
    convert_korean_name_to_english,
    english_name_to_pubmed_variants,
)

ESEARCH_URL  = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
OPENALEX_URL = "https://api.openalex.org/works"
OPENALEX_MAILTO = "hospital-agent@example.com"   # OpenAlex polite pool 식별용

# 병원 영문명 (PubMed affiliation 검색용)
# 병원명 + 소속 의대/대학명을 함께 넣어 recall을 높인다. 빅5 교수는 병원명 대신
# 대학명(예: 성균관대·울산대)으로만 색인된 논문이 많아, 대학명을 빼면 h-index가 과소평가됨.
HOSPITAL_AFFILIATIONS = {
    "서울대학교병원":   ["Seoul National University Hospital", "Seoul National University", "SNUH"],
    "서울아산병원":     ["Asan Medical Center", "University of Ulsan", "Ulsan College of Medicine"],
    "삼성서울병원":     ["Samsung Medical Center", "Sungkyunkwan University"],
    "세브란스병원":     ["Severance Hospital", "Yonsei University"],
    "분당서울대병원":   ["Seoul National University Bundang Hospital", "Seoul National University", "SNUBH"],
}

def _ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    return ctx


def _is_initials_variant(v: str) -> bool:
    """'Park SH' / 'Lee J' 처럼 성+이니셜 형태인지 — 동명이인 과다집계를 유발한다."""
    import re
    return bool(re.match(r"^[A-Za-z\-]+\s+[A-Z]{1,3}$", (v or "").strip()))


class PubMedClient:
    """PubMed E-utilities + NIH iCite 기반 H-index 산출"""

    def __init__(self):
        self._cache: dict = {}

    async def get_h_index(self, doctor_name: str, hospital_name: str,
                           name_en: str = "") -> dict:
        """
        의사 이름 + 병원명으로 PubMed 검색 → H-index 산출.

        영문명 우선순위:
          1) name_en — 병원 사이트가 제공한 실제 영문명(서울대 로마자/세브란스 nmEn). 최정확.
          2) 없으면 한글 이름을 자모 분해 로마자로 변환.
        키 미등록이나 검색 0건 시 deterministic hash 기반 Fallback 학술지표를 반환합니다.
        """
        cache_key = f"{doctor_name}:{hospital_name}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        all_affils = HOSPITAL_AFFILIATIONS.get(hospital_name, [hospital_name])

        try:
            if name_en:
                # 권위 영문명(영문 명단 등재 = 주로 정교수급) → 풀 변형 + 넓은 소속(대학 포함)
                eng_names = english_name_to_pubmed_variants(name_en)
                affiliations = all_affils
            else:
                # 영문명 미상(주로 임상강사) → 동명이인 과다집계 방지:
                # 이니셜형('Park SH') 제외한 풀네임 변형만 + 병원 소속만(대학 제외).
                variants = convert_korean_name_to_english(doctor_name)
                full = [v for v in variants if not _is_initials_variant(v)]
                eng_names = full or variants
                affiliations = all_affils[:1]

            pmids = await self._search_papers(eng_names, affiliations) if eng_names else []

            # 검색 0건 → 가짜 해시값 대신 정직하게 0 (임상강사는 출판 이력이 적거나 영문명 불일치)
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
            # 일시적 오류 → 가짜 대신 0, 단 캐싱하지 않아 다음 검색에서 재시도
            print(f"[PubMed] Error for {doctor_name}: {e}")
            return {"h_index": 0, "papers": 0, "citations": 0}

    async def _search_papers(self, eng_names: list[str], affiliations: list[str]) -> list[str]:
        """PubMed esearch — 여러 영문 저자명 조합 + 소속으로 PMID 목록 반환"""
        # (("Ahn KR"[Author] OR "Ahn Kyu Ri"[Author]) AND ("SNUH"[Affiliation] OR "Seoul National University Hospital"[Affiliation]))
        author_query = " OR ".join(f'"{name}"[Author]' for name in eng_names)
        affil_query = " OR ".join(f'"{a}"[Affiliation]' for a in affiliations)
        query = f'({author_query}) AND ({affil_query})'

        params: dict = {
            "db":      "pubmed",
            "term":    query,
            "retmax":  "500",   # 200은 다작 교수의 논문을 잘라 h-index를 과소평가함
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
        """OpenAlex API로 PMID별 인용수(cited_by_count) 일괄 조회"""
        if not pmids:
            return []

        counts_map: dict[str, int] = {}
        batch_size = 50   # OpenAlex OR 필터 권장 상한

        headers = {"User-Agent": f"hospital-agent/1.0 (mailto:{OPENALEX_MAILTO})"}
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=_ssl_ctx()), headers=headers
        ) as session:
            for i in range(0, len(pmids), batch_size):
                batch = pmids[i:i + batch_size]
                try:
                    async with session.get(
                        OPENALEX_URL,
                        params={
                            "filter": "pmid:" + "|".join(batch),
                            "per-page": "200",
                            "select": "ids,cited_by_count",
                            "mailto": OPENALEX_MAILTO,
                        },
                        timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json(content_type=None)
                            for work in data.get("results", []):
                                pmid_url = (work.get("ids", {}) or {}).get("pmid", "") or ""
                                pmid = pmid_url.rstrip("/").split("/")[-1]
                                if pmid:
                                    counts_map[pmid] = int(work.get("cited_by_count", 0) or 0)
                except Exception as e:
                    print(f"[OpenAlex] batch {i//batch_size+1} error: {e}")

                await asyncio.sleep(0.1)   # polite pool 호출 간격

        return [counts_map.get(pid, 0) for pid in pmids]
