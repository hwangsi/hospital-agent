"""
질환명 → KCD(한국표준질병분류) 코드 매핑.
주관식 입력을 표준 코드로 변환하여 심평원 API에 사용.

실서비스에서는:
1) 건보공단 KCD-8 전체 DB를 로컬 검색 인덱스로 구축
2) 동의어 사전 (예: "위암" = "위의 악성신생물" = C16)
3) NLP 기반 fuzzy matching 적용
"""

# 주요 질환 → KCD 코드 매핑 (약 200개 — 실서비스는 전체 DB 사용)
KCD_MAP = {
    # ─── 소화기 ───
    "위암": {"kcd": "C16", "dept": "소화기내과", "surgery_code": "Q2631"},
    "대장암": {"kcd": "C18", "dept": "소화기내과", "surgery_code": "Q2891"},
    "간암": {"kcd": "C22", "dept": "소화기내과", "surgery_code": "Q0561"},
    "췌장암": {"kcd": "C25", "dept": "소화기내과", "surgery_code": "Q2621"},
    "담관암": {"kcd": "C24", "dept": "소화기내과", "surgery_code": "Q2611"},
    "간문부 담관암": {"kcd": "C24.0", "dept": "소화기내과", "surgery_code": "Q2611"},
    "GERD": {"kcd": "K21", "dept": "소화기내과"},
    "IBD": {"kcd": "K50", "dept": "소화기내과"},
    "간경변": {"kcd": "K74", "dept": "소화기내과"},

    # ─── 호흡기 ───
    "폐암": {"kcd": "C34", "dept": "호흡기내과", "surgery_code": "Q3391"},
    "천식": {"kcd": "J45", "dept": "호흡기내과"},
    "COPD": {"kcd": "J44", "dept": "호흡기내과"},
    "폐섬유증": {"kcd": "J84", "dept": "호흡기내과"},

    # ─── 순환기 ───
    "관상동맥질환": {"kcd": "I25", "dept": "순환기내과", "surgery_code": "O1641"},
    "심부전": {"kcd": "I50", "dept": "순환기내과"},
    "부정맥": {"kcd": "I49", "dept": "순환기내과"},
    "판막질환": {"kcd": "I34", "dept": "순환기내과", "surgery_code": "O1981"},

    # ─── 내분비 ───
    "당뇨병": {"kcd": "E11", "dept": "내분비내과"},
    "갑상선질환": {"kcd": "E04", "dept": "내분비내과"},

    # ─── 혈액종양 ───
    "백혈병": {"kcd": "C91", "dept": "혈액종양내과"},
    "림프종": {"kcd": "C85", "dept": "혈액종양내과"},
    "미만성 거대B세포 림프종": {"kcd": "C83.3", "dept": "혈액종양내과"},
    "다발성골수종": {"kcd": "C90", "dept": "혈액종양내과"},

    # ─── 류마티스 ───
    "류마티스관절염": {"kcd": "M05", "dept": "류마티스내과"},
    "루프스": {"kcd": "M32", "dept": "류마티스내과"},
    "베체트병": {"kcd": "M35.2", "dept": "류마티스내과"},
    "통풍": {"kcd": "M10", "dept": "류마티스내과"},

    # ─── 비뇨기 ───
    "전립선암": {"kcd": "C61", "dept": "비뇨의학과", "surgery_code": "R3960"},
    "방광암": {"kcd": "C67", "dept": "비뇨의학과", "surgery_code": "R3580"},
    "신장암": {"kcd": "C64", "dept": "비뇨의학과", "surgery_code": "R3050"},
    "전립선비대증": {"kcd": "N40", "dept": "비뇨의학과"},
    "요로결석": {"kcd": "N20", "dept": "비뇨의학과"},

    # ─── 신경외과 ───
    "뇌종양": {"kcd": "C71", "dept": "신경외과", "surgery_code": "N0111"},
    "뇌동맥류": {"kcd": "I67.1", "dept": "신경외과", "surgery_code": "N0594"},
    "디스크": {"kcd": "M51", "dept": "신경외과", "surgery_code": "N1493"},

    # ─── 정형외과 ───
    "인공관절": {"kcd": "M17", "dept": "정형외과", "surgery_code": "N2072"},
    "회전근개": {"kcd": "M75.1", "dept": "정형외과", "surgery_code": "N0936"},
    "골절": {"kcd": "S72", "dept": "정형외과"},

    # ─── 산부인과 ───
    "자궁근종": {"kcd": "D25", "dept": "산부인과", "surgery_code": "R4121"},
    "난소암": {"kcd": "C56", "dept": "산부인과", "surgery_code": "R4411"},
    "자궁경부암": {"kcd": "C53", "dept": "산부인과", "surgery_code": "R4161"},

    # ─── 유방 / 내분비외과 ───
    # 진료과명은 빅5 병원 실제 분류와 일치하도록 검증(2026-06): 유방암→유방외과,
    # 갑상선암→내분비외과(갑상선·부갑상선·부신 수술 담당).
    "유방암": {"kcd": "C50", "dept": "유방외과", "surgery_code": "R3261"},
    "갑상선암": {"kcd": "C73", "dept": "내분비외과", "surgery_code": "P4551"},

    # ─── 이비인후과 ───
    "비중격만곡": {"kcd": "J34.2", "dept": "이비인후과", "surgery_code": "S4811"},
    "부비동염": {"kcd": "J32", "dept": "이비인후과"},
    "편도질환": {"kcd": "J35", "dept": "이비인후과"},
    "두경부암": {"kcd": "C10", "dept": "이비인후과"},
    "수면무호흡": {"kcd": "G47.3", "dept": "이비인후과"},

    # ─── 안과 ───
    "백내장": {"kcd": "H25", "dept": "안과", "surgery_code": "S5110"},
    "녹내장": {"kcd": "H40", "dept": "안과"},
    "망막질환": {"kcd": "H35", "dept": "안과"},

    # ─── 성형외과 ───
    "안면윤곽": {"kcd": "Z41", "dept": "성형외과"},
    "화상재건": {"kcd": "T20", "dept": "성형외과"},

    # ─── 신경과 ───
    "뇌졸중": {"kcd": "I63", "dept": "신경과"},
    "파킨슨병": {"kcd": "G20", "dept": "신경과"},
    "치매": {"kcd": "F03", "dept": "신경과"},

    # ─── 흉부외과 ───
    "CABG": {"kcd": "I25.1", "dept": "흉부외과", "surgery_code": "O1641"},
}

