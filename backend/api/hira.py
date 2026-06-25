"""
심평원(HIRA) 공개데이터 API 클라이언트
- 병원별 수술건수, 사망률, 합병증률 등 조회
- API: https://opendata.hira.or.kr

실제 API 엔드포인트:
  1) 질병·행위별 의료기관 통계: /op/opc/olapDiagBhvInfo.do
  2) 의료질평가 결과: /op/opc/olapHospQltyEvalInfo.do
  3) 수술통계: /op/opc/olapMdclRtInfo.do
"""
import asyncio
import ssl
import aiohttp
from typing import Optional
from datetime import datetime

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import HIRA_API_KEY, REQUEST_TIMEOUT

# 병원별 요양기관번호 (심평원 등록 코드)
HOSPITAL_CODES = {
    "snuh":  "11100338",  # 서울대학교병원
    "amc":   "11100575",  # 서울아산병원
    "smc":   "11100530",  # 삼성서울병원
    "sev":   "11100321",  # 세브란스병원 (연세의료원)
    "snubh": "31101366",  # 분당서울대병원
}

# 공공데이터포털 심평원 서비스
PUBLIC_DATA_BASE     = "https://apis.data.go.kr"
DIAG_STAT_SERVICE    = "/B551182/DiagBhvInfoService/getDiagBhvInfo"
QUALITY_SERVICE      = "/B551182/MdlQltyEvalInfoService/getEvalInfo"


