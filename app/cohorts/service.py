from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.cohorts.repository import CohortRepository
from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.privacy import sanitize_payload
from app.database import get_connection, transaction


CRITERIA_FIELDS = (
    "product_codes",
    "protocol_codes",
    "site_types",
    "capabilities",
    "categories",
    "risk_levels",
    "project_codes",
    "require_product_active",
    "require_product_registered",
    "require_observation",
    "require_evidence_types",
    "require_evidence_min_accepted",
    "min_rating_count",
    "min_metric",
    "audience_types",
    "notes",
)

DEFAULT_CRITERIA: dict[str, Any] = {
    "product_codes": [],
    "protocol_codes": [],
    "site_types": [],
    "capabilities": [],
    "categories": [],
    "risk_levels": [],
    "project_codes": [],
    "require_product_active": True,
    "require_product_registered": False,
    "require_observation": True,
    "require_evidence_types": [],
    "require_evidence_min_accepted": 0,
    "min_rating_count": 0,
    "min_metric": {},
    "audience_types": [],
    "notes": "",
}

PRODUCT_PARAM_KEYS = ("product_code", "product_codes", "products")


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def parse_cutoff(value: str) -> datetime:
    """解析截止时点，要求带时区（接受 Z 后缀）。"""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError("截止时点不是合法的 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValidationError("截止时点必须包含时区信息")
    return parsed.astimezone(UTC)


def reason(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


class CohortFreezeService:
    """生成不可变队列版本、候选修订与可复现去标识导出。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = CohortRepository(self.connection)

    # ---------- 规则与快照 ----------

    @staticmethod
    def normalize_criteria(payload: dict[str, Any]) -> dict[str, Any]:
        criteria = {**DEFAULT_CRITERIA}
        for field in CRITERIA_FIELDS:
            if field in payload and payload[field] is not None:
                criteria[field] = payload[field]
        for list_field in ("product_codes", "protocol_codes", "site_types", "capabilities", "categories",
                           "risk_levels", "project_codes", "require_evidence_types", "audience_types"):
            criteria[list_field] = list(dict.fromkeys(criteria[list_field]))
        criteria["min_metric"] = {str(key): float(value) for key, value in sorted(criteria["min_metric"].items())}
        return {field: criteria[field] for field in CRITERIA_FIELDS}

    @staticmethod
    def _member_ref(code: str, session_id: int) -> str:
        return "M" + hashlib.sha256(f"cohort|{code}|session|{session_id}".encode()).hexdigest()[:12]

    def _validate_criteria_references(self, criteria: dict[str, Any]) -> None:
        unknown_products = [code for code in criteria["product_codes"]
                            if self.connection.execute("SELECT 1 FROM health_products WHERE code=?", (code,)).fetchone() is None]
        if unknown_products:
            raise ValidationError("部分产品在目录中不存在", context={"product_codes": unknown_products})
        unknown_protocols = [code for code in criteria["protocol_codes"]
                             if self.connection.execute("SELECT 1 FROM pilot_protocols WHERE code=?", (code,)).fetchone() is None]
        if unknown_protocols:
            raise ValidationError("部分方案版本在目录中不存在", context={"protocol_codes": unknown_protocols})

    @staticmethod
    def _extract_product_codes(parameters: dict[str, Any]) -> list[str]:
        codes: list[str] = []
        for key in PRODUCT_PARAM_KEYS:
            value = parameters.get(key)
            if isinstance(value, str) and value:
                codes.append(value)
            elif isinstance(value, list):
                codes.extend(str(item) for item in value if item)
        return list(dict.fromkeys(codes))

    @staticmethod
    def _as_of_evidence_status(row: sqlite3.Row, cutoff: datetime) -> str:
        reviewed_at = row["reviewed_at"]
        if not reviewed_at:
            return "submitted"
        if parse_cutoff(reviewed_at) > cutoff:
            return "submitted"
        return str(row["status"])

    def _evaluate(self, *, code: str, cutoff: datetime, criteria: dict[str, Any]) -> list[dict[str, Any]]:
        cutoff_text = to_storage(cutoff)
        sessions = self.connection.execute(
            "SELECT s.*,p.code AS protocol_code,p.capability,p.version AS protocol_version "
            "FROM pilot_sessions s JOIN pilot_protocols p ON p.id=s.protocol_id "
            "WHERE s.created_at<=? ORDER BY s.id",
            (cutoff_text,),
        ).fetchall()
        members: list[dict[str, Any]] = []
        need_product = bool(criteria["product_codes"] or criteria["categories"] or criteria["risk_levels"]
                            or criteria["require_product_registered"] or criteria["require_evidence_types"]
                            or criteria["require_evidence_min_accepted"] > 0 or criteria["min_rating_count"] > 0)
        for session in sessions:
            inclusion: list[dict[str, str]] = []
            exclusion: list[dict[str, str]] = []
            parameters = json.loads(session["parameters_json"])
            inclusion.append(reason("session_before_cutoff", f"场次在截止时点 {cutoff_text} 前提交"))

            # 方案与项目
            if criteria["protocol_codes"] and session["protocol_code"] not in criteria["protocol_codes"]:
                exclusion.append(reason("protocol_mismatch", f"方案 {session['protocol_code']} 不在指定方案版本内"))
            else:
                inclusion.append(reason("protocol_matched", f"方案 {session['protocol_code']} 符合方案版本条件"))
            if criteria["project_codes"] and session["project_code"] not in criteria["project_codes"]:
                exclusion.append(reason("project_mismatch", f"项目 {session['project_code']} 不在参与条件内"))

            # 截止时点前的观察版本
            observation = self.connection.execute(
                "SELECT * FROM pilot_observations WHERE session_id=? AND created_at<=? ORDER BY version DESC LIMIT 1",
                (session["id"], cutoff_text),
            ).fetchone()
            later_observation = self.connection.execute(
                "SELECT 1 FROM pilot_observations WHERE session_id=? AND created_at>? LIMIT 1",
                (session["id"], cutoff_text),
            ).fetchone()
            obs_version: int | None = None
            obs_digest: str | None = None
            metrics: dict[str, Any] = {}
            site_row: sqlite3.Row | None = None
            if observation is not None:
                obs_version = int(observation["version"])
                obs_digest = str(observation["observation_digest"])
                metrics = json.loads(observation["metrics_json"])
                inclusion.append(reason("observation_before_cutoff", f"截止时点前最新观察版本为 v{obs_version}"))
                site_row = self.connection.execute(
                    "SELECT * FROM pilot_sites WHERE code=?", (observation["created_by"],)
                ).fetchone()
                if site_row is None and (criteria["site_types"] or criteria["capabilities"]):
                    exclusion.append(reason("site_unavailable",
                                            f"观察回执执行场地 {observation['created_by']} 未在场地目录登记，无法核验场地条件"))
            elif criteria["require_observation"]:
                if later_observation is not None:
                    exclusion.append(reason("observation_late", "观察回执在截止时点之后才到达（迟到回执）"))
                else:
                    exclusion.append(reason("observation_missing", "截止时点前没有观察回执"))

            # 场地类型与能力（以回执执行场地为准）
            site_snapshot: dict[str, Any] | None = None
            if site_row is not None:
                site_snapshot = {
                    "code": site_row["code"], "name": site_row["name"], "site_type": site_row["site_type"],
                    "region": site_row["region"], "capabilities": json.loads(site_row["capabilities_json"]),
                    "status": site_row["status"], "max_concurrent": site_row["max_concurrent"],
                }
                if criteria["site_types"] and site_row["site_type"] not in criteria["site_types"]:
                    exclusion.append(reason("site_type_mismatch", f"场地类型 {site_row['site_type']} 不在指定类型内"))
                else:
                    inclusion.append(reason("site_type_matched", f"执行场地类型 {site_row['site_type']} 符合条件"))
                site_caps = set(json.loads(site_row["capabilities_json"]))
                missing_caps = [item for item in criteria["capabilities"] if item not in site_caps]
                if missing_caps:
                    exclusion.append(reason("site_capability_missing", f"场地缺少能力：{'、'.join(missing_caps)}"))
            elif criteria["site_types"] or criteria["capabilities"]:
                exclusion.append(reason("site_unavailable", "场地条件要求可核验的执行场地，但截止时点前没有回执场地"))

            # 产品关联与质量规则
            product_codes = self._extract_product_codes(parameters)
            product_snapshots: list[dict[str, Any]] = []
            evidence_snapshot: list[dict[str, Any]] = []
            unknown_products = [item for item in product_codes
                                if self.connection.execute("SELECT 1 FROM health_products WHERE code=?", (item,)).fetchone() is None]
            if unknown_products:
                exclusion.append(reason("product_missing", f"场次参数引用的产品不存在：{'、'.join(unknown_products)}"))
            if need_product and not product_codes:
                exclusion.append(reason("product_unlinked", "场次参数未关联任何产品，无法适用产品与证据质量规则"))
            if criteria["product_codes"] and not any(item in criteria["product_codes"] for item in product_codes):
                exclusion.append(reason("product_not_selected", "场次关联产品不在队列指定产品范围内"))
            for product_code in product_codes:
                product = self.connection.execute(
                    "SELECT * FROM health_products WHERE code=?", (product_code,)
                ).fetchone()
                if product is None:
                    continue
                snapshot = {"code": product["code"], "name": product["name"], "category": product["category"],
                            "risk_level": product["risk_level"], "regulatory_status": product["regulatory_status"],
                            "active": bool(product["active"])}
                product_snapshots.append(snapshot)
                product_ok = True
                if criteria["categories"] and product["category"] not in criteria["categories"]:
                    exclusion.append(reason("category_mismatch", f"产品 {product_code} 类别 {product['category']} 不符合"))
                    product_ok = False
                if criteria["risk_levels"] and product["risk_level"] not in criteria["risk_levels"]:
                    exclusion.append(reason("risk_level_mismatch", f"产品 {product_code} 风险级别 {product['risk_level']} 不符合"))
                    product_ok = False
                if criteria["require_product_active"] and not product["active"]:
                    exclusion.append(reason("product_inactive", f"产品 {product_code} 已停用"))
                    product_ok = False
                if criteria["require_product_registered"] and product["regulatory_status"] != "已注册":
                    exclusion.append(reason("product_not_registered", f"产品 {product_code} 合规状态为 {product['regulatory_status']}，未达到已注册"))
                    product_ok = False
                if product_ok:
                    inclusion.append(reason("product_matched", f"产品 {product_code} 的类别、风险级别与合规状态符合条件"))

                # 截止时点证据状态快照与质量规则
                evidence_rows = self.connection.execute(
                    "SELECT id,evidence_type,version,status,content_digest,submitted_at,reviewed_at "
                    "FROM evidence_documents WHERE product_id=? AND submitted_at<=? ORDER BY evidence_type,id",
                    (product["id"], cutoff_text),
                ).fetchall()
                accepted_types: set[str] = set()
                accepted_total = 0
                for row in evidence_rows:
                    as_of_status = self._as_of_evidence_status(row, cutoff)
                    evidence_snapshot.append({
                        "product_code": product_code,
                        "evidence_id": row["id"],
                        "evidence_type": row["evidence_type"],
                        "version": row["version"],
                        "status_as_of_cutoff": as_of_status,
                        "content_digest": row["content_digest"],
                        "submitted_at": row["submitted_at"],
                        "reviewed_at": row["reviewed_at"],
                    })
                    if as_of_status == "accepted":
                        accepted_types.add(str(row["evidence_type"]))
                        accepted_total += 1
                missing_types = [item for item in criteria["require_evidence_types"] if item not in accepted_types]
                if missing_types:
                    exclusion.append(reason("evidence_type_missing", f"产品 {product_code} 截止时点前缺少已接受证据：{'、'.join(missing_types)}"))
                if accepted_total < criteria["require_evidence_min_accepted"]:
                    exclusion.append(reason("evidence_count_below_minimum",
                                            f"产品 {product_code} 截止时点前已接受证据 {accepted_total} 份，少于 {criteria['require_evidence_min_accepted']} 份"))
                if not missing_types and (criteria["require_evidence_types"] or criteria["require_evidence_min_accepted"] > 0):
                    inclusion.append(reason("evidence_quality_matched", f"产品 {product_code} 证据质量规则满足（已接受 {accepted_total} 份）"))

                # 截止时点前反馈数量
                feedback_sql = "SELECT COUNT(*) FROM public_feedback WHERE product_id=? AND created_at<=?"
                feedback_params: list[Any] = [product["id"], cutoff_text]
                if criteria["audience_types"]:
                    placeholders = ",".join("?" for _ in criteria["audience_types"])
                    feedback_sql += f" AND audience_type IN ({placeholders})"
                    feedback_params.extend(criteria["audience_types"])
                feedback_count = int(self.connection.execute(feedback_sql, tuple(feedback_params)).fetchone()[0])
                if feedback_count < criteria["min_rating_count"]:
                    exclusion.append(reason("feedback_count_below_minimum",
                                            f"产品 {product_code} 截止时点前反馈 {feedback_count} 条，少于 {criteria['min_rating_count']} 条"))

            # 观察指标阈值
            for metric_name, threshold in criteria["min_metric"].items():
                value = metrics.get(metric_name)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    exclusion.append(reason("metric_missing", f"观察指标 {metric_name} 缺失或不是数值"))
                elif float(value) < threshold:
                    exclusion.append(reason("metric_below_threshold",
                                            f"观察指标 {metric_name}={value} 低于阈值 {threshold}"))
            if criteria["min_metric"] and not any(item["code"].startswith("metric_") for item in exclusion):
                inclusion.append(reason("metric_matched", "观察指标阈值全部满足"))

            # 会话状态只作记录，不作为规则（状态在截止时点后可能继续变化）
            context = {
                "project_code": session["project_code"],
                "protocol_code": session["protocol_code"],
                "protocol_version": session["protocol_version"],
                "session_status_now": session["status"],
                "parameters": sanitize_payload(parameters),
                "products": product_snapshots,
                "site": site_snapshot,
                "observation": None if obs_version is None else {
                    "version": obs_version,
                    "digest": obs_digest,
                    "metrics": sanitize_payload(metrics),
                    "site_code": None if site_row is None else site_row["code"],
                },
            }
            members.append({
                "session_id": int(session["id"]),
                "member_ref": self._member_ref(code, int(session["id"])),
                "included": not exclusion,
                "inclusion_reasons": [] if exclusion else inclusion,
                "exclusion_reasons": exclusion,
                "observation_version": obs_version,
                "context": context,
                "evidence_snapshot": evidence_snapshot,
            })
        return members

    @staticmethod
    def _roster_payload(members: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ordered = sorted(members, key=lambda item: (item["member_ref"], item["session_id"]))
        return [{
            "member_ref": item["member_ref"],
            "session_id": item["session_id"],
            "included": item["included"],
            "inclusion_reasons": item["inclusion_reasons"],
            "exclusion_reasons": item["exclusion_reasons"],
            "observation_version": item["observation_version"],
            "context_digest": digest(item["context"]),
            "evidence_snapshot_digest": digest(item["evidence_snapshot"]),
        } for item in ordered]

    def _build_version(
        self,
        *,
        code: str,
        name: str,
        cutoff: datetime,
        criteria: dict[str, Any],
        status: str,
        parent_id: int | None,
        actor: str,
        now: str,
    ) -> dict[str, Any]:
        self._validate_criteria_references(criteria)
        members = self._evaluate(code=code, cutoff=cutoff, criteria=criteria)
        roster = self._roster_payload(members)
        roster_digest = digest(roster)
        included_count = sum(1 for item in members if item["included"])
        version_id = self.repository.insert_version(
            code=code, version=self._next_version(code), name=name, cutoff_at=to_storage(cutoff),
            criteria=criteria, criteria_digest=digest(criteria), status=status, parent_id=parent_id,
            created_by=actor, now=now, roster_digest=roster_digest,
            included_count=included_count, excluded_count=len(members) - included_count,
        )
        self.repository.insert_members(version_id, members, now)
        self.repository.add_event(
            version_id, action="freeze" if status == "frozen" else "propose",
            actor=actor, note=criteria.get("notes", ""), payload={"member_count": len(members)}, now=now,
        )
        return self.get_version(version_id)

    def _next_version(self, code: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS next FROM cohort_versions WHERE code=?", (code,)
        ).fetchone()
        return int(row["next"])

    # ---------- 对外操作 ----------

    def create_freeze(self, payload: dict[str, Any]) -> dict[str, Any]:
        code = payload["code"]
        cutoff = parse_cutoff(payload["cutoff_at"])
        criteria = self.normalize_criteria(payload)
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CohortRepository(connection)
            if repository.latest_version(code) is not None:
                raise ConflictError("队列编码已存在，请对最新签发版本提出候选修订")
            self.repository = repository
            self.connection = connection
            try:
                return self._build_version(
                    code=code, name=payload["name"], cutoff=cutoff, criteria=criteria,
                    status="frozen", parent_id=None, actor=payload["created_by"], now=now,
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("队列编码已存在或被并发创建") from exc

    def propose_revision(self, code: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CohortRepository(connection)
            parent = repository.latest_version(code)
            if parent is None:
                raise NotFoundError("队列不存在，需要先冻结首个版本")
            if repository.proposed_version(code) is not None:
                raise ConflictError("该队列已有待签发的候选修订，请先签发或放弃")
            if "cutoff_at" not in payload:
                raise ValidationError("候选修订必须指定新的截止时点")
            cutoff = parse_cutoff(payload["cutoff_at"])
            if cutoff <= parse_cutoff(parent["cutoff_at"]):
                raise ValidationError("候选修订的截止时点必须晚于上一版截止时点")
            parent_criteria = json.loads(parent["criteria_json"])
            overrides = {key: payload[key] for key in CRITERIA_FIELDS if key in payload and payload[key] is not None}
            criteria = self.normalize_criteria({**parent_criteria, **overrides})
            self.repository = repository
            self.connection = connection
            try:
                version = self._build_version(
                    code=code, name=payload.get("name") or parent["name"], cutoff=cutoff, criteria=criteria,
                    status="proposed", parent_id=int(parent["id"]), actor=payload["created_by"], now=now,
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("该队列已有待签发的候选修订") from exc
            version["diff"] = self.diff(int(version["id"]))
            return version

    def issue_revision(self, code: str, version: int, actor: str, note: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CohortRepository(connection)
            row = repository.version_by_ref(code, version)
            if row is None:
                raise NotFoundError("队列版本不存在")
            if row["status"] != "proposed":
                raise ConflictError("只有候选修订可以签发，已签发版本不可修改")
            before = self._diff_payload(int(row["id"]))
            repository.issue(int(row["id"]), actor, note, now)
            repository.add_event(
                int(row["id"]), action="issue", actor=actor, note=note,
                payload={"added": len(before["added"]), "removed": len(before["removed"])}, now=now,
            )
            return self.get_version(int(row["id"]))

    def discard_revision(self, code: str, version: int, actor: str, note: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = CohortRepository(connection)
            row = repository.version_by_ref(code, version)
            if row is None:
                raise NotFoundError("队列版本不存在")
            if row["status"] != "proposed":
                raise ConflictError("只有候选修订可以放弃")
            # 候选版本允许删除：先去掉子成员与事件（候选清单尚未签发，不构成记录）
            connection.execute("DELETE FROM cohort_members WHERE cohort_version_id=?", (row["id"],))
            connection.execute("DELETE FROM cohort_events WHERE cohort_version_id=?", (row["id"],))
            connection.execute("DELETE FROM cohort_versions WHERE id=? AND status='proposed'", (row["id"],))
            return {"discarded": True, "code": code, "version": version, "actor": actor, "note": note}

    def list_versions(self, code: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_versions(code)

    def get_version(self, version_id: int) -> dict[str, Any]:
        row = self.repository.version_by_id(version_id)
        if row is None:
            raise NotFoundError("队列版本不存在")
        result = dict(row)
        result["criteria"] = json.loads(row["criteria_json"])
        del result["criteria_json"]
        result["verification"] = self.verify(version_id)
        if row["parent_id"]:
            result["diff"] = self.diff(version_id)
        return result

    def get_version_by_ref(self, code: str, version: int | str) -> dict[str, Any]:
        if version == "latest":
            row = self.repository.latest_version(code)
        else:
            row = self.repository.version_by_ref(code, int(version))
        if row is None:
            raise NotFoundError("队列版本不存在")
        return self.get_version(int(row["id"]))

    def list_members(self, code: str, version: int | str, *, included: bool | None = None) -> dict[str, Any]:
        row = self._require_version(code, version)
        members = self.repository.members(int(row["id"]), included=included)
        for member in members:
            member["inclusion_reasons"] = json.loads(member["inclusion_reasons_json"])
            member["exclusion_reasons"] = json.loads(member["exclusion_reasons_json"])
            member["context"] = json.loads(member["context_json"])
            member["evidence_snapshot"] = json.loads(member["evidence_snapshot_json"])
            for internal in (
                "inclusion_reasons_json", "exclusion_reasons_json", "context_json",
                "evidence_snapshot_json", "parameters_json", "requested_by",
                "id", "cohort_version_id", "protocol_id", "capability", "created_at",
            ):
                del member[internal]
        return {"code": code, "version": row["version"], "status": row["status"], "items": members}

    def _require_version(self, code: str, version: int | str) -> sqlite3.Row:
        row = self.repository.latest_version(code) if version == "latest" else self.repository.version_by_ref(code, int(version))
        if row is None:
            raise NotFoundError("队列版本不存在")
        return row

    def _diff_payload(self, version_id: int) -> dict[str, Any]:
        row = self.repository.version_by_id(version_id)
        if row is None:
            raise NotFoundError("队列版本不存在")
        if not row["parent_id"]:
            return {"added": [], "removed": [], "unchanged": [], "criteria_changes": []}
        current_members = self.repository.members(version_id)
        parent_members = self.repository.members(int(row["parent_id"]))
        current_included = {m["session_id"]: m for m in current_members if m["included"]}
        parent_included = {m["session_id"]: m for m in parent_members if m["included"]}
        added_ids = sorted(set(current_included) - set(parent_included))
        removed_ids = sorted(set(parent_included) - set(current_included))
        unchanged_ids = sorted(set(current_included) & set(parent_included))

        def member_brief(member: sqlite3.Row | dict[str, Any], *, removed: bool = False) -> dict[str, Any]:
            data = dict(member)
            brief = {
                "member_ref": data["member_ref"],
                "session_id": data["session_id"],
                "project_code": data.get("project_code"),
                "observation_version": data["observation_version"],
            }
            if removed:
                # 在新版本中被排除时，排除理由来自新版本成员行
                new_row = next((item for item in current_members if item["session_id"] == data["session_id"]), None)
                brief["reasons"] = json.loads(new_row["exclusion_reasons_json"]) if new_row is not None else [
                    reason("out_of_universe", "场次不在新版本截止时点的候选范围内")
                ]
            else:
                brief["reasons"] = json.loads(data["inclusion_reasons_json"])
            return brief

        added = [member_brief(current_included[item]) for item in added_ids]
        removed = [member_brief(parent_included[item], removed=True) for item in removed_ids]
        unchanged = [{"member_ref": current_included[item]["member_ref"], "session_id": item} for item in unchanged_ids]

        parent_criteria = json.loads(self.repository.version_by_id(int(row["parent_id"]))["criteria_json"])
        current_criteria = json.loads(row["criteria_json"])
        changes = []
        for field in CRITERIA_FIELDS:
            if parent_criteria.get(field) != current_criteria.get(field):
                changes.append({"field": field, "from": parent_criteria.get(field), "to": current_criteria.get(field)})
        return {
            "parent_version": self.repository.version_by_id(int(row["parent_id"]))["version"],
            "cutoff_from": self.repository.version_by_id(int(row["parent_id"]))["cutoff_at"],
            "cutoff_to": row["cutoff_at"],
            "added": added,
            "removed": removed,
            "unchanged_count": len(unchanged),
            "criteria_changes": changes,
        }

    def diff(self, version_id: int) -> dict[str, Any]:
        return self._diff_payload(version_id)

    def revision_diff(self, code: str, version: int | str) -> dict[str, Any]:
        row = self._require_version(code, version)
        if not row["parent_id"]:
            return {"code": code, "version": row["version"], "added": [], "removed": [], "unchanged_count": row["included_count"], "criteria_changes": []}
        payload = self._diff_payload(int(row["id"]))
        payload["code"] = code
        payload["version"] = row["version"]
        payload["status"] = row["status"]
        return payload

    def events(self, code: str, version: int | str) -> dict[str, Any]:
        row = self._require_version(code, version)
        return {"code": code, "version": row["version"], "items": self.repository.events(int(row["id"]))}

    def verify(self, version_id: int) -> dict[str, Any]:
        """重算成员清单与规则摘要，与版本保存的摘要核对。"""
        row = self.repository.version_by_id(version_id)
        if row is None:
            raise NotFoundError("队列版本不存在")
        stored_members = self.repository.members(version_id)
        roster = self._roster_payload([
            {
                "member_ref": item["member_ref"],
                "session_id": item["session_id"],
                "included": bool(item["included"]),
                "inclusion_reasons": json.loads(item["inclusion_reasons_json"]),
                "exclusion_reasons": json.loads(item["exclusion_reasons_json"]),
                "observation_version": item["observation_version"],
                "context": json.loads(item["context_json"]),
                "evidence_snapshot": json.loads(item["evidence_snapshot_json"]),
            }
            for item in stored_members
        ])
        recomputed = digest(roster)
        criteria = json.loads(row["criteria_json"])
        return {
            "roster_digest_match": recomputed == row["roster_digest"],
            "criteria_digest_match": digest(criteria) == row["criteria_digest"],
            "roster_digest": row["roster_digest"],
            "recomputed_roster_digest": recomputed,
            "member_count": len(stored_members),
        }

    # ---------- 去标识确定性导出 ----------

    def export_artifact(self, code: str, version: int | str, payload: dict[str, Any]) -> dict[str, Any]:
        row = self._require_version(code, version)
        if row["status"] != "frozen":
            raise ConflictError("候选修订签发前不能生成导出制品")
        include_excluded = bool(payload.get("include_excluded", False))
        idempotency_key = payload["idempotency_key"]
        request_digest = digest({"idempotency_key": idempotency_key, "include_excluded": include_excluded})
        with transaction(immediate=True) as connection:
            repository = CohortRepository(connection)
            existing = repository.artifact_by_key(int(row["id"]), idempotency_key)
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ConflictError("同一导出幂等键不能用于不同的导出请求")
                return {**json.loads(existing["content_json"]), "replayed": True, "artifact_id": existing["id"]}
            content = self._build_artifact(row, include_excluded, idempotency_key, payload["requested_by"])
            artifact_id = repository.insert_artifact(
                int(row["id"]), idempotency_key=idempotency_key, request_digest=request_digest,
                content=content, content_digest=content["content_digest"],
                member_count=content["summary"]["included_count"],
                created_by=payload["requested_by"], now=to_storage(self.clock.now()),
            )
            repository.add_event(
                int(row["id"]), action="export", actor=payload["requested_by"],
                note=f"artifact#{artifact_id}", payload={"include_excluded": include_excluded},
                now=to_storage(self.clock.now()),
            )
            return {**content, "replayed": False, "artifact_id": artifact_id}

    def _build_artifact(self, version_row: sqlite3.Row, include_excluded: bool, idempotency_key: str, requested_by: str) -> dict[str, Any]:
        members = self.repository.members(int(version_row["id"]))
        members.sort(key=lambda item: (item["member_ref"], item["session_id"]))

        def deidentify(row: sqlite3.Row) -> dict[str, Any]:
            context = json.loads(row["context_json"])
            observation = context.get("observation") or {}
            evidence = json.loads(row["evidence_snapshot_json"])
            item = {
                "member_ref": row["member_ref"],
                "session_id": row["session_id"],
                "project_code": context.get("project_code"),
                "protocol_code": context.get("protocol_code"),
                "protocol_version": context.get("protocol_version"),
                "products": [{"code": item["code"], "category": item["category"], "risk_level": item["risk_level"],
                              "regulatory_status": item["regulatory_status"], "active": item["active"]}
                             for item in context.get("products", [])],
                "site": None if context.get("site") is None else {
                    "code": context["site"]["code"], "site_type": context["site"]["site_type"],
                    "region": context["site"]["region"], "status": context["site"]["status"],
                },
                "observation_version": row["observation_version"],
                "observation_digest": observation.get("digest"),
                "metrics": observation.get("metrics", {}),
                "inclusion_reasons": json.loads(row["inclusion_reasons_json"]),
                "evidence_refs": [{"product_code": item["product_code"], "evidence_type": item["evidence_type"],
                                   "version": item["version"], "status_as_of_cutoff": item["status_as_of_cutoff"],
                                   "content_digest": item["content_digest"]} for item in evidence],
            }
            return item

        included = [deidentify(row) for row in members if row["included"]]
        excluded = []
        if include_excluded:
            for row in members:
                if row["included"]:
                    continue
                item = deidentify(row)
                item["exclusion_reasons"] = json.loads(row["exclusion_reasons_json"])
                excluded.append(item)
        criteria = json.loads(version_row["criteria_json"])
        body = {
            "artifact_type": "cohort_export",
            "cohort_code": version_row["code"],
            "cohort_version": version_row["version"],
            "cohort_status": version_row["status"],
            "name": version_row["name"],
            "cutoff_at": version_row["cutoff_at"],
            "criteria_digest": version_row["criteria_digest"],
            "roster_digest": version_row["roster_digest"],
            "criteria": criteria,
            "request": {"idempotency_key": idempotency_key, "include_excluded": include_excluded},
            "generated_by": requested_by,
            "included_members": included,
            "excluded_members": excluded,
            "summary": {
                "included_count": len(included),
                "excluded_count_reported": len(excluded),
                "total_candidates": len(members),
            },
            "digest_algorithm": "sha256 over canonical JSON of this object excluding content_digest",
        }
        body["content_digest"] = digest({key: value for key, value in body.items() if key != "content_digest"})
        return body
