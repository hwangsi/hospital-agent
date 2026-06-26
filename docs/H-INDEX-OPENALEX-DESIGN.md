# H-index 산출 OpenAlex 저자 엔티티 전환 — 설계

> 목적: 현재 PubMed 이름-문자열 매칭(+iCite)을 **OpenAlex 저자 엔티티(disambiguation)** 기반으로
> 교체해 동명이인 오염·500 cap·인용 과소 문제를 제거한다. 레퍼런스 구현은
> [`hwangsi/researcher-kg`](https://github.com/hwangsi/researcher-kg) (브라우저 단일-저자 앱).

---

## 0. 현재 문제 (요약)

분당서울대 김기동 실측:

| 방식 | 논문 | 인용 | h-index |
|---|---|---|---|
| 현재 앱 (PubMed `"Kim K"`+iCite) | 500(cap) | 1,066 | **12** |
| 변형버그만 수정한 PubMed | 156 | 1,564 | 21 |
| Semantic Scholar 저자 엔티티 | 191 | 2,792 | **30** |

원인 3종: ① `english_name_to_pubmed_variants`가 외자 이니셜 `Kim K` 생성 → 동명이인 폭증,
② esearch 관련도순 + retmax 500 cap → 본인 중간급 논문 누락, ③ iCite 인용 커버리지 255/500.
→ 위/아래 양방향 왜곡. **사람을 식별하지 못하는 게 근본 원인.**

---

## 1. researcher-kg 동작 (레퍼런스)

`js/api.js` `searchAuthors(input)` 파이프라인:

1. **소속 해석** `_resolveInstitution(institution)` → `/institutions?search=` → OpenAlex 기관 ID 목록.
2. **후보 저자** `_fetchCandidateAuthors` → `/authors?search={name}&filter=affiliations.institution.id:{ids}|...,affiliations.institution.country_code:KR`.
3. **기준 필터** `_matchesCriteria` — 이름 토큰 전부 포함 + (기관 ID 일치 **또는** 기관 토큰 일치) + (옵션) ORCID.
4. **전공 검증** `_filterByWorkSpecialty` — 후보별 상위 50편 work 샘플을 주제/저널/제목 정규식으로 매칭, 증거량 임계치(`_passesSpecialtyEvidence`) 통과자만.
5. **분리 ID 그룹화** `_dedupeAndGroup` — 같은 정규화 이름 + **공유 기관 토큰**(또는 동일 ORCID)이면 동일인 후보(`_duplicateGroupIds`)로 묶음.
6. (인간) 저자 선택 + 분리 ID 체크박스 **수동 병합** → `_mergedIds`.
7. **전체 works** `fetchAllWorks(authorId)` — `/works?filter=author.id:{id}` 커서 페이지네이션(정렬=ID, 누락 방지), merge된 ID마다.
8. **h-index 계산** `dashboard.js _renderStats` — **merge된 works 합집합**의 `cited_by_count` 정렬 후 h. (OpenAlex `summary_stats.h_index`가 아니라 직접 계산 → 병합 반영)

핵심 설계 결정: **(a)** 저자=엔티티(문자열 아님), **(b)** ORCID 최우선, **(c)** 분리 ID 수동 병합,
**(d)** h는 works 합집합에서 계산, **(e)** 전공은 work 증거로 검증.

---

## 2. 우리 제약 (researcher-kg와 다른 점)

| 항목 | researcher-kg | hospital-agent |
|---|---|---|
| 트리거 | 저자 1명, 인터랙티브 | 검색당 의사 수십~수백, 자동 |
| 식별 보조 | 인간이 선택·병합, ORCID 입력 | **인간 없음, ORCID 없음** (크롤러: 이름·name_en·병원·진료과) |
| 레이트리밋 | 브라우저, polite pool, 1명씩 | **서버 버스트 → OpenAlex 429** (이미 경험) |
| 산출물 | 리치 시각화 | h_index·papers·citations 3값 |

따라서 그대로 못 옮긴다. **자동 disambiguation + 레이트리밋 방어 + PubMed 폴백**이 추가 설계 포인트.

---

## 3. 제안 아키텍처

### 3.1 신규 모듈 `backend/api/openalex.py`

```python
class OpenAlexClient:
    """OpenAlex 저자 엔티티 기반 h-index. PubMed(수정본)로 폴백."""
    _throttle_lock = asyncio.Lock()
    _last = 0.0
    POLITE = "mailto=hwangsi49@gmail.com"      # polite pool (10req/s, 안정 레이트)
    BASE = "https://api.openalex.org"

    # 빅5 → OpenAlex 기관 ID (최초 1회 /institutions?search=로 해석 후 PIN, 6.2 참고)
    INSTITUTION_IDS = {
        "서울대학교병원":  ["I..."],   # Seoul National University Hospital (+ Seoul National University)
        "서울아산병원":    ["I..."],   # Asan Medical Center (+ University of Ulsan)
        "삼성서울병원":    ["I..."],   # Samsung Medical Center (+ Sungkyunkwan University)
        "세브란스병원":    ["I..."],   # Severance / Yonsei University
        "분당서울대병원":  ["I..."],   # SNU Bundang Hospital (+ Seoul National University)
    }
```

### 3.2 의사 1명 해석 알고리즘 (자동, 인간 대체)

입력: `name_en`(크롤러 영문명), `hospital_name`, `department`(전공 힌트), `orcid`(있으면).

```
1. orcid 있으면:  GET /authors?filter=orcid:{orcid}        → 확정 1건. (최정확)
2. 없으면:        GET /authors?search={name_en}
                    &filter=affiliations.institution.id:{hospital_ids 합집합}
                    &select=id,display_name,orcid,works_count,cited_by_count,
                            summary_stats,affiliations,topics
                    &per-page=25
3. 후보 필터 (researcher-kg _matchesCriteria 포팅):
     - 이름 토큰 전부 display_name에 포함
     - 기관 ID 일치 (filter로 이미 보장; 0건이면 기관 토큰 폴백)
4. 분리 ID 그룹화 (researcher-kg _dedupeAndGroup 자동판):
     - 동일 정규화 이름 + 공유 기관 ID → 동일인 후보 그룹으로 자동 병합
       (인간 병합을 "동명+동일기관=동일인"으로 근사. 보수적이라 과병합 위험 낮음)
5. h-index:
     - 기본(BATCH) 모드: 그룹 내 works_count 최대 엔티티의 summary_stats.h_index
                         + 그룹 works_count·cited_by_count 합산 (논문/인용 표기용)
     - 정밀(ACCURATE) 모드: 그룹 전 ID의 fetchAllWorks 합집합 → cited_by_count로 h 직접계산
       (researcher-kg와 동일. 비용 ↑ → 상위 N명 또는 캐시미스에만)
6. 전공 교차검증(옵션): 그룹 대표의 topics에 진료과 매핑 토픽이 없고 후보가 2+이면
   work 샘플(50편) 매칭으로 재정렬 (researcher-kg _filterByWorkSpecialty 축약).
7. 0건/실패 → PubMed(수정본) 폴백.
```

### 3.3 h-index 계산 — 두 모드 트레이드오프

| 모드 | 비용/의사 | 정확도 | 용도 |
|---|---|---|---|
| **BATCH** (summary_stats) | OpenAlex **1요청** | 분리 ID 있으면 약간 과소(max h) | 기본(검색 결과 수십명) |
| **ACCURATE** (works 합집합) | 1 + ⌈works/200⌉ 요청 | researcher-kg 동일 | 상위 노출·상세보기 |

> 분리 ID는 h를 **합산 불가**(h는 가산적이지 않음). BATCH는 그룹 내 **max(h_index)** 를 하한으로 사용.
> 진짜 병합값이 필요하면 ACCURATE로 승급. 실측 김기동: summary_stats 단일 엔티티 h=30이 이미 충분.

### 3.4 레이트리밋 방어 (서버측 필수)

- **polite pool** `mailto=` 부착 (10 req/s 안정 구간).
- **전역 스로틀** `_throttle`(≈0.15s 간격) + **동시성 세마포어**(예: 4) — PubMed `_throttle_esearch`/`_ENRICH_SEM`와 동형.
- **영속 캐시**: `(name_en, hospital)` → 결과. h-index는 천천히 변함 → **TTL 7~30일**, 디스크 저장(`.cache/openalex.json`).
- **429 백오프**: 지수 백오프 3회 → 실패 시 **PubMed 폴백**(0 캐싱 안 함, 다음 검색 재시도).
- 우리가 겪은 429는 5개 리서치 에이전트 동시 버스트가 원인 — 스로틀+캐시+세마포어로 해소.

### 3.5 통합 지점 `backend/main.py` `_enrich_doctor`

```python
async with _ENRICH_SEM:
    pub_task   = openalex_client.get_h_index(doc["name_en"] or doc["name"],
                                             hospital_names[hospital_id],
                                             department=doc.get("department",""),
                                             orcid=doc.get("orcid",""))
    naver_task = naver_client.get_news_count(doc["name"], hospital_names[hospital_id])
    pub_result, news_count = await asyncio.gather(pub_task, naver_task, ...)
```

`get_h_index` 반환 형식은 현행과 동일 `{h_index, papers, citations}` (+ 선택 `oa_author_id`, `source`),
프론트 변경 불필요. `source`로 "OpenAlex/PubMed-fallback" 구분 노출 가능.

---

## 4. researcher-kg ↔ 제안 매핑

| researcher-kg | 제안(hospital-agent) | 비고 |
|---|---|---|
| `_resolveInstitution` | `INSTITUTION_IDS` PIN(1회 해석) | 빅5 고정이라 런타임 해석 불필요 |
| `_fetchCandidateAuthors` | 동일 `/authors?search=&filter=inst.id` | country_code:KR도 추가 가능 |
| `_matchesCriteria` | 이름 토큰 + 기관 일치 포팅 | 전공은 옵션 |
| `_filterByWorkSpecialty` | 후보 2+ 동점일 때만 축약 적용 | 비용 절감 |
| `_dedupeAndGroup` (수동 병합) | **자동 병합**: 동명+동일기관 | 인간 부재 → 보수적 규칙 |
| ORCID 입력 | 크롤러가 ORCID 수집 시 최우선 | 6.3 향후 |
| `fetchAllWorks`+`_renderStats` h | ACCURATE 모드에 동일 채택 | works 합집합 h |
| `fetchSourceStats`(JCR IF) | **미채택** | 우리는 IF 미표시 |

핵심 차이: researcher-kg는 **인간이 선택·병합**, 우리는 **동명+동일기관=동일인 자동 근사**.
빅5 소속 + 병원 제공 영문명(name_en)이라 후보가 좁아 자동 근사의 오류 위험이 낮음.

---

## 5. 코드 스케치 (`openalex.py` 핵심)

```python
async def get_h_index(self, name_en, hospital_name, department="", orcid="") -> dict:
    key = f"oa:{orcid or name_en}:{hospital_name}"
    if key in self._cache: return self._cache[key]
    try:
        if orcid:
            authors = await self._get(f"/authors?filter=orcid:{orcid}")
        else:
            inst = "|".join(self.INSTITUTION_IDS.get(hospital_name, []))
            authors = await self._get(
                f"/authors?search={quote(name_en)}"
                f"&filter=affiliations.institution.id:{inst}"
                f"&select=id,display_name,orcid,works_count,cited_by_count,summary_stats,affiliations"
                f"&per-page=25")
        cands = [a for a in authors if self._name_ok(a, name_en)]
        if not cands:
            return await self._pubmed_fallback(name_en, hospital_name)
        group = self._merge_same_person(cands)            # 동명+동일기관 그룹
        rep   = max(group, key=lambda a: a["works_count"])
        h = max(a["summary_stats"]["h_index"] for a in group)   # BATCH 하한
        res = {"h_index": h,
               "papers": sum(a["works_count"] for a in group),
               "citations": sum(a["cited_by_count"] for a in group),
               "oa_author_id": rep["id"], "source": "openalex"}
        self._cache[key] = res
        return res
    except RateLimited:
        return await self._pubmed_fallback(name_en, hospital_name)
```

`_get`은 스로틀+백오프+polite pool. `_pubmed_fallback`은 **수정된** PubMed 파이프라인 호출.

---

## 6. 마이그레이션 단계

### 6.1 즉시 (폴백 견고화 — OpenAlex 무관하게 선행)
- `english_name_to_pubmed_variants`에서 **외자 이니셜 변형 제거**
  (영문명 분기에도 `_is_initials_variant` 필터 적용). → 김기동 12→21.
- 이 PubMed 경로가 그대로 OpenAlex의 폴백이 됨.

### 6.2 기관 ID 핀
- 최초 1회 `/institutions?search=` 로 빅5 OpenAlex ID 확정 → `INSTITUTION_IDS`에 하드코딩.
- (현재 IP가 429라 미해결 — 레이트리밋 풀린 뒤 1회 실행. 예: SNUH/AMC/SMC/Severance/SNUBH + 모대학.)

### 6.3 OpenAlex 클라이언트 + 통합
- `openalex.py` 구현, `_enrich_doctor` 배선, 영속 캐시·스로틀·세마포어.
- BATCH 모드 기본. 결과에 `source` 부착.

### 6.4 (향후) 정밀화
- ACCURATE 모드(상위 노출 의사 works 합집합 h).
- 크롤러가 ORCID 수집(일부 병원 프로필에 ORCID 노출) → 최우선 사용.
- 전공(work 샘플) 교차검증 활성화.

---

## 7. 검증 계획

- **회귀 픽스처**: 김기동(분당, 산부인과) → OpenAlex h ≈ 30 (S2 교차검증값), 현행 12 대비.
- 동명이인 흔한 성씨(김/이/박) 정교수 5명 표본 → OpenAlex vs (수정)PubMed vs S2 3원 비교표.
- 레이트리밋: 1회 검색(의사 ~80명) 시 OpenAlex 요청수·429율·평균지연 측정(목표 429=0, 캐시히트 후 <1s).
- 폴백 경로: OpenAlex 강제 실패 주입 시 PubMed(수정본)로 graceful degrade 확인.

## 8. 리스크 / 결정 필요

| 리스크 | 완화 |
|---|---|
| OpenAlex 서버측 레이트리밋 | polite pool + 스로틀 + 세마포어 + 영속캐시 + 폴백 |
| 자동 병합 과병합(다른 사람 합산) | 보수 규칙(동명 **AND** 동일 기관 ID), 빅5로 모집단 축소 |
| 분리 ID로 BATCH h 과소 | max(h) 하한, 필요시 ACCURATE 승급 |
| name_en 부정확(임상강사 등) | OpenAlex 0건 → PubMed 폴백(수정본) |
| OpenAlex 한국 인용 커버리지 | iCite보다 넓음(이미 우위), S2 교차검증 |

**결정 필요**: (a) 기본 모드 BATCH로 시작 OK? (b) 캐시 TTL(7일 제안)? (c) 6.1 즉시 픽스를 지금 적용할지.
