from __future__ import annotations

import json
import sqlite3
from typing import Any


class CohortRepository:
    """封装队列冻结、版本、成员清单与截止时点快照的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # ---- 队列定义 -------------------------------------------------------

    def cohort_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM cohort_freezes WHERE code=?", (code,)).fetchone()

    def cohort_by_id(self, cohort_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM cohort_freezes WHERE id=?", (cohort_id,)).fetchone()

    def list_cohorts(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT c.*,("
            "SELECT COUNT(*) FROM cohort_versions v WHERE v.cohort_id=c.id) AS version_total,"
            "MAX(v.version_no) AS latest_version_no,"
            "MAX(CASE WHEN v.status='issued' THEN v.version_no ELSE 0 END) AS issued_version_no "
            "FROM cohort_freezes c LEFT JOIN cohort_versions v ON v.cohort_id=c.id "
            "GROUP BY c.id ORDER BY c.created_at DESC,c.id DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def create_cohort(self, *, code: str, name: str, criteria: dict[str, Any], criteria_digest: str, created_by: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO cohort_freezes(code,name,criteria_json,rule_digest,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (code, name, json.dumps(criteria, ensure_ascii=False, sort_keys=True), criteria_digest, created_by, now),
        )
        return dict(self.cohort_by_id(cursor.lastrowid))

    # ---- 版本 -----------------------------------------------------------

    def version_by_id(self, version_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM cohort_versions WHERE id=?", (version_id,)).fetchone()

    def versions_for_cohort(self, cohort_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM cohort_versions WHERE cohort_id=? ORDER BY version_no", (cohort_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def latest_version(self, cohort_id: int, *, status: str | None = None) -> sqlite3.Row | None:
        if status is None:
            return self.connection.execute(
                "SELECT * FROM cohort_versions WHERE cohort_id=? ORDER BY version_no DESC LIMIT 1", (cohort_id,)
            ).fetchone()
        return self.connection.execute(
            "SELECT * FROM cohort_versions WHERE cohort_id=? AND status=? ORDER BY version_no DESC LIMIT 1",
            (cohort_id, status),
        ).fetchone()

    def create_version(self, *, cohort_id: int, version_no: int, cutoff_at: str, created_by: str, now: str,
                       warnings: list[str], manifest_digest: str, member_total: int,
                       included_total: int, excluded_total: int) -> int:
        cursor = self.connection.execute(
            "INSERT INTO cohort_versions(cohort_id,version_no,status,cutoff_at,manifest_digest,"
            "member_total,included_total,excluded_total,warnings_json,created_by,created_at) "
            "VALUES(?,?, 'candidate',?,?,?,?,?,?,?,?)",
            (cohort_id, version_no, cutoff_at, manifest_digest, member_total, included_total,
             excluded_total, json.dumps(warnings, ensure_ascii=False), created_by, now),
        )
        return int(cursor.lastrowid)

    def save_version_diff(self, version_id: int, diff: dict[str, Any]) -> None:
        self.connection.execute(
            "UPDATE cohort_versions SET diff_json=? WHERE id=? AND status='candidate'",
            (json.dumps(diff, ensure_ascii=False, sort_keys=True), version_id),
        )

    def issue_version(self, version_id: int, issued_by: str, issued_at: str) -> None:
        cursor = self.connection.execute(
            "UPDATE cohort_versions SET status='issued',issued_by=?,issued_at=? "
            "WHERE id=? AND status='candidate'",
            (issued_by, issued_at, version_id),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("候选版本不存在或已经签发")

    # ---- 成员 -----------------------------------------------------------

    def add_member(self, *, version_id: int, ordinal: int, session_id: int, included: bool, reason: str, payload: dict[str, Any], member_digest: str, now: str) -> None:
        self.connection.execute(
            "INSERT INTO cohort_members(version_id,ordinal,session_id,included,decision_reason,payload_json,member_digest,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (version_id, ordinal, session_id, 1 if included else 0, reason, json.dumps(payload, ensure_ascii=False, sort_keys=True), member_digest, now),
        )

    def members_of_version(self, version_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM cohort_members WHERE version_id=? ORDER BY ordinal", (version_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    # ---- 截止时点快照 ----------------------------------------------------

    def snapshot_at(self, entity_table: str, entity_id: int, cutoff_at: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT state_json FROM cohort_entity_snapshots WHERE entity_table=? AND entity_id=? AND changed_at<=? "
            "ORDER BY changed_at DESC,id DESC LIMIT 1",
            (entity_table, entity_id, cutoff_at),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["state_json"])

    def existing_entity_ids_at(self, entity_table: str, cutoff_at: str) -> list[int]:
        rows = self.connection.execute(
            "SELECT DISTINCT entity_id FROM cohort_entity_snapshots WHERE entity_table=? AND changed_at<=? ORDER BY entity_id",
            (entity_table, cutoff_at),
        ).fetchall()
        return [int(row["entity_id"]) for row in rows]

    def observations_for_session(self, session_id: int, cutoff_at: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM pilot_observations WHERE session_id=? AND created_at<=? ORDER BY version",
            (session_id, cutoff_at),
        ).fetchall()
        return [dict(row) for row in rows]
