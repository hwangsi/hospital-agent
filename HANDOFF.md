# HANDOFF — 빅5 병원 통합 예약 에이전트

> 갱신: 2026-07-05. 새 세션은 이 문서부터 읽고 이어가면 됩니다. 모든 커밋은 `master`에 푸시됨.

## 1. 프로젝트 한 줄 요약
서울대·아산·삼성·세브란스·분당서울대(빅5)의 **실제 의료진을 크롤링**해 통합 검색하고,
각 의사의 **h-index·언론노출**, 병원의 **수술건수·적정성평가 등급**으로 비교한 뒤
세미오토(프리필) 예약으로 연결하는 단일 페이지 앱.

- 백엔드: FastAPI (`backend/main.py`), 포트 8000
- 프론트: 단일 `index.html` (React+Babel CDN, 빌드 없음), 정적 서버 포트 8080
- 저장소: GitHub `hwangsi/hospital-agent`, 브랜치 `master`
- 파이썬: `.venv/Scripts/python.exe` (Windows). AMC 크롤러만 Playwright(Chromium 설치됨) 사용.

## 2. 실행 방법 (새 세션에서 서버 재기동 필요 — 백그라운드 서버는 세션 종료 시 죽음)
```bash
# 1) 백엔드 (repo 루트)
.venv/Scripts/python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 --log-level warning
```
```python
# 2) no-cache 정적 서버 (브라우저가 옛 index.html 캐시하는 문제 방지 — 필수)
import http.server, socketserver, os
os.chdir(r"C:\Users\hwang\AIprojects\hospital-agent")
class H(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control","no-store, no-cache, must-revalidate, max-age=0")
        super().end_headers()
socketserver.TCPServer(("127.0.0.1",8080), H).serve_forever()
```
- 브라우저 열기(기본 브라우저): PowerShell `Start-Process "http://127.0.0.1:8080/index.html?v=<임의숫자>"`.
- `?v=N`은 캐시버스트. index.html은 `http://127.0.0.1:8000/api/search` 호출, CORS `*`.
- Chrome 확장(claude-in-chrome)은 이 환경에서 **미연결** → 브라우저 자동조작 불가. Playwright로 스크린샷/검증.
- ⚠️ **stale `.pyc` 주의**: 코드 바꿨는데 옛 동작이면 `find backend -name __pycache__ -type d -exec rm -rf {} +`.

## 3. 데이터 소스 현황 (★ 전부 실데이터, 가짜 표기는 명확히 구분)
| 지표 | 상태 | 소스 / 비고 |
|---|---|---|
| 의료진 목록·**전문분야** | ✅ 실크롤링 | 5개 병원. 전문분야 텍스트로 질환 매칭 필터 적용 |
| 의사 **소개페이지 링크** | ✅ 실링크 | 병원별 프로필 딥링크(5장) — §5 |
| **수술건수 (병원별)** | ✅ CSV 실데이터 / 없으면 추정(배지) | `data/hira_stats.csv` 18행, 병원 Outcomes Book — §4 |
| **적정성평가 등급** | ✅ data.go.kr 실 API | 우수기관병원평가정보서비스 — §4 |
| **h-index** | ✅ 실데이터 (체인) | OpenAlex→SemanticScholar→PubMed — §6 |
| 언론노출 (Naver) | ✅ 실데이터 | `.env` 실키(커밋 안 됨) |
| KCI (국내논문) | ❌ 제거됨 | 이전 세션 완전 삭제 |

`.env`(gitignore): `NAVER_CLIENT_ID/SECRET`=실제, `HIRA_API_KEY`=실제(활성), `OPENALEX_MAILTO`=hwangsi49@gmail.com,
`NCBI_API_KEY`=실제(2026-07-05 등록·`X-RateLimit-Limit: 10` 라이브 확인 — esearch 3→10req/s, 스로틀 0.35→0.11s 자동).

## 4. HIRA(심평원) — 하이브리드 (완료·라이브 검증됨)
**결정: 하이브리드** — 정량(수술건수)은 **CSV**, 적정성평가 "등급"은 **data.go.kr 실 API**.
(병원별×질환별 수술건수/사망률은 공개 REST API에 없음 재확인 → CSV가 유일 실데이터 경로.)

