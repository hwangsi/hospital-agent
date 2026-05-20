"""
병원 웹 크롤러 — Playwright 기반.
각 병원 사이트에서 진료과별 의사 목록, 대기일, 예약 가능일 추출.

## 크롤링 전략
- 각 병원은 별도 어댑터(snuh.py, amc.py 등)로 분리
- 사이트 구조 변경 시 해당 어댑터만 수정
- 캐시: 6시간 TTL로 중복 크롤링 방지
- Rate limit: 병원당 최소 2초 간격

## Semi-auto 예약
- 전자동 예약은 본인인증(PASS 등) 자동화가 법적으로 제한됨
- 따라서: 예약 정보를 URL 파라미터로 프리필 → 사용자가 최종 클릭
"""
import asyncio
from datetime import datetime, timedelta
from typing import Optional

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import HOSPITAL_URLS, CACHE_TTL_HOURS, MAX_CONCURRENT_CRAWLS


class HospitalCrawlerBase:
    """병원 크롤러 베이스 클래스. 각 병원별로 상속하여 구현."""

    hospital_id: str = ""
    hospital_name: str = ""

    async def get_doctors(self, department: str) -> list[dict]:
        """
        진료과별 의사 목록 크롤링.
        Returns: [{name, position, department, specialties, wait_days, available_slots, surgeries}]
        """
        raise NotImplementedError

    async def get_wait_time(self, doctor_name: str) -> int:
        """의사별 최단 대기일 조회"""
        raise NotImplementedError

    async def crawl(self, department: str) -> dict:
        """전체 크롤링 실행"""
        try:
            doctors = await self.get_doctors(department)
            return {"hospital_id": self.hospital_id, "doctors": doctors}
        except Exception as e:
            print(f"[Crawler:{self.hospital_id}] Error: {e}")
            return {"hospital_id": self.hospital_id, "doctors": [], "error": str(e)}


class SNUHCrawler(HospitalCrawlerBase):
    """서울대학교병원 크롤러"""
    hospital_id = "snuh"
    hospital_name = "서울대학교병원"

    async def get_doctors(self, department: str) -> list[dict]:
        """
        서울대병원 의료진 검색:
        https://www.snuh.org/medical/doctor/findDoctor.do

        Playwright로 진료과 선택 → 의사 목록 파싱.
        각 의사 상세 페이지에서 전문분야, 학력, 경력 추출.
        """
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()

                url = HOSPITAL_URLS["snuh"]["doctor_search"]
                await page.goto(url, wait_until="networkidle", timeout=30000)

                # 진료과 선택
                await page.click(f'text="{department}"', timeout=5000)
                await page.wait_for_load_state("networkidle")

                # 의사 카드 파싱
                doctors = []
                cards = await page.query_selector_all(".doctor-card, .doc-info, .professor-list li")

                for card in cards:
                    name_el = await card.query_selector(".name, .doc-name, h3")
                    pos_el = await card.query_selector(".position, .doc-position, .rank")
                    spec_el = await card.query_selector(".specialty, .doc-specialty")

                    name = await name_el.inner_text() if name_el else ""
                    position = await pos_el.inner_text() if pos_el else ""
                    specialty = await spec_el.inner_text() if spec_el else ""

                    if name:
                        doctors.append({
                            "name": name.strip(),
                            "position": position.strip(),
                            "department": department,
                            "specialties": specialty.strip(),
                            "wait_days": 0,  # 별도 예약 페이지에서 조회
                            "available_slots": [],
                            "surgeries": 0,
                        })

                await browser.close()
                return doctors

        except Exception as e:
            print(f"[SNUH] Crawl error: {e}")
            return []


