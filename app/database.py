from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.core.clock import to_storage, utc_now


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "health-innovation.db"
_local = threading.local()


SCHEMA = r'''
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    manager TEXT NOT NULL,
    phone TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    department_id INTEGER REFERENCES departments(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled','locked')),
    failed_login_count INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    password_changed_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    is_system INTEGER NOT NULL DEFAULT 0 CHECK(is_system IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS permissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    resource TEXT NOT NULL,
    action TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS role_permissions (
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    permission_id INTEGER NOT NULL REFERENCES permissions(id) ON DELETE CASCADE,
    granted_at TEXT NOT NULL,
    PRIMARY KEY(role_id, permission_id)
);
CREATE TABLE IF NOT EXISTS user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    assigned_by INTEGER REFERENCES users(id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY(user_id, role_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_digest TEXT NOT NULL UNIQUE,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT,
    revoke_reason TEXT,
    client_label TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id),
    actor_name TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('success','denied','failure')),
    before_json TEXT,
    after_json TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    correlation_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at DESC);
CREATE TABLE IF NOT EXISTS idempotency_records (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_json TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);
CREATE TABLE IF NOT EXISTS department_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    department_id INTEGER NOT NULL REFERENCES departments(id),
    title TEXT NOT NULL DEFAULT '',
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, department_id, starts_at)
);
CREATE TABLE IF NOT EXISTS background_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    deduplication_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','running','completed','failed','cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at TEXT NOT NULL,
    locked_at TEXT,
    locked_by TEXT,
    result_json TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_ready ON background_jobs(status,available_at);

CREATE TABLE IF NOT EXISTS health_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    organization TEXT NOT NULL,
    origin_country TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('康复设备','辅助诊断','数字疗法','慢病管理','数字中医','健康消费')),
    intended_use TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK(risk_level IN ('low','medium','high')),
    regulatory_status TEXT NOT NULL DEFAULT '展示' CHECK(regulatory_status IN ('展示','研究','已注册','暂停')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    site_type TEXT NOT NULL CHECK(site_type IN ('展会体验点','医院','康复机构','研究机构','产业伙伴')),
    region TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    max_concurrent INTEGER NOT NULL DEFAULT 1 CHECK(max_concurrent > 0),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','suspended','closed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES health_products(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL CHECK(evidence_type IN ('临床','性能','安全','合规','体验')),
    title TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_region TEXT NOT NULL,
    version TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    summary_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'submitted' CHECK(status IN ('submitted','accepted','rejected','superseded')),
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    UNIQUE(product_id, evidence_type, version, content_digest)
);
CREATE TABLE IF NOT EXISTS public_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES health_products(id) ON DELETE CASCADE,
    site_id INTEGER NOT NULL REFERENCES pilot_sites(id) ON DELETE CASCADE,
    session_reference TEXT NOT NULL,
    audience_type TEXT NOT NULL CHECK(audience_type IN ('公众','临床人员','采购商','产业伙伴')),
    rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
    tags_json TEXT NOT NULL DEFAULT '[]',
    comment TEXT NOT NULL DEFAULT '',
    contact_digest TEXT NOT NULL DEFAULT '',
    consent_to_follow_up INTEGER NOT NULL DEFAULT 0 CHECK(consent_to_follow_up IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, session_reference, audience_type, contact_digest)
);

CREATE TABLE IF NOT EXISTS pilot_protocols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    capability TEXT NOT NULL,
    product_id INTEGER REFERENCES health_products(id),
    version INTEGER NOT NULL DEFAULT 1,
    parameter_schema_json TEXT NOT NULL,
    default_parameters_json TEXT NOT NULL DEFAULT '{}',
    max_runtime_seconds INTEGER NOT NULL CHECK(max_runtime_seconds > 0),
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pilot_quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL CHECK(subject_type IN ('user','role','project')),
    subject_key TEXT NOT NULL,
    max_queued INTEGER NOT NULL CHECK(max_queued >= 0),
    max_running INTEGER NOT NULL CHECK(max_running >= 0),
    daily_submissions INTEGER NOT NULL CHECK(daily_submissions >= 0),
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(subject_type, subject_key)
);
CREATE TABLE IF NOT EXISTS pilot_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id INTEGER NOT NULL REFERENCES pilot_protocols(id) ON DELETE RESTRICT,
    project_code TEXT NOT NULL,
    requested_by TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    parameter_digest TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50 CHECK(priority BETWEEN 0 AND 100),
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','cancel_requested','cancelled','succeeded','failed')),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
    available_at TEXT NOT NULL,
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT NOT NULL DEFAULT '',
    current_observation_version INTEGER,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_message TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    started_at TEXT,
    finished_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requested_by, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_pilot_queue ON pilot_sessions(status,priority DESC,available_at,created_at);
CREATE TABLE IF NOT EXISTS pilot_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    observation_json TEXT NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    observation_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, version)
);
CREATE TABLE IF NOT EXISTS pilot_interventions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES pilot_sessions(id) ON DELETE CASCADE,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    batch_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pilot_interventions ON pilot_interventions(session_id,id);

CREATE TABLE IF NOT EXISTS cohort_freezes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    criteria_json TEXT NOT NULL,
    rule_digest TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cohort_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cohort_id INTEGER NOT NULL REFERENCES cohort_freezes(id) ON DELETE RESTRICT,
    version_no INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','issued')),
    cutoff_at TEXT NOT NULL,
    manifest_digest TEXT NOT NULL DEFAULT '',
    member_total INTEGER NOT NULL DEFAULT 0,
    included_total INTEGER NOT NULL DEFAULT 0,
    excluded_total INTEGER NOT NULL DEFAULT 0,
    warnings_json TEXT NOT NULL DEFAULT '[]',
    diff_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    issued_by TEXT,
    issued_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(cohort_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_cohort_versions_cohort ON cohort_versions(cohort_id,version_no);
CREATE TABLE IF NOT EXISTS cohort_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES cohort_versions(id) ON DELETE RESTRICT,
    ordinal INTEGER NOT NULL,
    session_id INTEGER NOT NULL,
    included INTEGER NOT NULL CHECK(included IN (0,1)),
    decision_reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    member_digest TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(version_id, session_id),
    UNIQUE(version_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_cohort_members_session ON cohort_members(session_id);
CREATE TABLE IF NOT EXISTS cohort_entity_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_table TEXT NOT NULL,
    entity_id INTEGER NOT NULL,
    changed_at TEXT NOT NULL,
    state_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cohort_snap_lookup ON cohort_entity_snapshots(entity_table,entity_id,changed_at,id);

DROP TRIGGER IF EXISTS trg_cohort_snap_products_ins;
DROP TRIGGER IF EXISTS trg_cohort_snap_products_upd;
DROP TRIGGER IF EXISTS trg_cohort_snap_sites_ins;
DROP TRIGGER IF EXISTS trg_cohort_snap_sites_upd;
DROP TRIGGER IF EXISTS trg_cohort_snap_evidence_ins;
DROP TRIGGER IF EXISTS trg_cohort_snap_evidence_upd;
DROP TRIGGER IF EXISTS trg_cohort_snap_protocols_ins;
DROP TRIGGER IF EXISTS trg_cohort_snap_protocols_upd;
DROP TRIGGER IF EXISTS trg_cohort_snap_sessions_ins;
DROP TRIGGER IF EXISTS trg_cohort_snap_sessions_upd;
DROP TRIGGER IF EXISTS trg_cohort_version_issued_no_update;
DROP TRIGGER IF EXISTS trg_cohort_version_issued_no_delete;
DROP TRIGGER IF EXISTS trg_cohort_members_issued_no_update;
DROP TRIGGER IF EXISTS trg_cohort_members_issued_no_delete;
CREATE TRIGGER IF NOT EXISTS trg_cohort_snap_products_ins AFTER INSERT ON health_products
BEGIN
    INSERT INTO cohort_entity_snapshots(entity_table,entity_id,changed_at,state_json)
    VALUES('health_products',NEW.id,NEW.created_at,json_object(
        'id',NEW.id,'code',NEW.code,'name',NEW.name,'organization',NEW.organization,
        'origin_country',NEW.origin_country,'category',NEW.category,'intended_use',NEW.intended_use,
        'risk_level',NEW.risk_level,'regulatory_status',NEW.regulatory_status,'active',NEW.active,
        'created_at',NEW.created_at,'updated_at',NEW.updated_at));
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_snap_products_upd AFTER UPDATE ON health_products
BEGIN
    INSERT INTO cohort_entity_snapshots(entity_table,entity_id,changed_at,state_json)
    VALUES('health_products',NEW.id,NEW.updated_at,json_object(
        'id',NEW.id,'code',NEW.code,'name',NEW.name,'organization',NEW.organization,
        'origin_country',NEW.origin_country,'category',NEW.category,'intended_use',NEW.intended_use,
        'risk_level',NEW.risk_level,'regulatory_status',NEW.regulatory_status,'active',NEW.active,
        'created_at',NEW.created_at,'updated_at',NEW.updated_at));
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_snap_sites_ins AFTER INSERT ON pilot_sites
BEGIN
    INSERT INTO cohort_entity_snapshots(entity_table,entity_id,changed_at,state_json)
    VALUES('pilot_sites',NEW.id,NEW.created_at,json_object(
        'id',NEW.id,'code',NEW.code,'name',NEW.name,'site_type',NEW.site_type,'region',NEW.region,
        'capabilities_json',NEW.capabilities_json,'max_concurrent',NEW.max_concurrent,'status',NEW.status,
        'created_at',NEW.created_at,'updated_at',NEW.updated_at));
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_snap_sites_upd AFTER UPDATE ON pilot_sites
BEGIN
    INSERT INTO cohort_entity_snapshots(entity_table,entity_id,changed_at,state_json)
    VALUES('pilot_sites',NEW.id,NEW.updated_at,json_object(
        'id',NEW.id,'code',NEW.code,'name',NEW.name,'site_type',NEW.site_type,'region',NEW.region,
        'capabilities_json',NEW.capabilities_json,'max_concurrent',NEW.max_concurrent,'status',NEW.status,
        'created_at',NEW.created_at,'updated_at',NEW.updated_at));
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_snap_evidence_ins AFTER INSERT ON evidence_documents
BEGIN
    INSERT INTO cohort_entity_snapshots(entity_table,entity_id,changed_at,state_json)
    VALUES('evidence_documents',NEW.id,NEW.submitted_at,json_object(
        'id',NEW.id,'product_id',NEW.product_id,'evidence_type',NEW.evidence_type,'title',NEW.title,
        'source_name',NEW.source_name,'source_region',NEW.source_region,'version',NEW.version,
        'content_digest',NEW.content_digest,'summary_json',NEW.summary_json,'status',NEW.status,
        'submitted_by',NEW.submitted_by,'submitted_at',NEW.submitted_at,'reviewed_by',NEW.reviewed_by,'reviewed_at',NEW.reviewed_at));
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_snap_evidence_upd AFTER UPDATE ON evidence_documents
BEGIN
    INSERT INTO cohort_entity_snapshots(entity_table,entity_id,changed_at,state_json)
    VALUES('evidence_documents',NEW.id,COALESCE(NEW.reviewed_at,NEW.submitted_at),json_object(
        'id',NEW.id,'product_id',NEW.product_id,'evidence_type',NEW.evidence_type,'title',NEW.title,
        'source_name',NEW.source_name,'source_region',NEW.source_region,'version',NEW.version,
        'content_digest',NEW.content_digest,'summary_json',NEW.summary_json,'status',NEW.status,
        'submitted_by',NEW.submitted_by,'submitted_at',NEW.submitted_at,'reviewed_by',NEW.reviewed_by,'reviewed_at',NEW.reviewed_at));
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_snap_protocols_ins AFTER INSERT ON pilot_protocols
BEGIN
    INSERT INTO cohort_entity_snapshots(entity_table,entity_id,changed_at,state_json)
    VALUES('pilot_protocols',NEW.id,NEW.created_at,json_object(
        'id',NEW.id,'code',NEW.code,'name',NEW.name,'capability',NEW.capability,'product_id',NEW.product_id,'version',NEW.version,
        'parameter_schema_json',NEW.parameter_schema_json,'default_parameters_json',NEW.default_parameters_json,
        'max_runtime_seconds',NEW.max_runtime_seconds,'max_attempts',NEW.max_attempts,'active',NEW.active,
        'created_by',NEW.created_by,'created_at',NEW.created_at,'updated_at',NEW.updated_at));
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_snap_protocols_upd AFTER UPDATE ON pilot_protocols
BEGIN
    INSERT INTO cohort_entity_snapshots(entity_table,entity_id,changed_at,state_json)
    VALUES('pilot_protocols',NEW.id,NEW.updated_at,json_object(
        'id',NEW.id,'code',NEW.code,'name',NEW.name,'capability',NEW.capability,'product_id',NEW.product_id,'version',NEW.version,
        'parameter_schema_json',NEW.parameter_schema_json,'default_parameters_json',NEW.default_parameters_json,
        'max_runtime_seconds',NEW.max_runtime_seconds,'max_attempts',NEW.max_attempts,'active',NEW.active,
        'created_by',NEW.created_by,'created_at',NEW.created_at,'updated_at',NEW.updated_at));
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_snap_sessions_ins AFTER INSERT ON pilot_sessions
BEGIN
    INSERT INTO cohort_entity_snapshots(entity_table,entity_id,changed_at,state_json)
    VALUES('pilot_sessions',NEW.id,NEW.created_at,json_object(
        'id',NEW.id,'protocol_id',NEW.protocol_id,'project_code',NEW.project_code,'requested_by',NEW.requested_by,
        'parameters_json',NEW.parameters_json,'parameter_digest',NEW.parameter_digest,'priority',NEW.priority,
        'idempotency_key',NEW.idempotency_key,'status',NEW.status,'attempt_count',NEW.attempt_count,
        'max_attempts',NEW.max_attempts,'available_at',NEW.available_at,'lease_owner',NEW.lease_owner,
        'lease_expires_at',NEW.lease_expires_at,'current_observation_version',NEW.current_observation_version,
        'last_error_code',NEW.last_error_code,'last_error_message',NEW.last_error_message,'version',NEW.version,
        'started_at',NEW.started_at,'finished_at',NEW.finished_at,'created_at',NEW.created_at,'updated_at',NEW.updated_at));
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_snap_sessions_upd AFTER UPDATE ON pilot_sessions
BEGIN
    INSERT INTO cohort_entity_snapshots(entity_table,entity_id,changed_at,state_json)
    VALUES('pilot_sessions',NEW.id,NEW.updated_at,json_object(
        'id',NEW.id,'protocol_id',NEW.protocol_id,'project_code',NEW.project_code,'requested_by',NEW.requested_by,
        'parameters_json',NEW.parameters_json,'parameter_digest',NEW.parameter_digest,'priority',NEW.priority,
        'idempotency_key',NEW.idempotency_key,'status',NEW.status,'attempt_count',NEW.attempt_count,
        'max_attempts',NEW.max_attempts,'available_at',NEW.available_at,'lease_owner',NEW.lease_owner,
        'lease_expires_at',NEW.lease_expires_at,'current_observation_version',NEW.current_observation_version,
        'last_error_code',NEW.last_error_code,'last_error_message',NEW.last_error_message,'version',NEW.version,
        'started_at',NEW.started_at,'finished_at',NEW.finished_at,'created_at',NEW.created_at,'updated_at',NEW.updated_at));
END;

CREATE TRIGGER IF NOT EXISTS trg_cohort_version_issued_no_update
BEFORE UPDATE ON cohort_versions
WHEN OLD.status='issued'
BEGIN
    SELECT RAISE(ABORT,'issued cohort version is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_version_candidate_fixed_fields
BEFORE UPDATE ON cohort_versions
WHEN OLD.status='candidate' AND (
    NEW.cutoff_at<>OLD.cutoff_at OR NEW.manifest_digest<>OLD.manifest_digest
    OR NEW.version_no<>OLD.version_no OR NEW.cohort_id<>OLD.cohort_id
    OR NEW.member_total<>OLD.member_total OR NEW.included_total<>OLD.included_total
    OR NEW.excluded_total<>OLD.excluded_total
)
BEGIN
    SELECT RAISE(ABORT,'cohort candidate core fields are fixed');
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_version_issued_no_delete
BEFORE DELETE ON cohort_versions
WHEN OLD.status='issued'
BEGIN
    SELECT RAISE(ABORT,'issued cohort version is immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_members_issued_no_update
BEFORE UPDATE ON cohort_members
BEGIN
    SELECT RAISE(ABORT,'cohort members are write-once');
END;
CREATE TRIGGER IF NOT EXISTS trg_cohort_members_issued_no_delete
BEFORE DELETE ON cohort_members
WHEN EXISTS(SELECT 1 FROM cohort_versions v WHERE v.id=OLD.version_id AND v.status='issued')
BEGIN
    SELECT RAISE(ABORT,'issued cohort members are immutable');
END;
'''


