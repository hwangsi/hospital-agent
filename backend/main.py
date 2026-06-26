"""
빅5 병원 통합 예약 에이전트 — FastAPI 서버
"""
import asyncio
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.api.hira import HIRAClient
from backend.api.pubmed import PubMedClient
from backend.api.openalex import OpenAlexClient
from backend.api.naver_news import NaverNewsClient
from backend.crawlers.base import CrawlerOrchestrator
from backend.utils.kcd_mapper import KCDMapper
from backend.utils.encryption import encrypt_ssn

app = FastAPI(
    title="빅5 병원 통합 예약 에이전트 API",
    version="1.0.0",
    description="서울대·아산·삼성·세브란스·분당서울대 통합 검색 및 예약",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── Clients (싱글톤) ────────────────────────────────
hira_client = HIRAClient()
pubmed_client = PubMedClient()
# H-index 1순위 = OpenAlex 저자 엔티티, 실패/레이트리밋 시 PubMed(수정본) 폴백.
hindex_client = OpenAlexClient(pubmed_fallback=pubmed_client)
naver_client = NaverNewsClient()
crawler = CrawlerOrchestrator()
kcd_mapper = KCDMapper()

# 의사 보강(PubMed/iCite/Naver)의 전역 동시성 제한.
# 한 검색에 수십~수백 명을 보강하므로, 제한이 없으면 외부 API(NCBI/OpenAlex)의
# rate limit을 초과해 h-index/인용이 0으로 떨어진다.
_ENRICH_SEM = asyncio.Semaphore(6)


# ─── Request/Response Models ────────────────────────
class SearchRequest(BaseModel):
    disease: str                      # 질환명 (주관식 or 선택)
    department: Optional[str] = None  # 진료과 (chip 선택 시)
    purpose: str                      # diagnosis / second_opinion / surgery / complication
    age: int
    gender: str

class DoctorResult(BaseModel):
    id: str
    name: str
    position: str
    hospital_id: str
    hospital_name: str
    department: str
    h_index: int
    wait_days: int
    news_count: int
    papers: int             # PubMed(국제) 논문 수
    citations: int          # PubMed(국제) 피인용 수
    doc_surgeries: int
    hira_data: dict
    available_slots: list
    emp_id: str = ""             # 병원 내부 의사 식별자 (예: 아산 empId)
    reservation_url: str = ""    # 의사 프리필 예약 URL (가능한 경우)

class ReservationRequest(BaseModel):
    doctor_id: str
    hospital_id: str
    slot_date: str
    slot_time: str
    patient_name: str
    phone: str
    ssn: Optional[str] = None         # 주민등록번호 (선택)
    address: Optional[str] = None     # 주소 (선택)
    notes: Optional[str] = None


# ─── Endpoints ───────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.post("/api/search")
async def search_doctors(req: SearchRequest):
    """
    1단계: 질환 기반 의사 통합 검색
    - 심평원 수술건수 조회
    - 병원 크롤링 (대기일, 의사 목록)
    - PubMed H-index 산출
    - 네이버 뉴스 언론노출 집계
    """
    # 주관식 질환 → KCD 코드 + 관련 진료과 매핑
    disease_info = kcd_mapper.map_disease(req.disease)
    department = req.department or disease_info.get("department", "내과")
    kcd_code = disease_info.get("kcd_code", "")

    # 진료 목적이 '수술'/'합병증 치료'면 해당 질환의 외과로 라우팅 (명시 선택보다 우선).
    # 예: 위암 + 수술 → 소화기내과(내과)가 아니라 위장관외과로 검색.
    if req.purpose in ("surgery", "complication") and disease_info.get("surgery_dept"):
        department = disease_info["surgery_dept"]

    hospital_ids = ["snuh", "amc", "smc", "sev", "snubh"]

    # 병렬 실행: 크롤링 + API 동시 호출
    tasks = []
    for hid in hospital_ids:
        tasks.append(
            _fetch_hospital_data(hid, department, req.disease, kcd_code)
        )

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 결과 합산
    all_doctors = []
    for r in results:
        if isinstance(r, Exception):
            print(f"[WARN] Hospital fetch failed: {r}")
            continue
        all_doctors.extend(r)

    return {
        "doctors": all_doctors,
        "disease": req.disease,
        "kcd_code": kcd_code,
        "department": department,
        "total": len(all_doctors),
    }


async def _fetch_hospital_data(
    hospital_id: str,
    department: str,
    disease: str,
    kcd_code: str,
) -> list[dict]:
    """단일 병원에 대한 모든 데이터를 병렬 수집"""

    # 1) 심평원 수술건수
    hira_task = hira_client.get_surgery_stats(hospital_id, kcd_code)

    # 2) 병원 크롤링 (의사 목록 + 대기일)
    crawl_task = crawler.crawl_hospital(hospital_id, department, disease)

    # 3) 결과 대기
    hira_data, crawl_data = await asyncio.gather(
        hira_task, crawl_task, return_exceptions=True
    )

    if isinstance(hira_data, Exception):
        hira_data = {"annualSurgeries": 0, "error": str(hira_data)}
    if isinstance(crawl_data, Exception):
        crawl_data = {"doctors": []}

    doctors = crawl_data.get("doctors", [])

    # 4) 각 의사별 H-index + 뉴스 (병렬)
    enriched = []
    sub_tasks = []
    for doc in doctors:
        sub_tasks.append(_enrich_doctor(doc, hospital_id, hira_data))

    enriched = await asyncio.gather(*sub_tasks, return_exceptions=True)

    return [d for d in enriched if not isinstance(d, Exception)]


async def _enrich_doctor(doc: dict, hospital_id: str, hira_data: dict) -> dict:
    """의사 1명에 대해 H-index, 뉴스 수 추가"""
    hospital_names = {
        "snuh": "서울대학교병원", "amc": "서울아산병원",
        "smc": "삼성서울병원", "sev": "세브란스병원",
        "snubh": "분당서울대병원",
    }

    # PubMed(국제) + Naver(뉴스) 동시 호출
    # 병원 제공 영문명(name_en)이 있으면 PubMed 검색 정확도가 크게 향상됨.
    # 세마포어로 동시 보강 수를 제한해 외부 API rate limit 초과를 방지.
    async with _ENRICH_SEM:
        # 1순위 OpenAlex 저자 엔티티 → 실패/레이트리밋 시 PubMed(수정본) 자동 폴백
        pubmed_task = hindex_client.get_h_index(
            doc["name"], hospital_names[hospital_id],
            name_en=doc.get("name_en", ""),
            department=doc.get("department", ""),
            orcid=doc.get("orcid", ""),
        )
        naver_task = naver_client.get_news_count(doc["name"], hospital_names[hospital_id])

        pub_result, news_count = await asyncio.gather(
            pubmed_task, naver_task, return_exceptions=True
        )

    if isinstance(pub_result, Exception):
        pub_result = {"h_index": 0, "papers": 0, "citations": 0}
    if isinstance(news_count, Exception):
        news_count = 0

    return {
        "id": f"{hospital_id}-{doc.get('name', 'unknown')}",
        "name": doc.get("name", ""),
        "position": doc.get("position", ""),
        "hospital_id": hospital_id,
        "hospital_name": hospital_names[hospital_id],
        "department": doc.get("department", ""),
        "h_index": pub_result.get("h_index", 0),
        "wait_days": doc.get("wait_days", 0),
        "news_count": news_count,
        "papers": pub_result.get("papers", 0),
        "citations": pub_result.get("citations", 0),
        "hindex_source": pub_result.get("source", ""),   # openalex | pubmed-fallback
        "doc_surgeries": doc.get("surgeries", 0),
        "hira_data": hira_data,
        "available_slots": doc.get("available_slots", []),
        "emp_id": doc.get("emp_id", ""),
        "reservation_url": doc.get("reservation_url", ""),
        "profile_url": doc.get("profile_url", ""),
    }


@app.post("/api/reserve")
async def make_reservation(req: ReservationRequest):
    """
    2단계: 예약 실행
    Semi-auto 방식: 예약 정보 프리필 후 병원 예약 페이지로 리다이렉트
    """
    # 주민번호 암호화
    encrypted_ssn = None
    if req.ssn:
        encrypted_ssn = encrypt_ssn(req.ssn)

    # 병원별 예약 페이지 URL 생성 (프리필 파라미터 포함)
    reservation_url = crawler.build_reservation_url(
        hospital_id=req.hospital_id,
        doctor_id=req.doctor_id,
        date=req.slot_date,
        time=req.slot_time,
    )

    # 예약 정보 저장 (DB 연동 시)
    reservation_record = {
        "id": f"RES-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        "hospital_id": req.hospital_id,
        "doctor_id": req.doctor_id,
        "slot": {"date": req.slot_date, "time": req.slot_time},
        "patient_name": req.patient_name,
        "phone": req.phone,
        "has_ssn": bool(encrypted_ssn),
        "address": req.address,
        "notes": req.notes,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "reservation_url": reservation_url,
    }

    return {
        "success": True,
        "reservation": reservation_record,
        "redirect_url": reservation_url,
        "message": "예약 정보가 준비되었습니다. 병원 사이트에서 본인인증 후 최종 확정해주세요.",
    }


@app.get("/api/hira/{hospital_id}")
async def get_hira_stats(hospital_id: str, kcd_code: str = "C61"):
    """심평원 데이터 단건 조회"""
    data = await hira_client.get_surgery_stats(hospital_id, kcd_code)
    return data


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
