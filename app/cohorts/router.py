from __future__ import annotations

from fastapi import APIRouter, Query

from app.cohorts.schemas import CohortCreate, CohortExportRequest, CohortIssueRequest, CohortRevisionRequest
from app.cohorts.service import CohortFreezeService


router = APIRouter(prefix="/api/cohorts", tags=["队列冻结"])


def service() -> CohortFreezeService:
    return CohortFreezeService()


@router.post("", status_code=201)
def create_cohort(payload: CohortCreate):
    return service().create_freeze(payload.model_dump())


@router.get("")
def list_cohorts(code: str | None = None):
    return {"items": service().list_versions(code)}


@router.get("/{code}/versions/{version}")
def get_cohort_version(code: str, version: str):
    return service().get_version_by_ref(code, version)


@router.get("/{code}/versions/{version}/members")
def list_cohort_members(
    code: str,
    version: str,
    included: bool | None = Query(default=None),
):
    return service().list_members(code, version, included=included)


@router.get("/{code}/versions/{version}/diff")
def cohort_diff(code: str, version: str):
    return service().revision_diff(code, version)


@router.get("/{code}/versions/{version}/events")
def cohort_events(code: str, version: str):
    return service().events(code, version)


@router.post("/{code}/revisions", status_code=201)
def propose_revision(code: str, payload: CohortRevisionRequest):
    return service().propose_revision(code, payload.model_dump(exclude_unset=True))


@router.post("/{code}/versions/{version}/issue")
def issue_revision(code: str, version: int, payload: CohortIssueRequest):
    return service().issue_revision(code, version, payload.actor, payload.note)


@router.post("/{code}/versions/{version}/discard")
def discard_revision(code: str, version: int, payload: CohortIssueRequest):
    return service().discard_revision(code, version, payload.actor, payload.note)


@router.post("/{code}/versions/{version}/exports", status_code=201)
def export_cohort(code: str, version: str, payload: CohortExportRequest):
    return service().export_artifact(code, version, payload.model_dump())
