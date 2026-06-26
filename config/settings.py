"""
API 키 및 설정.
실제 배포 시 환경변수 또는 .env 파일 사용 권장.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ─── 심평원 (HIRA) ─────────────────────────────────
HIRA_API_KEY = os.getenv("HIRA_API_KEY", "YOUR_HIRA_API_KEY")
HIRA_BASE_URL = "https://opendata.hira.or.kr/op/opc/olapDiagBhvInfo.do"

# ─── 네이버 검색 API ────────────────────────────────
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "YOUR_NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "YOUR_NAVER_CLIENT_SECRET")
NAVER_SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"

# ─── PubMed (NCBI E-utilities) ──────────────────────
NCBI_API_KEY = os.getenv("NCBI_API_KEY", "")  # 선택사항, 없으면 rate limit 3/sec
NCBI_BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

# ─── 병원 예약 페이지 URL ────────────────────────────
HOSPITAL_URLS = {
    "snuh": {
        "home": "https://www.snuh.org",
        "reservation": "https://www.snuh.org/reservation",
        "doctor_search": "https://www.snuh.org/medical/doctor/findDoctor.do",
    },
    "amc": {
        "home": "https://www.amc.seoul.kr",
        "reservation": "https://www.amc.seoul.kr/asan/reservation",
        "doctor_search": "https://www.amc.seoul.kr/asan/search/doctor",
    },
    "smc": {
        "home": "https://www.samsunghospital.com",
        "reservation": "https://www.samsunghospital.com/reservation",
        "doctor_search": "https://www.samsunghospital.com/home/doctor/doctorFind.do",
    },
    "sev": {
        "home": "https://sev.severance.healthcare",
        "reservation": "https://sev.severance.healthcare/reservation",
        "doctor_search": "https://sev.severance.healthcare/doctor/search.do",
    },
    "snubh": {
        "home": "https://www.snubh.org",
        "reservation": "https://www.snubh.org/reservation",
        "doctor_search": "https://www.snubh.org/medical/doctor/findDoctor.do",
    },
}

# ─── 서버 설정 ───────────────────────────────────────
CACHE_TTL_HOURS = 6          # 크롤링 데이터 캐시 유효시간
MAX_CONCURRENT_CRAWLS = 3    # 동시 크롤링 수
REQUEST_TIMEOUT = 30         # 초
ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "change-this-in-production-32bytes!")