- `backend/api/hira.py`: 죽은 `opendata.hira.or.kr` olap 경로 전부 제거.
  - **CSV 1순위**(`data/hira_stats.csv`): 행 있으면 `isEstimate=False` 실데이터, 없으면 `isEstimate=True` 추정치.
  - **등급 API**: `apis.data.go.kr/B551182/exclInstHospAsmInfoService1/getExclInstHospAsmInfo1` — 우수기관 전체목록(~6600건) 락으로 1회 적재·캐시 후 빅5 **yadmNm 정확일치**(부분일치 금지: 강남/용인세브란스·강릉아산 오매칭). 필드 `asmNm`(항목)/`asmGrdNm`("3회연속"/"최근우수")/`yadmNm`. 라이브 결과 snuh7·amc9·smc9·sev9·snubh6.
  - `HOSPITAL_YADM`: amc=`재단법인아산사회복지재단서울아산병원`, sev=신촌 본원만.
- **CSV 24행 적재**: 병원 공식 Outcomes Book/암병원 연보 기준(심평원은 병원별 대상건수 비공개). AMC 8종, SNUBH 5종(갑상선은 '종양'), **SMC 9종**(공식 통계연보 6종 + 뉴스 공시 3종), SNUH 2종(유방≈/전립선≈). 근사치는 `source`에 "약" 명시.
  - **SMC 확충(2026-07-05)**: 삼성서울 암병원 **암환자통계 2008-2022** PDF(공식·연도별 수술건수 실측)에서 위1663/대장1359/전립선800/갑상선761/자궁경부148/자궁체부293 적재(2022, trend 3년). 단 이 지표는 "진단 4개월 내 초치료 수술"이라 수술 총량보다 작음 → 총량 공시값 있는 유방/폐/간은 기존 뉴스 행 유지. PDF URL은 `data/README.md`.
  - **AMC 갑상선·SEV 전체는 구조적 비공개 재확인**(AMC Outcomes Book 7개 센터에 갑상선 없음, SEV는 누적치뿐) → 추정 폴백 유지. 재조사 불필요.
  - `trend` 컬럼 형식 `"YYYY:건수|YYYY:건수|..."` (병원별 가용연도 상이). `data/README.md`에 채우는 법.
  - 사망률/합병증/재원/연간진료는 비공개→CSV에 0→**UI에서 "—"**.
- `index.html`: **"추정치" 배지** + **적정성평가 우수기관 등급 블록**(있을 때만).
- ⏭ 미적재분: ~~SMC 대장/전립선~~ ✅ 적재 완료. AMC 갑상선·SEV 전체는 공개 데이터 없음(위 재조사 결론) — 추정치가 최종 상태.

## 5. 의사 소개페이지 딥링크 (완료·실브라우저 검증됨)
각 의사 카드 "이 의사로 예약 →" 옆 **"🔗 병원 소개페이지"** 버튼(새 탭). URL 빌더는 `backend/crawlers/base.py`, 필드 `profile_url`.
- amc: `staffBaseInfoDetail.do?drEmpId={emp_id}` / smc: `doctorProfile.do?DR_NO={emp_id}`
- sev: `doctor-view.do?empNo={emp_id}&deptSeq={seq}`(empNo 이미 인코딩) / snubh: `drIntroduce.do?sDpCd={dept}&sDrSid={emp_id}&sDrStfNo={stf}&sDpTp=O`
- snuh: 내부사번 미보유 → 통합검색 `search.snuh.org/...?wnquery={이름}` (직행 대신 검색결과)

## 6. h-index — OpenAlex 저자 엔티티 체인 (구현 완료·**라이브 검증됨 2026-07-05**)
이름-문자열 매칭(PubMed)의 동명이인 오염을 **disambiguation된 저자 엔티티**로 교체. 참고구현 `github.com/hwangsi/researcher-kg`(OpenAlex). 설계문서 `docs/H-INDEX-OPENALEX-DESIGN.md`.

