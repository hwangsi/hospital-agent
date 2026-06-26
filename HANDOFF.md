# HANDOFF — 빅5 병원 통합 예약 에이전트

> 작성: 2026-06-26 (세션 종료 시점). 다음 세션은 이 문서부터 읽고 이어가면 됩니다.

## 1. 프로젝트 한 줄 요약
서울대·아산·삼성·세브란스·분당서울대(빅5) 병원의 **실제 의료진을 크롤링**해 통합 검색하고,
각 의사의 **h-index(PubMed+iCite), 언론노출(Naver)** 등 학술/평판 지표로 비교한 뒤
세미오토(프리필) 예약으로 연결하는 단일 페이지 앱.

- 백엔드: FastAPI (`backend/main.py`), 포트 8000
- 프론트: 단일 `index.html` (React+Babel CDN, 빌드 없음), 정적 서버 포트 8080
- 저장소: GitHub `hwangsi/hospital-agent`, 브랜치 `master`

## 2. 지금 실행하는 법 (새 세션에서 서버 재기동 필요)
이전 세션의 백그라운드 서버는 종료됩니다. 새로 띄우세요.

```bash
# 1) 백엔드 (repo 루트에서)
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000

# 2) 정적 서버 — 반드시 no-cache 헤더로 서빙해야 브라우저가 옛 JS를 캐시하지 않음
#    (간단히) .venv/Scripts/python.exe -m http.server 8080 --bind 127.0.0.1
#    단, http.server는 캐시를 막지 못하므로 아래 no-cache 서버 권장:
```
no-cache 정적 서버 스니펫(권장):
```python
import http.server, socketserver, os
os.chdir(r"C:\Users\hwang\AIprojects\hospital-agent")
class H(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control","no-store, no-cache, must-revalidate, max-age=0")
        super().end_headers()
socketserver.TCPServer(("127.0.0.1",8080), H).serve_forever()
```
- 브라우저: `http://127.0.0.1:8080/index.html?v=N` (캐시버스트용 `?v=N`을 매번 바꿔서 열기)
- index.html은 `http://127.0.0.1:8000/api/search`를 호출. CORS는 `allow_origins=["*"]`.
- 첫 검색은 의사 수 × esearch 스로틀로 30~60초. 이후 6시간 캐시.

## 3. 데이터 소스 현황 (★ 중요)
| 지표 | 상태 | 비고 |
|---|---|---|
| 의료진 목록/전문분야 | ✅ 실제 크롤링 | 5개 병원 모두 실검증 |
| h-index (PubMed esearch + NIH iCite 인용) | ✅ 실데이터 | 스로틀+재시도+동시성제한 적용 |
| 언론노출 (Naver 뉴스) | ✅ 실데이터 | `.env`에 실제 키(커밋 안 됨) |
| KCI (국내논문) | ❌ 제거됨 | 이번 세션에 코드 완전 삭제 |
| **수술실적 (HIRA/심평원)** | ⚠️ **시뮬레이션** | **다음 작업 — 아래 4번** |

`.env` (gitignore됨): NAVER_CLIENT_ID/SECRET = 실제, HIRA_API_KEY = placeholder,
NCBI_API_KEY = 빈값(선택사항이지만 넣으면 esearch 3→10req/s로 빨라짐).

## 4. 심평원(HIRA) 수술실적 — 하이브리드 구조로 재작성 완료 (키 대기 중)
**결정: 하이브리드** — 수술건수/사망률 등 정량지표는 **CSV**, 적정성평가 "등급"은 **data.go.kr 실 API**.
(병원별×질환별 수술건수/사망률은 공개 REST API로 제공 안 됨이 재확인됨 → 정량은 CSV가 유일한 실데이터 경로.)

### 이번 세션에 한 것 (코드 완료, 검증됨)
- `backend/api/hira.py` **전면 재작성**: 죽은 `opendata.hira.or.kr` olap 경로 전부 제거.
  - CSV 1순위(`data/hira_stats.csv`) → 행 있으면 `isEstimate=False` 실데이터, 없으면 `isEstimate=True` 추정치(명확 표기).
  - 적정성평가 등급: **우수기관병원평가정보서비스** `data.go.kr B551182/exclInstHospAsmInfoService1/getExclInstHospAsmInfo1` ✅ **라이브 작동 확인**. 평가항목별 우수기관 전체목록(~6600건)을 락으로 1회 적재·캐시 후 빅5 **yadmNm 정확일치** 매칭(ykiho 불필요). 실응답 필드: `asmNm`(항목) `asmGrd`(유형코드) `asmGrdNm`(라벨 "3회연속"/"최근우수") `yadmNm`. 키 미활성 시 grades=[] (정직).
  - 매칭 정확일치 필수: 부분일치는 분원 오매칭(강남/용인 세브란스, 강릉아산, 분당서울대)을 흡수함. 등록 정식명 `HOSPITAL_YADM` 사용. amc=`재단법인아산사회복지재단서울아산병원`, sev=신촌 본원만.
  - 라이브 결과: snuh 7 / amc 9 / smc 9 / sev 9 / snubh 6 항목.
