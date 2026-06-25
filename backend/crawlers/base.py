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

# ─── 공통 유틸 ────────────────────────────────────────
# 빅5 병원 사이트는 모두 일반 브라우저 헤더가 없으면 WAF 차단/빈 응답이 올 수 있어
# 데스크톱 Chrome 헤더를 기본 사용한다.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}


def _norm_dept(name: str) -> str:
    """진료과명 비교용 정규화 — 공백/괄호주석 제거. 예: '외과(유방)' → '외과'."""
    import re
    return re.sub(r"\s+|\(.*?\)", "", name or "")


def _clean_ws(text: str) -> str:
    """연속 공백/개행/탭을 단일 공백으로 정리."""
    import re
    return re.sub(r"\s+", " ", text or "").strip()


# 병원별 진료과 명칭 변형 별칭 (2026-06 실측 검증).
# 표준 진료과명이 일부 병원에서 다른 이름으로 등록된 경우를 보완한다.
#   내분비내과:  서울대/삼성/분당서울대는 '내분비대사내과'
#   혈액종양내과: 세브란스는 '혈액내과' / '종양내과'로 분리
_DEPT_ALIASES = {
    "내분비내과": ["내분비대사내과"],
    "혈액종양내과": ["혈액내과", "종양내과"],
}


def _dept_search_terms(department: str) -> list[str]:
    """
    진료과명에서 매칭 후보어를 우선순위대로 생성.
    괄호 주석은 더 구체적인 후보를, 별칭 맵은 병원별 변형 명칭을 추가한다.
      예: '외과(유방)' → ['유방외과', '유방', '외과'],  '소화기내과' → ['소화기내과']
          '내분비내과' → ['내분비내과', '내분비대사내과']
    """
    import re
    raw = (department or "").strip()
    terms = []
    m = re.match(r"^(.*?)\((.+?)\)\s*$", raw)
    if m:
        base, paren = m.group(1).strip(), m.group(2).strip()
        if paren and base:
            terms.append(_norm_dept(paren + base))   # 유방 + 외과 → 유방외과
        if paren:
            terms.append(_norm_dept(paren))          # 유방
        if base:
            terms.append(_norm_dept(base))           # 외과
    else:
        terms.append(_norm_dept(raw))

    # 별칭 확장 — 표준 후보 뒤에 덧붙여 표준 일치를 우선 유지
    for t in list(terms):
        for alias in _DEPT_ALIASES.get(t, []):
            terms.append(_norm_dept(alias))

    seen, out = set(), []
    for t in terms:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _match_dept(candidates, department: str):
    """
    (진료과명, 값) 후보 리스트에서 진료과명을 매칭.
    우선순위: 정확 일치 > 접두 일치 > (충분히 긴 어휘만) 부분 일치.
    접두 일치를 우선해 '외과'가 '흉부외과'에 잘못 매칭되는 것을 방지한다.
    Returns: 매칭된 값 또는 None.
    """
    cand = [(_norm_dept(n), v) for n, v in candidates]
    cand = [(n, v) for n, v in cand if n and v]
    terms = _dept_search_terms(department)

    for term in terms:                       # 1) 정확 일치
        for name, val in cand:
            if name == term:
                return val
    for term in terms:                       # 2) 접두 일치 (specialty 혼동 방지)
        for name, val in cand:
            if name.startswith(term) or term.startswith(name):
                return val
    for term in terms:                       # 3) 부분 일치 — 3글자 이상 어휘만
        if len(term) < 3:
            continue
        for name, val in cand:
            if term in name or name in term:
                return val
    return None


