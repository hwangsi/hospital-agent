"""
OpenAlex 저자 엔티티 기반 H-index 산출 (1순위, PubMed 폴백).

PubMed 이름-문자열 매칭(`"Kim K"[Author]`)은 동명이인을 구분하지 못해 500 cap·h-index
왜곡을 낳는다. OpenAlex는 ML로 논문을 **disambiguation된 저자 엔티티**에 귀속시키므로
특정 인물 1명의 지표를 얻을 수 있다.

레퍼런스 구현: https://github.com/hwangsi/researcher-kg
  - js/api.js  searchAuthors  : /authors?search=&filter=affiliations.institution.id  → 후보 → 필터
  - js/api.js  _dedupeAndGroup: 동명 + 공유 기관 → 분리 ID 동일인 그룹 (여기선 자동 병합)
  - js/dashboard.js _renderStats: works 합집합 cited_by_count 정렬로 h 직접계산(ACCURATE 모드)

설계 문서: docs/H-INDEX-OPENALEX-DESIGN.md

전략(BATCH 기본):
  1. orcid 있으면 orcid 필터로 확정. 없으면 name_en + 병원 기관ID 필터로 후보 조회.
  2. 이름 토큰 일치 후보만. 동명+동일기관은 분리 ID 동일인으로 자동 병합.
  3. h = 그룹 내 summary_stats.h_index 최댓값(분리 ID 합산 불가 → 하한),
     papers/citations = 그룹 합산. (정밀값은 ACCURATE 모드 compute_accurate_h)
  4. 0건/레이트리밋/오류 → PubMed(수정본) 폴백.

서버측 레이트리밋 방어: polite pool(mailto) + 전역 스로틀 + 3회 백오프 + 결과 캐시.
"""
import asyncio
import re
import ssl
import time
from urllib.parse import quote

import aiohttp

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import OPENALEX_MAILTO, REQUEST_TIMEOUT

BASE = "https://api.openalex.org"
AUTHOR_SELECT = "id,display_name,orcid,works_count,cited_by_count,summary_stats,affiliations"

# 병원 → OpenAlex 기관 검색 키워드. 런타임 1회 해석 후 ID 캐시(_inst_cache).
# 병원명 + 모대학을 함께 넣어 recall ↑ (빅5 교수는 대학명으로만 색인된 논문이 많음 — pubmed.py와 동일 근거).
HOSPITAL_INST_KEYWORDS = {
    "서울대학교병원":   ["Seoul National University Hospital", "Seoul National University"],
    "서울아산병원":     ["Asan Medical Center", "University of Ulsan"],
    "삼성서울병원":     ["Samsung Medical Center", "Sungkyunkwan University"],
    "세브란스병원":     ["Severance Hospital", "Yonsei University"],
    "분당서울대병원":   ["Seoul National University Bundang Hospital", "Seoul National University"],
}


def _ssl_ctx() -> ssl.SSLContext:
    return ssl.create_default_context()


