from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from app.cohorts.service import REASON_EVIDENCE_MISSING, REASON_PRODUCT_INACTIVE, CohortFreezeService
from app.core.clock import FrozenClock
from app.database import close_connection, get_connection, init_db
from app.catalog.service import CatalogService
from app.pilots.service import PilotOperationsService


PRODUCT = {
    "code": "bci-a", "name": "脑机接口康复仪", "organization": "神经科技公司",
    "origin_country": "中国", "category": "康复设备",
    "intended_use": "用于展会体验点与医院的脑机接口康复观察", "risk_level": "high",
    "regulatory_status": "研究",
}
SITE = {
    "code": "hospital-1", "name": "市一医院", "site_type": "医院", "region": "杭州",
    "capabilities": ["bci"], "max_concurrent": 2,
}
CRITERIA = {
    "product_categories": ["康复设备"],
    "protocol_codes": ["bci-rehab"],
    "site_types": ["医院"],
    "participation": {"session_statuses": ["succeeded"], "min_observation_versions": 1},
    "quality": {"evidence_required": ["临床"], "min_evidence_accepted": 1},
}


def _setup_world(clock: FrozenClock):
    conn = get_connection()
    catalog = CatalogService(conn, clock)
    pilots = PilotOperationsService(conn, clock)
    cohorts = CohortFreezeService(conn, clock)
    catalog.create_product(PRODUCT)
    catalog.create_site(SITE)
    evidence = catalog.submit_evidence({
        "product_code": "bci-a", "evidence_type": "临床", "title": "小样本临床观察",
        "source_name": "市一医院", "source_region": "杭州", "version": "v1",
        "content_digest": "sha256:" + "a" * 58, "summary": {"n": 12}, "submitted_by": "reviewer-1",
    })
    pilots.create_protocol({
        "code": "bci-rehab", "name": "脑机接口康复方案", "capability": "bci", "product_code": "bci-a",
        "parameter_schema": {"minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 30}},
        "default_parameters": {}, "max_runtime_seconds": 300, "max_attempts": 2,
    }, "admin")
    session = pilots.submit({
        "protocol_code": "bci-rehab", "project_code": "bci-obs-2026", "requested_by": "stat-owner",
        "parameters": {"minutes": 10}, "priority": 50, "idempotency_key": "session-001",
    })
    pilots.claim("hospital-1", ["bci"], 600)
    pilots.complete(session["id"], "hospital-1", {"score": 0.8}, {"seconds": 12})
    return catalog, pilots, cohorts, session, evidence


def test_freeze_records_exclusion_reason_and_pins_cutoff_state(client):
    init_db()
    clock = FrozenClock(datetime(2026, 9, 25, 8, 0, tzinfo=UTC))
    catalog, pilots, cohorts, session, evidence = _setup_world(clock)
    clock.advance(days=6)

    freeze = cohorts.create_freeze(
        {"code": "bci-2026-q3", "name": "脑机接口观察队列", "cutoff_at": clock.now(), "created_by": "stat-owner"},
        CRITERIA,
    )
    assert freeze["status"] == "issued"
    member = freeze["members"][0]
    assert member["session_id"] == session["id"]
    assert member["included"] is False
    assert member["payload"]["decision"]["reason_code"] == REASON_EVIDENCE_MISSING
    # 冻结引用截止时点的证据状态（submitted）与观察版本（1）
    assert member["payload"]["observation_total"] == 1
    assert {item["status"] for item in member["payload"]["evidence"]} == {"submitted"}

    # 截止时点之后证据才被接受：旧版本结论不变
    clock.advance(days=1)
    catalog.review_evidence(evidence["id"], "doc-1", "accepted", "临床数据可接受")
    verified = cohorts.verify_version("bci-2026-q3", 1)
    assert verified["matches"] is True
    assert verified["excluded_total"] == 1


def test_revision_diff_shows_add_remove_and_changed_reasons(client):
    init_db()
    clock = FrozenClock(datetime(2026, 9, 25, 8, 0, tzinfo=UTC))
    catalog, pilots, cohorts, session, evidence = _setup_world(clock)
    clock.advance(days=6)
    cohorts.create_freeze(
        {"code": "bci-2026-q3", "name": "脑机接口观察队列", "cutoff_at": clock.now(), "created_by": "stat-owner"},
        CRITERIA,
    )
    clock.advance(days=1)
    catalog.review_evidence(evidence["id"], "doc-1", "accepted", "临床数据可接受")
    # 迟到回执：观察版本增加到 2
    get_connection().execute(
        "INSERT INTO pilot_observations(session_id,version,observation_json,metrics_json,observation_digest,created_by,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (session["id"], 2, '{"score": 0.9}', '{"seconds": 14}', "digest-late-v2", "hospital-1",
         "2026-10-02T09:00:00+00:00"),
    )
    clock.advance(days=1)
    candidate = cohorts.revise("bci-2026-q3", {
        "cutoff_at": clock.now(), "actor": "stat-owner", "reason": "证据接受与迟到回执",
    })
    assert candidate["status"] == "candidate"
    assert [item["session_id"] for item in candidate["diff"]["added"]] == [session["id"]]
    assert candidate["members"][0]["payload"]["observation_total"] == 2

    # 候选阶段可先核验摘要，错误摘要不得签发
    with pytest.raises(Exception):
        cohorts.issue("bci-2026-q3", 2, {
            "actor": "lead", "reason": "签发", "expected_manifest_digest": "0" * 64,
        })
    issued = cohorts.issue("bci-2026-q3", 2, {
        "actor": "lead", "reason": "签发", "expected_manifest_digest": None,
    })
    assert issued["status"] == "issued"
    assert issued["issued_by"] == "lead"

    # 产品目录更新（停用）后再修订：成员被移除并给出理由
    clock.advance(days=2)
    catalog.update_product("bci-a", {"active": False})
    removed = cohorts.revise("bci-2026-q3", {
        "cutoff_at": clock.now(), "actor": "stat-owner", "reason": "产品目录更新",
    })
    assert removed["diff"]["removed"][0]["reason_code"] == REASON_PRODUCT_INACTIVE
    cohorts.discard_candidate("bci-2026-q3", 3, {"actor": "stat-owner", "reason": "暂不修订"})
    assert [v["version_no"] for v in cohorts.get_cohort("bci-2026-q3")["versions"]] == [1, 2]


