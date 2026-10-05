from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from app.catalog.service import CatalogService
from app.cohorts.service import CohortFreezeService, digest
from app.core.clock import FrozenClock
from app.database import get_connection
from app.pilots.service import PilotOperationsService


T1 = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
T2 = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)

CRITERIA = {
    "categories": ["数字疗法"],
    "site_types": ["医院"],
    "require_evidence_types": ["性能"],
    "require_evidence_min_accepted": 1,
    "require_product_active": True,
    "require_observation": True,
}


def _setup(clock: FrozenClock) -> tuple[PilotOperationsService, CatalogService]:
    pilots = PilotOperationsService(get_connection(), clock)
    catalog = CatalogService(get_connection(), clock)
    pilots.create_protocol({
        "code": "neuro-protocol",
        "name": "脑机与数字疗法观察方案",
        "capability": "neuro",
        "parameter_schema": {
            "minutes": {"type": "integer", "required": True, "minimum": 1, "maximum": 60},
            "product_code": {"type": "string", "required": True},
            "phone": {"type": "string", "required": False},
        },
        "default_parameters": {},
        "max_runtime_seconds": 600,
        "max_attempts": 2,
    }, "administrator")
    catalog.create_product({
        "code": "bci-calm", "name": "脑机放松训练", "organization": "神经科技示例",
        "origin_country": "中国", "category": "数字疗法", "intended_use": "用于焦虑干预的脑机反馈训练与观察",
        "risk_level": "medium", "regulatory_status": "研究",
    })
    catalog.create_product({
        "code": "bci-home", "name": "家用脑机监测", "organization": "神经科技示例",
        "origin_country": "中国", "category": "数字疗法", "intended_use": "用于居家脑电监测的慢病随访观察",
        "risk_level": "medium", "regulatory_status": "展示",
    })
    catalog.create_product({
        "code": "chronic-care", "name": "慢病管理助手", "organization": "慢病科技示例",
        "origin_country": "中国", "category": "慢病管理", "intended_use": "用于高血压糖尿病患者的日常管理随访",
        "risk_level": "low", "regulatory_status": "已注册",
    })
    catalog.create_site({
        "code": "hospital-a", "name": "联合医院观察点", "site_type": "医院", "region": "上海",
        "capabilities": ["neuro"], "max_concurrent": 3,
    })
    catalog.create_site({
        "code": "expo-b", "name": "展会体验点B", "site_type": "展会体验点", "region": "杭州",
        "capabilities": ["neuro"], "max_concurrent": 1,
    })
    return pilots, catalog


def _evidence(catalog: CatalogService, product: str, version: str, digest_value: str = "d" * 64) -> int:
    return catalog.submit_evidence({
        "product_code": product, "evidence_type": "性能", "title": f"{product} 性能观察",
        "source_name": "联合验证组", "source_region": "中国", "version": version,
        "content_digest": digest_value, "summary": {"samples": 100}, "submitted_by": "evidence-owner",
    })["id"]


def _run_session(pilots: PilotOperationsService, key: str, product: str, site: str,
                 metrics: dict | None = None, phone: str = "") -> int:
    parameters = {"minutes": 10, "product_code": product}
    if phone:
        parameters["phone"] = phone
    session = pilots.submit({
        "protocol_code": "neuro-protocol", "project_code": "bci-2026",
        "requested_by": "operator-x", "parameters": parameters, "priority": 50,
        "idempotency_key": key,
    })
    claimed = pilots.claim(site, ["neuro"], 300)
    assert claimed is not None and claimed["id"] == session["id"]
    pilots.complete(session["id"], site, {"value": 1}, metrics or {"score": 8})
    return int(session["id"])


def _freeze_service(clock: FrozenClock) -> CohortFreezeService:
    return CohortFreezeService(get_connection(), clock)


