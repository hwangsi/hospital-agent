"""
Semantic Scholar (S2) 저자 엔티티 기반 H-index — **빠른 단건 조회**.

OpenAlex 와 동일한 "disambiguation된 저자 엔티티" 접근이지만, S2 는 author 객체에 hIndex 가
**사전계산**되어 있어 저자 검색 **1요청**으로 최종 h-index 를 바로 얻는다(논문 일괄 조회 불필요).
→ PubMed 의 '논문 N개 fetch 후 h 계산' 대비 훨씬 빠르다. OpenAlex 가 레이트리밋(429)일 때의
빠른 1순위 폴백으로 사용한다.

체인:  OpenAlex(정밀·기관필터) → **Semantic Scholar(빠름)** → PubMed(최후 폴백)

참고: Google Scholar 는 공식 API가 없고 봇 차단(CAPTCHA)이 강해 서버 자동조회에 부적합 →
같은 '최종 h-index 1회 조회' 목적을 S2/OpenAlex 로 달성.
"""
import asyncio
import re
import ssl
import time
from urllib.parse import quote

import aiohttp

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import REQUEST_TIMEOUT

SEARCH_URL = "https://api.semanticscholar.org/graph/v1/author/search"
FIELDS = "name,hIndex,paperCount,citationCount,affiliations"

# 병원 → 매칭 힌트(있으면 affiliation 일치 우선)
HOSPITAL_HINTS = {
    "서울대학교병원":   ["seoul national", "snuh"],
    "서울아산병원":     ["asan", "ulsan"],
    "삼성서울병원":     ["samsung", "sungkyunkwan"],
    "세브란스병원":     ["severance", "yonsei"],
    "분당서울대병원":   ["seoul national", "bundang", "snubh"],
}


def _ssl_ctx() -> ssl.SSLContext:
    return ssl.create_default_context()


class SemanticScholarClient:
    """S2 author search 1요청으로 h-index. 실패/0건 시 PubMed 폴백."""

    _throttle_lock = asyncio.Lock()
    _last = 0.0

    # 회로차단 — 연속 429 시 S2를 일정 시간 건너뛰고 바로 PubMed 폴백(의사당 재시도 지연 제거)
    _fail_count = 0
    _cooldown_until = 0.0
    _COOLDOWN = 600.0
    _FAIL_THRESHOLD = 3

    def __init__(self, pubmed_fallback=None):
        self._cache: dict = {}
        self._pubmed = pubmed_fallback

    async def get_h_index(self, doctor_name: str, hospital_name: str,
                          name_en: str = "", department: str = "",
                          orcid: str = "") -> dict:
        """OpenAlexClient 와 동일한 호출 형태. name_en 없으면 바로 PubMed 폴백."""
        name_en = (name_en or "").strip()
        if not name_en:
            return await self._fallback(doctor_name, hospital_name, name_en)

        cache_key = f"s2:{name_en}:{hospital_name}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        # 회로차단 열림 → S2 건너뛰고 즉시 PubMed 폴백
        if time.monotonic() < SemanticScholarClient._cooldown_until:
            return await self._fallback(doctor_name, hospital_name, name_en)

        try:
            data = await self._get(
                f"{SEARCH_URL}?query={quote(name_en)}&fields={FIELDS}")
            cands = [a for a in ((data or {}).get("data") or [])
                     if self._name_ok(a, name_en)]
            if not cands:
                return await self._fallback(doctor_name, hospital_name, name_en)

            best = self._pick(cands, hospital_name)
            result = {
                "h_index":   int(best.get("hIndex") or 0),
                "papers":    int(best.get("paperCount") or 0),
                "citations": int(best.get("citationCount") or 0),
                "source":    "semantic-scholar",
            }
            SemanticScholarClient._fail_count = 0
            self._cache[cache_key] = result
            return result
        except Exception as e:
            SemanticScholarClient._fail_count += 1
            if SemanticScholarClient._fail_count >= SemanticScholarClient._FAIL_THRESHOLD:
                SemanticScholarClient._cooldown_until = time.monotonic() + SemanticScholarClient._COOLDOWN
                print(f"[S2] 회로차단 — {int(SemanticScholarClient._COOLDOWN/60)}분간 폴백 (last={e})")
            return await self._fallback(doctor_name, hospital_name, name_en)

    # ── 내부 ──

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").lower()).strip()

    def _name_ok(self, author: dict, name_en: str) -> bool:
        disp = self._norm(author.get("name", ""))
        toks = [t for t in re.split(r"[\s,\-]+", name_en.lower()) if len(t) >= 3]
        return bool(toks) and all(t in disp for t in toks)

    def _pick(self, cands: list[dict], hospital_name: str) -> dict:
        """소속 힌트가 affiliation 에 있으면 우선, 없으면 paperCount 최대(가장 저명한 엔티티)."""
        hints = HOSPITAL_HINTS.get(hospital_name, [])
        if hints:
            for a in sorted(cands, key=lambda x: x.get("paperCount") or 0, reverse=True):
                aff = " ".join(a.get("affiliations") or []).lower()
                if aff and any(h in aff for h in hints):
                    return a
        return max(cands, key=lambda a: a.get("paperCount") or 0)

    async def _throttle(self):
        # S2 무인증 권장 ~1req/s. 세마포어(_ENRICH_SEM)와 함께 0.34s 간격.
        async with SemanticScholarClient._throttle_lock:
            wait = 0.34 - (time.monotonic() - SemanticScholarClient._last)
            if wait > 0:
                await asyncio.sleep(wait)
            SemanticScholarClient._last = time.monotonic()

    async def _get(self, url: str) -> dict | None:
        last = None
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=_ssl_ctx())
        ) as session:
            for attempt in range(2):   # 회로차단이 빠르게 열리도록 2회로 제한
                await self._throttle()
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                    ) as resp:
                        if resp.status == 200:
                            return await resp.json(content_type=None)
                        last = resp.status
                        if resp.status in (429, 500, 502, 503, 504):
                            await asyncio.sleep(0.5)
                            continue
                        return None
                except Exception as e:
                    last = str(e)
                    await asyncio.sleep(0.5)
        raise RuntimeError(f"s2 failed after retries (last={last})")

    async def _fallback(self, doctor_name: str, hospital_name: str, name_en: str) -> dict:
        if self._pubmed is not None:
            r = dict(await self._pubmed.get_h_index(doctor_name, hospital_name, name_en=name_en))
            r["source"] = "pubmed-fallback"
            return r
        return {"h_index": 0, "papers": 0, "citations": 0, "source": "none"}
