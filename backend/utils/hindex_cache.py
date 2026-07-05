"""
h-index 결과 영속 캐시 (SQLite) — docs/H-INDEX-OPENALEX-DESIGN.md §3.4 구현.

클라이언트 인메모리 캐시(L1)는 서버 재시작 시 소실되어 첫 검색마다 외부 API를
전부 재호출했다(레이트리밋 소모 + 첫 검색 느림). 이 모듈이 디스크 L2 캐시를 제공한다.

정책 (h-index 는 천천히 변함):
  - openalex / semantic-scholar 결과: TTL 7일
  - pubmed-fallback (h>0): TTL 1일 — 정밀경로(OpenAlex) 복구 시 빨리 재조회되도록 짧게
  - h_index<=0 또는 미지정 source: 저장 안 함 (다음 검색 재시도 — 기존 "0 캐싱 금지" 정책)

동시성: 단일 프로세스·단일 이벤트루프 전제. sqlite3 연결은 스레드락으로 보호하고
트랜잭션은 마이크로초 단위라 이벤트루프 블로킹은 무시 가능한 수준.
"""
import json
import os
import sqlite3
import threading
import time

_TTL_BY_SOURCE = {
    "openalex": 7 * 86400,
    "semantic-scholar": 7 * 86400,
    "pubmed-fallback": 1 * 86400,
}

# 캐시에 저장할 필드만 (hira 등 무관 필드 유입 방지)
_FIELDS = ("h_index", "papers", "citations", "source", "oa_author_id")


class HIndexCache:
    """(name_en|이름):병원 키 → h-index 결과. TTL 만료는 조회 시 무시(지연 삭제)."""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS hindex ("
            "  key TEXT PRIMARY KEY,"
            "  value TEXT NOT NULL,"
            "  expires_at REAL NOT NULL)"
        )
        # 기동 시 만료분 일괄 정리 (지연 삭제 보완)
        self._conn.execute("DELETE FROM hindex WHERE expires_at < ?", (time.time(),))
        self._conn.commit()

    def get(self, key: str):
        """유효한 캐시 결과 dict 또는 None."""
        try:
            with self._lock:
                row = self._conn.execute(
                    "SELECT value, expires_at FROM hindex WHERE key = ?", (key,)
                ).fetchone()
            if not row or row[1] < time.time():
                return None
            return json.loads(row[0])
        except Exception as e:
            print(f"[HIndexCache] get error: {e}")
            return None

    def set(self, key: str, result: dict) -> None:
        """정책상 저장 대상일 때만 기록. 실패해도 서비스 흐름에 영향 없음."""
        try:
            ttl = _TTL_BY_SOURCE.get((result or {}).get("source") or "")
            if not ttl or int(result.get("h_index") or 0) <= 0:
                return
            slim = {k: result[k] for k in _FIELDS if k in result}
            with self._lock:
                self._conn.execute(
                    "INSERT OR REPLACE INTO hindex (key, value, expires_at) VALUES (?, ?, ?)",
                    (key, json.dumps(slim, ensure_ascii=False), time.time() + ttl),
                )
                self._conn.commit()
        except Exception as e:
            print(f"[HIndexCache] set error: {e}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
