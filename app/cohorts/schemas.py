from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


RiskLevel = Literal["low", "medium", "high"]
SiteType = Literal["展会体验点", "医院", "康复机构", "研究机构", "产业伙伴"]


class CohortCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    cutoff_at: str = Field(min_length=8, max_length=40, description="截止时点（ISO 8601，含时区或 Z）")
    created_by: str = Field(min_length=1, max_length=120)
    product_codes: list[str] = Field(default_factory=list, max_length=200)
    protocol_codes: list[str] = Field(default_factory=list, max_length=200)
    site_types: list[SiteType] = Field(default_factory=list, max_length=20)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    categories: list[str] = Field(default_factory=list, max_length=20)
    risk_levels: list[RiskLevel] = Field(default_factory=list, max_length=10)
    project_codes: list[str] = Field(default_factory=list, max_length=100)
    require_product_active: bool = True
    require_product_registered: bool = False
    require_observation: bool = True
    require_evidence_types: list[str] = Field(default_factory=list, max_length=20)
    require_evidence_min_accepted: int = Field(default=0, ge=0, le=100)
    min_rating_count: int = Field(default=0, ge=0, le=100000)
    min_metric: dict[str, float] = Field(default_factory=dict, max_length=50)
    audience_types: list[str] = Field(default_factory=list, max_length=10)
    notes: str = Field(default="", max_length=2000)

    @field_validator("cutoff_at")
    @classmethod
    def _parse_cutoff(cls, value: str) -> str:
        from app.cohorts.service import parse_cutoff

        parse_cutoff(value)
        return value

    @field_validator(
        "product_codes",
        "protocol_codes",
        "site_types",
        "capabilities",
        "categories",
        "risk_levels",
        "project_codes",
        "require_evidence_types",
        "audience_types",
    )
    @classmethod
    def _deduplicate(cls, value: list) -> list:
        return list(dict.fromkeys(value))


class CohortIssueRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    note: str = Field(default="", max_length=2000)


class CohortRevisionRequest(BaseModel):
    cutoff_at: str = Field(min_length=8, max_length=40)
    created_by: str = Field(min_length=1, max_length=120)
    name: str | None = Field(default=None, min_length=2, max_length=120)
    product_codes: list[str] | None = Field(default=None, max_length=200)
    protocol_codes: list[str] | None = Field(default=None, max_length=200)
    site_types: list[SiteType] | None = Field(default=None, max_length=20)
    capabilities: list[str] | None = Field(default=None, max_length=100)
    categories: list[str] | None = Field(default=None, max_length=20)
    risk_levels: list[RiskLevel] | None = Field(default=None, max_length=10)
    project_codes: list[str] | None = Field(default=None, max_length=100)
    require_product_active: bool | None = None
    require_product_registered: bool | None = None
    require_observation: bool | None = None
    require_evidence_types: list[str] | None = Field(default=None, max_length=20)
    require_evidence_min_accepted: int | None = Field(default=None, ge=0, le=100)
    min_rating_count: int | None = Field(default=None, ge=0, le=100000)
    min_metric: dict[str, float] | None = Field(default=None, max_length=50)
    audience_types: list[str] | None = Field(default=None, max_length=10)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("cutoff_at")
    @classmethod
    def _parse_cutoff(cls, value: str) -> str:
        from app.cohorts.service import parse_cutoff

        parse_cutoff(value)
        return value


class CohortExportRequest(BaseModel):
    requested_by: str = Field(min_length=1, max_length=120)
    include_excluded: bool = False
    idempotency_key: str = Field(min_length=6, max_length=160)