def test_issued_versions_are_immutable_in_storage(client):
    init_db()
    clock = FrozenClock(datetime(2026, 9, 25, 8, 0, tzinfo=UTC))
    _catalog, _pilots, cohorts, _session, _evidence = _setup_world(clock)
    freeze = cohorts.create_freeze(
        {"code": "immutable-q", "name": "不可变队列", "cutoff_at": clock.now(), "created_by": "stat-owner"},
        CRITERIA,
    )
    conn = get_connection()
    version_id = freeze["id"]
    for statement, params in [
        ("UPDATE cohort_versions SET cutoff_at='2026-10-09T00:00:00+00:00' WHERE id=?", (version_id,)),
        ("DELETE FROM cohort_versions WHERE id=?", (version_id,)),
        ("UPDATE cohort_members SET included=1 WHERE version_id=?", (version_id,)),
        ("DELETE FROM cohort_members WHERE version_id=?", (version_id,)),
    ]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement, params)


def test_export_is_deidentified_with_stable_order_and_replay_artifact(client):
    init_db()
    clock = FrozenClock(datetime(2026, 9, 25, 8, 0, tzinfo=UTC))
    _catalog, _pilots, cohorts, _session, _evidence = _setup_world(clock)
    freeze = cohorts.create_freeze(
        {"code": "export-q", "name": "导出队列", "cutoff_at": clock.now(), "created_by": "stat-owner"},
        CRITERIA,
    )
    first = cohorts.export_version("export-q", 1, "export-key-001")
    replay = cohorts.export_version("export-q", 1, "export-key-001")
    assert first["replayed"] is False
    assert replay["replayed"] is True
    assert first["content_digest"] == replay["content_digest"]
    assert first["manifest_digest"] == freeze["manifest_digest"]

    # 稳定顺序
    ordinals = [row["ordinal"] for row in first["members"]]
    assert ordinals == sorted(ordinals)
    # 去标识：无提交方、联系人，成员只出现稳定假名
    serialized = json.dumps(first, ensure_ascii=False)
    assert "stat-owner" not in serialized
    assert "contact" not in serialized
    assert all(row["pseudonym"].startswith("P-") for row in first["members"])

    # 未签发版本不能导出
    clock.advance(days=3)
    candidate = cohorts.revise("export-q", {
        "cutoff_at": clock.now(), "actor": "stat-owner", "reason": "新数据",
    })
    from app.core.errors import ConflictError
    with pytest.raises(ConflictError):
        cohorts.export_version("export-q", candidate["version_no"], "export-key-002")


def test_freeze_survives_process_restart_without_change(client):
    init_db()
    clock = FrozenClock(datetime(2026, 9, 25, 8, 0, tzinfo=UTC))
    _catalog, _pilots, cohorts, _session, _evidence = _setup_world(clock)
    freeze = cohorts.create_freeze(
        {"code": "restart-q", "name": "恢复队列", "cutoff_at": clock.now(), "created_by": "stat-owner"},
        CRITERIA,
    )
    digest_before = freeze["manifest_digest"]
    close_connection()
    init_db()
    restarted = CohortFreezeService(get_connection(), clock)
    report = restarted.verify_version("restart-q", 1)
    assert report["matches"] is True
    assert report["manifest_digest"] == digest_before
    export = restarted.export_version("restart-q", 1, "same-key")
    assert export["manifest_digest"] == digest_before


