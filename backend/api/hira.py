"""
심평원(HIRA) 데이터 클라이언트 — 하이브리드 소스

설계 (HANDOFF 4번 결정사항: "하이브리드"):
  · 수술건수/사망률/재원일수 등 정량 지표  →  로컬 CSV (data/hira_stats.csv)
        병원별 × 질환별 수술건수·사망률은 공개 REST API로 사실상 제공되지 않으므로,
        HIRA 공개통계/적정성평가 보고서에서 추출한 수치를 CSV로 적재한다.
  · 적정성평가 "등급"(평가항목별 우수기관)  →  data.go.kr 실 API
        우수기관병원평가정보서비스(B551182/exclInstHospAsmInfoService1/getExclInstHospAsmInfo1)에서
        평가항목별 우수기관 목록을 받아 병원명(yadmNm)으로 매칭한다(암호화 ykiho 불필요).

CSV 행이 없으면 정량 지표는 "추정치(isEstimate=True)"로 명확히 표기한다(가짜를 실데이터로
위장하지 않는다). API 키(data.go.kr serviceKey)가 없으면 등급(grades)은 빈 배열을 반환한다.

폐기됨: opendata.hira.or.kr 의 olap*.do 경로는 REST API가 아니라 Any-ID SSO 로그인
웹페이지를 반환하므로(키가 있어도 동작 안 함) 전부 제거되었다.
"""
import os
import csv
import ssl
import asyncio
from typing import Optional
from datetime import datetime

import aiohttp

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import (
    HIRA_API_KEY,
    REQUEST_TIMEOUT,
    PUBLIC_DATA_BASE,
    HIRA_EXCL_ASM_PATH,
)

# 병원별 요양기관번호(평문) — 참고용/병원 식별용. 우수기관 등급은 yadmNm 매칭이라 미사용.
HOSPITAL_CODES = {
    "snuh":  "11100338",
    "amc":   "11100575",
    "smc":   "11100530",
    "sev":   "11100321",
    "snubh": "31101366",
}

# 우수기관 목록의 병원명(yadmNm) — 빅5의 "정식 등록명"(정규화) 정확일치용.
# 부분일치는 동명 분원(강남/용인/원주 세브란스, 분당서울대, 강릉아산 등)을 잘못 흡수하므로 금지.
# 값은 라이브 응답(getExclInstHospAsmInfo1)에서 확인한 실제 yadmNm 의 정규화형이다.
HOSPITAL_YADM = {
    "snuh":  {"서울대학교병원"},
    "amc":   {"재단법인아산사회복지재단서울아산병원", "서울아산병원"},
    "smc":   {"삼성서울병원"},
    "sev":   {"연세대학교의과대학세브란스병원"},          # 신촌 본원만 (강남/용인 제외)
    "snubh": {"분당서울대학교병원"},
}


def _norm(s: str) -> str:
    return (s or "").replace(" ", "").replace("(", "").replace(")", "")

CSV_COLUMNS = (
    "hospital_id,kcd_code,annualSurgeries,annualCases,mortalityRate,"
    "complicationRate,readmissionRate,avgLOS,trend,dataYear,source"
)


def _key_ready() -> bool:
    return bool(HIRA_API_KEY) and "YOUR_HIRA" not in HIRA_API_KEY


