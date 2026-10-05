from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from fastapi.testclient import TestClient

from app.database import close_connection, database_path, get_connection, init_db
from app.main import app


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def init_database() -> int:
    init_db()
    _print({"database": str(database_path()), "initialized": True})
    return 0


def check_database() -> int:
    init_db()
    connection = get_connection()
    _print({
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
    })
    return 0


def smoke() -> int:
    with tempfile.TemporaryDirectory(prefix="health-smoke-") as directory:
        os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = os.path.join(directory, "smoke.db")
        close_connection()
        with TestClient(app) as client:
            root = client.get("/")
            health = client.get("/api/system/health")
            if root.status_code != 200 or health.status_code != 200:
                _print({"root": root.text, "health": health.text})
                return 1
            _print({"root": root.json(), "health": health.json()})
        close_connection()
    return 0


def pilot_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="health-demo-") as directory:
        os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = os.path.join(directory, "demo.db")
        close_connection()
        with TestClient(app) as client:
            product = client.post("/api/catalog/products", json={
                "code": "exoskeleton-a",
                "name": "轻量助力外骨骼",
                "organization": "示例康复科技",
                "origin_country": "中国",
                "category": "康复设备",
                "intended_use": "用于展会和康复机构的步态助力体验与运行数据观察",
                "risk_level": "medium",
                "regulatory_status": "展示",
            })
            site = client.post("/api/catalog/sites", json={
                "code": "expo-hall-a",
                "name": "数智医疗体验点",
                "site_type": "展会体验点",
                "region": "杭州",
                "capabilities": ["gait-assist"],
                "max_concurrent": 2,
            })
            protocol = client.post("/api/pilots/protocols?actor=demo", json={
                "code": "gait-assist",
                "name": "外骨骼步态体验方案",
                "capability": "gait-assist",
                "parameter_schema": {"minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 30}},
                "default_parameters": {},
                "max_runtime_seconds": 1800,
                "max_attempts": 2,
            })
            submitted = client.post("/api/pilots/sessions", json={
                "protocol_code": "gait-assist",
                "project_code": "expo-2026",
                "requested_by": "operator-demo",
                "parameters": {"minutes": 8},
                "priority": 70,
                "idempotency_key": "demo-session-001",
            })
            claimed = client.post("/api/pilots/sessions/claim", json={"site_code": "expo-hall-a", "capabilities": ["gait-assist"], "lease_seconds": 60})
            values = [product, site, protocol, submitted, claimed]
            if any(response.status_code >= 400 for response in values):
                _print({"errors": [response.text for response in values]})
                return 1
            _print({"product": product.json()["code"], "site": site.json()["code"], "session": claimed.json()["session"]})
        close_connection()
    return 0


def cohort_demo() -> int:
    with tempfile.TemporaryDirectory(prefix="health-cohort-") as directory:
        os.environ["HEALTH_INNOVATION_DATABASE_PATH"] = os.path.join(directory, "cohort.db")
        close_connection()
        with TestClient(app) as client:
            client.post("/api/catalog/products", json={
                "code": "bci-calm",
                "name": "脑机放松训练",
                "organization": "示例神经科技",
                "origin_country": "中国",
                "category": "数字疗法",
                "intended_use": "用于焦虑干预的脑机反馈训练与阶段评审观察",
                "risk_level": "medium",
                "regulatory_status": "研究",
            })
            client.post("/api/catalog/sites", json={
                "code": "hospital-a",
                "name": "联合医院观察点",
                "site_type": "医院",
                "region": "上海",
                "capabilities": ["neuro"],
                "max_concurrent": 2,
            })
            client.post("/api/pilots/protocols?actor=demo", json={
                "code": "neuro",
                "name": "脑机观察方案",
                "capability": "neuro",
                "parameter_schema": {
                    "minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 60},
                    "product_code": {"type": "string", "required": True},
                },
                "default_parameters": {},
                "max_runtime_seconds": 600,
                "max_attempts": 2,
            })
            evidence = client.post("/api/catalog/evidence", json={
                "product_code": "bci-calm",
                "evidence_type": "性能",
                "title": "脑机训练性能摘要",
                "source_name": "联合验证组",
                "source_region": "中国",
                "version": "2026.09",
                "content_digest": "c" * 64,
                "summary": {"samples": 120},
                "submitted_by": "evidence-owner",
            }).json()
            submitted = client.post("/api/pilots/sessions", json={
                "protocol_code": "neuro",
                "project_code": "bci-2026",
                "requested_by": "operator-demo",
                "parameters": {"minutes": 12, "product_code": "bci-calm"},
                "priority": 70,
                "idempotency_key": "cohort-demo-session-001",
            }).json()
            client.post("/api/pilots/sessions/claim", json={"site_code": "hospital-a", "capabilities": ["neuro"], "lease_seconds": 300})
            client.post(
                f"/api/pilots/sessions/{submitted['id']}/complete",
                json={"site_code": "hospital-a", "observation": {"value": 1}, "metrics": {"score": 8}},
            )
            client.post(
                f"/api/catalog/evidence/{evidence['id']}/review",
                json={"reviewer": "reviewer-a", "decision": "accepted", "note": "来源和版本可追溯"},
            )
            frozen = client.post("/api/cohorts", json={
                "code": "bci-review",
                "name": "脑机阶段评审队列",
                "cutoff_at": "2026-10-06T23:59:59Z",
                "created_by": "stat-lead",
                "categories": ["数字疗法"],
                "site_types": ["医院"],
                "require_evidence_types": ["性能"],
                "require_evidence_min_accepted": 1,
            })
            export = client.post(
                "/api/cohorts/bci-review/versions/1/exports",
                json={"requested_by": "reviewer-a", "include_excluded": True, "idempotency_key": "cohort-demo-export-001"},
            )
            values = [frozen, export]
            if any(response.status_code >= 400 for response in values):
                _print({"errors": [response.text for response in values]})
                return 1
            body = export.json()
            _print({
                "cohort": f"{body['cohort_code']}@v{body['cohort_version']}",
                "included": body["summary"]["included_count"],
                "roster_digest": body["roster_digest"],
                "content_digest": body["content_digest"],
                "member_ref": body["included_members"][0]["member_ref"],
                "session_id": body["included_members"][0]["session_id"],
            })
        close_connection()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="全球健康创新试点运营服务命令行")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db", help="初始化 SQLite 数据库")
    sub.add_parser("check-db", help="检查数据库完整性")
    sub.add_parser("smoke", help="进程内检查根路径和健康接口")
    sub.add_parser("pilot-demo", help="运行产品、场地、方案和场次演示")
    sub.add_parser("cohort-demo", help="运行队列冻结、成员快照与去标识导出演示")
    return parser


def main(argv: list[str] | None = None) -> int:
    command = build_parser().parse_args(argv).command
    actions = {
        "init-db": init_database,
        "check-db": check_database,
        "smoke": smoke,
        "pilot-demo": pilot_demo,
        "cohort-demo": cohort_demo,
    }
    try:
        return actions[command]()
    finally:
        close_connection()


if __name__ == "__main__":
    sys.exit(main())

