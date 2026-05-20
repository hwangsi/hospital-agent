"""
주민등록번호 암호화 유틸리티.
AES-256 암호화 후 예약 완료 시 즉시 파기.
"""
import base64
import os
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from config.settings import ENCRYPTION_KEY


def _get_fernet() -> Fernet:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"big5-hospital-agent-salt",  # 프로덕션에서는 랜덤 salt 사용
        iterations=100000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(ENCRYPTION_KEY.encode()))
    return Fernet(key)


def encrypt_ssn(ssn: str) -> str:
    """주민등록번호 암호화"""
    if not ssn:
        return ""
    f = _get_fernet()
    return f.encrypt(ssn.encode()).decode()


def decrypt_ssn(encrypted: str) -> str:
    """주민등록번호 복호화 (예약 처리 시에만 사용)"""
    if not encrypted:
        return ""
    f = _get_fernet()
    return f.decrypt(encrypted.encode()).decode()