class HIRAClient:
    """심평원 데이터 — CSV(정량) + data.go.kr 적정성평가 등급(API) 하이브리드."""

    def __init__(self):
        self._cache: dict = {}          # surgery_stats 결과 캐시
        self._grade_cache: dict = {}    # '__index__' -> {yadmNm: [grades]}
        self._index_lock = asyncio.Lock()   # 우수기관 목록 1회만 적재
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self._csv_path = os.path.join(base_dir, "data", "hira_stats.csv")

    # ──────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────

    async def get_surgery_stats(
        self,
        hospital_id: str,
        kcd_code: str,
        year: Optional[str] = None,
    ) -> dict:
        """
        병원별 질환(KCD) 통계 반환.

        반환 구조 = (CSV 또는 추정) 정량지표  ⊕  (API) 적정성평가 등급(grades).
        """
        cache_key = f"{hospital_id}:{kcd_code}:{year or 'latest'}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if hospital_id not in HOSPITAL_CODES:
            return {"error": f"Unknown hospital: {hospital_id}"}

        if not year:
            year = str(datetime.now().year - 1)

        # 1) 정량 지표 — CSV 우선, 없으면 추정치(명확히 표기)
        stats = self._load_from_csv(hospital_id, kcd_code)
        if stats is None:
            stats = self._estimate_stats(hospital_id, kcd_code)

        # 2) 적정성평가 등급 — data.go.kr 실 API (키 있을 때만)
        stats["grades"] = await self._get_grades(hospital_id)

        self._cache[cache_key] = stats
        return stats

    async def get_quality_evaluation(self, hospital_id: str) -> dict:
        """병원 적정성평가 등급만 단건 조회(외부 직접 호출용)."""
        if hospital_id not in HOSPITAL_CODES:
            return {}
        return {"grades": await self._get_grades(hospital_id)}

    # ──────────────────────────────────────────────────
    # 1. CSV (정량 지표)
    # ──────────────────────────────────────────────────

    def _load_from_csv(self, hospital_id: str, kcd_code: str) -> Optional[dict]:
        if not os.path.exists(self._csv_path):
            return None
        try:
            with open(self._csv_path, mode="r", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    if row.get("hospital_id") == hospital_id and row.get("kcd_code") == kcd_code:
                        return self._row_to_stats(row)
        except Exception as e:
            print(f"[HIRA CSV] read error: {e}")
        return None

    @staticmethod
    def _row_to_stats(row: dict) -> dict:
        def _i(k):
            try:
                return int(float(row.get(k, 0) or 0))
            except (ValueError, TypeError):
                return 0

        def _f(k):
            try:
                return round(float(row.get(k, 0) or 0), 1)
            except (ValueError, TypeError):
                return 0.0

        # trend 컬럼 형식: "YYYY:count|YYYY:count|..." (연도 명시, 병원별 가용연도 상이)
        trend = []
        for part in str(row.get("trend", "") or "").split("|"):
            y, sep, v = part.partition(":")
            if sep:
                try:
                    trend.append({"year": int(y.strip()), "surgeries": int(float(v.strip()))})
                except (ValueError, TypeError):
                    pass

        return {
            "annualSurgeries":  _i("annualSurgeries"),
            "annualCases":      _i("annualCases"),
            "mortalityRate":    _f("mortalityRate"),
            "complicationRate": _f("complicationRate"),
            "readmissionRate":  _f("readmissionRate"),
            "avgLOS":           _i("avgLOS"),
            "trend":            trend,
            "dataYear":         str(row.get("dataYear", "") or ""),
            "isEstimate":       False,
            "source":           row.get("source") or "병원 공식 통계 (data/hira_stats.csv)",
        }

    # ──────────────────────────────────────────────────
    # 2. 적정성평가 등급 — data.go.kr 우수기관병원평가정보서비스
    #    평가항목별 "우수기관" 목록을 1회 적재(전 병원 공용)하고 병원명으로 필터.
    # ──────────────────────────────────────────────────

    async def _get_grades(self, hospital_id: str) -> list:
        """빅5 병원이 '우수기관'으로 등재된 평가항목·등급 목록. 키 없으면 빈 배열."""
        if not _key_ready():
            return []
        index = await self._load_excellent_index()
        canon = {_norm(a) for a in HOSPITAL_YADM.get(hospital_id, set())}
        grades = []
        for nm, rows in index.items():
            if nm in canon:   # 정확일치(분원 오매칭 방지)
                grades.extend(rows)
        # (평가항목, 유형) 중복 제거
        seen, uniq = set(), []
        for g in grades:
            k = (g["item"], g["label"])
            if k not in seen:
                seen.add(k)
                uniq.append(g)
        return uniq

    async def _load_excellent_index(self) -> dict:
        """
        우수기관 목록 전체를 페이지네이션으로 적재 → {정규화 yadmNm: [{item,label,grade}]}.
        병원 무관 단일 캐시(_grade_cache['__index__']). 총 ~6600건, 락으로 1회만 적재.
        """
        if "__index__" in self._grade_cache:
            return self._grade_cache["__index__"]

        async with self._index_lock:
            # 락 획득 사이에 다른 코루틴이 이미 적재했을 수 있음
            if "__index__" in self._grade_cache:
                return self._grade_cache["__index__"]
            return await self._do_load_excellent_index()

    async def _do_load_excellent_index(self) -> dict:
        index: dict = {}
        ok = False
        try:
            page = 1
            while page <= 20:   # 안전 상한 (우수기관 목록은 수천 건 이하)
                params = {
                    "serviceKey": HIRA_API_KEY,
                    "pageNo": str(page),
                    "numOfRows": "500",
                    "_type": "json",
                }
                data = await self._get_json(PUBLIC_DATA_BASE + HIRA_EXCL_ASM_PATH, params)
                items = self._items(data)
                if not items:
                    break
                for it in items:
                    nm = _norm(str(it.get("yadmNm", "")))
                    if not nm:
                        continue
                    # 실응답: asmNm=평가항목, asmGrd=유형코드(int), asmGrdNm=유형명("최근우수"/"N회연속")
                    item_nm = str(it.get("asmNm") or it.get("asmItemNm") or it.get("itemNm") or "").strip()
                    label   = str(it.get("asmGrdNm") or it.get("grdNm") or "").strip()
                    grade   = str(it.get("asmGrd") or it.get("grade") or "").strip()
                    if item_nm:
                        index.setdefault(nm, []).append({
                            "item": item_nm,
                            "label": label or "우수기관",
                            "grade": grade,
                        })
                if len(items) < 500:
                    ok = True
                    break
                page += 1
            else:
                ok = True   # 페이지 상한 도달도 정상 종료로 간주
        except Exception as e:
            print(f"[HIRA excellent] load error: {e}")

        # 완전 성공 + 비어있지 않을 때만 캐시(일시 실패는 다음 요청에서 재시도)
        if ok and index:
            self._grade_cache["__index__"] = index
        return index

    # ──────────────────────────────────────────────────
    # HTTP / 파싱 헬퍼
    # ──────────────────────────────────────────────────

    @staticmethod
    async def _get_json(url: str, params: dict) -> dict:
        connector = aiohttp.TCPConnector(ssl=ssl.create_default_context())
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                url, params=params,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"HTTP {resp.status}")
                return await resp.json(content_type=None)

    @staticmethod
    def _items(data: dict) -> list:
        """data.go.kr 표준 응답 → item 리스트. 오류헤더면 빈 리스트."""
        if not isinstance(data, dict):
            return []
        resp = data.get("response", {})
        header = resp.get("header", {})
        code = str(header.get("resultCode", "")) if header else ""
        if code and code not in ("00", "0000"):
            print(f"[HIRA] API result: {header.get('resultMsg', code)}")
            return []
        items = (resp.get("body", {}) or {}).get("items", {})
        if not items:
            return []
        item = items.get("item", []) if isinstance(items, dict) else items
        if isinstance(item, dict):
            return [item]
        return item or []

    # ──────────────────────────────────────────────────
    # 3. CSV 미적재 시 추정치 (명확히 isEstimate=True)
    # ──────────────────────────────────────────────────

    def _estimate_stats(self, hospital_id: str, kcd_code: str) -> dict:
        """
        CSV에 해당 행이 없을 때만 사용하는 결정론적 추정치.
        실데이터가 아님을 isEstimate=True 와 source 문구로 명확히 표기한다.
        """
        s = sum(ord(c) for c in (hospital_id + kcd_code))

        def r(n: int) -> float:
            return ((s * 9301 + 49297 + n * 173) % 233280) / 233280.0

        factors = {"snuh": 1.1, "amc": 1.25, "smc": 1.05, "sev": 0.95, "snubh": 0.8}
        f = factors.get(hospital_id, 1.0)

        as_ = int(80 + r(1) * 600 * f)
        return {
            "annualSurgeries":  as_,
            "annualCases":      int(as_ * (1.5 + r(2) * 2)),
            "mortalityRate":    round(max(0.1, 2.5 - r(3) * 2.2), 1),
            "complicationRate": round(max(1.0, 8.0 - r(4) * 6.0), 1),
            "readmissionRate":  round(max(1.0, 10.0 - r(5) * 7.0), 1),
            "avgLOS":           int(4 + r(6) * 12),
            "trend": [
                {"year": 2023 + i, "surgeries": int(as_ * (0.85 + r(10 + i) * 0.3))}
                for i in range(3)
            ],
            "dataYear":   "2025",
            "isEstimate": True,
            "source":     "추정치 (공개통계 미적재 — data/hira_stats.csv 채우면 실데이터로 대체)",
        }