PERMISSIONS = [
    ("users.read", "查看用户", "users", "read"),
    ("users.write", "维护用户", "users", "write"),
    ("roles.read", "查看角色", "roles", "read"),
    ("roles.write", "维护角色", "roles", "write"),
    ("departments.read", "查看协作团队", "departments", "read"),
    ("departments.write", "维护协作团队", "departments", "write"),
    ("catalog.read", "查看健康创新目录", "catalog", "read"),
    ("catalog.write", "维护健康创新目录", "catalog", "write"),
    ("evidence.review", "审阅产品证据", "evidence", "review"),
    ("feedback.read", "查看体验反馈", "feedback", "read"),
    ("audit.read", "查看审计", "audit", "read"),
    ("jobs.run", "执行后台任务", "jobs", "run"),
    ("cohorts.read", "查看队列冻结", "cohorts", "read"),
    ("cohorts.freeze", "创建与修订队列冻结", "cohorts", "freeze"),
    ("cohorts.issue", "签发队列版本", "cohorts", "issue"),
    ("cohorts.export", "导出队列制品", "cohorts", "export"),
]


def database_path() -> Path:
    raw = os.getenv("HEALTH_INNOVATION_DATABASE_PATH", str(DEFAULT_DB_PATH))
    return Path(raw).expanduser().resolve()


