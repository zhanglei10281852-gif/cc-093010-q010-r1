from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

PRODUCT_CATEGORIES = ["康复设备", "辅助诊断", "数字疗法", "慢病管理", "数字中医", "健康消费"]
SITE_TYPES = ["展会体验点", "医院", "康复机构", "研究机构", "产业伙伴"]
EVIDENCE_TYPES = ["临床", "性能", "安全", "合规", "体验"]
SESSION_STATUSES = ["queued", "running", "cancel_requested", "cancelled", "succeeded", "failed"]
REGULATORY_STATUSES = ["展示", "研究", "已注册", "暂停"]


class ParticipationCriteria(BaseModel):
    """参与条件：限定项目、提交方、场次状态与观察记录门槛。"""

    project_codes: list[str] = Field(default_factory=list, max_length=200)
    requested_by: list[str] = Field(default_factory=list, max_length=200)
    session_statuses: list[Literal["queued", "running", "cancel_requested", "cancelled", "succeeded", "failed"]] = Field(
        default_factory=lambda: ["succeeded"]
    )
    min_observation_versions: int = Field(default=1, ge=0, le=1000)
    require_product_link: bool = True


class QualityRules(BaseModel):
    """质量规则：截止时点证据、产品合规状态、场地状态等硬性门槛。"""

    evidence_required: list[Literal["临床", "性能", "安全", "合规", "体验"]] = Field(default_factory=list)
    min_evidence_accepted: int = Field(default=0, ge=0, le=1000)
    accepted_evidence_only: bool = True
    require_active_product: bool = True
    allowed_regulatory_status: list[Literal["展示", "研究", "已注册", "暂停"]] = Field(
        default_factory=lambda: ["展示", "研究", "已注册"]
    )
    require_active_protocol: bool = True
    require_active_site: bool = True


class CohortCriteria(BaseModel):
    """队列冻结规则快照：产品、方案版本、场地类型、参与条件与质量规则。"""

    product_categories: list[Literal["康复设备", "辅助诊断", "数字疗法", "慢病管理", "数字中医", "健康消费"]] = Field(
        default_factory=list
    )
    product_codes: list[str] = Field(default_factory=list, max_length=500)
    protocol_codes: list[str] = Field(default_factory=list, max_length=500)
    protocol_version: int | None = Field(default=None, ge=1)
    site_types: list[Literal["展会体验点", "医院", "康复机构", "研究机构", "产业伙伴"]] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list, max_length=200)
    participation: ParticipationCriteria = Field(default_factory=ParticipationCriteria)
    quality: QualityRules = Field(default_factory=QualityRules)

    @field_validator("product_codes", "protocol_codes")
    @classmethod
    def _normalize_codes(cls, value: list[str]) -> list[str]:
        return sorted({item.strip().lower() for item in value if item.strip()})

    @field_validator("regions")
    @classmethod
    def _strip_regions(cls, value: list[str]) -> list[str]:
        return sorted({item.strip() for item in value if item.strip()})


class CohortCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=160)
    cutoff_at: datetime
    criteria: CohortCriteria = Field(default_factory=CohortCriteria)
    created_by: str = Field(min_length=1, max_length=120)


class CohortRevisionRequest(BaseModel):
    cutoff_at: datetime
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class CohortIssueRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=1000)
    expected_manifest_digest: str | None = Field(default=None, min_length=8, max_length=128)


class CohortDiscardRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=1000)


class CohortExportRequest(BaseModel):
    idempotency_key: str = Field(min_length=6, max_length=160)