def test_api_cohort_freeze_revision_and_export_flow(client):
    # 目录与场次准备
    product = client.post("/api/catalog/products", json=PRODUCT)
    assert product.status_code == 201
    assert client.post("/api/catalog/sites", json=SITE).status_code == 201
    evidence = client.post("/api/catalog/evidence", json={
        "product_code": "bci-a", "evidence_type": "临床", "title": "小样本临床观察",
        "source_name": "市一医院", "source_region": "杭州", "version": "v1",
        "content_digest": "sha256:" + "a" * 58, "summary": {}, "submitted_by": "reviewer-1",
    })
    assert evidence.status_code == 201
    protocol = client.post("/api/pilots/protocols?actor=admin", json={
        "code": "bci-rehab", "name": "脑机接口康复方案", "capability": "bci", "product_code": "bci-a",
        "parameter_schema": {"minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 30}},
        "default_parameters": {}, "max_runtime_seconds": 300, "max_attempts": 2,
    })
    assert protocol.status_code == 201, protocol.text
    submitted = client.post("/api/pilots/sessions", json={
        "protocol_code": "bci-rehab", "project_code": "bci-obs-2026", "requested_by": "stat-owner",
        "parameters": {"minutes": 10}, "priority": 50, "idempotency_key": "api-session-001",
    })
    assert submitted.status_code == 202
    sid = submitted.json()["id"]
    assert client.post("/api/pilots/sessions/claim", json={
        "site_code": "hospital-1", "capabilities": ["bci"], "lease_seconds": 60,
    }).json()["session"]["id"] == sid
    assert client.post(f"/api/pilots/sessions/{sid}/complete", json={
        "site_code": "hospital-1", "observation": {"score": 0.8}, "metrics": {},
    }).status_code == 200

    freeze = client.post("/api/cohorts", json={
        "code": "api-q", "name": "接口队列", "cutoff_at": "2026-12-31T23:59:59Z",
        "criteria": CRITERIA, "created_by": "stat-owner",
    })
    assert freeze.status_code == 201, freeze.text
    assert freeze.json()["status"] == "issued"
    assert freeze.json()["members"][0]["included"] is False

    assert client.post(f"/api/catalog/evidence/{evidence.json()['id']}/review", json={
        "reviewer": "doc-1", "decision": "accepted", "note": "临床数据可接受",
    }).status_code == 200
    revision = client.post("/api/cohorts/api-q/revisions", json={
        "cutoff_at": "2027-01-31T23:59:59Z", "actor": "stat-owner", "reason": "证据状态变化",
    })
    assert revision.status_code == 201, revision.text
    assert revision.json()["diff"]["added"][0]["session_id"] == sid
    assert revision.json()["status"] == "candidate"
    issue = client.post("/api/cohorts/api-q/versions/2/issue", json={"actor": "lead", "reason": "评审通过"})
    assert issue.status_code == 200 and issue.json()["status"] == "issued"

    verify = client.get("/api/cohorts/api-q/versions/2/verify")
    assert verify.status_code == 200 and verify.json()["matches"] is True
    export_a = client.post("/api/cohorts/api-q/versions/2/export", json={"idempotency_key": "exp-000001"})
    export_b = client.post("/api/cohorts/api-q/versions/2/export", json={"idempotency_key": "exp-000001"})
    assert export_a.json()["content_digest"] == export_b.json()["content_digest"]
    assert export_a.json()["replayed"] is False and export_b.json()["replayed"] is True


def test_revision_requires_later_cutoff_and_single_open_candidate(client):
    from app.core.errors import ConflictError, ValidationError

    init_db()
    clock = FrozenClock(datetime(2026, 9, 25, 8, 0, tzinfo=UTC))
    _catalog, _pilots, cohorts, _session, _evidence = _setup_world(clock)
    cohorts.create_freeze(
        {"code": "guard-q", "name": "守卫队列", "cutoff_at": clock.now(), "created_by": "stat-owner"},
        CRITERIA,
    )
    clock.advance(days=2)
    cohorts.revise("guard-q", {"cutoff_at": clock.now(), "actor": "a", "reason": "修订一"})
    # 已有候选修订时不能再提
    with pytest.raises(ConflictError):
        cohorts.revise("guard-q", {"cutoff_at": clock.now(), "actor": "a", "reason": "修订二"})
    # 截止时点不得回退
    cohorts.discard_candidate("guard-q", 2, {"actor": "a", "reason": "废弃"})
    with pytest.raises(ValidationError):
        cohorts.revise("guard-q", {"cutoff_at": datetime(2026, 9, 1, tzinfo=UTC), "actor": "a", "reason": "回退"})
