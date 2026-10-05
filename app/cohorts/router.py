from __future__ import annotations

from fastapi import APIRouter

from app.cohorts.schemas import CohortCreate, CohortDiscardRequest, CohortExportRequest, CohortIssueRequest, CohortRevisionRequest
from app.cohorts.service import CohortFreezeService

router = APIRouter(prefix="/api/cohorts", tags=["观察队列冻结"])


def service() -> CohortFreezeService:
    return CohortFreezeService()


@router.post("", status_code=201)
def create_freeze(payload: CohortCreate):
    return service().create_freeze(
        {"code": payload.code, "name": payload.name, "cutoff_at": payload.cutoff_at, "created_by": payload.created_by},
        payload.criteria.model_dump(),
    )


@router.get("")
def list_cohorts():
    return {"items": service().list_cohorts()}


@router.get("/{code}")
def get_cohort(code: str):
    return service().get_cohort(code)


@router.post("/{code}/revisions", status_code=201)
def revise(code: str, payload: CohortRevisionRequest):
    return service().revise(code, payload.model_dump())


@router.get("/{code}/versions/{version_no}")
def get_version(code: str, version_no: int):
    return service().get_version(code, version_no)


@router.post("/{code}/versions/{version_no}/issue")
def issue_version(code: str, version_no: int, payload: CohortIssueRequest):
    return service().issue(code, version_no, payload.model_dump())


@router.post("/{code}/versions/{version_no}/discard")
def discard_candidate(code: str, version_no: int, payload: CohortDiscardRequest):
    return service().discard_candidate(code, version_no, payload.model_dump())


@router.get("/{code}/versions/{version_no}/verify")
def verify_version(code: str, version_no: int):
    return service().verify_version(code, version_no)


@router.post("/{code}/versions/{version_no}/export")
def export_version(code: str, version_no: int, payload: CohortExportRequest):
    return service().export_version(code, version_no, payload.idempotency_key)