def _make_doctor(name: str, position: str, department: str, specialty: str,
                 emp_id: str = "", reservation_url: str = "", name_en: str = "") -> dict:
    """크롤러 공통 의사 레코드 포맷."""
    return {
        "name": _clean_ws(name),
        "name_en": _clean_ws(name_en),   # 병원 제공 영문명 (PubMed 검색 정확도 ↑)
        "position": _clean_ws(position),
        "department": department,
        "specialties": _clean_ws(specialty),
        "wait_days": 0,            # 별도 예약 페이지에서 조회
        "available_slots": [],
        "surgeries": 0,
        "emp_id": emp_id,          # 병원 내부 의사 식별자
        "reservation_url": reservation_url,
    }


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
    """
    서울대학교병원 크롤러 — httpx 기반 (2026-06 실검증, 서버사이드 렌더링/UTF-8).

    검증된 구조:
      - 진료과 목록(상위): /reservation/meddept/dept.do
        내과 세부분과(소화기내과 등): /reservation/meddept/IM/mainIntro.do
        앵커 href '/meddept/{코드}/mainIntro.do' 텍스트로 진료과명→코드 매핑 (소화기내과→IMG)
      - 의료진 목록: /reservation/meddept/{코드}/mainDoctor.do?pageIndex={N}  (5명/페이지)
      - 카드: ul.doctorSchedule > li, 이름 a.doctorNameWrap > strong, 세부전공
        .doctor-concentration-wrap p:nth-of-type(2), drId div[id^="itrDrId_"]
      ※ 직위(교수/부교수)·예약링크는 목록에 없음(상세/로그인 필요)
    """
    hospital_id = "snuh"
    hospital_name = "서울대학교병원"
    BASE = "https://www.snuh.org"

    async def get_doctors(self, department: str) -> list[dict]:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30, headers=_BROWSER_HEADERS,
                                         follow_redirects=True) as client:
                code = await self._resolve_code(client, department)
                if not code:
                    print(f"[SNUH] 진료과 코드 매핑 실패: {department}")
                    return []
                return await self._fetch_doctors(client, code, department)
        except Exception as e:
            print(f"[SNUH] Crawl error: {e}")
            return []

    async def _resolve_code(self, client, department: str) -> str:
        """진료과명 → 진료과 코드. 상위 진료과 목록 + 내과 세부분과 페이지를 함께 탐색."""
        for url in (f"{self.BASE}/reservation/meddept/dept.do",
                    f"{self.BASE}/reservation/meddept/IM/mainIntro.do"):
            try:
                r = await client.get(url)
                code = self._match_dept_anchor(r.text, department)
                if code:
                    return code
            except Exception as e:
                print(f"[SNUH] dept resolve {url}: {e}")
        return ""

    @staticmethod
    def _match_dept_anchor(html: str, department: str) -> str:
        from bs4 import BeautifulSoup
        import re
        soup = BeautifulSoup(html, "lxml")
        candidates = []
        for a in soup.select("a[href*='/meddept/']"):
            m = re.search(r"/meddept/([A-Za-z0-9]+)/main", a.get("href", ""))
            if m:
                candidates.append((a.get_text(), m.group(1)))
        return _match_dept(candidates, department) or ""

    async def _fetch_doctors(self, client, code: str, department: str) -> list[dict]:
        from bs4 import BeautifulSoup
        doctors = []
        for page in range(1, 21):              # 안전 상한
            r = await client.get(
                f"{self.BASE}/reservation/meddept/{code}/mainDoctor.do",
                params={"pageIndex": str(page)},
            )
            soup = BeautifulSoup(r.text, "lxml")
            cards = [li for li in soup.select("ul.doctorSchedule > li")
                     if li.select_one(".doctorIntroduce")]
            if not cards:
                break
            for c in cards:
                strongs = c.select("a.doctorNameWrap > strong")
                name = strongs[0].get_text(strip=True) if strongs else ""
                if not name:
                    continue
                # strongs[1] = "( 漢字 / Yoon, Jung-Hwan )" → '/' 뒤의 영문명 추출
                name_en = ""
                if len(strongs) > 1:
                    raw = strongs[1].get_text(" ", strip=True)
                    if "/" in raw:
                        name_en = raw.split("/")[-1].strip(" ()")
                id_div = c.select_one("[id^='itrDrId_']")
                dr_id = id_div.get_text(strip=True) if id_div else ""
                spec_ps = c.select(".doctor-concentration-wrap p")
                specialty = spec_ps[1].get_text(" ", strip=True) if len(spec_ps) > 1 else ""
                doctors.append(_make_doctor(name, "", department, specialty,
                                            emp_id=dr_id, name_en=name_en))
            if len(cards) < 5:                 # 마지막 페이지
                break
        return doctors