- `config/settings.py`: `PUBLIC_DATA_BASE` + `HIRA_EXCL_ASM_PATH` 추가, 죽은 `HIRA_BASE_URL` 제거.
- **실 키 활성**: `.env` HIRA_API_KEY = `a467…ce4e6` (Decoding) — 401이었다가 활성화됨, 라이브 200 확인.
- ⚠️ stale `.pyc` 주의: 이전 세션 버전이 하드코딩 가짜 등급을 남겨 `__pycache__`에 잔존했었음 → 제거함. 의심되면 `find backend -name __pycache__ -exec rm -rf {} +`.
- `data/` 폴더 생성: 헤더만 있는 `hira_stats.csv` 템플릿 + `data/README.md`(컬럼정의·출처·채우는 법).
- `index.html`: 추정치 **"추정치" 배지** + **적정성평가 등급 블록**(grades 있을 때만 표시) 추가.
- `.env.example`: HIRA 키 발급처를 data.go.kr 15094093/15001698 로 갱신.
- 검증: CSV행→실데이터, 행없음→추정, 키없음→grades[], uvicorn 부팅+HTTP 응답 OK.

### ⏭ 남은 작업
1. ~~키 401 해소~~ ✅. ~~grades 필드명 검증~~ ✅. ~~CSV 수술건수 실수치 적재~~ ✅ (아래).
2. **CSV 적재 완료**(`data/hira_stats.csv`, 18행): 병원 공식 Outcomes Book/암병원 연보 기준 실수술건수+연도별 추이.
   - AMC 8종(위/대장/폐/유방/간/전립선/자궁경부/자궁내막), SNUBH 5종(위/대장/폐/유방/갑상선※종양), SMC 3종(폐≈/유방/간≈), SNUH 2종(유방≈/전립선≈). ※근사치는 source에 "약" 명시.
   - **심평원 적정성평가는 병원별 대상건수 비공개** → 출처는 병원 공식 Outcomes Book(검증 URL 有). SEV는 공개 연간건수 전무 → 추정 폴백 유지.
   - trend 컬럼 형식 변경: `surgeries_2023/24/25` → `trend="YYYY:건수|..."` (병원별 가용연도 상이 대응). `_row_to_stats` 파싱.
   - 사망률/합병증/재원/연간진료는 비공개 → CSV에 0 → **UI에서 "—" 표시**(index.html 가드 추가).
3. (선택) 미적재분 확충: AMC 갑상선, SMC 대장/전립선, SEV 전체 등은 추정치 표시 중. 출처 확보 시 CSV 행 추가하면 자동 실데이터화.

(이하 과거 조사기록 — 참고용)

### 조사로 확인된 사실 (재조사 불필요)
- 코드의 `opendata.hira.or.kr/op/opc/olapDiagBhvInfo.do` 등은 **REST API가 아니라 로그인(Any-ID SSO) 웹페이지**를 반환함 → 키가 있어도 안 됨. **이 경로는 폐기 대상.**
- `apis.data.go.kr/B551182/DiagBhvInfoService/getDiagBhvInfo` = 더미키로 500. 서비스 존재 불확실.
- data.go.kr 검색 "심평원 수술" = 0건. 정확한 데이터셋명 확인 필요.
- **병원별 × 질환(KCD)별 수술건수/사망률은 공개 REST API로 거의 제공 안 됨**(공개 API는 전국/지역 집계 위주). 병원별 데이터는 **적정성평가(병원평가정보서비스)** 또는 **다운로드 통계파일**에만 존재.

### 권장 진행안 (택1)
- **A. CSV 적재 (가장 확실)**: 코드가 `data/hira_stats.csv`를 **1순위**로 읽음(현재 `data/` 폴더 없음).
  컬럼: `hospital_id, kcd_code, annualSurgeries, annualCases, mortalityRate, complicationRate, readmissionRate, avgLOS, surgeries_2023, surgeries_2024, surgeries_2025, dataYear, source`.
  HIRA 공개 통계를 빅5 × 주요질환만 채우면 실데이터. → `data/` 생성 + 템플릿 + 실수치 채우기.