def test_freeze_records_inclusion_exclusion_and_cutoff_evidence_state(client):
    clock = FrozenClock(datetime(2026, 10, 4, 8, 0, tzinfo=UTC))
    pilots, catalog = _setup(clock)
    evidence_calm = _evidence(catalog, "bci-calm", "2026.09")
    evidence_home = _evidence(catalog, "bci-home", "2026.09", "e" * 64)

    clock.advance(hours=1)  # 09:00
    session_a = _run_session(pilots, "session-a", "bci-calm", "hospital-a", phone="13812345678")
    session_b = _run_session(pilots, "session-b", "chronic-care", "expo-b")
    session_d = _run_session(pilots, "session-d", "bci-home", "hospital-a")

    clock.advance(hours=1)  # 10:00：bci-calm 证据在截止时点前接受
    catalog.review_evidence(evidence_calm, "reviewer-a", "accepted", "来源可追溯，接受")

    service = _freeze_service(clock)
    frozen = service.create_freeze({
        "code": "bci-queue", "name": "脑机观察评审队列", "cutoff_at": T1.isoformat(),
        "created_by": "stat-lead", **CRITERIA,
    })
    assert frozen["status"] == "frozen"
    assert frozen["version"] == 1
    assert frozen["included_count"] == 1
    assert frozen["excluded_count"] == 2

    members = service.list_members("bci-queue", 1)["items"]
    included = [m for m in members if m["included"]]
    excluded = [m for m in members if not m["included"]]
    assert [m["session_id"] for m in included] == [session_a]

    # 纳入成员保留观察版本与证据状态快照，可追溯
    member_a = included[0]
    assert member_a["observation_version"] == 1
    assert member_a["context"]["observation"]["version"] == 1
    assert member_a["context"]["site"]["site_type"] == "医院"
    assert member_a["evidence_snapshot"][0]["status_as_of_cutoff"] == "accepted"
    assert member_a["evidence_snapshot"][0]["content_digest"] == "d" * 64

    # 联系人信息被去标识：参数中的手机号已打码
    member_text = json.dumps(member_a, ensure_ascii=False)
    assert "13812345678" not in member_text
    assert "138****5678" in member_text

    # D 的证据在 T1 时仍为 submitted（即使后来状态变化），保留当时状态并给出排除原因
    member_d = next(m for m in excluded if m["session_id"] == session_d)
    assert any(r["code"] == "evidence_type_missing" for r in member_d["exclusion_reasons"])
    assert member_d["evidence_snapshot"][0]["status_as_of_cutoff"] == "submitted"
    assert member_d["observation_version"] == 1  # 观察仍可追溯

    # B 同时因类别与场地类型被排除
    member_b = next(m for m in excluded if m["session_id"] == session_b)
    reason_codes = {r["code"] for r in member_b["exclusion_reasons"]}
    assert {"category_mismatch", "site_type_mismatch"} <= reason_codes

    # 摘要可复验
    verification = frozen["verification"]
    assert verification["roster_digest_match"] is True
    assert verification["criteria_digest_match"] is True