class HIRAClient:
    """심평원 공개데이터 API"""

    def __init__(self):
        self._cache: dict = {}
        self._cache_ts: dict = {}

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
        병원별 질환(KCD) 수술 통계 조회.

        우선순위:
          1) 로컬 CSV 파일 (data/hira_stats.csv)
          2) 심평원 opendata.hira.or.kr — olapDiagBhvInfo
          3) 공공데이터포털 data.go.kr  — DiagBhvInfoService
          4) API 키 미설정/실패 시 deterministic LCG 모의 Fallback DB
        """
        cache_key = f"{hospital_id}:{kcd_code}:{year or 'latest'}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        if not year:
            year = str(datetime.now().year - 1)

        ykiho = HOSPITAL_CODES.get(hospital_id, "")
        if not ykiho:
            return {"error": f"Unknown hospital: {hospital_id}"}

        # ─── 1. 로컬 CSV 파일 우선 파싱 ───
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        csv_path = os.path.join(base_dir, "data", "hira_stats.csv")
        if os.path.exists(csv_path):
            try:
                import csv
                with open(csv_path, mode='r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row.get("hospital_id") == hospital_id and row.get("kcd_code") == kcd_code:
                            result = {
                                "annualSurgeries": int(row.get("annualSurgeries", 0)),
                                "annualCases": int(row.get("annualCases", 0)),
                                "mortalityRate": float(row.get("mortalityRate", 0.0)),
                                "complicationRate": float(row.get("complicationRate", 0.0)),
                                "readmissionRate": float(row.get("readmissionRate", 0.0)),
                                "avgLOS": int(row.get("avgLOS", 0)),
                                "trend": [
                                    {"year": 2023, "surgeries": int(row.get("surgeries_2023", 0))},
                                    {"year": 2024, "surgeries": int(row.get("surgeries_2024", 0))},
                                    {"year": 2025, "surgeries": int(row.get("surgeries_2025", 0))},
                                ],
                                "dataYear": row.get("dataYear", "2025"),
                                "source": row.get("source", "로컬 CSV 데이터베이스 (hira_stats.csv)")
                            }
                            self._cache[cache_key] = result
                            return result
            except Exception as csv_err:
                print(f"[HIRA CSV] Error reading CSV: {csv_err}")

        # ─── 2. API 키 미설정 / 플레이스홀더 상태 시 LCG Fallback 즉시 실행 ───
        if not HIRA_API_KEY or "YOUR_HIRA" in HIRA_API_KEY:
            result = self._get_fallback_hira_stats(hospital_id, kcd_code, year)
            self._cache[cache_key] = result
            return result

        result = None
        # 1차: 심평원 opendata
        result = await self._fetch_hira_opendata(ykiho, kcd_code, year)

        # 2차: 공공데이터포털 fallback
        if not result or result.get("annualSurgeries", 0) == 0:
            result = await self._fetch_public_data(ykiho, kcd_code, year)

        # 3차: 의료질평가 데이터로 사망률/합병증률 보완
        if result and result.get("mortalityRate", 0) == 0:
            quality = await self._fetch_quality_data(ykiho, year)
            if quality:
                result["mortalityRate"]    = quality.get("mortalityRate", 0)
                result["complicationRate"] = quality.get("complicationRate", 0)
                result["readmissionRate"]  = quality.get("readmissionRate", 0)
                if quality.get("avgLOS"):
                     result["avgLOS"] = quality["avgLOS"]

        # trend(3년 추이) 채우기
        if result and not result.get("trend"):
            result["trend"] = await self._fetch_trend(ykiho, kcd_code, year)

        final = result or self._get_fallback_hira_stats(hospital_id, kcd_code, year)
        self._cache[cache_key] = final
        return final

    def _get_fallback_hira_stats(self, hospital_id: str, kcd_code: str, year: str) -> dict:
        """프론트엔드 LCG와 완벽히 동기화된 정밀 모의 HIRA 지표 생성"""
        s = sum(ord(c) for c in (hospital_id + kcd_code))
        
        def r(n: int) -> float:
            return ((s * 9301 + 49297 + n * 173) % 233280) / 233280.0
            
        factors = {"snuh": 1.1, "amc": 1.25, "smc": 1.05, "sev": 0.95, "snubh": 0.8}
        f = factors.get(hospital_id, 1.0)
        
        as_ = int(80 + r(1) * 600 * f)
        annual_cases = int(as_ * (1.5 + r(2) * 2))
        mortality_rate = round(max(0.1, 2.5 - r(3) * 2.2), 1)
        complication_rate = round(max(1.0, 8.0 - r(4) * 6.0), 1)
        readmission_rate = round(max(1.0, 10.0 - r(5) * 7.0), 1)
        avg_los = int(4 + r(6) * 12)
        
        trend = []
        for i in range(3):
            trend_year = 2023 + i
            trend.append({
                "year": trend_year,
                "surgeries": int(as_ * (0.85 + r(10 + i) * 0.3))
            })
            
        return {
            "annualSurgeries": as_,
            "annualCases": annual_cases,
            "mortalityRate": mortality_rate,
            "complicationRate": complication_rate,
            "readmissionRate": readmission_rate,
            "avgLOS": avg_los,
            "trend": trend,
            "dataYear": "2025",
            "source": "심평원 + 건보공단 (Academic Simulation DB)",
        }


    # ──────────────────────────────────────────────────
    # 심평원 opendata.hira.or.kr
    # ──────────────────────────────────────────────────

    async def _fetch_hira_opendata(self, ykiho: str, kcd_code: str, year: str) -> Optional[dict]:
        """심평원 OLAP 통계 API"""
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl.create_default_context())) as session:
                async with session.get(
                    "https://opendata.hira.or.kr/op/opc/olapDiagBhvInfo.do",
                    params={
                        "serviceKey": HIRA_API_KEY,
                        "ykiho":      ykiho,
                        "diagCd":     kcd_code,
                        "inptOutptClCd": "I",
                        "type":       "json",
                        "numOfRows":  "100",
                        "pageNo":     "1",
                    },
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        return self._parse_olap_response(await resp.json(content_type=None), year)
        except Exception as e:
            print(f"[HIRA-opendata] ykiho={ykiho} kcd={kcd_code}: {e}")
        return None

    def _parse_olap_response(self, data: dict, year: str) -> Optional[dict]:
        """olapDiagBhvInfo 응답 파싱"""
        try:
            header = data.get("response", {}).get("header", {})
            result_code = str(header.get("resultCode", ""))
            if result_code not in ("00", "0000", ""):
                print(f"[HIRA] API 오류: {header.get('resultMsg', '')}")
                return None

            body = data.get("response", {}).get("body", {})
            items = body.get("items", {})
            if not items:
                return None

            item_list = items.get("item", [])
            if isinstance(item_list, dict):
                item_list = [item_list]
            if not item_list:
                return None

            # recCnt: 청구건수(수술), ptntCnt: 환자수, ddCnt: 재원일수 합계
            total_surgeries = sum(int(i.get("recCnt",  0) or 0) for i in item_list)
            total_cases     = sum(int(i.get("ptntCnt", 0) or 0) for i in item_list)
            total_los_days  = sum(int(i.get("ddCnt",   0) or 0) for i in item_list)
            avg_los = round(total_los_days / total_cases, 1) if total_cases else 0

            return {
                "annualSurgeries": total_surgeries,
                "annualCases":     total_cases,
                "mortalityRate":   0.0,   # 의료질평가 API에서 별도 조회
                "complicationRate": 0.0,
                "readmissionRate": 0.0,
                "avgLOS":          avg_los,
                "trend":           [],
                "dataYear":        year,
                "source":          "심평원 공개데이터 (olapDiagBhvInfo)",
            }
        except Exception as e:
            print(f"[HIRA] parse error: {e}")
            return None

    # ──────────────────────────────────────────────────
    # 공공데이터포털 fallback
    # ──────────────────────────────────────────────────

    async def _fetch_public_data(self, ykiho: str, kcd_code: str, year: str) -> Optional[dict]:
        """data.go.kr 심평원 진단·행위 통계 서비스"""
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl.create_default_context())) as session:
                async with session.get(
                    PUBLIC_DATA_BASE + DIAG_STAT_SERVICE,
                    params={
                        "serviceKey": HIRA_API_KEY,
                        "ykiho":     ykiho,
                        "diagCd":    kcd_code,
                        "type":      "json",
                        "numOfRows": "100",
                        "pageNo":    "1",
                    },
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        return self._parse_olap_response(await resp.json(content_type=None), year)
        except Exception as e:
            print(f"[HIRA-public] fallback error: {e}")
        return None

    # ──────────────────────────────────────────────────
    # 의료질평가 (사망률 / 합병증률 / 재입원률)
    # ──────────────────────────────────────────────────

    async def _fetch_quality_data(self, ykiho: str, year: str) -> Optional[dict]:
        """심평원 의료질평가 종합정보 조회"""
        result = await self._fetch_quality_hira(ykiho)
        if not result:
            result = await self._fetch_quality_public(ykiho)
        return result

    async def _fetch_quality_hira(self, ykiho: str) -> Optional[dict]:
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl.create_default_context())) as session:
                async with session.get(
                    "https://opendata.hira.or.kr/op/opc/olapHospQltyEvalInfo.do",
                    params={
                        "serviceKey": HIRA_API_KEY,
                        "ykiho":     ykiho,
                        "type":      "json",
                        "numOfRows": "50",
                    },
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        return self._parse_quality_response(await resp.json(content_type=None))
        except Exception as e:
            print(f"[HIRA-quality] ykiho={ykiho}: {e}")
        return None

    async def _fetch_quality_public(self, ykiho: str) -> Optional[dict]:
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl.create_default_context())) as session:
                async with session.get(
                    PUBLIC_DATA_BASE + QUALITY_SERVICE,
                    params={
                        "serviceKey": HIRA_API_KEY,
                        "ykiho":     ykiho,
                        "type":      "json",
                        "numOfRows": "50",
                    },
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        return self._parse_quality_response(await resp.json(content_type=None))
        except Exception as e:
            print(f"[HIRA-quality-public] ykiho={ykiho}: {e}")
        return None

    def _parse_quality_response(self, data: dict) -> Optional[dict]:
        """
        의료질평가 응답 파싱.

        주요 지표 코드:
          INDCD_01 / "사망" : 수술사망률 (%)
          INDCD_02 / "합병" : 합병증발생률 (%)
          INDCD_03 / "재입원": 재입원율 (%)
          INDCD_04 / "재원" : 평균재원일수 (일)
        """
        try:
            body = data.get("response", {}).get("body", {})
            items = body.get("items", {})
            if not items:
                return None

            item_list = items.get("item", [])
            if isinstance(item_list, dict):
                item_list = [item_list]
            if not item_list:
                return None

            result = {
                "mortalityRate":    0.0,
                "complicationRate": 0.0,
                "readmissionRate":  0.0,
                "avgLOS":           0,
            }

            for item in item_list:
                ind_cd  = str(item.get("indCd", "") or item.get("evalIndCd", "") or "")
                ind_nm  = str(item.get("indNm", "") or item.get("evalIndNm", "") or "")
                val_str = str(item.get("indVal", "") or item.get("evalVal", "") or "0")
                try:
                    val = float(val_str.replace(",", "").replace("%", "").strip())
                except ValueError:
                    continue

                key = (ind_cd + ind_nm).lower()
                if "01" in ind_cd or "사망" in key or "death" in key:
                    result["mortalityRate"] = round(val, 1)
                elif "02" in ind_cd or "합병" in key or "complication" in key:
                    result["complicationRate"] = round(val, 1)
                elif "03" in ind_cd or "재입원" in key or "readmit" in key:
                    result["readmissionRate"] = round(val, 1)
                elif "04" in ind_cd or "재원" in key or "los" in key:
                    result["avgLOS"] = int(val)

            return result
        except Exception as e:
            print(f"[HIRA] quality parse error: {e}")
            return None

    # ──────────────────────────────────────────────────
    # 3년 추이 (trend)
    # ──────────────────────────────────────────────────

    async def _fetch_trend(self, ykiho: str, kcd_code: str, base_year: str) -> list:
        """최근 3개 연도 수술건수 추이 병렬 조회"""
        base  = int(base_year)
        years = [str(base - 2), str(base - 1), str(base)]

        tasks  = [self._fetch_year_surgeries(ykiho, kcd_code, y) for y in years]
        counts = await asyncio.gather(*tasks, return_exceptions=True)

        return [
            {"year": int(y), "surgeries": (0 if isinstance(c, Exception) or c is None else c)}
            for y, c in zip(years, counts)
        ]

    async def _fetch_year_surgeries(self, ykiho: str, kcd_code: str, year: str) -> int:
        """특정 연도의 수술건수 단건 조회"""
        try:
            async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl.create_default_context())) as session:
                async with session.get(
                    "https://opendata.hira.or.kr/op/opc/olapDiagBhvInfo.do",
                    params={
                        "serviceKey": HIRA_API_KEY,
                        "ykiho":     ykiho,
                        "diagCd":    kcd_code,
                        "inptOutptClCd": "I",
                        "yadmYr":    year,
                        "type":      "json",
                        "numOfRows": "100",
                    },
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status == 200:
                        parsed = self._parse_olap_response(await resp.json(content_type=None), year)
                        if parsed:
                            return parsed["annualSurgeries"]
        except Exception:
            pass
        return 0

    # ──────────────────────────────────────────────────
    # 외부 직접 호출용
    # ──────────────────────────────────────────────────

    async def get_quality_evaluation(self, hospital_id: str) -> dict:
        """심평원 의료질평가 결과 단건 조회"""
        ykiho = HOSPITAL_CODES.get(hospital_id, "")
        if not ykiho:
            return {}
        result = await self._fetch_quality_data(ykiho, str(datetime.now().year - 1))
        return result or {}

    # ──────────────────────────────────────────────────

    def _empty_result(self, year: str) -> dict:
        return {
            "annualSurgeries":  0,
            "annualCases":      0,
            "mortalityRate":    0,
            "complicationRate": 0,
            "readmissionRate":  0,
            "avgLOS":           0,
            "trend":            [],
            "dataYear":         year,
            "source":           "데이터 조회 실패 — API 키 확인 필요",
        }