**체인: OpenAlex(정밀·기관필터) → Semantic Scholar(빠른 1요청) → PubMed(최후 폴백)**
- `backend/api/openalex.py`: `/authors?search=&filter=affiliations.institution.id`→이름토큰 필터→동명+동일기관 자동병합→`h=max(summary_stats.h_index)`, papers/cites 합산. ORCID 우선. 회로차단(연속 3회 429시 10분 스킵)+기관해석 락+polite pool.
- `backend/api/semantic_scholar.py`: S2 author search **1요청**으로 사전계산 hIndex(논문 fetch 불필요=빠름). 회로차단 동일.
- `backend/api/pubmed.py`: 최후 폴백. **6.1 픽스 적용됨** — `_variants`가 외자 이니셜(`Kim K`) 미생성(동명이인 폭증 방지). 김기동 h 12→23.
- `main.py`: `hindex_client = OpenAlexClient(fallback=S2(fallback=PubMed))`, 응답에 `hindex_source`.
- **Google Scholar는 부적합**(공식 API 없음+봇차단) — 같은 "최종 h-index 1회 조회"를 OpenAlex/S2로 달성.

✅ **라이브 검증 완료 (2026-07-05)**: `scripts/validate_openalex.py` 실행 → 5명 전원 `source='openalex'`
(김기동 h=32·노동영 72·방영주 106·김열홍 52·정현철 88 — 김기동은 지난 세션 S2 교차검증값 30과 근접, PubMed 오염값 4~23 아님).
- **기관 ID 핀 적용됨**(`HOSPITAL_INST_IDS`): 런타임 키워드 검색이 강북삼성(I4210103535)·강남세브란스(I4210144108)를
  끌어오던 오매칭 제거 + 기동 시 해석요청 10회 절약. 키워드 검색은 미등록 병원 폴백으로만 유지. 핀 후 재검증 결과 동일.
- ⚠️ S2 알려진 한계: 무인증 S2는 429가 매우 잦음(반복 테스트 금지 — IP 소진됨). 또한 S2 후보선택이 이름 표기 변형
  ("Ki-Dong Kim" vs "Kidong Kim")에 따라 다른 엔티티(h=7)를 집을 수 있음. OpenAlex 장애 시에만 쓰는 폴백이라 영향 제한적.
- 참고: `scripts/validate_openalex.py`는 cp949 콘솔에서 ✅ 문자로 UnicodeEncodeError → `PYTHONIOENCODING=utf-8`로 실행.

## 7. 질환→의사 매칭 (전문분야 필터, 완료·검증됨)
진료과(예: 외과)에 위암/대장/간담췌가 섞여 나오던 문제 해결. **크롤링된 전문분야 텍스트**로 질환 매칭.
- `kcd_mapper.specialty_terms(질환)` → 키워드, `main.py _filter_doctors_by_specialty`가 필터.
- 예: 위암 검색 → SNUBH 33명→6명(진짜 위암외과), **강성범(대장암) 제외** ✅.
- 안전장치: 전문분야 미상은 유지, 매칭 0이면 전체 유지(빈 결과 방지).

## 8. 기타 UI/데이터 규칙
- **"평균 기관별 수술수"**: 개인 수술건수는 심평원에 없어(가짜 0), `기관 연간수술 ÷ 표시된 의사수`로 평균 산출·표기. 없으면 "—".
- 목적=수술 → 외과 라우팅(위암→위장관외과 등). 더미 완전 제거(실패 시 빈 결과, 캐싱 안 함=자가복구). 모든 화면 "↺ 처음으로".

## 9. 핵심 기술 메모
- **name_en 소스**: SNUH=카드 로마자 / SEV=`nmEn` / AMC=`eng.amc.seoul.kr`(drEmpId 조인) / SMC=`/en/departments/{slug}/doctors.do`(DR_NO, 슬러그맵 `SMCCrawler._EN_SLUG`) / SNUBH=`en_drIntroduce.do`(sDrSid). emp_id=병원식별자(프로필 링크에도 사용).
- **enrich 동시성** `_ENRICH_SEM=Semaphore(6)` (main.py). h-index/naver 병렬.
- **크롤러 실패=빈 결과**(가짜 안 만듦), 실패/빈결과 캐싱 안 함, 실결과만 6h 캐시.
- HIRA 우수기관 인덱스는 프로세스 1회 적재(락). h-index 캐시는 **2단**: 클라이언트 인메모리 L1 + `.cache/hindex.sqlite3` 영속 L2(`backend/utils/hindex_cache.py`, main.py `_get_hindex_cached`). L2 TTL: openalex/S2 7일·pubmed-fallback 1일·h=0 미저장.

