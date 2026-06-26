"""
OpenAlex H-index 경로 라이브 검증 + 기관 ID 핀 보조.

구현 세션에서는 IP가 OpenAlex 레이트리밋(429)에 걸려 라이브 경로를 검증하지 못했다.
레이트리밋이 풀린 환경(또는 다른 네트워크)에서 이 스크립트를 1회 실행해:
  1) 빅5 → OpenAlex 기관 ID 가 정상 해석되는지 확인 (원하면 HOSPITAL_INST_KEYWORDS 옆에 핀)
  2) 표본 의사들의 OpenAlex h-index vs PubMed(수정본) 폴백을 대조
  3) source='openalex' 가 실제로 나오는지(폴백이 아니라) 확인

실행:  .venv/Scripts/python.exe scripts/validate_openalex.py
"""
import asyncio
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.api.pubmed import PubMedClient
from backend.api.openalex import OpenAlexClient

# (이름, 병원, name_en, 진료과) — name_en 은 병원 영문명단 기준. 동명이인 흔한 표본 위주.
SAMPLES = [
    ("김기동", "분당서울대병원",   "Kidong Kim",      "산부인과"),
    ("노동영", "서울대학교병원",   "Dong-Young Noh",  "유방외과"),
    ("방영주", "서울대학교병원",   "Yung-Jue Bang",   "혈액종양내과"),
    ("김열홍", "서울아산병원",     "Yeul Hong Kim",   "종양내과"),
    ("정현철", "세브란스병원",     "Hyun Cheol Chung","종양내과"),
]


async def main():
    oa = OpenAlexClient(pubmed_fallback=PubMedClient())

    print("=== 1) 기관 ID 해석 ===")
    for hosp in ["서울대학교병원", "서울아산병원", "삼성서울병원", "세브란스병원", "분당서울대병원"]:
        ids = await oa._resolve_institutions(hosp)
        print(f"  {hosp:10s} -> {ids or '(해석 실패/429)'}")

    print("\n=== 2) OpenAlex h-index vs 폴백 ===")
    print(f"  {'이름':6s} {'병원':10s} {'h':>3} {'papers':>6} {'cites':>6}  source")
    for name, hosp, name_en, dept in SAMPLES:
        r = await oa.get_h_index(name, hosp, name_en=name_en, department=dept)
        flag = "  ✅OpenAlex" if r.get("source") == "openalex" else "  ⚠️폴백"
        print(f"  {name:6s} {hosp:10s} {r['h_index']:>3} {r['papers']:>6} {r['citations']:>6}  {r.get('source','')}{flag}")

    print("\n폴백만 보이면 OpenAlex 가 여전히 429 이거나 매칭 실패. source='openalex' 가 보이면 전환 성공.")


if __name__ == "__main__":
    asyncio.run(main())