class OpenAlexClient:
    """OpenAlex 저자 엔티티 H-index. 실패 시 주입된 PubMedClient로 폴백."""

    # 전역 스로틀 — polite pool은 10req/s 안정. 0.12s 간격 + 세마포어(main의 _ENRICH_SEM)로
    # 검색당 수십 명 동시 보강 시에도 429를 방지.
    _throttle_lock = asyncio.Lock()
    _last = 0.0

    def __init__(self, pubmed_fallback=None):
        self._cache: dict = {}        # (orcid|name_en, hospital) -> 결과
        self._inst_cache: dict = {}   # hospital_name -> [institution id]
        self._pubmed = pubmed_fallback

    # ──────────────────────────────────────────────────
    # Public
    # ──────────────────────────────────────────────────

    async def get_h_index(self, doctor_name: str, hospital_name: str,
                          name_en: str = "", department: str = "",
                          orcid: str = "") -> dict:
        """
        반환: {h_index, papers, citations, source[, oa_author_id]}.
        PubMed 클라이언트와 동일한 호출 형태 → main.py 교체만으로 동작.
        name_en/ orcid 둘 다 없으면(주로 임상강사) OpenAlex 매칭 신뢰도가 낮아 바로 PubMed 폴백.
        """
        name_en = (name_en or "").strip()
        orcid = (orcid or "").strip()

        if not name_en and not orcid:
            return await self._fallback(doctor_name, hospital_name, name_en)

        cache_key = f"{orcid or name_en}:{hospital_name}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            if orcid:
                data = await self._get(f"/authors?filter=orcid:{quote(orcid)}"
                                       f"&select={AUTHOR_SELECT}&per-page=5")
                cands = (data or {}).get("results", []) if data else []
            else:
                inst_ids = await self._resolve_institutions(hospital_name)
                filt = (f"affiliations.institution.id:{'|'.join(inst_ids)}"
                        if inst_ids else "affiliations.institution.country_code:KR")
                data = await self._get(f"/authors?search={quote(name_en)}&filter={filt}"
                                       f"&select={AUTHOR_SELECT}&per-page=25")
                cands = [a for a in ((data or {}).get("results", []) if data else [])
                         if self._name_ok(a, name_en)]

            if not cands:
                return await self._fallback(doctor_name, hospital_name, name_en)

            group = self._merge_same_person(cands)
            rep = max(group, key=lambda a: a.get("works_count") or 0)
            result = {
                "h_index":   max((self._h_of(a) for a in group), default=0),
                "papers":    sum((a.get("works_count") or 0) for a in group),
                "citations": sum((a.get("cited_by_count") or 0) for a in group),
                "oa_author_id": rep.get("id", ""),
                "source":    "openalex",
            }
            self._cache[cache_key] = result
            return result

        except Exception as e:
            print(f"[OpenAlex] {name_en or doctor_name}/{hospital_name}: {e}")
            return await self._fallback(doctor_name, hospital_name, name_en)

    async def compute_accurate_h(self, author_ids: list[str]) -> dict:
        """
        ACCURATE 모드 — 분리 ID 합집합 works 의 cited_by_count 로 h 직접계산
        (researcher-kg dashboard.js _renderStats 와 동일). 비용 ↑ → 상위 노출/상세에만.
        """
        all_cites: list[int] = []
        seen_work: set = set()
        for aid in author_ids:
            short = aid.rsplit("/", 1)[-1]
            cursor = "*"
            for _ in range(100):  # 안전 상한 (200/page)
                data = await self._get(
                    f"/works?filter=author.id:{short}&per-page=200"
                    f"&cursor={quote(cursor)}&select=id,cited_by_count")
                if not data:
                    break
                for w in data.get("results", []):
                    wid = w.get("id")
                    if wid and wid not in seen_work:
                        seen_work.add(wid)
                        all_cites.append(int(w.get("cited_by_count") or 0))
                cursor = (data.get("meta") or {}).get("next_cursor")
                if not cursor:
                    break
        all_cites.sort(reverse=True)
        h = sum(1 for i, c in enumerate(all_cites) if c >= i + 1)
        return {"h_index": h, "papers": len(all_cites),
                "citations": sum(all_cites), "source": "openalex-accurate"}

    # ──────────────────────────────────────────────────
    # 내부
    # ──────────────────────────────────────────────────

    @staticmethod
    def _h_of(author: dict) -> int:
        return int((author.get("summary_stats") or {}).get("h_index") or 0)

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "").lower()).strip()

    @staticmethod
    def _tokens(s: str) -> list[str]:
        return [t for t in re.split(r"[\s,;/|\-]+", (s or "").lower()) if t]

    def _name_ok(self, author: dict, name_en: str) -> bool:
        """이름 토큰(3글자+ 전체 토큰)이 모두 display_name 에 포함되는지 (researcher-kg _matchesCriteria)."""
        disp = self._norm(author.get("display_name", ""))
        toks = [t for t in self._tokens(name_en) if len(t) >= 3]
        return bool(toks) and all(t in disp for t in toks)

    def _merge_same_person(self, cands: list[dict]) -> list[dict]:
        """
        분리 ID 자동 병합 — 대표(works_count 최대)와 **정규화 이름이 같은** 후보를 동일인으로 묶음.
        (모든 후보가 이미 동일 병원 기관필터를 통과 → 동명+동일기관=동일인 근사. 보수적.)
        """
        rep = max(cands, key=lambda a: a.get("works_count") or 0)
        rep_name = self._norm(rep.get("display_name", ""))
        group = [a for a in cands if self._norm(a.get("display_name", "")) == rep_name]
        return group or [rep]

    async def _resolve_institutions(self, hospital_name: str) -> list[str]:
        """병원 → OpenAlex 기관 ID 목록 (런타임 해석·캐시)."""
        if hospital_name in self._inst_cache:
            return self._inst_cache[hospital_name]
        ids: list[str] = []
        for kw in HOSPITAL_INST_KEYWORDS.get(hospital_name, [hospital_name]):
            try:
                data = await self._get(f"/institutions?search={quote(kw)}"
                                       f"&per-page=3&select=id,display_name")
                for it in ((data or {}).get("results", []) or [])[:2]:
                    oid = (it.get("id") or "").rsplit("/", 1)[-1]   # 'I12345'
                    if oid and oid not in ids:
                        ids.append(oid)
            except Exception as e:
                print(f"[OpenAlex inst] {kw}: {e}")
        self._inst_cache[hospital_name] = ids
        return ids

    async def _throttle(self):
        async with OpenAlexClient._throttle_lock:
            wait = 0.12 - (time.monotonic() - OpenAlexClient._last)
            if wait > 0:
                await asyncio.sleep(wait)
            OpenAlexClient._last = time.monotonic()

    async def _get(self, path: str) -> dict | None:
        """스로틀 + polite pool + 429/5xx 백오프(3회). 4xx(기타)는 즉시 None. 최종 실패는 예외."""
        sep = "&" if "?" in path else "?"
        url = f"{BASE}{path}{sep}mailto={OPENALEX_MAILTO}"
        last = None
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(ssl=_ssl_ctx())
        ) as session:
            for attempt in range(3):
                await self._throttle()
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
                    ) as resp:
                        if resp.status == 200:
                            return await resp.json(content_type=None)
                        last = resp.status
                        if resp.status in (429, 500, 502, 503, 504):
                            await asyncio.sleep(0.8 * (attempt + 1))
                            continue
                        return None   # 그 외 4xx → 재시도 무의미
                except Exception as e:
                    last = str(e)
                    await asyncio.sleep(0.8 * (attempt + 1))
        raise RuntimeError(f"openalex failed after retries (last={last})")

    async def _fallback(self, doctor_name: str, hospital_name: str, name_en: str) -> dict:
        """PubMed(수정본) 폴백. 없으면 0."""
        if self._pubmed is not None:
            r = dict(await self._pubmed.get_h_index(doctor_name, hospital_name, name_en=name_en))
            r["source"] = "pubmed-fallback"
            return r
        return {"h_index": 0, "papers": 0, "citations": 0, "source": "none"}
