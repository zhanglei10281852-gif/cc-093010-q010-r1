from __future__ import annotations

import json
import sqlite3
from typing import Any


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class CohortRepository:
    """封装队列冻结版本、成员清单、事件与导出制品的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def version_by_ref(self, code: str, version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM cohort_versions WHERE code=? AND version=?", (code, version)
        ).fetchone()

    def version_by_id(self, version_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM cohort_versions WHERE id=?", (version_id,)).fetchone()

    def latest_version(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM cohort_versions WHERE code=? ORDER BY version DESC LIMIT 1", (code,)
        ).fetchone()

    def proposed_version(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM cohort_versions WHERE code=? AND status='proposed'", (code,)
        ).fetchone()

    def list_versions(self, code: str | None) -> list[dict[str, Any]]:
        if code:
            rows = self.connection.execute(
                "SELECT * FROM cohort_versions WHERE code=? ORDER BY created_at DESC,version DESC", (code,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM cohort_versions ORDER BY code,version DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_version(
        self,
        *,
        code: str,
        version: int,
        name: str,
        cutoff_at: str,
        criteria: dict[str, Any],
        criteria_digest: str,
        status: str,
        parent_id: int | None,
        created_by: str,
        now: str,
        roster_digest: str,
        included_count: int,
        excluded_count: int,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO cohort_versions(code,version,name,cutoff_at,criteria_json,criteria_digest,status,parent_id,created_by,created_at,included_count,excluded_count,roster_digest)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (code, version, name, cutoff_at, json.dumps(criteria, ensure_ascii=False, sort_keys=True), criteria_digest,
             status, parent_id, created_by, now, included_count, excluded_count, roster_digest),
        )
        return int(cursor.lastrowid)

    def issue(self, version_id: int, actor: str, note: str, now: str) -> None:
        cursor = self.connection.execute(
            "UPDATE cohort_versions SET status='frozen',issued_by=?,issued_at=?,issue_note=? WHERE id=? AND status='proposed'",
            (actor, now, note, version_id),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("候选修订签发失败：版本不存在或已签发")

    def insert_members(self, version_id: int, members: list[dict[str, Any]], now: str) -> None:
        self.connection.executemany(
            "INSERT INTO cohort_members(cohort_version_id,session_id,member_ref,included,inclusion_reasons_json,"
            "exclusion_reasons_json,observation_version,context_json,evidence_snapshot_json,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    version_id,
                    member["session_id"],
                    member["member_ref"],
                    1 if member["included"] else 0,
                    json.dumps(member["inclusion_reasons"], ensure_ascii=False, sort_keys=True),
                    json.dumps(member["exclusion_reasons"], ensure_ascii=False, sort_keys=True),
                    member["observation_version"],
                    json.dumps(member["context"], ensure_ascii=False, sort_keys=True),
                    json.dumps(member["evidence_snapshot"], ensure_ascii=False, sort_keys=True),
                    now,
                )
                for member in members
            ],
        )

    def members(self, version_id: int, *, included: bool | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT m.*,s.project_code,s.requested_by,s.protocol_id,p.code AS protocol_code,p.capability,"
            "s.parameters_json,s.created_at AS session_created_at,s.status AS session_status "
            "FROM cohort_members m JOIN pilot_sessions s ON s.id=m.session_id "
            "JOIN pilot_protocols p ON p.id=s.protocol_id WHERE m.cohort_version_id=?"
        )
        if included is not None:
            sql += " AND m.included=?"
            rows = self.connection.execute(sql + " ORDER BY m.member_ref,m.session_id", (version_id, 1 if included else 0)).fetchall()
        else:
            rows = self.connection.execute(sql + " ORDER BY m.member_ref,m.session_id", (version_id,)).fetchall()
        return [dict(row) for row in rows]

    def add_event(self, version_id: int, *, action: str, actor: str, note: str, payload: dict[str, Any], now: str) -> None:
        self.connection.execute(
            "INSERT INTO cohort_events(cohort_version_id,action,actor,note,payload_json,created_at) VALUES(?,?,?,?,?,?)",
            (version_id, action, actor, note, json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
        )

    def events(self, version_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM cohort_events WHERE cohort_version_id=? ORDER BY id", (version_id,)
        ).fetchall()]

    def artifact_by_key(self, version_id: int, idempotency_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM cohort_artifacts WHERE cohort_version_id=? AND idempotency_key=?",
            (version_id, idempotency_key),
        ).fetchone()

    def insert_artifact(
        self,
        version_id: int,
        *,
        idempotency_key: str,
        request_digest: str,
        content: dict[str, Any],
        content_digest: str,
        member_count: int,
        created_by: str,
        now: str,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO cohort_artifacts(cohort_version_id,idempotency_key,request_digest,artifact_type,format,content_json,content_digest,member_count,created_by,created_at)"
            " VALUES(?,?,?,'export','json',?,?,?,?,?)",
            (version_id, idempotency_key, request_digest,
             json.dumps(content, ensure_ascii=False, sort_keys=True), content_digest,
             member_count, created_by, now),
        )
        return int(cursor.lastrowid)