- **B. data.go.kr 병원평가정보서비스(적정성평가) API**: 위암/대장암/폐암 수술 적정성평가 등 병원별 데이터.
  사용자가 data.go.kr 활용신청(1~2일 승인) 후 serviceKey 제공 → 올바른 엔드포인트로 `hira.py` 수정.
- **C. 보류**: HIRA는 시뮬레이션 유지(소스 표기를 "추정"으로 명확화).

### 해야 할 코드 작업
1. `hira.py`에서 죽은 `opendata.hira.or.kr` 호출 경로 제거.
2. 선택한 방안(A/B)으로 실데이터 경로 구현 + 검증.
3. (선택) HIRA가 진짜가 되면 `_get_fallback_hira_stats`도 KCI/더미처럼 제거 검토.

## 5. 이번 세션에서 한 일 (최근 커밋, 전부 master에 있음/푸시 예정)
- 5개 병원 **실제 크롤러** 구현·검증 (AMC=Playwright/EUC-KR, 나머지 httpx; SEV는 JSON API)
- **h-index 실데이터화**: PubMed esearch + NIH iCite. 영문명 우선순위(병원 영문명 → 자모 로마자).
- **h-index 정확도 다단계 수정**: retmax 500, 소속에 대학명 추가, 영문명 유무 **비대칭 쿼리**(임상강사 동명이인 과대집계 방지), **iCite로 인용소스 복귀**(OpenAlex 예산한도 회피), **esearch 스로틀+재시도 + 동시성 세마포어(6)**로 rate-limit 0 방지.
- **Naver 언론노출 실데이터**: `"이름 교수" "병원"` 정밀 쿼리(부고/동명이인 제거).
- **목적=수술 → 외과 라우팅** (위암→위장관외과 등), SMC 외과 영문슬러그 보강.
- **더미 완전 제거**: genDocs/mkHira/_get_fallback_doctors/_get_fallback_stats 삭제. 실패 시 빈 결과(캐싱 안 함, 자가복구).
- **UX**: 로딩 화면을 실데이터 도착까지 유지, 빈결과 안내문 개선, 모든 화면 **"↺ 처음으로"** 버튼, no-cache 서빙.
- **질환 매핑**: 자궁암/자궁내막암 등 부인암 추가.
- **KCI 통합 완전 제거**.

## 6. 핵심 기술 메모 (이어서 할 때 알아둘 것)
- **name_en(영문 출판명) 소스**: SNUH=카드 로마자 strong / SEV=`nmEn` / AMC=`eng.amc.seoul.kr`(drEmpId 조인) / SMC=`/en/departments/{slug}/doctors.do`(DR_NO 조인, 슬러그맵=`SMCCrawler._EN_SLUG`) / SNUBH=`en_drIntroduce.do?S_DP_CD`(sDrSid 조인).
- **h-index 비대칭 로직**(`backend/api/pubmed.py`): name_en 있으면 풀변형+넓은소속(대학포함), 없으면 이니셜형 제거+병원소속만(임상강사 과대집계 방지). 검색 0건이면 정직하게 h=0(가짜 폴백 없음).
- **rate limit**: esearch 전역 스로틀(`_throttle_esearch`, 무키 0.35s) + 3회 재시도, iCite는 무제한. enrich 동시성 `_ENRICH_SEM = Semaphore(6)` (main.py).
- **크롤러 실패 시 빈 결과**(가짜 생성 안 함), 빈/실패는 캐싱 안 함 → 일시 실패 자가복구. 실제 결과만 6h 캐시.
- **브라우저 캐시 주의**: index.html이 캐시되어 옛 UI가 보이는 일이 잦았음. no-cache 서버 + `?v=N`로 해결. 사용자가 "옛 화면" 호소하면 캐시 의심.
- AMC만 Playwright 필요(Chromium 설치돼 있음). 동시성 하에서 가끔 실패 가능.

## 7. 알려진 한계 / 후속 후보
- HIRA 실데이터(위 4번) — 최우선.
- 흔한 성씨(김/이/박) 정교수는 대학소속 추가로 h-index가 약간 과대될 수 있음(동명이인). ORCID급 식별 필요.
- 영문명단에 없는 일부 교수(최근 부임, 높은 DR_NO)는 h=0으로 과소집계.
- SNUH 외과 세부분과 미해결(수술 검색 시 SNUH 0건). SNUH resolver가 dept.do 상위+IM 페이지만 탐색.
- `/api/reserve`는 인메모리(미저장), AMC 실제 예약 URL 미연동.
- 첫 검색 느림(esearch 스로틀). NCBI 무료 키 넣으면 개선.

## 8. 푸시 상태
- 미푸시 커밋: `476b899` (KCI 제거) + 이 HANDOFF. 새 세션 시작 전/직후 `git push origin master` 권장.