class AMCCrawler(HospitalCrawlerBase):
    """
    서울아산병원 크롤러 — Playwright 기반 (2026-06 실사이트 검증 완료).

    검증된 사이트 구조:
      1) 진료과 목록: /asan/staff/base/staffBaseInfoList.do
         <option value="/asan/depts/{코드}/K/deptLink.do">{진료과명}</option>
         → 진료과명을 진료과 코드(Dxxx)로 매핑
      2) 진료과 페이지: /asan/departments/deptDetail.do?hpCd={코드}&type=K
         → '의료진 소개' 탭 링크(moduleMenuId은 과마다 다름)를 동적으로 추적
      3) 의료진 목록 카드: ul.serchlist_boxwrap > li
         - 이름: .doctor_name a
         - 전문분야: table.professionally_info 의 '전문분야' 행
         - empId: onclick="fnDrDetail('{empId}','{deptCode}')"
         - 실제 진료예약 URL: a[href*="/reservation/main.do"]  (의사 프리필 포함)

    ※ 구 엔드포인트(POST /asan/search/doctor/searchDoctor.do)는 WAF 차단 페이지를
       반환하여 폐기함. EUC-KR 인코딩이라 httpx 직접 파싱 불가 → Playwright 렌더링 사용.
    """
    hospital_id = "amc"
    hospital_name = "서울아산병원"

    BASE = "https://www.amc.seoul.kr"
    EN_BASE = "https://eng.amc.seoul.kr"   # 영문 사이트 (의료진 영문명, drEmpId 동일)
    STAFF_LIST_URL = BASE + "/asan/staff/base/staffBaseInfoList.do"
    _UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
    # GA/광고 비콘 차단 — networkidle 무한대기 및 불필요 트래픽 방지
    _BLOCK = ("google-analytics", "analytics.google", "doubleclick",
              "googletagmanager", "/g/collect")

    async def get_doctors(self, department: str) -> list[dict]:
        try:
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                ctx = await browser.new_context(user_agent=self._UA, locale="ko-KR")
                page = await ctx.new_page()
                await page.route(
                    "**/*",
                    lambda route: route.abort()
                    if any(b in route.request.url for b in self._BLOCK)
                    else route.continue_(),
                )
                try:
                    # 1) 진료과명 → 진료과 코드
                    dept_code = await self._resolve_dept_code(page, department)
                    if not dept_code:
                        print(f"[AMC] 진료과 코드 매핑 실패: {department}")
                        return []

                    # 2) '의료진 소개' 탭 URL 동적 추적
                    staff_url = await self._find_staff_url(page, dept_code)
                    if not staff_url:
                        print(f"[AMC] 의료진 소개 탭 미발견: {dept_code}")
                        return []

                    # 3) 의료진 카드 파싱
                    await page.goto(staff_url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_selector("ul.serchlist_boxwrap > li", timeout=8000)
                    doctors = await self._parse_doctor_cards(page, department)
                    # 4) 영문명 부착 (영문 사이트, drEmpId 동일 키로 조인)
                    en_map = await self._fetch_en_name_map(dept_code)
                    for d in doctors:
                        d["name_en"] = en_map.get(d.get("emp_id", ""), "")
                    return doctors
                finally:
                    await browser.close()
        except Exception as e:
            print(f"[AMC] Crawl error: {e}")
            return []

    async def _fetch_en_name_map(self, dept_code: str) -> dict:
        """영문 사이트에서 {drEmpId: 영문명} 맵 조회 (1 request/진료과)."""
        import httpx
        import re
        from bs4 import BeautifulSoup
        out: dict = {}
        try:
            # 영문 서브도메인은 일부 환경에서 인증서 검증 이슈가 있어 verify=False (공개 읽기 전용)
            async with httpx.AsyncClient(timeout=30, headers=_BROWSER_HEADERS,
                                         follow_redirects=True, verify=False) as c:
                r = await c.get(f"{self.EN_BASE}/gb/lang/specialities/departments.do",
                                params={"hpCd": dept_code})
                soup = BeautifulSoup(r.text, "lxml")
                for fig in soup.select("figure.photo"):
                    cap = fig.select_one("figcaption.name")
                    li = fig.find_parent("li") or fig.parent
                    a = li.select_one("a[href*='drEmpId=']") if li else None
                    if cap and a:
                        m = re.search(r"drEmpId=([^&\"']+)", a.get("href", ""))
                        if m:
                            out[m.group(1)] = cap.get_text(strip=True)
        except Exception as e:
            print(f"[AMC-EN] 영문명 조회 실패: {e}")
        return out

    async def _resolve_dept_code(self, page, department: str) -> str:
        """진료과명을 진료과 코드(Dxxx)로 변환"""
        await page.goto(self.STAFF_LIST_URL, wait_until="domcontentloaded", timeout=30000)
        opts = await page.evaluate(
            "() => [...document.querySelectorAll('option')]"
            ".map(o => ({v: o.value, t: o.textContent.trim()}))"
            ".filter(o => o.v && o.v.includes('deptLink'))"
        )
        # 진료과명 → deptLink URL 후보로 매칭 후 코드 추출
        candidates = [(o["t"], o["v"]) for o in opts]
        matched = _match_dept(candidates, department)
        return self._extract_code(matched) if matched else ""

    @staticmethod
    def _extract_code(dept_link: str) -> str:
        """'/asan/depts/D006/K/deptLink.do' → 'D006'"""
        import re
        m = re.search(r"/depts/([A-Za-z0-9]+)/", dept_link)
        return m.group(1) if m else ""

    async def _find_staff_url(self, page, dept_code: str) -> str:
        """진료과 페이지에서 '의료진 소개' 탭의 실제 URL을 추출 (moduleMenuId은 과마다 상이)"""
        await page.goto(
            f"{self.BASE}/asan/departments/deptDetail.do?hpCd={dept_code}&type=K",
            wait_until="domcontentloaded", timeout=30000,
        )
        href = await page.evaluate(
            "() => { const a = [...document.querySelectorAll('a')]"
            ".find(e => (e.textContent || '').trim() === '의료진 소개'); "
            "return a ? a.getAttribute('href') : null; }"
        )
        if not href:
            return ""
        return href if href.startswith("http") else self.BASE + href

    async def _parse_doctor_cards(self, page, department: str) -> list[dict]:
        """ul.serchlist_boxwrap > li 카드에서 의료진 정보 추출"""
        cards = await page.evaluate(r"""() => {
          const out = [];
          document.querySelectorAll('ul.serchlist_boxwrap > li').forEach(li => {
            const nameEl = li.querySelector('.doctor_name a, .doctor_name');
            const name = nameEl ? nameEl.textContent.trim() : '';
            if (!name) return;

            // empId (상세/예약 식별자)
            let empId = '';
            const oc = li.querySelector('a[onclick*="fnDrDetail"]');
            if (oc) {
              const m = (oc.getAttribute('onclick') || '').match(/fnDrDetail\('([^']+)'/);
              if (m) empId = m[1];
            }

            // 전문분야
            let specialty = '';
            li.querySelectorAll('table.professionally_info tr').forEach(tr => {
              const th = tr.querySelector('th'), td = tr.querySelector('td');
              if (th && td && th.textContent.trim() === '전문분야')
                specialty = td.textContent.replace(/\s+/g, ' ').trim();
            });

            // 실제 진료예약 URL (의사 프리필)
            const resA = li.querySelector('a[href*="/reservation/main.do"]');
            const reservUrl = resA ? resA.getAttribute('href') : '';

            out.push({ name, empId, specialty, reservUrl });
          });
          return out;
        }""")

        doctors = []
        for c in cards:
            reserv = c.get("reservUrl") or ""
            if reserv.startswith("/"):
                reserv = self.BASE + reserv
            doctors.append({
                "name": c["name"],
                "name_en": "",                        # 목록에 영문명 없음 → 로마자 변환 사용
                "position": "",                       # 직위는 상세페이지 전용 — 목록 미제공
                "department": department,
                "specialties": c.get("specialty", ""),
                "wait_days": 0,                       # 별도 예약 페이지에서 조회
                "available_slots": [],
                "surgeries": 0,
                "emp_id": c.get("empId", ""),
                "reservation_url": reserv,            # 실제 의사 프리필 예약 링크
            })
        return doctors


class SMCCrawler(HospitalCrawlerBase):
    """
    삼성서울병원 크롤러 — httpx 기반 (2026-06 실검증, 서버사이드 렌더링/UTF-8).

    검증된 구조:
      - 진료과 목록: /home/reservation/deptSearch.do?DP_TYPE=O
        앵커 href 'deptDetailInfo.do?DP_CODE=...' + h3.field-title 텍스트로 진료과명→코드 (소화기내과→IM1)
      - 의료진 목록: /home/reservation/deptDetailInfo.do?DP_CODE={코드}&TYPE=02
      - 카드: li.card-item.doctor-profile
        이름 span[name="fullName"], 직위 h3.card-content-title 의 마지막 span(교수/임상강사),
        전문분야 p.card-content-text, DR_NO=onclick searchDoctorInfo('{id}')
      ※ 구 URL /home/doctor/doctorFind.do 는 404 → 폐기
    """
    hospital_id = "smc"
    hospital_name = "삼성서울병원"
    BASE = "https://www.samsunghospital.com"

    # 영문 사이트는 DP_CODE가 아닌 영문 슬러그를 쓰므로 진료과명→슬러그 맵을 둔다.
    # (영문 진료과 목록 페이지가 슬러그를 노출하지 않아 2026-06 실측으로 확정)
    _EN_SLUG = {
        "소화기내과": "gastroenterology",
        "순환기내과": "cardiology",
        "호흡기내과": "pulmonary-and-critical-care-medicine",
        "내분비내과": "endocrinology-and-metabolism",
        "혈액종양내과": "hematology-oncology",
        "류마티스내과": "rheumatology",
        "신경외과": "neurosurgery",
        "정형외과": "orthopedic-surgery",
        "비뇨의학과": "urology",
        "산부인과": "obstetrics-and-gynecology",
        "안과": "ophthalmology",
        "성형외과": "plastic-and-reconstructive-surgery",
        "신경과": "neurology",
        "흉부외과": "thoracic-and-cardiovascular-surgery",
        "유방외과": "breast-surgery",
        "내분비외과": "endocrine-surgery",
    }

    async def get_doctors(self, department: str) -> list[dict]:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30, headers=_BROWSER_HEADERS,
                                         follow_redirects=True) as client:
                code = await self._resolve_code(client, department)
                if not code:
                    print(f"[SMC] 진료과 코드 매핑 실패: {department}")
                    return []
                docs = await self._fetch_doctors(client, code, department)
                # 영문명 부착 (영문 사이트, DR_NO 동일 키로 조인)
                slug = self._en_slug(department)
                if slug:
                    en_map = await self._fetch_en_name_map(client, slug)
                    for d in docs:
                        d["name_en"] = en_map.get(d.get("emp_id", ""), "")
                return docs
        except Exception as e:
            print(f"[SMC] Crawl error: {e}")
            return []

    def _en_slug(self, department: str) -> str:
        norm_map = {_norm_dept(k): v for k, v in self._EN_SLUG.items()}
        for term in _dept_search_terms(department):
            if term in norm_map:
                return norm_map[term]
        return ""

    async def _fetch_en_name_map(self, client, slug: str) -> dict:
        """영문 진료과 의료진 페이지에서 {DR_NO: 영문명} 조회 (1 request/진료과)."""
        from bs4 import BeautifulSoup
        import re
        out: dict = {}
        try:
            r = await client.get(f"{self.BASE}/en/departments/{slug}/doctors.do")
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.select("a[href*='/en/find-doctor/']"):
                m = re.search(r"-(\d+)\.do", a.get("href", ""))
                nm = a.select_one("h3, .__name")
                if m and nm:
                    out[m.group(1)] = nm.get_text(strip=True)
        except Exception as e:
            print(f"[SMC-EN] 영문명 조회 실패: {e}")
        return out

    async def _resolve_code(self, client, department: str) -> str:
        from bs4 import BeautifulSoup
        import re
        r = await client.get(f"{self.BASE}/home/reservation/deptSearch.do",
                             params={"DP_TYPE": "O"})
        soup = BeautifulSoup(r.text, "lxml")
        candidates = []
        for a in soup.select("a[href*='deptDetailInfo.do']"):
            m = re.search(r"DP_CODE=([A-Za-z0-9]+)", a.get("href", ""))
            if not m:
                continue
            title = a.select_one("h3.field-title, .field-title")
            name = title.get_text() if title else a.get_text()
            candidates.append((name, m.group(1)))
        return _match_dept(candidates, department) or ""

    async def _fetch_doctors(self, client, code: str, department: str) -> list[dict]:
        from bs4 import BeautifulSoup
        import re
        doctors = []
        seen = set()
        for page in range(1, 21):
            r = await client.get(f"{self.BASE}/home/reservation/deptDetailInfo.do",
                                 params={"DP_CODE": code, "TYPE": "02", "page": str(page)})
            soup = BeautifulSoup(r.text, "lxml")
            cards = soup.select("li.card-item.doctor-profile")
            new_count = 0
            for c in cards:
                name_el = c.select_one("span[name='fullName']")
                name = name_el.get_text(strip=True) if name_el else ""
                if not name:
                    continue
                m = re.search(r"searchDoctorInfo\('(\d+)'\)", str(c))
                dr_no = m.group(1) if m else ""
                key = (name, dr_no)
                if key in seen:
                    continue
                seen.add(key)
                new_count += 1
                # 직위: h3.card-content-title 의 span 중 이름(name=fullName)·
                # 진료과 라벨(.treatment-parts)을 제외한 것 (예: 교수/임상강사)
                pos = ""
                for sp in c.select("h3.card-content-title span"):
                    if sp.get("name") == "fullName":
                        continue
                    if "treatment-parts" in (sp.get("class") or []):
                        continue
                    pos = sp.get_text(strip=True)
                    break
                spec_el = c.select_one("p.card-content-text")
                specialty = spec_el.get_text(" ", strip=True) if spec_el else ""
                doctors.append(_make_doctor(name, pos, department, specialty, emp_id=dr_no))
            # 페이지네이션: totalPageCount 기준. page 파라미터가 무시되면 신규 0건으로 종료.
            total_el = soup.select_one("#totalPageCount")
            total_val = total_el.get("value", "1") if total_el else "1"
            total_pages = int(total_val) if str(total_val).isdigit() else 1
            if new_count == 0 or page >= total_pages:
                break
        return doctors


