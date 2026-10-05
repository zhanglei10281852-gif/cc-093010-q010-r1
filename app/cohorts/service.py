from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.cohorts.repository import CohortRepository
from app.cohorts.schemas import CohortCriteria
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.privacy import sanitize_payload
from app.database import get_connection, transaction
from app.services.idempotency import IdempotencyService

REASON_INCLUDED = "符合全部纳入条件与质量规则"

# 排除理由（按评估顺序，code 稳定，便于跨版本比较）
REASON_PROJECT = "project_not_allowed"
REASON_REQUESTER = "requester_not_allowed"
REASON_STATUS = "session_status_not_allowed"
REASON_PRODUCT_LINK = "protocol_without_product_link"
REASON_PRODUCT_SCOPE = "product_out_of_scope"
REASON_PROTOCOL_SCOPE = "protocol_out_of_scope"
REASON_PROTOCOL_VERSION = "protocol_version_mismatch"
REASON_PROTOCOL_INACTIVE = "protocol_inactive"
REASON_PRODUCT_INACTIVE = "product_inactive"
REASON_REGULATORY = "regulatory_status_not_allowed"
REASON_EVIDENCE_MISSING = "required_evidence_missing"
REASON_EVIDENCE_COUNT = "accepted_evidence_insufficient"
REASON_SITE_MISSING = "executing_site_unresolved"
REASON_SITE_TYPE = "site_type_not_allowed"
REASON_REGION = "site_region_not_allowed"
REASON_SITE_INACTIVE = "site_inactive"
REASON_OBSERVATIONS = "observation_versions_insufficient"

REASON_MESSAGES = {
    REASON_PROJECT: "场次项目不在参与条件允许范围内",
    REASON_REQUESTER: "提交方不在参与条件允许范围内",
    REASON_STATUS: "场次截止时点状态不在允许范围内",
    REASON_PRODUCT_LINK: "体验方案在截止时点未关联健康创新产品",
    REASON_PRODUCT_SCOPE: "关联产品不在冻结的产品范围内",
    REASON_PROTOCOL_SCOPE: "体验方案不在冻结的方案范围内",
    REASON_PROTOCOL_VERSION: "体验方案版本与冻结指定版本不一致",
    REASON_PROTOCOL_INACTIVE: "体验方案在截止时点已停用",
    REASON_PRODUCT_INACTIVE: "产品在截止时点已停用",
    REASON_REGULATORY: "产品合规状态不在允许范围内",
    REASON_EVIDENCE_MISSING: "缺少截止时点已接受的必需类型证据",
    REASON_EVIDENCE_COUNT: "已接受证据数量低于质量规则下限",
    REASON_SITE_MISSING: "无法从观察记录或租约确定执行场地",
    REASON_SITE_TYPE: "执行场地类型不在冻结范围内",
    REASON_REGION: "执行场地区域不在冻结范围内",
    REASON_SITE_INACTIVE: "执行场地在截止时点不可用",
    REASON_OBSERVATIONS: "观察记录版本数低于参与条件下限",
}


def canonical_digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def pseudonym(subject: str, *scope: Any) -> str:
    return "P-" + canonical_digest([subject, *scope])[:12].upper()


