"""
심평원 API 연동 테스트.
실행: python test_hira.py
(먼저 .env에 HIRA_API_KEY 입력 필요)
"""
import asyncio
import sys
sys.path.insert(0, ".")

from backend.api.hira import HIRAClient
from config.settings import HIRA_API_KEY


async def main():
    if HIRA_API_KEY in ("YOUR_HIRA_API_KEY", ""):
        print("❌ .env 파일에 HIRA_API_KEY를 입력하세요.")
        print("   발급: https://opendata.hira.or.kr 또는 https://data.go.kr")
        return

    client = HIRAClient()
    print(f"✅ HIRA_API_KEY 감지됨: {HIRA_API_KEY[:8]}...")
    print()

    tests = [
        ("snuh",  "C16",  "서울대병원 + 위암(C16)"),
        ("amc",   "C34",  "아산병원 + 폐암(C34)"),
        ("smc",   "C61",  "삼성병원 + 전립선암(C61)"),
        ("sev",   "C50",  "세브란스 + 유방암(C50)"),
        ("snubh", "I63",  "분당서울대 + 뇌졸중(I63)"),
    ]

    for hospital_id, kcd, label in tests:
        print(f"🔍 {label}")
        result = await client.get_surgery_stats(hospital_id, kcd)
        print(f"   연간수술: {result['annualSurgeries']}건")
        print(f"   연간진료: {result['annualCases']}건")
        print(f"   사망률:   {result['mortalityRate']}%")
        print(f"   합병증:   {result['complicationRate']}%")
        print(f"   재입원:   {result['readmissionRate']}%")
        print(f"   평균재원: {result['avgLOS']}일")
        print(f"   3년추이:  {result['trend']}")
        print(f"   출처:     {result['source']}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