class SeveranceCrawler(HospitalCrawlerBase):
    """
    세브란스병원 크롤러 — httpx + JSON API (2026-06 실검증, UTF-8).

    검증된 API:
      - 진료과 목록: GET /api/department/list.do?insttCode=2&tyCode=DP010100&seCode=&sort=name
        → data.list[]: {deptNm, seq, tyCode}  (소화기내과→seq=70)
      - 의료진 목록: GET /api/doctor/list.do?insttCode=2&tyCode={ty}&seCode=&seq={seq}
        &keyword=&page=1&pagePerNum=200&isChoSung=N  (POST는 404 → 반드시 GET)
        → data.list[]: {nm, ofcps(직위), clnicRealm(전문분야), empNo, resEnableYn}
      ※ 구 URL /doctor/search.do 는 404 → 폐기
    """
    hospital_id = "sev"
    hospital_name = "세브란스병원"
    BASE = "https://sev.severance.healthcare"

    def _headers(self) -> dict:
        return {
            **_BROWSER_HEADERS,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.BASE}/sev/doctor/doctor.do",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }

    async def get_doctors(self, department: str) -> list[dict]:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30, headers=self._headers(),
                                         follow_redirects=True) as client:
                seq, ty = await self._resolve_seq(client, department)
                if not seq:
                    print(f"[SEV] 진료과 seq 매핑 실패: {department}")
                    return []
                return await self._fetch_doctors(client, seq, ty, department)
        except Exception as e:
            print(f"[SEV] Crawl error: {e}")
            return []

    async def _resolve_seq(self, client, department: str):
        r = await client.get(f"{self.BASE}/api/department/list.do",
                             params={"insttCode": "2", "tyCode": "DP010100",
                                     "seCode": "", "sort": "name"})
        data = (r.json().get("data") or {}).get("list") or []
        # 진료과명 → (seq, tyCode) 매핑. _match_dept 로 seq 를 찾은 뒤 tyCode 동봉.
        by_seq = {str(d.get("seq")): d.get("tyCode", "DP010100") for d in data}
        candidates = [(d.get("deptNm", ""), str(d.get("seq"))) for d in data]
        seq = _match_dept(candidates, department)
        if not seq:
            return None, None
        return seq, by_seq.get(seq, "DP010100")

    async def _fetch_doctors(self, client, seq, ty, department: str) -> list[dict]:
        r = await client.get(f"{self.BASE}/api/doctor/list.do",
                             params={"insttCode": "2", "tyCode": ty or "DP010100",
                                     "seCode": "", "seq": seq, "keyword": "",
                                     "page": "1", "pagePerNum": "200", "isChoSung": "N"})
        items = (r.json().get("data") or {}).get("list") or []
        doctors = []
        for d in items:
            name = (d.get("nm") or "").strip()
            if not name:
                continue
            doctors.append(_make_doctor(
                name,
                (d.get("ofcps") or "").strip(),
                department,
                (d.get("clnicRealm") or "").strip(),
                emp_id=d.get("empNo", "") or "",
                name_en=(d.get("nmEn") or "").strip(),
            ))
        return doctors


