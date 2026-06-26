"""
한글 의사 성명 → PubMed 검색용 영문 표기 변환 유틸리티.

두 가지 입력 경로:
  1) 병원 사이트가 영문명을 제공하는 경우(서울대 로마자, 세브란스 nmEn 등)
     → english_name_to_pubmed_variants() 로 실제 출판명 기반 변형 생성 (가장 정확)
  2) 한글 이름만 있는 경우
     → convert_korean_name_to_english(): 한글 자모를 분해해 국립국어원
        로마자 표기법(Revised Romanization)으로 변환 (성씨는 관용 표기 우선)

기존 구현은 ~40개 음절만 매핑한 사전 + 'Gildong' 폴백이라 대부분의 실명을
'Yoon Jung don' 처럼 엉터리로 변환했음 → 자모 분해 알고리즘으로 전면 교체.
"""
import re

# 주요 명의 영문 매핑 사전 — 사이트 영문명이 없을 때 가장 정확한 출판명 (최우선)
FAMOUS_DOCTORS_MAP = {
    # 서울대병원
    "안규리": ["Ahn KR", "Ahn Kyu Ri", "Kyu Ri Ahn", "Kyu-Ri Ahn"],
    "양한광": ["Yang HK", "Yang Han Kwang", "Han Kwang Yang", "Han-Kwang Yang"],
    "방영주": ["Bang YJ", "Bang Yung-Jue", "Yung-Jue Bang"],
    "노동영": ["Noh DY", "Noh Dong-Young", "Dong-Young Noh"],
    # 서울아산병원
    "이승규": ["Lee SG", "Lee Sung-Gyu", "Sung-Gyu Lee"],
    "송재관": ["Song JK", "Song Jae-Kwan", "Jae-Kwan Song"],
    "김송철": ["Kim SC", "Kim Song-Cheol", "Song-Cheol Kim"],
    # 삼성서울병원
    "박표원": ["Park PW", "Park Pyowon", "Pyowon Park"],
    "이우용": ["Lee WY", "Lee Woo Yong", "Woo Yong Lee"],
    "안진석": ["Ahn JS", "Ahn Jin Seok", "Jin Seok Ahn"],
    # 세브란스병원
    "장양수": ["Jang YS", "Jang Yangsoo", "Yangsoo Jang"],
    "노성훈": ["Noh SH", "Noh Sung Hoon", "Sung Hoon Noh"],
    "정현철": ["Chung HC", "Chung Hyun Cheol", "Hyun Cheol Chung"],
    # 분당서울대병원
    "전상훈": ["Jeon SH", "Jeon Sang Hoon", "Sang Hoon Jeon"],
    "오창완": ["Oh CW", "Oh Chang-Wan", "Chang-Wan Oh"],
}

# 한글 성의 관용 로마자 표기 (PubMed 출판명은 RR이 아닌 관용표기를 따름: Lee/Kim/Park/Yoon …)
KOREAN_LAST_NAMES = {
    "김": "Kim", "이": "Lee", "박": "Park", "최": "Choi", "정": "Jung",
    "강": "Kang", "조": "Cho", "윤": "Yoon", "장": "Jang", "임": "Lim",
    "한": "Han", "오": "Oh", "서": "Seo", "신": "Shin", "권": "Kwon",
    "황": "Hwang", "안": "Ahn", "송": "Song", "류": "Ryu", "유": "Yoo",
    "홍": "Hong", "전": "Jeon", "고": "Ko", "문": "Moon", "양": "Yang",
    "손": "Sohn", "배": "Bae", "백": "Baek", "허": "Hur", "노": "Noh",
    "심": "Shim", "남": "Nam", "구": "Koo", "곽": "Kwak", "성": "Sung",
    "차": "Cha", "주": "Joo", "우": "Woo", "민": "Min", "라": "Ra",
}

# ─── 한글 자모 분해 → 국립국어원 로마자 표기(RR) ──────────────────
_CHO = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss",
        "", "j", "jj", "ch", "k", "t", "p", "h"]
_JUNG = ["a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae",
         "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i"]