def _create_connection() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    return connection


def get_connection() -> sqlite3.Connection:
    connection = getattr(_local, "connection", None)
    if connection is None:
        connection = _create_connection()
        _local.connection = connection
    return connection


def close_connection() -> None:
    connection = getattr(_local, "connection", None)
    if connection is not None:
        connection.close()
        _local.connection = None


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    connection = get_connection()
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except Exception:
        connection.rollback()
        raise
    else:
        connection.commit()


def init_db() -> None:
    now = to_storage(utc_now())
    with transaction(immediate=True) as connection:
        connection.executescript(SCHEMA)
        connection.execute("PRAGMA user_version=3")
        _ensure_column(connection, "pilot_protocols", "product_id", "INTEGER REFERENCES health_products(id)")
        backfill_entity_snapshots(connection)
        for code, name, resource, action in PERMISSIONS:
            connection.execute(
                "INSERT OR IGNORE INTO permissions(code,name,resource,action) VALUES(?,?,?,?)",
                (code, name, resource, action),
            )
        roles = [
            ("administrator", "系统管理员", "拥有全部系统权限"),
            ("operator", "试点运营员", "维护目录、场地和体验场次"),
            ("reviewer", "证据审阅员", "审阅产品证据与体验反馈"),
            ("auditor", "审计查看员", "只读查看运行与审计记录"),
        ]
        for code, name, description in roles:
            connection.execute(
                "INSERT OR IGNORE INTO roles(code,name,description,is_system,created_at,updated_at) VALUES(?,?,?,1,?,?)",
                (code, name, description, now, now),
            )
        administrator = connection.execute("SELECT id FROM roles WHERE code='administrator'").fetchone()[0]
        connection.execute(
            "INSERT OR IGNORE INTO role_permissions(role_id,permission_id,granted_at) SELECT ?,id,? FROM permissions",
            (administrator, now),
        )