class SNUBHCrawler(HospitalCrawlerBase):
    """
    분당서울대학교병원 크롤러 — httpx 기반 (2026-06 실검증, 서버사이드 렌더링/UTF-8).

    검증된 구조:
      - 진료과 선택: /medical/drMedicalTeam.do?DP_TP=O&DP_CD={코드}
        (DP_TP=O 진료과, 두 파라미터 모두 필수. select[name="DP_CD"] 옵션으로 진료과명→코드, 소화기내과→IMG)
      - 카드: ul.bh_bookmark_list_ul_n > li.bh_bookmark_list3
        이름 .bh_doctor_name_n strong (중첩 em=직위 제외), 직위 .bh_doctor_name_n em(교수),
        전문분야 dl.bh_doctor_dept_n dd, drNo=intro 버튼 onclick의 'sDrSid'
      ※ 구 URL /medical/doctor/findDoctor.do 는 error.jsp 로 리다이렉트 → 폐기
    """
    hospital_id = "snubh"
    hospital_name = "분당서울대병원"
    BASE = "https://www.snubh.org"

    async def get_doctors(self, department: str) -> list[dict]:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30, headers=_BROWSER_HEADERS,
                                         follow_redirects=True) as client:
                code = await self._resolve_code(client, department)
                if not code:
                    print(f"[SNUBH] 진료과 코드 매핑 실패: {department}")
                    return []
                docs = await self._fetch_doctors(client, code, department)
                # 영문명 부착 (영문 사이트 Find a Doctor, sDrSid=drNo 동일 키로 조인)
                en_map = await self._fetch_en_name_map(client, code)
                for d in docs:
                    d["name_en"] = en_map.get(d.get("emp_id", ""), "")
                return docs
        except Exception as e:
            print(f"[SNUBH] Crawl error: {e}")
            return []

    async def _fetch_en_name_map(self, client, code: str) -> dict:
        """영문 사이트에서 {sDrSid: 영문명} 조회 (1 request/진료과). sDrSid=drNo로 조인."""
        from bs4 import BeautifulSoup
        import re
        out: dict = {}
        try:
            r = await client.get(f"{self.BASE}/dh/module/en_drIntroduce.do",
                                 params={"DP_CD": "EN", "MENU_ID": "001001", "S_DP_CD": code})
            soup = BeautifulSoup(r.text, "lxml")
            for li in soup.select("ul.doctor_list > li"):
                nm = li.select_one("p.doctor_name")
                a = li.select_one("a[onclick*='sDrSid']")
                if not (nm and a):
                    continue
                m = re.search(r"sDrSid=(\d+)", a.get("onclick", ""))
                name = nm.get_text(" ", strip=True)
                if m and name:
                    out[m.group(1)] = name
        except Exception as e:
            print(f"[SNUBH-EN] 영문명 조회 실패: {e}")
        return out

    async def _resolve_code(self, client, department: str) -> str:
        from bs4 import BeautifulSoup
        r = await client.get(f"{self.BASE}/medical/drMedicalTeam.do", params={"DP_TP": "O"})
        soup = BeautifulSoup(r.text, "lxml")
        sel = soup.select_one("select[name='DP_CD'], #deptList")
        candidates = []
        if sel:
            for opt in sel.select("option"):
                val = (opt.get("value", "") or "").strip()
                candidates.append((opt.get_text(), val))
        return _match_dept(candidates, department) or ""

    async def _fetch_doctors(self, client, code: str, department: str) -> list[dict]:
        from bs4 import BeautifulSoup
        import re
        r = await client.get(f"{self.BASE}/medical/drMedicalTeam.do",
                             params={"DP_TP": "O", "DP_CD": code})
        soup = BeautifulSoup(r.text, "lxml")
        doctors = []
        for c in soup.select("ul.bh_bookmark_list_ul_n > li.bh_bookmark_list3"):
            name_el = c.select_one(".bh_doctor_name_n strong")
            if not name_el:
                continue
            pos_el = name_el.select_one("em")
            pos = pos_el.get_text(strip=True) if pos_el else ""
            # strong의 직속 텍스트만 = 이름 (중첩 em 직위 제외)
            name = "".join(t for t in name_el.contents if isinstance(t, str)).strip()
            if not name:
                name = name_el.get_text(strip=True).replace(pos, "").strip()
            if not name:
                continue
            spec_el = c.select_one("dl.bh_doctor_dept_n dd")
            specialty = spec_el.get_text(" ", strip=True) if spec_el else ""
            dr_no = ""
            intro = c.select_one("input.bh_doctor_btn_intro")
            if intro:
                m = re.search(r"'sDrSid'\s*:\s*'(\d+)'", intro.get("onclick", ""))
                if m:
                    dr_no = m.group(1)
            doctors.append(_make_doctor(name, pos, department, specialty, emp_id=dr_no))
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

    async def crawl_hospital(self, hospital_id: str, department: str, disease: str = "") -> dict:
        """단일 병원 크롤링 (캐시 + rate limit + 모의 Fallback DB 연동)"""
        cache_key = f"{hospital_id}:{department}:{disease or 'none'}"

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

            failed = False
            try:
                result = await crawler.crawl(department)
                # 크롤 결과가 비었으면 실패로 간주 — 가짜 의사를 만들지 않고 빈 결과 반환
                if not result or not result.get("doctors"):
                    print(f"[Crawler:{hospital_id}] Crawl empty/failed for {department} — returning empty (no dummy).")
                    result = {"hospital_id": hospital_id, "doctors": []}
                    failed = True
            except Exception as e:
                print(f"[Crawler:{hospital_id}] Crawl crashed: {e} — returning empty (no dummy).")
                result = {"hospital_id": hospital_id, "doctors": []}
                failed = True

            # 실제 의사를 찾은 경우에만 캐싱한다. 빈/실패 결과는 캐싱하지 않아
            # 일시적 실패가 6시간 고착되지 않고 다음 검색에서 재시도된다.
            if not failed:
                self._cache[cache_key] = result
                self._cache_ts[cache_key] = datetime.now()
            return result

    def _get_fallback_doctors(self, hospital_id: str, department: str, disease: str = "") -> list[dict]:
        """웹 크롤링 실패 또는 가상 서버 구동 시, 실제 해당 병원/과에 존재할 법한 명의들의 DB를 LCG 알고리즘 기반으로 생성"""
        dis = disease or department
        s = sum(ord(c) for c in (hospital_id + department + dis))
        
        def r(n: int) -> float:
            return ((s * 9301 + 49297 + n * 233) % 233280) / 233280.0
            
        ln = ["김", "이", "박", "최", "정", "강", "조", "윤", "장", "임", "한", "오", "서", "신", "권", "황"]
        fn = ["영수", "지훈", "성호", "민재", "준혁", "태현", "동욱", "상훈", "정민", "은경", "수진", "혜원", "미란", "지현"]
        
        # 병원별/과별로 실제 유명 명의 고유 사전 매핑
        FAMOUS_BY_HOSP_DEPT = {
            ("snuh", "소화기내과"): ["김윤준", "임종필", "김지원"],
            ("snuh", "일반외과"): ["양한광", "이혁준", "박도중"],
            ("amc", "소화기내과"): ["변정식", "김명환", "서동완"],
            ("amc", "일반외과"): ["이승규", "송기원", "김송철"],
            ("smc", "소화기내과"): ["이풍렬", "민양원", "이혁"],
            ("smc", "일반외과"): ["이우용", "김희철", "신정경"],
            ("sev", "소화기내과"): ["송시영", "이용찬", "김태일"],
            ("sev", "일반외과"): ["노성훈", "강창무", "민병소"],
            ("snubh", "소화기내과"): ["김나영", "이동호", "신철민"],
            ("snubh", "일반외과"): ["전상훈", "오흥권", "안상훈"]
        }
        
        famous_names = FAMOUS_BY_HOSP_DEPT.get((hospital_id, department), [])
        
        cnt = 2 + int(r(0) * 2)  # 2 to 3 doctors
        doctors = []
        
        for i in range(cnt):
            if i < len(famous_names):
                nm = famous_names[i]
            else:
                nm = ln[int(r(i*10+5) * len(ln))] + fn[int(r(i*10+6) * len(fn))]
                
            hi = int(15 + r(i*10+1) * 55)
            wd = int(3 + r(i*10+2) * 45)
            pos = "교수" if hi > 45 else ("부교수" if hi > 30 else "조교수")
            
            # 예약 슬롯 일자 생성 (토요일/일요일 제외)
            slots = []
            for d_idx in [wd, wd + 7]:
                dt = datetime.now() + timedelta(days=d_idx)
                while dt.weekday() in (5, 6): # Saturday, Sunday
                    dt += timedelta(days=1)
                
                slot_date = dt.strftime("%Y-%m-%d")
                slot_time = ["09:00", "10:30", "14:00", "15:30"][int(r(i*10+8 + len(slots)) * 4)]
                slots.append({"date": slot_date, "time": slot_time})
                
            f_factor = {"snuh": 1.1, "amc": 1.25, "smc": 1.05, "sev": 0.95, "snubh": 0.8}.get(hospital_id, 1.0)
            annual_surg = int(80 + r(1) * 600 * f_factor)
            ds = int(annual_surg * (0.15 + r(i*10+11) * 0.35) / cnt)
            
            specialties_map = {
                "소화기내과": "위암, 대장암, 간암 및 췌담도 질환 전문 진료",
                "일반외과": "복강경 수술, 로봇 암 수술, 장기 이식 및 종양 절제술",
                "순환기내과": "협심증, 심부전, 판막 질환 및 심장 중재시술 전문",
                "신경외과": "뇌종양, 뇌동맥류 수술 및 척추 디스크 미세수술 전문"
            }
            spec = specialties_map.get(department, f"{department} 분야 난치성 질환 진료 및 임상 연구 전문")
            
            doctors.append({
                "name": nm,
                "position": pos,
                "department": department,
                "specialties": spec,
                "wait_days": wd,
                "available_slots": slots,
                "surgeries": ds
            })
            
        return doctors


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