def test_late_receipt_evidence_change_and_catalog_update_drive_revision_diff(client):
    clock = FrozenClock(datetime(2026, 10, 4, 8, 0, tzinfo=UTC))
    pilots, catalog = _setup(clock)
    evidence_calm = _evidence(catalog, "bci-calm", "2026.09")
    evidence_home = _evidence(catalog, "bci-home", "2026.09", "e" * 64)

    clock.advance(hours=1)
    session_a = _run_session(pilots, "session-a", "bci-calm", "hospital-a")
    session_d = _run_session(pilots, "session-d", "bci-home", "hospital-a")
    clock.advance(hours=1)
    catalog.review_evidence(evidence_calm, "reviewer-a", "accepted", "接受")

    service = _freeze_service(clock)
    v1 = service.create_freeze({
        "code": "bci-queue", "name": "脑机观察评审队列", "cutoff_at": T1.isoformat(),
        "created_by": "stat-lead", **CRITERIA,
    })
    assert v1["included_count"] == 1

    # 第二天：迟到回执（C 在 T1 后才提交并回执）、证据状态变化、产品目录更新
    clock.current = datetime(2026, 10, 5, 7, 0, tzinfo=UTC)
    catalog.update_product("bci-calm", {"active": False})  # 目录更新：停用 v1 成员产品
    clock.advance(hours=1)  # 08:00 迟到回执
    session_c = _run_session(pilots, "session-c", "bci-home", "hospital-a")
    clock.advance(hours=1)  # 09:00 证据才被接受（T1 时未接受）
    catalog.review_evidence(evidence_home, "reviewer-a", "accepted", "补充材料后接受")

    proposed = service.propose_revision("bci-queue", {
        "cutoff_at": T2.isoformat(), "created_by": "stat-lead",
    })
    assert proposed["status"] == "proposed"
    assert proposed["version"] == 2
    diff = proposed["diff"]
    added_ids = {item["session_id"] for item in diff["added"]}
    removed_ids = {item["session_id"] for item in diff["removed"]}
    assert added_ids == {session_c, session_d}
    assert removed_ids == {session_a}
    removed_a = next(item for item in diff["removed"] if item["session_id"] == session_a)
    assert any(r["code"] == "product_inactive" for r in removed_a["reasons"])
    # 迟到回执成员的纳入理由可以追到观察版本
    added_c = next(item for item in diff["added"] if item["session_id"] == session_c)
    assert added_c["observation_version"] == 1

    # 签发前不能重复提出候选
    with pytest.raises(Exception, match="候选修订"):
        service.propose_revision("bci-queue", {"cutoff_at": T2.isoformat(), "created_by": "stat-lead"})

    issued = service.issue_revision("bci-queue", 2, "reviewer-b", "评审会确认增减")
    assert issued["status"] == "frozen"
    assert issued["issued_by"] == "reviewer-b"

    # 进程恢复/重跑不能让已签发队列变化：重复签发冲突
    with pytest.raises(Exception):
        service.issue_revision("bci-queue", 2, "reviewer-b", "再次签发")

    # 数据库层触发器兜底：已签发版本与其成员、制品不可改删
    connection = get_connection()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE cohort_members SET included=0 WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM cohort_versions WHERE code='bci-queue' AND version=1")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE cohort_versions SET name='hacked' WHERE code='bci-queue' AND version=1")

    # v1 成员仍然是 A（产品停用不影响已签发版本快照）
    v1_again = service.get_version_by_ref("bci-queue", 1)
    assert v1_again["included_count"] == 1
    v1_members = service.list_members("bci-queue", 1, included=True)["items"]
    assert [m["session_id"] for m in v1_members] == [session_a]
    assert v1_members[0]["context"]["products"][0]["active"] is True


def test_discarded_proposal_does_not_advance_versions(client):
    clock = FrozenClock(datetime(2026, 10, 4, 8, 0, tzinfo=UTC))
    pilots, catalog = _setup(clock)
    evidence_id = _evidence(catalog, "bci-calm", "2026.09")
    clock.advance(hours=1)
    _run_session(pilots, "session-a", "bci-calm", "hospital-a")
    clock.advance(hours=1)
    connection = get_connection()
    connection.execute("UPDATE evidence_documents SET status='accepted',reviewed_by='r',reviewed_at=? WHERE id=?",
                       (clock.now().isoformat(timespec="seconds"), evidence_id))
    service = _freeze_service(clock)
    service.create_freeze({
        "code": "bci-queue", "name": "队列", "cutoff_at": T1.isoformat(),
        "created_by": "stat-lead", **CRITERIA,
    })
    proposed = service.propose_revision("bci-queue", {
        "cutoff_at": T2.isoformat(), "created_by": "stat-lead",
    })
    assert proposed["version"] == 2
    result = service.discard_revision("bci-queue", 2, "stat-lead", "规则填写有误，放弃重来")
    assert result["discarded"] is True
    # 放弃后可以重新提出，版本号仍为 2
    again = service.propose_revision("bci-queue", {
        "cutoff_at": T2.isoformat(), "created_by": "stat-lead",
    })
    assert again["version"] == 2