def migrate_db() -> None:
    init_db()


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def backfill_entity_snapshots(connection: sqlite3.Connection) -> None:
    """为触发器启用前已经存在的实体补建首条快照，已有快照的实体跳过。"""
    entities = [
        ("health_products", "created_at", (
            "id,code,name,organization,origin_country,category,intended_use,"
            "risk_level,regulatory_status,active,created_at,updated_at"
        )),
        ("pilot_sites", "created_at", (
            "id,code,name,site_type,region,capabilities_json,max_concurrent,status,created_at,updated_at"
        )),
        ("evidence_documents", "submitted_at", (
            "id,product_id,evidence_type,title,source_name,source_region,version,content_digest,"
            "summary_json,status,submitted_by,submitted_at,reviewed_by,reviewed_at"
        )),
        ("pilot_protocols", "created_at", (
            "id,code,name,capability,product_id,version,parameter_schema_json,default_parameters_json,"
            "max_runtime_seconds,max_attempts,active,created_by,created_at,updated_at"
        )),
        ("pilot_sessions", "created_at", (
            "id,protocol_id,project_code,requested_by,parameters_json,parameter_digest,priority,"
            "idempotency_key,status,attempt_count,max_attempts,available_at,lease_owner,lease_expires_at,"
            "current_observation_version,last_error_code,last_error_message,version,started_at,finished_at,"
            "created_at,updated_at"
        )),
    ]
    for table, timestamp_column, columns in entities:
        existing = {
            int(row[0])
            for row in connection.execute(
                "SELECT DISTINCT entity_id FROM cohort_entity_snapshots WHERE entity_table=?", (table,)
            ).fetchall()
        }
        rows = connection.execute(f"SELECT {columns} FROM {table} ORDER BY id").fetchall()
        for row in rows:
            if int(row["id"]) in existing:
                continue
            state = {key: row[key] for key in row.keys()}
            connection.execute(
                "INSERT INTO cohort_entity_snapshots(entity_table,entity_id,changed_at,state_json) VALUES(?,?,?,?)",
                (table, int(row["id"]), row[timestamp_column], json.dumps(state, ensure_ascii=False, sort_keys=True)),
            )