_JONG = ["", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l",
         "l", "p", "l", "m", "p", "p", "t", "t", "ng", "t", "t", "k",
         "t", "p", "t"]


def romanize_syllable(ch: str) -> str:
    """한글 음절 1자를 로마자로 변환. 예: '정'→'jeong', '환'→'hwan', '렬'→'ryeol'."""
    code = ord(ch) - 0xAC00
    if code < 0 or code > 11171:
        return ch  # 한글 음절이 아니면 그대로
    cho = code // 588
    jung = (code % 588) // 28
    jong = code % 28
    return _CHO[cho] + _JUNG[jung] + _JONG[jong]


def _variants(surname_eng: str, given_parts: list[str]) -> list[str]:
    """성 + 이름 음절 로마자 조각으로 PubMed 저자 검색 변형을 우선순위대로 생성."""
    given_parts = [p for p in given_parts if p]
    if not given_parts:
        return [surname_eng]
    initials = "".join(p[0] for p in given_parts).upper()
    joined = " ".join(given_parts)
    hyphen = "-".join(given_parts)
    variants = []
    # 외자(단일 글자) 이니셜 변형 금지: 'Kim K'[Author] 는 동명이인(예: 서울대 Kim K* 전부)을
    # 긁어와 500 cap·h-index 왜곡을 유발한다. 2글자 이상 이니셜만 허용('Yoon JH').
    if len(initials) >= 2:
        variants.append(f"{surname_eng} {initials}")   # Yoon JH ← PubMed 'LastName Initials'
    variants += [
        f"{surname_eng} {joined}",     # Yoon Jeong Hwan
        f"{surname_eng} {hyphen}",     # Yoon Jeong-Hwan
        f"{joined} {surname_eng}",     # Jeong Hwan Yoon
    ]
    seen, out = set(), []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def convert_korean_name_to_english(name: str) -> list[str]:
    """
    한글 의사 이름 → PubMed 검색용 영문 변형 리스트.
    예: '윤정환' → ['Yoon JH', 'Yoon Jeong Hwan', 'Yoon Jeong-Hwan', 'Jeong Hwan Yoon']
    """
    name = (name or "").strip()
    if not name:
        return []

    # 1) 명의 사전 최우선 (실제 출판명)
    if name in FAMOUS_DOCTORS_MAP:
        return FAMOUS_DOCTORS_MAP[name]

    # 2) 성(1자) + 이름(나머지) 분해. 성은 관용표기 우선, 이름은 RR.
    surname, given = name[0], name[1:]
    surname_eng = KOREAN_LAST_NAMES.get(surname) or romanize_syllable(surname).capitalize()
    given_parts = [romanize_syllable(c).capitalize() for c in given]
    return _variants(surname_eng, given_parts)


def english_name_to_pubmed_variants(name_en: str) -> list[str]:
    """
    병원 사이트가 제공한 영문명을 PubMed 저자 변형으로 변환 (실제 출판명 기반 → 최정확).
    지원 형식:
      'Yoon, Jung-Hwan'  (성, 이름 — 서울대/세브란스)
      'Kang, Huapyong'   (성, 단일이름)
      'Ga Hee Kim'       (이름 ... 성)
      'DONGYUN KIM'      (대문자 혼합)
    """
    s = (name_en or "").strip()
    if not s:
        return []

    surname, given = "", ""
    if "," in s:
        a, b = s.split(",", 1)
        surname, given = a.strip(), b.strip()
    else:
        toks = s.split()
        if len(toks) < 2:
            return [s]
        known = {v for v in KOREAN_LAST_NAMES.values()}
        if toks[-1].capitalize() in known:        # 'Ga Hee Kim' → 성=Kim
            surname, given = toks[-1], " ".join(toks[:-1])
        elif toks[0].capitalize() in known:        # 'Kim Ga Hee' → 성=Kim
            surname, given = toks[0], " ".join(toks[1:])
        else:                                      # 알 수 없으면 마지막 토큰을 성으로 가정
            surname, given = toks[-1], " ".join(toks[:-1])

    surname = surname.capitalize()
    given_parts = [p.capitalize() for p in re.split(r"[\s\-]+", given) if p]
    return _variants(surname, given_parts)