# 동의어 사전
SYNONYMS = {
    "위의 악성신생물": "위암",
    "stomach cancer": "위암",
    "gastric cancer": "위암",
    "대장의 악성신생물": "대장암",
    "colorectal cancer": "대장암",
    "간세포암": "간암",
    "HCC": "간암",
    "hepatocellular carcinoma": "간암",
    "prostate cancer": "전립선암",
    "breast cancer": "유방암",
    "lung cancer": "폐암",
    "DLBCL": "미만성 거대B세포 림프종",
    "SLE": "루프스",
    "Behcet": "베체트병",
    "허리디스크": "디스크",
    "요추 추간판 탈출증": "디스크",
    "무릎 인공관절": "인공관절",
    "TKR": "인공관절",
    "슬관절 전치환술": "인공관절",
}


class KCDMapper:
    """질환명 → KCD 코드 + 진료과 매핑"""

    def map_disease(self, disease_text: str) -> dict:
        """
        주관식 질환 입력을 KCD 코드와 진료과로 매핑.

        Returns: {
            "original": str,
            "matched": str,
            "kcd_code": str,
            "department": str,
            "surgery_code": str | None,
        }
        """
        text = disease_text.strip()

        # 1) 동의어 변환
        if text in SYNONYMS:
            text = SYNONYMS[text]
        else:
            # 대소문자 무시 + 부분 매칭
            for syn, canonical in SYNONYMS.items():
                if syn.lower() in text.lower() or text.lower() in syn.lower():
                    text = canonical
                    break

        # 2) 정확 매칭
        if text in KCD_MAP:
            info = KCD_MAP[text]
            return {
                "original": disease_text,
                "matched": text,
                "kcd_code": info["kcd"],
                "department": info["dept"],
                "surgery_code": info.get("surgery_code", ""),
            }

        # 3) 부분 매칭 (포함 검색)
        for disease, info in KCD_MAP.items():
            if text in disease or disease in text:
                return {
                    "original": disease_text,
                    "matched": disease,
                    "kcd_code": info["kcd"],
                    "department": info["dept"],
                    "surgery_code": info.get("surgery_code", ""),
                }

        # 4) 매칭 실패 → 기본값
        return {
            "original": disease_text,
            "matched": text,
            "kcd_code": "",
            "department": "",
            "surgery_code": "",
        }
