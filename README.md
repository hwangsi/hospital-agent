# 빅5 병원 통합 예약 에이전트 — 백엔드

## 아키텍처

```
big5-agent/
├── backend/
│   ├── main.py                 # FastAPI 서버 진입점
│   ├── crawlers/               # 병원별 크롤러
│   │   ├── base.py             # 크롤러 베이스 클래스
│   │   ├── snuh.py             # 서울대병원
│   │   ├── amc.py              # 서울아산병원
│   │   ├── smc.py              # 삼성서울병원
│   │   ├── severance.py        # 세브란스병원
│   │   └── snubh.py            # 분당서울대병원
│   ├── api/
│   │   ├── hira.py             # 심평원 공개데이터 API
│   │   ├── pubmed.py           # PubMed E-utilities (H-index)
│   │   ├── kci.py              # KCI 한국학술지인용색인
│   │   └── naver_news.py       # 네이버 뉴스 검색 API
│   └── utils/
│       ├── kcd_mapper.py       # 질환명 → KCD코드 매핑
│       └── encryption.py       # 주민번호 암호화
├── frontend/
│   └── hospital-agent.html     # 프론트엔드 (API 연동 버전)
├── config/
│   └── settings.py             # API 키 및 설정
├── requirements.txt
└── README.md
```

## 데이터 소스별 연동 현실성

| 소스 | 방식 | 난이도 | 비고 |
|------|------|--------|------|
| 심평원 수술건수 | REST API (공개) | ★☆☆ | opendata.hira.or.kr, API키 발급 즉시 |
| PubMed H-index | E-utilities API (무료) | ★☆☆ | NCBI API key 권장 |
| KCI 논문 | REST API (공개) | ★★☆ | kci.go.kr 학술정보 API |
| 네이버 뉴스 | 검색 API (무료) | ★☆☆ | developers.naver.com 앱 등록 |
| 병원 대기일 | 웹 크롤링 (Playwright) | ★★★ | 병원 사이트 구조 변경 시 유지보수 필요 |
| 예약 자동화 | Playwright + 본인인증 | ★★★★ | 간편인증 자동화 법적 제약 → semi-auto 권장 |

## 빠른 시작 (Claude Code)

```bash
# 1. 의존성 설치
pip install -r requirements.txt
playwright install chromium

# 2. API 키 설정
cp config/settings.py.example config/settings.py
# settings.py에 API 키 입력

# 3. 서버 실행
cd backend
uvicorn main:app --reload --port 8000

# 4. 프론트엔드 열기
# frontend/hospital-agent.html을 브라우저에서 열기
# 또는 python -m http.server 3000 --directory frontend
```

## API 키 발급 가이드

### 심평원 (HIRA)
1. https://opendata.hira.or.kr 회원가입
2. 마이페이지 → 인증키 발급
3. "진료행위별 의료기관 통계" API 활용 신청

### 네이버 검색 API
1. https://developers.naver.com 로그인
2. 애플리케이션 등록 → 검색 API 선택
3. Client ID / Secret 발급

### PubMed (NCBI)
1. https://www.ncbi.nlm.nih.gov/account/ 계정 생성
2. Settings → API Key 발급 (없어도 가능, rate limit 다름)