class CohortFreezeService:
    """生成不可变队列成员清单，并以候选修订方式管理后续版本。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = CohortRepository(self.connection)

    # ---- 查询 -----------------------------------------------------------

    def list_cohorts(self) -> list[dict[str, Any]]:
        return self.repository.list_cohorts()

    def get_cohort(self, code: str) -> dict[str, Any]:
        cohort = self.repository.cohort_by_code(code.strip().lower())
        if cohort is None:
            raise NotFoundError("队列冻结不存在")
        result = dict(cohort)
        result["criteria"] = json.loads(result.pop("criteria_json"))
        result["versions"] = self.repository.versions_for_cohort(int(cohort["id"]))
        return result

    def get_version(self, code: str, version_no: int) -> dict[str, Any]:
        version_row = self._require_version(code, version_no)
        return self._version_detail(version_row)

    # ---- 创建冻结 -------------------------------------------------------

    def create_freeze(self, payload: dict[str, Any], criteria: dict[str, Any]) -> dict[str, Any]:
        code = payload["code"].strip().lower()
        cutoff_at = to_storage(payload["cutoff_at"])
        now = to_storage(self.clock.now())
        criteria = CohortCriteria.model_validate(criteria).model_dump()
        criteria_digest = canonical_digest(criteria)
        with transaction(immediate=True) as connection:
            repository = CohortRepository(connection)
            if repository.cohort_by_code(code):
                raise ConflictError("队列冻结编码已存在")
            cohort = repository.create_cohort(
                code=code, name=payload["name"].strip(), criteria=criteria,
                criteria_digest=criteria_digest, created_by=payload["created_by"], now=now,
            )
            evaluation = self._evaluate(connection, cohort, criteria, cutoff_at)
            manifest = self._manifest_from_members(cohort, 1, cutoff_at, criteria_digest, evaluation["members"])
            version_id = repository.create_version(
                cohort_id=int(cohort["id"]), version_no=1, cutoff_at=cutoff_at,
                created_by=payload["created_by"], now=now, warnings=evaluation["warnings"], **manifest,
            )
            self._persist_members(connection, version_id, evaluation["members"], now)
            diff = {"base_version": None, "added": [m["diff_entry"] for m in evaluation["members"] if m["included"]],
                    "removed": [], "changed": []}
            repository.save_version_diff(version_id, diff)
            repository.issue_version(version_id, issued_by=payload["created_by"], issued_at=now)
            return self._version_detail(repository.version_by_id(version_id))

    # ---- 候选修订 -------------------------------------------------------

    def revise(self, code: str, payload: dict[str, Any]) -> dict[str, Any]:
        code = code.strip().lower()
        cutoff_at = to_storage(payload["cutoff_at"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CohortRepository(connection)
            cohort = repository.cohort_by_code(code)
            if cohort is None:
                raise NotFoundError("队列冻结不存在")
            open_candidate = repository.latest_version(int(cohort["id"]), status="candidate")
            if open_candidate is not None:
                raise ConflictError("已有待签发的候选修订，请先签发或废弃")
            issued = repository.latest_version(int(cohort["id"]), status="issued")
            if issued is None:
                raise ConflictError("首个冻结版本尚未签发，不能提出修订")
            if cutoff_at <= issued["cutoff_at"]:
                raise ValidationError("修订的截止时点必须晚于已签发版本的截止时点")
            version_no = int(issued["version_no"]) + 1
            criteria = json.loads(cohort["criteria_json"])
            evaluation = self._evaluate(connection, cohort, criteria, cutoff_at)
            manifest = self._manifest_from_members(cohort, version_no, cutoff_at, cohort["rule_digest"], evaluation["members"])
            version_id = repository.create_version(
                cohort_id=int(cohort["id"]), version_no=version_no, cutoff_at=cutoff_at,
                created_by=payload["actor"], now=now, warnings=evaluation["warnings"], **manifest,
            )
            self._persist_members(connection, version_id, evaluation["members"], now)
            diff = self._diff_against(connection, issued, evaluation["members"], payload["reason"])
            repository.save_version_diff(version_id, diff)
            return self._version_detail(repository.version_by_id(version_id))

    def issue(self, code: str, version_no: int, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CohortRepository(connection)
            cohort = repository.cohort_by_code(code.strip().lower())
            if cohort is None:
                raise NotFoundError("队列冻结不存在")
            version = self._require_version_row(repository, int(cohort["id"]), version_no)
            if version["status"] != "candidate":
                raise ConflictError("只有候选版本可以签发")
            manifest = self._manifest(cohort, version, repository.members_of_version(version["id"]))
            if payload.get("expected_manifest_digest") and payload["expected_manifest_digest"] != manifest["manifest_digest"]:
                raise ConflictError("候选清单摘要与评审人持有的摘要不一致，已拒绝签发")
            if version["manifest_digest"] != manifest["manifest_digest"]:
                raise ConflictError("候选清单已经发生变化，请重新核对后再签发")
            repository.issue_version(version["id"], issued_by=payload["actor"], issued_at=now)
            return self._version_detail(repository.version_by_id(version["id"]))

    def discard_candidate(self, code: str, version_no: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            repository = CohortRepository(connection)
            cohort = repository.cohort_by_code(code.strip().lower())
            if cohort is None:
                raise NotFoundError("队列冻结不存在")
            version = self._require_version_row(repository, int(cohort["id"]), version_no)
            if version["status"] != "candidate":
                raise ConflictError("只有候选版本可以废弃")
            connection.execute("DELETE FROM cohort_members WHERE version_id=?", (version["id"],))
            connection.execute("DELETE FROM cohort_versions WHERE id=? AND status='candidate'", (version["id"],))
            return {"discarded": True, "cohort": cohort["code"], "version_no": version_no,
                    "actor": payload["actor"], "reason": payload["reason"]}

    # ---- 核验 -----------------------------------------------------------

    def verify_version(self, code: str, version_no: int) -> dict[str, Any]:
        """重算成员与清单摘要，核验已保存的冻结版本是否仍然一致。"""
        with transaction() as connection:
            repository = CohortRepository(connection)
            cohort = repository.cohort_by_code(code.strip().lower())
            if cohort is None:
                raise NotFoundError("队列冻结不存在")
            version = self._require_version_row(repository, int(cohort["id"]), version_no)
            members = repository.members_of_version(version["id"])
            manifest = self._manifest(cohort, version, members)
            return {
                "cohort": cohort["code"],
                "version_no": version_no,
                "status": version["status"],
                "manifest_digest": version["manifest_digest"],
                "recomputed_manifest_digest": manifest["manifest_digest"],
                "matches": version["manifest_digest"] == manifest["manifest_digest"],
                "member_total": manifest["member_total"],
                "included_total": manifest["included_total"],
                "excluded_total": manifest["excluded_total"],
            }

    # ---- 去标识导出（幂等重放）-------------------------------------------

    def export_version(self, code: str, version_no: int, idempotency_key: str) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            repository = CohortRepository(connection)
            cohort = repository.cohort_by_code(code.strip().lower())
            if cohort is None:
                raise NotFoundError("队列冻结不存在")
            version = self._require_version_row(repository, int(cohort["id"]), version_no)
            if version["status"] != "issued":
                raise ConflictError("只有已签发版本可以导出")

            def build() -> tuple[dict[str, Any], int]:
                return self._build_export(cohort, version, repository.members_of_version(version["id"]), repository), 200

            stored = IdempotencyService(connection, self.clock).execute(
                scope=f"cohort-export:{version['id']}", key=idempotency_key, payload={}, operation=build,
            )
            return {**stored.body, "replayed": stored.replayed}

    # ---- 评估引擎 -------------------------------------------------------

    def _evaluate(self, connection: sqlite3.Connection, cohort: sqlite3.Row, criteria: dict[str, Any], cutoff_at: str) -> dict[str, Any]:
        repository = CohortRepository(connection)
        products = {eid: repository.snapshot_at("health_products", eid, cutoff_at)
                    for eid in repository.existing_entity_ids_at("health_products", cutoff_at)}
        sites = {eid: repository.snapshot_at("pilot_sites", eid, cutoff_at)
                 for eid in repository.existing_entity_ids_at("pilot_sites", cutoff_at)}
        protocols = {eid: repository.snapshot_at("pilot_protocols", eid, cutoff_at)
                     for eid in repository.existing_entity_ids_at("pilot_protocols", cutoff_at)}
        sessions = {eid: repository.snapshot_at("pilot_sessions", eid, cutoff_at)
                    for eid in repository.existing_entity_ids_at("pilot_sessions", cutoff_at)}
        evidence = [repository.snapshot_at("evidence_documents", eid, cutoff_at)
                    for eid in repository.existing_entity_ids_at("evidence_documents", cutoff_at)]
        site_by_code = {state["code"]: state for state in sites.values() if state}

        warnings = self._scope_warnings(criteria, products, protocols)
        members: list[dict[str, Any]] = []
        ordered_sessions = sorted(sessions.values(), key=lambda s: (s["created_at"], s["id"]))
        for ordinal, session in enumerate(ordered_sessions, start=1):
            members.append(self._evaluate_one(
                ordinal, session, criteria, cutoff_at, protocols, products, site_by_code, evidence, repository,
            ))
        return {"members": members, "warnings": warnings}

    def _evaluate_one(self, ordinal: int, session: dict[str, Any], criteria: dict[str, Any], cutoff_at: str,
                      protocols: dict[int, dict[str, Any]], products: dict[int, dict[str, Any]],
                      site_by_code: dict[str, dict[str, Any]], evidence: list[dict[str, Any]],
                      repository: CohortRepository) -> dict[str, Any]:
        participation = criteria["participation"]
        quality = criteria["quality"]

        protocol = protocols.get(int(session["protocol_id"]))
        product = products.get(int(protocol["product_id"])) if protocol and protocol.get("product_id") is not None else None
        observations = repository.observations_for_session(int(session["id"]), cutoff_at)
        latest_observation = observations[-1] if observations else None
        site_code = latest_observation["created_by"] if latest_observation else session.get("lease_owner") or ""
        site = site_by_code.get(site_code) if site_code else None
        product_evidence = [item for item in evidence if item and int(item["product_id"]) == int(product["id"])] if product else []

        checks: list[tuple[str, bool]] = [
            (REASON_PROJECT, not participation["project_codes"] or session["project_code"] in participation["project_codes"]),
            (REASON_REQUESTER, not participation["requested_by"] or session["requested_by"] in participation["requested_by"]),
            (REASON_STATUS, session["status"] in participation["session_statuses"]),
            (REASON_PROTOCOL_SCOPE, not criteria["protocol_codes"] or (protocol is not None and protocol["code"] in criteria["protocol_codes"])),
        ]
        if criteria.get("protocol_version") is not None:
            checks.append((REASON_PROTOCOL_VERSION, protocol is not None and int(protocol["version"]) == int(criteria["protocol_version"])))
        if quality["require_active_protocol"]:
            checks.append((REASON_PROTOCOL_INACTIVE, protocol is not None and bool(protocol["active"])))
        checks.append((REASON_PRODUCT_LINK, product is not None or not participation["require_product_link"]))
        if product is not None:
            in_scope = (
                (not criteria["product_codes"] or product["code"] in criteria["product_codes"])
                and (not criteria["product_categories"] or product["category"] in criteria["product_categories"])
            )
            checks.extend([
                (REASON_PRODUCT_SCOPE, in_scope),
                (REASON_PRODUCT_INACTIVE, not quality["require_active_product"] or bool(product["active"])),
                (REASON_REGULATORY, product["regulatory_status"] in quality["allowed_regulatory_status"]),
            ])
            accepted = [item for item in product_evidence if item["status"] == "accepted"]
            pool = accepted if quality["accepted_evidence_only"] else product_evidence
            checks.extend([
                (REASON_EVIDENCE_MISSING, all(
                    any(item["evidence_type"] == required for item in pool)
                    for required in quality["evidence_required"]
                )),
                (REASON_EVIDENCE_COUNT, len(accepted) >= int(quality["min_evidence_accepted"])),
            ])
        checks.append((REASON_OBSERVATIONS, len(observations) >= int(participation["min_observation_versions"])))
        if site is None:
            checks.append((REASON_SITE_MISSING, not (criteria["site_types"] or criteria["regions"] or quality["require_active_site"])))
        else:
            checks.extend([
                (REASON_SITE_TYPE, not criteria["site_types"] or site["site_type"] in criteria["site_types"]),
                (REASON_REGION, not criteria["regions"] or site["region"] in criteria["regions"]),
                (REASON_SITE_INACTIVE, not quality["require_active_site"] or site["status"] == "active"),
            ])

        failed = next((code for code, passed in checks if not passed), None)
        included = failed is None
        reason_code = "included" if included else failed
        reason = REASON_INCLUDED if included else REASON_MESSAGES[failed]
        payload = self._member_payload(
            session=session, protocol=protocol, product=product, site=site,
            latest_observation=latest_observation, observation_total=len(observations),
            product_evidence=product_evidence, reason_code=reason_code, reason=reason,
            resolved_site_code=site_code,
        )
        member_digest = canonical_digest({"included": included, "reason_code": reason_code, "payload": payload})
        diff_entry = {
            "session_id": int(session["id"]), "project_code": session["project_code"],
            "reason_code": reason_code, "reason": reason, "member_digest": member_digest,
        }
        return {"ordinal": ordinal, "session_id": int(session["id"]), "included": included,
                "reason_code": reason_code, "reason": reason, "payload": payload,
                "member_digest": member_digest, "diff_entry": diff_entry}

    @staticmethod
    def _member_payload(*, session: dict[str, Any], protocol: dict[str, Any] | None, product: dict[str, Any] | None,
                        site: dict[str, Any] | None, latest_observation: dict[str, Any] | None,
                        observation_total: int, product_evidence: list[dict[str, Any]],
                        reason_code: str, reason: str, resolved_site_code: str) -> dict[str, Any]:
        return {
            "decision": {"reason_code": reason_code, "reason": reason},
            "session": {
                "id": int(session["id"]),
                "project_code": session["project_code"],
                "status": session["status"],
                "priority": session["priority"],
                "attempt_count": session["attempt_count"],
                "parameters_digest": session["parameter_digest"],
                "current_observation_version": session["current_observation_version"],
                "created_at": session["created_at"],
                "updated_at": session["updated_at"],
            },
            "protocol": None if protocol is None else {
                "id": int(protocol["id"]), "code": protocol["code"], "version": protocol["version"],
                "capability": protocol["capability"], "active": bool(protocol["active"]),
                "product_id": protocol["product_id"], "updated_at": protocol["updated_at"],
            },
            "product": None if product is None else {
                "id": int(product["id"]), "code": product["code"], "name": product["name"],
                "category": product["category"], "risk_level": product["risk_level"],
                "regulatory_status": product["regulatory_status"], "active": bool(product["active"]),
                "updated_at": product["updated_at"],
            },
            "site": None if site is None else {
                "id": int(site["id"]), "code": site["code"], "name": site["name"],
                "site_type": site["site_type"], "region": site["region"], "status": site["status"],
                "updated_at": site["updated_at"],
            },
            "resolved_site_code": resolved_site_code,
            "observation": None if latest_observation is None else {
                "version": int(latest_observation["version"]),
                "observation_digest": latest_observation["observation_digest"],
                "created_by": latest_observation["created_by"],
                "created_at": latest_observation["created_at"],
            },
            "observation_total": observation_total,
            "evidence": [
                {"id": int(item["id"]), "evidence_type": item["evidence_type"], "version": item["version"],
                 "status": item["status"], "content_digest": item["content_digest"],
                 "submitted_at": item["submitted_at"], "reviewed_at": item["reviewed_at"]}
                for item in sorted(product_evidence, key=lambda item: (item["submitted_at"], item["id"]))
            ],
        }

    @staticmethod
    def _scope_warnings(criteria: dict[str, Any], products: dict[int, dict[str, Any]],
                        protocols: dict[int, dict[str, Any]]) -> list[str]:
        warnings: list[str] = []
        known_product_codes = {state["code"] for state in products.values() if state}
        for code in criteria["product_codes"]:
            if code not in known_product_codes:
                warnings.append(f"产品 {code} 在截止时点不存在")
        known_protocol_codes = {state["code"] for state in protocols.values() if state}
        for code in criteria["protocol_codes"]:
            if code not in known_protocol_codes:
                warnings.append(f"方案 {code} 在截止时点不存在")
        return warnings

    # ---- 版本差异 -------------------------------------------------------

    def _diff_against(self, connection: sqlite3.Connection, base_version: sqlite3.Row,
                      new_members: list[dict[str, Any]], revision_reason: str) -> dict[str, Any]:
        base_members = CohortRepository(connection).members_of_version(base_version["id"])
        base_by_session = {int(row["session_id"]): row for row in base_members}
        added: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        changed: list[dict[str, Any]] = []
        for member in new_members:
            old = base_by_session.get(member["session_id"])
            if old is None:
                if member["included"]:
                    added.append(member["diff_entry"])
                continue
            old_included = bool(old["included"])
            if old_included and not member["included"]:
                removed.append(member["diff_entry"])
            elif not old_included and member["included"]:
                added.append(member["diff_entry"])
            elif old["member_digest"] != member["member_digest"]:
                old_payload = json.loads(old["payload_json"])
                change_entry = {
                    "session_id": member["session_id"],
                    "project_code": member["payload"]["session"]["project_code"],
                    "previous_digest": old["member_digest"],
                    "current_digest": member["member_digest"],
                }
                if old_included:
                    change_entry["changes"] = self._payload_changes(old_payload, member["payload"])
                else:
                    change_entry["changes"] = [
                        f"排除理由由 {old_payload['decision']['reason_code']} 变为 {member['reason_code']}"
                    ]
                changed.append(change_entry)
        new_by_session = {member["session_id"]: member for member in new_members}
        for session_id, old in base_by_session.items():
            if bool(old["included"]) and session_id not in new_by_session:
                removed.append({
                    "session_id": session_id,
                    "project_code": json.loads(old["payload_json"])["session"]["project_code"],
                    "reason_code": "no_longer_visible_at_cutoff",
                    "reason": "场次在新的截止时点不再可见",
                    "member_digest": None,
                })
        return {
            "base_version": int(base_version["version_no"]),
            "base_cutoff_at": base_version["cutoff_at"],
            "revision_reason": revision_reason,
            "added": sorted(added, key=lambda item: item["session_id"]),
            "removed": sorted(removed, key=lambda item: item["session_id"]),
            "changed": sorted(changed, key=lambda item: item["session_id"]),
        }

    @staticmethod
    def _payload_changes(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
        changes: list[str] = []
        old_session, new_session = old["session"], new["session"]
        if old_session["status"] != new_session["status"]:
            changes.append(f"场次状态由 {old_session['status']} 变为 {new_session['status']}")
        if old.get("observation") != new.get("observation"):
            changes.append("引用的观察记录版本或内容摘要发生变化")
        if old.get("product") != new.get("product"):
            changes.append("引用的产品快照发生变化")
        if old.get("site") != new.get("site"):
            changes.append("执行场地快照发生变化")
        old_evidence = {item["id"]: item["status"] for item in old.get("evidence", [])}
        new_evidence = {item["id"]: item["status"] for item in new.get("evidence", [])}
        if old_evidence != new_evidence:
            moved = [f"证据{eid}:{old_evidence[eid]}→{new_evidence[eid]}" for eid in sorted(set(old_evidence) | set(new_evidence)) if old_evidence.get(eid) != new_evidence.get(eid)]
            changes.append("证据状态变化（" + "，".join(moved) + "）")
        if old.get("protocol") != new.get("protocol"):
            changes.append("引用的方案快照发生变化")
        return changes

    # ---- 清单摘要与导出 --------------------------------------------------

    def _manifest_from_members(self, cohort: sqlite3.Row, version_no: int, cutoff_at: str,
                               rule_digest: str, members: list[dict[str, Any]]) -> dict[str, Any]:
        ordered = sorted(members, key=lambda item: item["ordinal"])
        manifest_input = {
            "cohort_code": cohort["code"],
            "version_no": int(version_no),
            "cutoff_at": cutoff_at,
            "rule_digest": rule_digest,
            "members": [item["member_digest"] for item in ordered],
        }
        return {
            "manifest_digest": canonical_digest(manifest_input),
            "member_total": len(ordered),
            "included_total": sum(1 for item in ordered if item["included"]),
            "excluded_total": sum(1 for item in ordered if not item["included"]),
        }

    def _manifest(self, cohort: sqlite3.Row, version: sqlite3.Row, members: list[dict[str, Any]]) -> dict[str, Any]:
        return self._manifest_from_members(
            cohort, int(version["version_no"]), version["cutoff_at"], cohort["rule_digest"],
            [{"ordinal": int(row["ordinal"]), "included": bool(row["included"]), "member_digest": row["member_digest"]}
             for row in members],
        )

    def _build_export(self, cohort: sqlite3.Row, version: sqlite3.Row, members: list[dict[str, Any]],
                      repository: CohortRepository) -> dict[str, Any]:
        criteria = json.loads(cohort["criteria_json"])
        manifest = self._manifest(cohort, version, members)
        rows: list[dict[str, Any]] = []
        for row in sorted(members, key=lambda item: int(item["ordinal"])):
            payload = json.loads(row["payload_json"])
            session = payload["session"]
            rows.append({
                "ordinal": int(row["ordinal"]),
                "pseudonym": pseudonym(session["id"], cohort["code"], int(version["version_no"])),
                "session_id": session["id"],
                "project_code": session["project_code"],
                "session_status": session["status"],
                "protocol_code": payload["protocol"]["code"] if payload.get("protocol") else None,
                "protocol_version": payload["protocol"]["version"] if payload.get("protocol") else None,
                "product_code": payload["product"]["code"] if payload.get("product") else None,
                "product_category": payload["product"]["category"] if payload.get("product") else None,
                "site_code": payload["site"]["code"] if payload.get("site") else None,
                "site_type": payload["site"]["site_type"] if payload.get("site") else None,
                "region": payload["site"]["region"] if payload.get("site") else None,
                "observation_version": payload["observation"]["version"] if payload.get("observation") else None,
                "observation_digest": payload["observation"]["observation_digest"] if payload.get("observation") else None,
                "evidence_refs": [
                    {"id": item["id"], "evidence_type": item["evidence_type"], "version": item["version"],
                     "status": item["status"], "content_digest": item["content_digest"]}
                    for item in payload["evidence"]
                ],
                "decision": payload["decision"],
                "member_digest": row["member_digest"],
            })
        artifact = {
            "artifact_type": "cohort_export",
            "cohort": {"code": cohort["code"], "name": cohort["name"]},
            "version_no": int(version["version_no"]),
            "cutoff_at": version["cutoff_at"],
            "issued_at": version["issued_at"],
            "order": "ordinal",
            "criteria_digest": cohort["rule_digest"],
            "manifest_digest": manifest["manifest_digest"],
            "rule_snapshot": criteria,
            "members": rows,
        }
        artifact["content_digest"] = canonical_digest({k: v for k, v in artifact.items() if k != "content_digest"})
        # 去标识防线：导出前再次净化，联系人样式信息不会离开服务
        return sanitize_payload(artifact)

    # ---- 组装与辅助 -----------------------------------------------------

    def _persist_members(self, connection: sqlite3.Connection, version_id: int, members: list[dict[str, Any]], now: str) -> None:
        repository = CohortRepository(connection)
        for member in members:
            repository.add_member(
                version_id=version_id, ordinal=member["ordinal"], session_id=member["session_id"],
                included=member["included"], reason=member["reason"], payload=member["payload"],
                member_digest=member["member_digest"], now=now,
            )

    def _require_version(self, code: str, version_no: int) -> sqlite3.Row:
        cohort = self.repository.cohort_by_code(code.strip().lower())
        if cohort is None:
            raise NotFoundError("队列冻结不存在")
        return self._require_version_row(self.repository, int(cohort["id"]), version_no)

    @staticmethod
    def _require_version_row(repository: CohortRepository, cohort_id: int, version_no: int) -> sqlite3.Row:
        versions = {int(row["version_no"]): row for row in repository.versions_for_cohort(cohort_id)}
        if version_no not in versions:
            raise NotFoundError("队列版本不存在")
        row = repository.version_by_id(versions[version_no]["id"])
        if row is None:
            raise NotFoundError("队列版本不存在")
        return row

    def _version_detail(self, version_row: sqlite3.Row) -> dict[str, Any]:
        result = dict(version_row)
        result["members"] = self.repository.members_of_version(version_row["id"])
        for member in result["members"]:
            member["included"] = bool(member["included"])
            member["payload"] = json.loads(member.pop("payload_json"))
        result["diff"] = json.loads(result.pop("diff_json") or "{}")
        result["warnings"] = json.loads(result.pop("warnings_json") or "[]")
        cohort = self.repository.cohort_by_id(int(version_row["cohort_id"]))
        result["cohort_code"] = cohort["code"]
        result["criteria_digest"] = cohort["rule_digest"]
        result["criteria"] = json.loads(cohort["criteria_json"])
        return result