## 10. 알려진 한계 / 후속 후보
- ~~h-index 라이브 검증~~ ✅ 완료(§6, 2026-07-05). 기관ID 핀도 적용.
- ~~HIRA 미적재 암종 CSV 확충~~ ✅ SMC 6종 적재 완료(§4, 2026-07-05). 잔여(AMC 갑상선·SEV)는 공개 데이터 없음 확인 — 종결. 사망률/합병증은 공개 안 됨(구조적).
- ~~SNUH 외과 세부분과~~ ✅ 해결(2026-07-05): resolver 1순위를 `/reservation/meddept/main.do`(전 진료과 서버렌더, `treatItemWrap`+`goDetail('코드')`)로 교체 — 위장관외과 GIS·대장항문외과 CRS·간담췌외과 HBPS·유방내분비외과 BEN 해석됨. `_match_dept` 부분일치를 최장(가장 구체적) 과명 우선으로 수정 → 흉부외과가 '외과'(GS)가 아닌 '심장혈관흉부외과'(TS)로 매칭(1→15명). 위암 수술 검색 시 SNUH 6명 중 5명(위암 전문) 반환 검증.
- ~~유방암·갑상선암 수술과 라우팅~~ ✅ 해결(2026-07-05): 두 질환은 `dept` 자체가 외과(유방외과/내분비외과)라 `surgery_dept` 불필요 — 진짜 문제는 병원별 과명 변형. `_DEPT_ALIASES`에 유방외과→[유방내분비외과 등]·내분비외과→[갑상선내분비외과 등] 추가('유방외과'는 '유방내분비외과'의 연속 부분문자열이 아니라 별칭 필수). 5개 병원 라이브 검증: 유방외과 snuh10/amc19/smc17/sev5/snubh33, 내분비외과 snuh10/amc8/smc4/sev7/snubh33(snubh는 외과 전체→필터 설계). 전문분야 필터: 유방암 snuh10→6·snubh33→4, 갑상선암 snuh10→3·snubh33→4.
- ~~`/api/reserve` 미저장~~ ✅ 영속화(2026-07-05): `var/reservations.sqlite3`(`backend/utils/reservation_store.py`, PII 포함이라 var/는 gitignore·SSN은 has_ssn 불리언만). 신규 API: GET `/api/reservations`(목록)·GET/PATCH `/api/reservations/{id}`(단건/상태갱신). TestClient로 저장→조회→상태갱신→재시작 후 조회 검증.
- ~~AMC 실제 예약 URL~~ ✅ 연동(2026-07-05): AMC 카드의 실제 프리필 딥링크(`main.do?doct={암호화블롭}&reservMode=DOCT` — doctor_id로 합성 불가)를 검색응답 `reservation_url`→프론트 `reservationUrl`→`/api/reserve` `reservation_url`로 관통. 백엔드 `_is_hospital_url`(https+빅5 도메인 화이트리스트, 서브도메인 허용)로 검증 후 `redirect_url` 사용, 실패/미전달 시 기존 `build_reservation_url` 폴백. 유닛 10케이스+e2e 4시나리오 통과. 타 병원(snuh 등)은 크롤링 딥링크 없어 폴백 유지.
- ~~h-index 캐시 영속화~~ ✅ 완료(2026-07-05, §9): SQLite L2, 재시작 검증(1.46s→0.7ms).
- ~~NCBI 키~~ ✅ 등록 완료(2026-07-05, §3). 첫 검색 속도는 h-index L2 캐시(§9)+NCBI 10req/s로 대부분 해소 — 남은 건 크롤링 자체 시간뿐.

## 11. 커밋/푸시 상태
- 전부 `master`에 푸시됨. 최근: `f178348`(평균수술수) `58156e3`(전문분야필터+S2) `0dedffa`(소개링크+OpenAlex) `806db84`(CSV) `ea19e41`(등급API).