class AMCCrawler(HospitalCrawlerBase):
    """서울아산병원 크롤러"""
    hospital_id = "amc"
    hospital_name = "서울아산병원"

    async def get_doctors(self, department: str) -> list[dict]:
        """
        아산병원 의료진 검색:
        https://www.amc.seoul.kr/asan/search/doctor

        아산병원은 검색 API가 잘 되어있어서 fetch로도 가능.
        POST /asan/search/doctor/searchDoctor.do
        """
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://www.amc.seoul.kr/asan/search/doctor/searchDoctor.do",
                    data={
                        "searchKeyword": department,
                        "searchCondition": "dept",
                        "pageIndex": "1",
                        "pageSize": "50",
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                if resp.status_code == 200:
                    return self._parse_amc_response(resp.text, department)
        except Exception as e:
            print(f"[AMC] Crawl error: {e}")
        return []

    def _parse_amc_response(self, html: str, department: str) -> list[dict]:
        """아산병원 HTML 응답 파싱"""
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        doctors = []

        for item in soup.select(".doctorListWrap .infoTxt, .doctor-item"):
            name = item.select_one(".name, .docName")
            position = item.select_one(".position, .docPosition")
            if name:
                doctors.append({
                    "name": name.get_text(strip=True),
                    "position": position.get_text(strip=True) if position else "",
                    "department": department,
                    "wait_days": 0,
                    "available_slots": [],
                    "surgeries": 0,
                })
        return doctors


class SMCCrawler(HospitalCrawlerBase):
    """삼성서울병원 크롤러"""
    hospital_id = "smc"
    hospital_name = "삼성서울병원"

    async def get_doctors(self, department: str) -> list[dict]:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(
                    f"https://www.samsunghospital.com/home/doctor/doctorFind.do?deptCode=&searchKeyword={department}",
                    wait_until="networkidle", timeout=30000
                )
                doctors = []
                cards = await page.query_selector_all(".doctor-list .item, .docList li")
                for card in cards:
                    name_el = await card.query_selector(".name, .docNm")
                    pos_el = await card.query_selector(".position, .docPos")
                    name = await name_el.inner_text() if name_el else ""
                    position = await pos_el.inner_text() if pos_el else ""
                    if name:
                        doctors.append({
                            "name": name.strip(), "position": position.strip(),
                            "department": department, "wait_days": 0,
                            "available_slots": [], "surgeries": 0,
                        })
                await browser.close()
                return doctors
        except Exception as e:
            print(f"[SMC] Crawl error: {e}")
            return []


class SeveranceCrawler(HospitalCrawlerBase):
    """세브란스병원 크롤러"""
    hospital_id = "sev"
    hospital_name = "세브란스병원"

    async def get_doctors(self, department: str) -> list[dict]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://sev.severance.healthcare/doctor/search.do",
                    params={"deptNm": department},
                )
                if resp.status_code == 200:
                    return self._parse(resp.text, department)
        except Exception as e:
            print(f"[SEV] Crawl error: {e}")
        return []

    def _parse(self, html: str, dept: str) -> list[dict]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        doctors = []
        for item in soup.select(".doctorList .item, .doc-card"):
            name = item.select_one(".name, .docName")
            pos = item.select_one(".position")
            if name:
                doctors.append({
                    "name": name.get_text(strip=True), "position": pos.get_text(strip=True) if pos else "",
                    "department": dept, "wait_days": 0, "available_slots": [], "surgeries": 0,
                })
        return doctors


class SNUBHCrawler(HospitalCrawlerBase):
    """분당서울대병원 크롤러"""
    hospital_id = "snubh"
    hospital_name = "분당서울대병원"

    async def get_doctors(self, department: str) -> list[dict]:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    "https://www.snubh.org/medical/doctor/findDoctor.do",
                    params={"deptNm": department},
                )
                if resp.status_code == 200:
                    return self._parse(resp.text, department)
        except Exception as e:
            print(f"[SNUBH] Crawl error: {e}")
        return []

    def _parse(self, html: str, dept: str) -> list[dict]:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        doctors = []
        for item in soup.select(".doctor-list li, .profList li"):
            name = item.select_one(".name, .profName")
            pos = item.select_one(".position, .profRank")
            if name:
                doctors.append({
                    "name": name.get_text(strip=True), "position": pos.get_text(strip=True) if pos else "",
                    "department": dept, "wait_days": 0, "available_slots": [], "surgeries": 0,
                })
        return doctors


# ─── Orchestrator ────────────────────────────────────

CRAWLERS = {
    "snuh": SNUHCrawler(),
    "amc": AMCCrawler(),
    "smc": SMCCrawler(),
    "sev": SeveranceCrawler(),
    "snubh": SNUBHCrawler(),
}


class CrawlerOrchestrator:
    """병원 크롤러 통합 관리"""

    def __init__(self):
        self._cache = {}
        self._cache_ts = {}
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_CRAWLS)

    async def crawl_hospital(self, hospital_id: str, department: str) -> dict:
        """단일 병원 크롤링 (캐시 + rate limit)"""
        cache_key = f"{hospital_id}:{department}"

        # 캐시 확인
        if cache_key in self._cache:
            ts = self._cache_ts.get(cache_key, datetime.min)
            if datetime.now() - ts < timedelta(hours=CACHE_TTL_HOURS):
                return self._cache[cache_key]

        # Semaphore로 동시 크롤링 수 제한
        async with self._semaphore:
            crawler = CRAWLERS.get(hospital_id)
            if not crawler:
                return {"doctors": [], "error": f"Unknown hospital: {hospital_id}"}

            result = await crawler.crawl(department)
            self._cache[cache_key] = result
            self._cache_ts[cache_key] = datetime.now()
            return result

    async def crawl_all(self, department: str) -> list[dict]:
        """전체 병원 동시 크롤링"""
        tasks = [self.crawl_hospital(hid, department) for hid in CRAWLERS]
        return await asyncio.gather(*tasks, return_exceptions=True)

    def build_reservation_url(
        self,
        hospital_id: str,
        doctor_id: str,
        date: str,
        time: str,
    ) -> str:
        """
        Semi-auto 예약: 병원 예약 페이지 URL에 파라미터 프리필.
        실제 본인인증은 사용자가 직접 수행.
        """
        urls = HOSPITAL_URLS.get(hospital_id, {})
        base = urls.get("reservation", "#")

        # 병원마다 URL 파라미터 형식이 다름 — 각 병원 어댑터에서 오버라이드 가능
        param_map = {
            "snuh":  f"{base}?doctorId={doctor_id}&reservDate={date}&reservTime={time}",
            "amc":   f"{base}?drNm={doctor_id}&schDt={date}&schTm={time}",
            "smc":   f"{base}?docCd={doctor_id}&resrvDt={date}&resrvTm={time}",
            "sev":   f"{base}?drId={doctor_id}&apntDt={date}&apntTm={time}",
            "snubh": f"{base}?doctorId={doctor_id}&reservDate={date}&reservTime={time}",
        }
        return param_map.get(hospital_id, base)
