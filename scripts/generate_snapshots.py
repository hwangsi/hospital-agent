"""
정적 데모(GitHub Pages)용 검색 스냅샷 생성기.

주요 질환 × 목적(진료/수술)별로 실제 검색 파이프라인(크롤링+심평원+h-index+뉴스)을
로컬에서 1회 실행해 data/snapshots/*.json 으로 저장한다. GitHub Pages 에서는
백엔드 없이 이 JSON 을 읽어 동일한 결과 화면을 보여준다 (index.html 스냅샷 모드).

실행:  PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/generate_snapshots.py
갱신은 로컬에서 재실행 후 커밋 (해외 CI 러너는 병원 사이트가 차단할 수 있음).
"""
import asyncio
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import backend.main as bm  # noqa: E402
from backend.main import search_doctors, SearchRequest  # noqa: E402
from backend.api.openalex import OpenAlexClient  # noqa: E402
from backend.utils.kcd_mapper import KCDMapper  # noqa: E402

# ── 대량 배치 모드 — 지속 부하에서 OpenAlex 429→회로차단(10분)→전원 폴백 방지 ──
# (첫 실행에서 807명 중 617명이 pubmed-fallback 으로 저장된 원인)
OpenAlexClient._MIN_INTERVAL = 0.3   # 0.12s → 0.3s (지속 ~3req/s, polite pool 여유)
OpenAlexClient._COOLDOWN = 60.0      # 회로차단 열려도 10분 아닌 1분 후 복구
bm._ENRICH_SEM = asyncio.Semaphore(3)  # 동시 보강 6 → 3

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "snapshots")

# 데모 지원 질환 (칩 UI에 있는 것 위주 — CSV 실데이터 커버리지가 좋은 암종 우선)
DISEASES = ["위암", "대장암", "간암", "췌장암", "폐암",
            "유방암", "갑상선암", "전립선암", "방광암", "신장암"]

# 목적 그룹: surgery/complication → 외과 라우팅(surg), diagnosis/second_opinion → med
PURPOSE_GROUPS = {"med": "diagnosis", "surg": "surgery"}

mapper = KCDMapper()


def _slug(disease: str) -> str:
    info = mapper.map_disease(disease)
    kcd = (info.get("kcd_code") or "x").replace(".", "_").lower()
    return kcd


async def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    catalog = {"generated_at": date.today().isoformat(), "snapshots": []}

    for disease in DISEASES:
        for group, purpose in PURPOSE_GROUPS.items():
            req = SearchRequest(disease=disease, department="", purpose=purpose,
                                age=60, gender="남성")
            try:
                res = await search_doctors(req)
            except Exception as e:
                print(f"[SKIP] {disease}/{group}: {e}")
                continue
            doctors = res.get("doctors", []) if isinstance(res, dict) else []
            fname = f"{_slug(disease)}_{group}.json"
            with open(os.path.join(OUT_DIR, fname), "w", encoding="utf-8") as f:
                json.dump(res, f, ensure_ascii=False)
            catalog["snapshots"].append({
                "disease": disease, "group": group,
                "file": fname, "doctors": len(doctors),
            })
            print(f"[OK] {disease}/{group}: {len(doctors)}명 -> {fname}")

    with open(os.path.join(OUT_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=1)
    total = sum(s["doctors"] for s in catalog["snapshots"])
    print(f"\ncatalog: {len(catalog['snapshots'])}개 스냅샷, 총 {total}명")


if __name__ == "__main__":
    asyncio.run(main())
