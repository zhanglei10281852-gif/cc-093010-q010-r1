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
            product = client.post("/api/catalog/products", json={
                "code": "bci-a",
                "name": "脑机接口康复仪",
                "organization": "示例神经科技",
                "origin_country": "中国",
                "category": "康复设备",
                "intended_use": "用于医院与展会体验点的脑机接口康复观察",
                "risk_level": "high",
                "regulatory_status": "研究",
            })
            site = client.post("/api/catalog/sites", json={
                "code": "hospital-a", "name": "示范医院", "site_type": "医院", "region": "杭州",
                "capabilities": ["bci"], "max_concurrent": 2,
            })
            evidence = client.post("/api/catalog/evidence", json={
                "product_code": "bci-a", "evidence_type": "临床", "title": "小样本临床观察",
                "source_name": "示范医院", "source_region": "杭州", "version": "v1",
                "content_digest": "sha256:" + "a" * 58, "summary": {"n": 12}, "submitted_by": "reviewer-demo",
            })
            protocol = client.post("/api/pilots/protocols?actor=demo", json={
                "code": "bci-rehab", "name": "脑机接口康复方案", "capability": "bci",
                "product_code": "bci-a",
                "parameter_schema": {"minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 30}},
                "default_parameters": {}, "max_runtime_seconds": 600, "max_attempts": 2,
            })
            submitted = client.post("/api/pilots/sessions", json={
                "protocol_code": "bci-rehab", "project_code": "bci-2026", "requested_by": "operator-demo",
                "parameters": {"minutes": 10}, "priority": 70, "idempotency_key": "cohort-demo-session-1",
            })
            client.post("/api/pilots/sessions/claim", json={"site_code": "hospital-a", "capabilities": ["bci"], "lease_seconds": 60})
            completed = client.post(
                f"/api/pilots/sessions/{submitted.json()['id']}/complete",
                json={"site_code": "hospital-a", "observation": {"score": 0.8}, "metrics": {}},
            )
            # 第一天冻结：场次已完成，但证据仍处于待审阅状态，清单记录排除理由
            first = client.post("/api/cohorts", json={
                "code": "bci-2026-q3", "name": "脑机接口观察队列",
                "cutoff_at": "2026-11-01T00:00:00Z",
                "criteria": {
                    "product_categories": ["康复设备"],
                    "protocol_codes": ["bci-rehab"],
                    "site_types": ["医院"],
                    "quality": {"evidence_required": ["临床"], "min_evidence_accepted": 1},
                },
                "created_by": "stats-demo",
            })
            # 证据接受后提出候选修订：新数据只能作为候选增加
            client.post(f"/api/catalog/evidence/{evidence.json()['id']}/review",
                        json={"reviewer": "doctor-demo", "decision": "accepted", "note": "临床数据可接受"})
            revision = client.post("/api/cohorts/bci-2026-q3/revisions", json={
                "cutoff_at": "2026-12-15T00:00:00Z", "actor": "stats-demo", "reason": "证据审阅完成",
            })
            issued = client.post("/api/cohorts/bci-2026-q3/versions/2/issue",
                                 json={"actor": "review-lead", "reason": "阶段评审通过"})
            export = client.post("/api/cohorts/bci-2026-q3/versions/2/export",
                                 json={"idempotency_key": "cohort-demo-export-1"})
            values = [product, site, evidence, protocol, submitted, completed, first, revision, issued, export]
            if any(response.status_code >= 400 for response in values):
                _print({"errors": [response.text for response in values if response.status_code >= 400]})
                return 1
            _print({
                "cohort": "bci-2026-q3",
                "v1_excluded": first.json()["excluded_total"],
                "v2_added": len(revision.json()["diff"]["added"]),
                "v2_manifest": issued.json()["manifest_digest"],
                "export_digest": export.json()["content_digest"],
                "members": len(export.json()["members"]),
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
    sub.add_parser("cohort-demo", help="运行队列冻结、修订、签发与导出演示")
    return parser


def main(argv: list[str] | None = None) -> int:
    command = build_parser().parse_args(argv).command
    actions = {"init-db": init_database, "check-db": check_database, "smoke": smoke, "pilot-demo": pilot_demo, "cohort-demo": cohort_demo}
    try:
        return actions[command]()
    finally:
        close_connection()


if __name__ == "__main__":
    sys.exit(main())

