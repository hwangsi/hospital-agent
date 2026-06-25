"""
한글 의사 성명 -> PubMed/영문 매핑용 이름 변환 유틸리티
"""
import re

# 주요 명의 영문 매핑 사전 (빅5 병원의 실제 명의 매핑 확률 극대화)
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

# 한글 성의 로마자 표기 매핑
KOREAN_LAST_NAMES = {
    "김": "Kim", "이": "Lee", "박": "Park", "최": "Choi", "정": "Jung",
    "강": "Kang", "조": "Cho", "윤": "Yoon", "장": "Jang", "임": "Lim",
    "한": "Han", "오": "Oh", "서": "Seo", "신": "Shin", "권": "Kwon",
    "황": "Hwang", "안": "Ahn", "송": "Song", "류": "Ryu", "유": "Yoo",
    "홍": "Hong", "전": "Jeon", "고": "Ko", "문": "Moon", "양": "Yang",
    "손": "Sohn", "배": "Bae", "백": "Baek", "허": "Hur", "노": "Noh",
}

# 한글 초성/중성/종성 로마자 변환을 위한 간이 맵 (이름 변환용)
KOREAN_SYLLABLE_MAP = {
    "민": "Min", "준": "Jun", "지": "Ji", "현": "Hyun", "우": "Woo",
    "아": "Ah", "영": "Young", "수": "Su", "훈": "Hoon", "재": "Jae",
    "성": "Sung", "호": "Ho", "석": "Seok", "태": "Tae", "동": "Dong",
    "철": "Chul", "광": "Kwang", "규": "Kyu", "원": "Won", "종": "Jong",
    "진": "Jin", "선": "Sun", "희": "Hee", "경": "Kyung", "미": "Mi",
    "은": "Eun", "정": "Jung", "혜": "Hye", "상": "Sang", "창": "Chang",
    "병": "Byung", "석": "Seok", "용": "Yong", "기": "Ki", "승": "Seung",
    "관": "Kwan", "율": "Yul", "철": "Cheol", "주": "Joo", "건": "Geon",
}

def convert_korean_name_to_english(name: str) -> list[str]:
    """
    한글 의사 이름을 PubMed 검색에 특화된 여러 영문 표기 조합으로 변환하여 리턴한다.
    예: '안규리' -> ['Ahn KR', 'Ahn Kyu Ri', 'Kyu Ri Ahn', 'Kyu-Ri Ahn']
    """
    name = name.strip()
    if not name:
        return []

    # 1. 명의 사전에 존재할 시 즉각 반환
    if name in FAMOUS_DOCTORS_MAP:
        return FAMOUS_DOCTORS_MAP[name]

    # 2. 일반 한국인 이름 변환 (3글자 이름 기준)
    if len(name) == 3:
        last = name[0]
        first1 = name[1]
        first2 = name[2]

        last_eng = KOREAN_LAST_NAMES.get(last, last)
        f1_eng = KOREAN_SYLLABLE_MAP.get(first1, "Gildong"[0:3]) # fallback
        f2_eng = KOREAN_SYLLABLE_MAP.get(first2, "Gildong"[3:6]) # fallback

        # 이니셜 추출
        f1_init = f1_eng[0] if f1_eng else ""
        f2_init = f2_eng[0] if f2_eng else ""

        combinations = [
            f"{last_eng} {f1_init}{f2_init}",       # Lee WY, Ahn KR
            f"{last_eng} {f1_eng} {f2_eng}",       # Lee Woo Yong
            f"{f1_eng} {f2_eng} {last_eng}",       # Woo Yong Lee
            f"{f1_eng}-{f2_eng} {last_eng}",       # Woo-Yong Lee
        ]
        return combinations

    # 3. 2글자 이름 또는 기타 예외
    elif len(name) == 2:
        last = name[0]
        first = name[1]
        last_eng = KOREAN_LAST_NAMES.get(last, last)
        f_eng = KOREAN_SYLLABLE_MAP.get(first, "Jin")
        f_init = f_eng[0] if f_eng else ""

        return [
            f"{last_eng} {f_init}",
            f"{last_eng} {f_eng}",
            f"{f_eng} {last_eng}",
        ]

    # 4. 기타
    return [name]
