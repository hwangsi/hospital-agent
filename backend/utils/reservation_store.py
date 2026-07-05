"""
예약 레코드 영속 저장소 (SQLite) — /api/reserve 가 반환만 하고 저장하지 않던 것을 영속화.

- 파일: var/reservations.sqlite3 (PII 포함 → var/ 는 gitignore, 절대 커밋 금지)
- 주민번호는 암호화본조차 저장하지 않는 기존 정책 유지 — has_ssn 불리언만 기록.
- 동시성: 단일 프로세스 전제, threading.Lock + 마이크로초 트랜잭션 (hindex_cache 와 동형).
"""
import json
import os
import sqlite3
import threading

# 레코드에서 그대로 JSON 컬럼에 들어가는 부가 필드 (스키마 컬럼 외)
_JSON_FIELDS = ("slot", "address", "notes", "reservation_url")


class ReservationStore:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS reservations ("
            "  id TEXT PRIMARY KEY,"
            "  hospital_id TEXT NOT NULL,"
            "  doctor_id TEXT,"
            "  patient_name TEXT,"
            "  phone TEXT,"
            "  has_ssn INTEGER NOT NULL DEFAULT 0,"
            "  status TEXT NOT NULL DEFAULT 'pending',"
            "  created_at TEXT NOT NULL,"
            "  extra TEXT NOT NULL DEFAULT '{}')"
        )
        self._conn.commit()

    def add(self, record: dict) -> None:
        extra = {k: record.get(k) for k in _JSON_FIELDS}
        with self._lock:
            self._conn.execute(
                "INSERT INTO reservations "
                "(id, hospital_id, doctor_id, patient_name, phone, has_ssn, status, created_at, extra) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record["id"], record["hospital_id"], record.get("doctor_id", ""),
                 record.get("patient_name", ""), record.get("phone", ""),
                 1 if record.get("has_ssn") else 0,
                 record.get("status", "pending"), record["created_at"],
                 json.dumps(extra, ensure_ascii=False)),
            )
            self._conn.commit()

    def get(self, rid: str):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reservations WHERE id = ?", (rid,)).fetchone()
        return self._to_dict(row) if row else None

    def list(self, limit: int = 50):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM reservations ORDER BY created_at DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [self._to_dict(r) for r in rows]

    def update_status(self, rid: str, status: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reservations SET status = ? WHERE id = ?", (status, rid))
            self._conn.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _to_dict(row) -> dict:
        d = {
            "id": row[0], "hospital_id": row[1], "doctor_id": row[2],
            "patient_name": row[3], "phone": row[4], "has_ssn": bool(row[5]),
            "status": row[6], "created_at": row[7],
        }
        try:
            d.update(json.loads(row[8] or "{}"))
        except Exception:
            pass
        return d