def test_export_is_deidentified_stable_and_replayable(client):
    clock = FrozenClock(datetime(2026, 10, 4, 8, 0, tzinfo=UTC))
    pilots, catalog = _setup(clock)
    evidence_id = _evidence(catalog, "bci-calm", "2026.09")
    clock.advance(hours=1)
    session_a = _run_session(pilots, "session-a", "bci-calm", "hospital-a", phone="13812345678")
    clock.advance(hours=1)
    catalog.review_evidence(evidence_id, "reviewer-a", "accepted", "接受")
    service = _freeze_service(clock)
    service.create_freeze({
        "code": "bci-queue", "name": "脑机观察评审队列", "cutoff_at": T1.isoformat(),
        "created_by": "stat-lead", **CRITERIA,
    })

    request_payload = {"requested_by": "reviewer-b", "include_excluded": False, "idempotency_key": "export-000001"}
    first = service.export_artifact("bci-queue", 1, request_payload)
    assert first["replayed"] is False
    assert first["summary"]["included_count"] == 1

    # 去标识：不含提交人、手机号；成员顺序稳定（只有一条，校验字段白名单）
    member = first["included_members"][0]
    assert member["session_id"] == session_a
    assert "requested_by" not in member
    assert "phone" not in json.dumps(member, ensure_ascii=False)
    assert member["site"]["code"] == "hospital-a"
    assert member["observation_digest"]
    assert member["evidence_refs"][0]["status_as_of_cutoff"] == "accepted"

    # 同一请求重放返回同一制品
    replay = service.export_artifact("bci-queue", 1, request_payload)
    assert replay["replayed"] is True
    assert replay["artifact_id"] == first["artifact_id"]
    assert replay["content_digest"] == first["content_digest"]

    # 内容摘要与内容自洽：用同样的规范化 JSON 算法重算
    body = {k: v for k, v in replay.items() if k not in {"replayed", "artifact_id", "content_digest"}}
    assert digest(body) == replay["content_digest"]

    # 同一幂等键更换参数被拒绝
    from app.core.errors import ConflictError
    with pytest.raises(ConflictError):
        service.export_artifact("bci-queue", 1, {**request_payload, "include_excluded": True})

    # 不同幂等键的新请求产生不同制品，但成员稳定顺序下成员摘要一致
    second = service.export_artifact("bci-queue", 1, {
        "requested_by": "reviewer-b", "include_excluded": True, "idempotency_key": "export-000002",
    })
    assert second["artifact_id"] != first["artifact_id"]
    assert second["included_members"][0]["member_ref"] == first["included_members"][0]["member_ref"]
    assert second["summary"]["excluded_count_reported"] == 0  # 本场景其余场次不存在


def test_cutoff_validation_and_unknown_references(client):
    from app.core.errors import ValidationError
    clock = FrozenClock(datetime(2026, 10, 4, 8, 0, tzinfo=UTC))
    _setup(clock)
    service = _freeze_service(clock)
    with pytest.raises(ValidationError, match="时区"):
        service.create_freeze({
            "code": "bad-queue", "name": "队列", "cutoff_at": "2026-10-04T12:00:00",
            "created_by": "stat-lead",
        })
    with pytest.raises(ValidationError, match="产品"):
        service.create_freeze({
            "code": "bad-queue", "name": "队列", "cutoff_at": T1.isoformat(),
            "created_by": "stat-lead", "product_codes": ["ghost-product"],
        })
    # 首个版本不能用修订接口
    from app.core.errors import NotFoundError
    with pytest.raises(NotFoundError):
        service.propose_revision("missing", {"cutoff_at": T2.isoformat(), "created_by": "stat-lead"})
