from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any


DB_DIR = Path(__file__).resolve().parent
LOCAL_DB_PATH = DB_DIR / "local.db"
SERVER_DB_PATH = DB_DIR / "server.db"


def open_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def open_local_connection() -> sqlite3.Connection:
    return open_connection(LOCAL_DB_PATH)


def open_server_connection() -> sqlite3.Connection:
    return open_connection(SERVER_DB_PATH)


def init_db() -> None:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    with open_local_connection() as local_conn:
        create_schema(local_conn)
        local_conn.commit()
    with open_server_connection() as server_conn:
        create_schema(server_conn)
        server_conn.commit()


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs(
            job_id TEXT PRIMARY KEY,
            created_ts TEXT,
            updated_ts TEXT,
            status TEXT,
            field_payload_json TEXT,
            final_response_json TEXT,
            requires_approval INTEGER,
            approved_by TEXT,
            approved_ts TEXT,
            guided_question TEXT,
            guided_answer TEXT,
            approval_due_ts TEXT,
            timed_out INTEGER DEFAULT 0,
            first_occurrence_fault INTEGER DEFAULT 0,
            assigned_tech_id TEXT,
            workflow_mode TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS decision_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            job_id TEXT,
            agent_id TEXT,
            action TEXT,
            input_json TEXT,
            output_json TEXT,
            confidence REAL,
            requires_human INTEGER
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sync_queue(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            entity TEXT,
            entity_id TEXT,
            payload_json TEXT,
            synced INTEGER DEFAULT 0,
            retry_count INTEGER DEFAULT 0,
            last_error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_steps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            agent_id TEXT,
            created_ts TEXT,
            step_order INTEGER,
            step_id TEXT,
            title TEXT,
            instructions TEXT,
            required_inputs_json TEXT,
            pass_criteria_json TEXT,
            recommended_parts_json TEXT,
            risk_level TEXT,
            step_kind TEXT,
            suppressed INTEGER DEFAULT 0,
            status TEXT,
            UNIQUE(job_id, step_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            job_id TEXT,
            step_id TEXT,
            actor_id TEXT,
            event_type TEXT,
            input_json TEXT,
            output_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_metrics_daily(
            day TEXT,
            agent_id TEXT,
            jobs_processed INTEGER DEFAULT 0,
            escalations INTEGER DEFAULT 0,
            approvals INTEGER DEFAULT 0,
            denials INTEGER DEFAULT 0,
            replans INTEGER DEFAULT 0,
            mean_confidence REAL DEFAULT 0.0,
            sample_count INTEGER DEFAULT 0,
            PRIMARY KEY(day, agent_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS supervisor_alerts(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT,
            job_id TEXT,
            alert_type TEXT,
            payload_json TEXT,
            acknowledged INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_records(
            issue_id TEXT PRIMARY KEY,
            job_id TEXT UNIQUE,
            created_ts TEXT,
            updated_ts TEXT,
            status TEXT,
            workflow_mode TEXT,
            equipment_id TEXT,
            fault_code TEXT,
            issue_text TEXT,
            symptoms TEXT,
            notes TEXT,
            location TEXT,
            requires_approval INTEGER,
            escalation_reasons_json TEXT,
            tags_json TEXT,
            attachment_count INTEGER DEFAULT 0,
            latest_attachment_ts TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_issue_records_status_updated
        ON issue_records(status, updated_ts)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_issue_records_equipment_fault
        ON issue_records(equipment_id, fault_code)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_issue_records_location
        ON issue_records(location)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_issue_records_updated
        ON issue_records(updated_ts)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS issue_attachments(
            attachment_id TEXT PRIMARY KEY,
            job_id TEXT,
            step_id TEXT,
            created_ts TEXT,
            captured_ts TEXT,
            source TEXT,
            filename TEXT,
            mime_type TEXT,
            byte_size INTEGER,
            sha256 TEXT,
            caption TEXT,
            local_rel_path TEXT,
            server_rel_path TEXT,
            sync_state TEXT,
            sync_error TEXT,
            vision_features_json TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_issue_attachments_job_created
        ON issue_attachments(job_id, created_ts)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_issue_attachments_step_id
        ON issue_attachments(step_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_issue_attachments_sha256
        ON issue_attachments(sha256)
        """
    )
    _ensure_column(conn, "jobs", "guided_question", "TEXT")
    _ensure_column(conn, "jobs", "guided_answer", "TEXT")
    _ensure_column(conn, "jobs", "approval_due_ts", "TEXT")
    _ensure_column(conn, "jobs", "timed_out", "INTEGER DEFAULT 0")
    _ensure_column(conn, "jobs", "first_occurrence_fault", "INTEGER DEFAULT 0")
    _ensure_column(conn, "jobs", "assigned_tech_id", "TEXT")
    _ensure_column(conn, "jobs", "workflow_mode", "TEXT")
    _ensure_column(conn, "sync_queue", "retry_count", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sync_queue", "last_error", "TEXT")
    _ensure_column(conn, "workflow_steps", "recommended_parts_json", "TEXT")
    _ensure_column(conn, "workflow_steps", "step_kind", "TEXT")
    _ensure_column(conn, "workflow_steps", "suppressed", "INTEGER DEFAULT 0")
    _ensure_column(conn, "issue_records", "attachment_count", "INTEGER DEFAULT 0")
    _ensure_column(conn, "issue_records", "latest_attachment_ts", "TEXT")
    _ensure_column(conn, "issue_attachments", "captured_ts", "TEXT")
    _ensure_column(conn, "issue_attachments", "source", "TEXT")
    _ensure_column(conn, "issue_attachments", "server_rel_path", "TEXT")
    _ensure_column(conn, "issue_attachments", "sync_state", "TEXT")
    _ensure_column(conn, "issue_attachments", "sync_error", "TEXT")
    _ensure_column(conn, "issue_attachments", "vision_features_json", "TEXT")


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, column_def: str) -> None:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    existing_columns = {str(row["name"]) for row in rows}
    if column_name in existing_columns:
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")


def _to_json(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _clamp_confidence(confidence: float) -> float:
    return max(0.0, min(1.0, float(confidence)))


def _tokenize_tags(*values: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for value in values:
        for token in re.findall(r"[a-zA-Z0-9_-]+", str(value or "").lower()):
            if len(token) < 3:
                continue
            if token in seen:
                continue
            seen.add(token)
            tokens.append(token)
    return tokens[:40]


def _build_issue_record_from_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = _parse_json(job.get("field_payload_json")) or {}
    final = _parse_json(job.get("final_response_json")) or {}
    escalation_reasons = final.get("escalation_reasons", [])
    if not isinstance(escalation_reasons, list):
        escalation_reasons = []

    tags = _tokenize_tags(
        str(payload.get("fault_code", "")),
        str(payload.get("equipment_id", "")),
        str(payload.get("issue_text", "")),
        str(payload.get("symptoms", "")),
        str(payload.get("notes", "")),
        " ".join(str(item) for item in escalation_reasons),
    )

    return {
        "issue_id": str(job.get("job_id", "")),
        "job_id": str(job.get("job_id", "")),
        "created_ts": job.get("created_ts"),
        "updated_ts": job.get("updated_ts"),
        "status": job.get("status"),
        "workflow_mode": job.get("workflow_mode") or final.get("workflow_mode"),
        "equipment_id": payload.get("equipment_id"),
        "fault_code": payload.get("fault_code"),
        "issue_text": payload.get("issue_text"),
        "symptoms": payload.get("symptoms"),
        "notes": payload.get("notes"),
        "location": payload.get("location"),
        "requires_approval": int(job.get("requires_approval", 0)),
        "escalation_reasons_json": escalation_reasons,
        "tags_json": tags,
    }


def upsert_issue_record(conn: sqlite3.Connection, record: dict[str, Any]) -> None:
    existing = conn.execute(
        "SELECT attachment_count, latest_attachment_ts FROM issue_records WHERE issue_id = ?",
        (record["issue_id"],),
    ).fetchone()
    attachment_count = int(existing["attachment_count"]) if existing else int(record.get("attachment_count", 0) or 0)
    latest_attachment_ts = existing["latest_attachment_ts"] if existing else record.get("latest_attachment_ts")

    conn.execute(
        """
        INSERT INTO issue_records(
            issue_id, job_id, created_ts, updated_ts, status, workflow_mode,
            equipment_id, fault_code, issue_text, symptoms, notes, location,
            requires_approval, escalation_reasons_json, tags_json, attachment_count, latest_attachment_ts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(issue_id) DO UPDATE SET
            job_id=excluded.job_id,
            created_ts=excluded.created_ts,
            updated_ts=excluded.updated_ts,
            status=excluded.status,
            workflow_mode=excluded.workflow_mode,
            equipment_id=excluded.equipment_id,
            fault_code=excluded.fault_code,
            issue_text=excluded.issue_text,
            symptoms=excluded.symptoms,
            notes=excluded.notes,
            location=excluded.location,
            requires_approval=excluded.requires_approval,
            escalation_reasons_json=excluded.escalation_reasons_json,
            tags_json=excluded.tags_json,
            attachment_count=excluded.attachment_count,
            latest_attachment_ts=excluded.latest_attachment_ts
        """,
        (
            record["issue_id"],
            record["job_id"],
            record.get("created_ts"),
            record.get("updated_ts"),
            record.get("status"),
            record.get("workflow_mode"),
            record.get("equipment_id"),
            record.get("fault_code"),
            record.get("issue_text"),
            record.get("symptoms"),
            record.get("notes"),
            record.get("location"),
            int(record.get("requires_approval", 0)),
            _to_json(record.get("escalation_reasons_json", [])),
            _to_json(record.get("tags_json", [])),
            int(record.get("attachment_count", attachment_count)),
            record.get("latest_attachment_ts", latest_attachment_ts),
        ),
    )


def upsert_job(conn: sqlite3.Connection, job: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO jobs(
            job_id, created_ts, updated_ts, status, field_payload_json,
            final_response_json, requires_approval, approved_by, approved_ts,
            guided_question, guided_answer, approval_due_ts, timed_out, first_occurrence_fault, assigned_tech_id,
            workflow_mode
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            created_ts=excluded.created_ts,
            updated_ts=excluded.updated_ts,
            status=excluded.status,
            field_payload_json=excluded.field_payload_json,
            final_response_json=excluded.final_response_json,
            requires_approval=excluded.requires_approval,
            approved_by=excluded.approved_by,
            approved_ts=excluded.approved_ts,
            guided_question=excluded.guided_question,
            guided_answer=excluded.guided_answer,
            approval_due_ts=excluded.approval_due_ts,
            timed_out=excluded.timed_out,
            first_occurrence_fault=excluded.first_occurrence_fault,
            assigned_tech_id=excluded.assigned_tech_id,
            workflow_mode=excluded.workflow_mode
        """,
        (
            job["job_id"],
            job["created_ts"],
            job["updated_ts"],
            job["status"],
            _to_json(job.get("field_payload_json", {})),
            _to_json(job.get("final_response_json", {})),
            int(job.get("requires_approval", 0)),
            job.get("approved_by"),
            job.get("approved_ts"),
            job.get("guided_question"),
            job.get("guided_answer"),
            job.get("approval_due_ts"),
            int(job.get("timed_out", 0)),
            int(job.get("first_occurrence_fault", 0)),
            job.get("assigned_tech_id"),
            job.get("workflow_mode"),
        ),
    )
    issue_record = _build_issue_record_from_job(job)
    upsert_issue_record(conn, issue_record)


def upsert_job_lww(conn: sqlite3.Connection, job: dict[str, Any]) -> bool:
    existing = conn.execute(
        "SELECT updated_ts FROM jobs WHERE job_id = ?",
        (job["job_id"],),
    ).fetchone()
    if existing and existing["updated_ts"] and job["updated_ts"] < existing["updated_ts"]:
        return False
    upsert_job(conn, job)
    return True


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["field_payload_json"] = _parse_json(data.get("field_payload_json"))
    data["final_response_json"] = _parse_json(data.get("final_response_json"))
    return data


def _parse_issue_record_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["requires_approval"] = int(item.get("requires_approval", 0))
    item["attachment_count"] = int(item.get("attachment_count", 0))
    item["escalation_reasons_json"] = _parse_json(item.get("escalation_reasons_json")) or []
    item["tags_json"] = _parse_json(item.get("tags_json")) or []
    return item


def get_issue_record(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM issue_records WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if not row:
        return None
    return _parse_issue_record_row(row)


def search_issue_records(
    conn: sqlite3.Connection,
    *,
    q: str | None = None,
    equipment_id: str | None = None,
    fault_code: str | None = None,
    location: str | None = None,
    status: str | None = None,
    workflow_mode: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []

    if q:
        where.append("(issue_text LIKE ? OR symptoms LIKE ? OR notes LIKE ? OR fault_code LIKE ?)")
        needle = f"%{q.strip()}%"
        params.extend([needle, needle, needle, needle])
    if equipment_id:
        where.append("equipment_id = ?")
        params.append(equipment_id.strip().upper())
    if fault_code:
        where.append("fault_code = ?")
        params.append(fault_code.strip().upper())
    if location:
        where.append("location LIKE ?")
        params.append(f"%{location.strip()}%")
    if status:
        where.append("status = ?")
        params.append(status.strip().upper())
    if workflow_mode:
        where.append("workflow_mode = ?")
        params.append(workflow_mode.strip().upper())
    if date_from:
        where.append("updated_ts >= ?")
        params.append(date_from)
    if date_to:
        where.append("updated_ts <= ?")
        params.append(date_to)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT *
        FROM issue_records
        {where_clause}
        ORDER BY updated_ts DESC
        LIMIT ? OFFSET ?
    """
    params.extend([int(limit), int(offset)])
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [_parse_issue_record_row(row) for row in rows]


def _parse_attachment_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["byte_size"] = int(item.get("byte_size", 0))
    item["vision_features_json"] = _parse_json(item.get("vision_features_json")) or {}
    return item


def count_job_step_attachments(conn: sqlite3.Connection, job_id: str, step_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM issue_attachments
        WHERE job_id = ? AND step_id = ?
        """,
        (job_id, step_id),
    ).fetchone()
    return int(row["count"]) if row else 0


def insert_issue_attachment(conn: sqlite3.Connection, attachment: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO issue_attachments(
            attachment_id, job_id, step_id, created_ts, captured_ts, source, filename, mime_type,
            byte_size, sha256, caption, local_rel_path, server_rel_path, sync_state, sync_error, vision_features_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            attachment["attachment_id"],
            attachment["job_id"],
            attachment.get("step_id"),
            attachment.get("created_ts"),
            attachment.get("captured_ts"),
            attachment.get("source"),
            attachment.get("filename"),
            attachment.get("mime_type"),
            int(attachment.get("byte_size", 0)),
            attachment.get("sha256"),
            attachment.get("caption"),
            attachment.get("local_rel_path"),
            attachment.get("server_rel_path"),
            attachment.get("sync_state", "pending"),
            attachment.get("sync_error"),
            _to_json(attachment.get("vision_features_json", {})),
        ),
    )


def upsert_issue_attachment(conn: sqlite3.Connection, attachment: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO issue_attachments(
            attachment_id, job_id, step_id, created_ts, captured_ts, source, filename, mime_type,
            byte_size, sha256, caption, local_rel_path, server_rel_path, sync_state, sync_error, vision_features_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(attachment_id) DO UPDATE SET
            job_id=excluded.job_id,
            step_id=excluded.step_id,
            created_ts=excluded.created_ts,
            captured_ts=excluded.captured_ts,
            source=excluded.source,
            filename=excluded.filename,
            mime_type=excluded.mime_type,
            byte_size=excluded.byte_size,
            sha256=excluded.sha256,
            caption=excluded.caption,
            local_rel_path=excluded.local_rel_path,
            server_rel_path=excluded.server_rel_path,
            sync_state=excluded.sync_state,
            sync_error=excluded.sync_error,
            vision_features_json=excluded.vision_features_json
        """,
        (
            attachment["attachment_id"],
            attachment["job_id"],
            attachment.get("step_id"),
            attachment.get("created_ts"),
            attachment.get("captured_ts"),
            attachment.get("source"),
            attachment.get("filename"),
            attachment.get("mime_type"),
            int(attachment.get("byte_size", 0)),
            attachment.get("sha256"),
            attachment.get("caption"),
            attachment.get("local_rel_path"),
            attachment.get("server_rel_path"),
            attachment.get("sync_state", "pending"),
            attachment.get("sync_error"),
            _to_json(attachment.get("vision_features_json", {})),
        ),
    )


def get_job_attachments(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM issue_attachments
        WHERE job_id = ?
        ORDER BY created_ts DESC, attachment_id DESC
        """,
        (job_id,),
    ).fetchall()
    return [_parse_attachment_row(row) for row in rows]


def get_attachment(conn: sqlite3.Connection, attachment_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM issue_attachments WHERE attachment_id = ?",
        (attachment_id,),
    ).fetchone()
    if not row:
        return None
    return _parse_attachment_row(row)


def refresh_issue_attachment_summary(conn: sqlite3.Connection, job_id: str) -> None:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count, MAX(created_ts) AS latest_ts
        FROM issue_attachments
        WHERE job_id = ?
        """,
        (job_id,),
    ).fetchone()
    count = int(row["count"]) if row else 0
    latest_ts = row["latest_ts"] if row else None
    cursor = conn.execute(
        """
        UPDATE issue_records
        SET attachment_count = ?, latest_attachment_ts = ?
        WHERE issue_id = ?
        """,
        (count, latest_ts, job_id),
    )
    if cursor.rowcount > 0:
        return
    job = get_job(conn, job_id)
    if not job:
        return
    upsert_issue_record(conn, _build_issue_record_from_job(job))
    conn.execute(
        """
        UPDATE issue_records
        SET attachment_count = ?, latest_attachment_ts = ?
        WHERE issue_id = ?
        """,
        (count, latest_ts, job_id),
    )


def insert_decision_log(conn: sqlite3.Connection, entry: dict[str, Any]) -> int:
    cursor = conn.execute(
        """
        INSERT INTO decision_log(
            ts, job_id, agent_id, action, input_json, output_json, confidence, requires_human
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry["ts"],
            entry["job_id"],
            entry["agent_id"],
            entry["action"],
            _to_json(entry.get("input_json", {})),
            _to_json(entry.get("output_json", {})),
            _clamp_confidence(float(entry.get("confidence", 0.0))),
            int(entry.get("requires_human", 0)),
        ),
    )
    return int(cursor.lastrowid)


def enqueue_sync_event(
    conn: sqlite3.Connection,
    ts: str,
    entity: str,
    entity_id: str,
    payload: dict[str, Any],
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO sync_queue(ts, entity, entity_id, payload_json, synced)
        VALUES (?, ?, ?, ?, 0)
        """,
        (ts, entity, entity_id, _to_json(payload)),
    )
    return int(cursor.lastrowid)


def get_unsynced_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM sync_queue WHERE synced = 0 ORDER BY id ASC"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["payload_json"] = _parse_json(item.get("payload_json"))
        result.append(item)
    return result


def mark_sync_event_synced(conn: sqlite3.Connection, sync_id: int) -> None:
    conn.execute("UPDATE sync_queue SET synced = 1 WHERE id = ?", (sync_id,))


def mark_sync_event_failed(conn: sqlite3.Connection, sync_id: int, error_message: str) -> dict[str, Any] | None:
    conn.execute(
        """
        UPDATE sync_queue
        SET retry_count = COALESCE(retry_count, 0) + 1,
            last_error = ?
        WHERE id = ?
        """,
        (error_message[:500], sync_id),
    )
    row = conn.execute("SELECT * FROM sync_queue WHERE id = ?", (sync_id,)).fetchone()
    return dict(row) if row else None


def fetch_pending_approval_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT j.job_id, j.updated_ts, j.status, j.field_payload_json, j.final_response_json, j.requires_approval,
               j.approval_due_ts, j.timed_out, ir.attachment_count, ir.latest_attachment_ts
        FROM jobs j
        LEFT JOIN issue_records ir ON ir.issue_id = j.job_id
        WHERE j.status IN ('PENDING_APPROVAL', 'TIMEOUT_HOLD')
        ORDER BY j.updated_ts DESC
        """
    ).fetchall()
    pending: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        payload = _parse_json(item.pop("field_payload_json")) or {}
        final_response = _parse_json(item.pop("final_response_json")) or {}
        pending.append(
            {
                "job_id": item["job_id"],
                "updated_ts": item["updated_ts"],
                "status": item["status"],
                "requires_approval": item["requires_approval"],
                "workflow_mode": final_response.get("workflow_mode"),
                "workflow_intent": final_response.get("workflow_intent"),
                "escalation_reasons": final_response.get("escalation_reasons", []),
                "risk_signals": final_response.get("risk_signals", {}),
                "approval_due_ts": item.get("approval_due_ts"),
                "timed_out": int(item.get("timed_out", 0)),
                "equipment_id": payload.get("equipment_id"),
                "fault_code": payload.get("fault_code"),
                "symptoms": payload.get("symptoms"),
                "location": payload.get("location"),
                "high_risk_failed_steps": _count_high_risk_failed_steps(conn, item["job_id"]),
                "attachment_count": int(item.get("attachment_count", 0) or 0),
                "latest_attachment_ts": item.get("latest_attachment_ts"),
            }
        )
    return pending


def fetch_job_with_logs(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    job = get_job(conn, job_id)
    if not job:
        return None
    log_rows = conn.execute(
        "SELECT * FROM decision_log WHERE job_id = ? ORDER BY id ASC",
        (job_id,),
    ).fetchall()
    logs: list[dict[str, Any]] = []
    for row in log_rows:
        item = dict(row)
        item["input_json"] = _parse_json(item.get("input_json"))
        item["output_json"] = _parse_json(item.get("output_json"))
        item["confidence"] = _clamp_confidence(float(item.get("confidence", 0.0)))
        logs.append(item)
    return {
        "job": job,
        "decision_log": logs,
        "attachments": get_job_attachments(conn, job_id),
        "workflow_steps": get_workflow_steps(conn, job_id),
        "workflow_events": fetch_workflow_events(conn, job_id),
    }


def fetch_job_timeline(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    decision_rows = conn.execute(
        """
        SELECT id, ts, job_id, agent_id, action, input_json, output_json, confidence, requires_human
        FROM decision_log
        WHERE job_id = ?
        ORDER BY id ASC
        """,
        (job_id,),
    ).fetchall()
    workflow_rows = conn.execute(
        """
        SELECT id, ts, job_id, step_id, actor_id, event_type, input_json, output_json
        FROM workflow_events
        WHERE job_id = ?
        ORDER BY id ASC
        """,
        (job_id,),
    ).fetchall()

    timeline: list[dict[str, Any]] = []
    for row in decision_rows:
        item = dict(row)
        timeline.append(
            {
                "ts": item.get("ts"),
                "kind": "decision_log",
                "event_id": int(item.get("id", 0)),
                "actor_id": item.get("agent_id"),
                "event_name": item.get("action"),
                "confidence": _clamp_confidence(float(item.get("confidence", 0.0))),
                "requires_human": int(item.get("requires_human", 0)),
                "input_json": _parse_json(item.get("input_json")) or {},
                "output_json": _parse_json(item.get("output_json")) or {},
            }
        )

    for row in workflow_rows:
        item = dict(row)
        timeline.append(
            {
                "ts": item.get("ts"),
                "kind": "workflow_event",
                "event_id": int(item.get("id", 0)),
                "actor_id": item.get("actor_id"),
                "event_name": item.get("event_type"),
                "step_id": item.get("step_id"),
                "input_json": _parse_json(item.get("input_json")) or {},
                "output_json": _parse_json(item.get("output_json")) or {},
            }
        )

    timeline.sort(key=lambda entry: ((entry.get("ts") or ""), entry.get("event_id", 0), entry.get("kind", "")))
    return timeline


def is_first_occurrence_fault(
    conn: sqlite3.Connection,
    *,
    equipment_id: str,
    fault_code: str,
    current_job_id: str,
) -> bool:
    rows = conn.execute(
        """
        SELECT job_id, field_payload_json
        FROM jobs
        WHERE job_id != ?
        """,
        (current_job_id,),
    ).fetchall()
    for row in rows:
        payload = _parse_json(row["field_payload_json"]) or {}
        if (
            str(payload.get("equipment_id", "")).strip().lower() == equipment_id.strip().lower()
            and str(payload.get("fault_code", "")).strip().lower() == fault_code.strip().lower()
        ):
            return False
    return True


def fetch_overdue_pending_jobs(conn: sqlite3.Connection, now_ts: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT *
        FROM jobs
        WHERE status = 'PENDING_APPROVAL'
          AND timed_out = 0
          AND approval_due_ts IS NOT NULL
          AND approval_due_ts <= ?
        ORDER BY approval_due_ts ASC
        """,
        (now_ts,),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["field_payload_json"] = _parse_json(item.get("field_payload_json"))
        item["final_response_json"] = _parse_json(item.get("final_response_json"))
        result.append(item)
    return result


def insert_supervisor_alert(conn: sqlite3.Connection, alert: dict[str, Any]) -> int:
    cursor = conn.execute(
        """
        INSERT INTO supervisor_alerts(ts, job_id, alert_type, payload_json, acknowledged)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            alert["ts"],
            alert.get("job_id"),
            alert["alert_type"],
            _to_json(alert.get("payload_json", {})),
            int(alert.get("acknowledged", 0)),
        ),
    )
    return int(cursor.lastrowid)


def fetch_supervisor_alerts(conn: sqlite3.Connection, include_acknowledged: bool = False) -> list[dict[str, Any]]:
    if include_acknowledged:
        rows = conn.execute(
            """
            SELECT id, ts, job_id, alert_type, payload_json, acknowledged
            FROM supervisor_alerts
            ORDER BY id DESC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, ts, job_id, alert_type, payload_json, acknowledged
            FROM supervisor_alerts
            WHERE acknowledged = 0
            ORDER BY id DESC
            """
        ).fetchall()
    alerts: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["payload_json"] = _parse_json(item.get("payload_json")) or {}
        alerts.append(item)
    return alerts


def replace_workflow_steps(
    conn: sqlite3.Connection,
    job_id: str,
    steps: list[dict[str, Any]],
    ts: str,
    agent_id: str = "orchestrator",
) -> None:
    conn.execute("DELETE FROM workflow_steps WHERE job_id = ?", (job_id,))
    for index, step in enumerate(steps):
        conn.execute(
            """
            INSERT INTO workflow_steps(
                job_id, agent_id, created_ts, step_order, step_id, title, instructions,
                required_inputs_json, pass_criteria_json, recommended_parts_json, risk_level,
                step_kind, suppressed, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                step.get("agent_id", agent_id),
                ts,
                int(step.get("step_order", index + 1)),
                step["step_id"],
                step.get("title", ""),
                step.get("instructions", ""),
                _to_json(step.get("required_inputs", [])),
                _to_json(step.get("pass_criteria", [])),
                _to_json(step.get("recommended_parts", [])),
                step.get("risk_level", "MEDIUM"),
                step.get("step_kind", "investigate"),
                int(step.get("suppressed", 0)),
                step.get("status", "pending"),
            ),
        )


def get_workflow_steps(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, job_id, agent_id, created_ts, step_order, step_id, title, instructions,
               required_inputs_json, pass_criteria_json, recommended_parts_json, risk_level,
               step_kind, suppressed, status
        FROM workflow_steps
        WHERE job_id = ?
        ORDER BY step_order ASC, id ASC
        """,
        (job_id,),
    ).fetchall()
    steps: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["required_inputs"] = _parse_json(item.pop("required_inputs_json")) or []
        item["pass_criteria"] = _parse_json(item.pop("pass_criteria_json")) or []
        item["recommended_parts"] = _parse_json(item.pop("recommended_parts_json")) or []
        item["step_kind"] = item.get("step_kind") or "investigate"
        item["suppressed"] = int(item.get("suppressed", 0))
        steps.append(item)
    return steps


def get_workflow_step(conn: sqlite3.Connection, job_id: str, step_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, job_id, agent_id, created_ts, step_order, step_id, title, instructions,
               required_inputs_json, pass_criteria_json, recommended_parts_json, risk_level,
               step_kind, suppressed, status
        FROM workflow_steps
        WHERE job_id = ? AND step_id = ?
        """,
        (job_id, step_id),
    ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["required_inputs"] = _parse_json(item.pop("required_inputs_json")) or []
    item["pass_criteria"] = _parse_json(item.pop("pass_criteria_json")) or []
    item["recommended_parts"] = _parse_json(item.pop("recommended_parts_json")) or []
    item["step_kind"] = item.get("step_kind") or "investigate"
    item["suppressed"] = int(item.get("suppressed", 0))
    return item


def update_workflow_step_status(conn: sqlite3.Connection, job_id: str, step_id: str, status: str) -> bool:
    cursor = conn.execute(
        "UPDATE workflow_steps SET status = ? WHERE job_id = ? AND step_id = ?",
        (status, job_id, step_id),
    )
    return cursor.rowcount > 0


def insert_workflow_event(conn: sqlite3.Connection, event: dict[str, Any]) -> int:
    cursor = conn.execute(
        """
        INSERT INTO workflow_events(
            ts, job_id, step_id, actor_id, event_type, input_json, output_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["ts"],
            event["job_id"],
            event.get("step_id"),
            event.get("actor_id", "workflow_engine"),
            event.get("event_type", "STEP_UPDATE"),
            _to_json(event.get("input_json", {})),
            _to_json(event.get("output_json", {})),
        ),
    )
    return int(cursor.lastrowid)


def fetch_workflow_events(conn: sqlite3.Connection, job_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, ts, job_id, step_id, actor_id, event_type, input_json, output_json
        FROM workflow_events
        WHERE job_id = ?
        ORDER BY id ASC
        """,
        (job_id,),
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["input_json"] = _parse_json(item.get("input_json"))
        item["output_json"] = _parse_json(item.get("output_json"))
        events.append(item)
    return events


def _count_high_risk_failed_steps(conn: sqlite3.Connection, job_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM workflow_steps
        WHERE job_id = ?
          AND risk_level IN ('HIGH', 'CRITICAL')
          AND status IN ('failed', 'blocked')
        """,
        (job_id,),
    ).fetchone()
    return int(row["count"]) if row else 0


def apply_metric_event(conn: sqlite3.Connection, event: dict[str, Any]) -> None:
    day = event["day"]
    agent_id = event["agent_id"]
    counter = event.get("counter")
    confidence = event.get("confidence")

    conn.execute(
        """
        INSERT INTO agent_metrics_daily(day, agent_id)
        VALUES (?, ?)
        ON CONFLICT(day, agent_id) DO NOTHING
        """,
        (day, agent_id),
    )

    if counter in {"jobs_processed", "escalations", "approvals", "denials", "replans"}:
        conn.execute(
            f"UPDATE agent_metrics_daily SET {counter} = {counter} + 1 WHERE day = ? AND agent_id = ?",
            (day, agent_id),
        )

    if confidence is not None:
        row = conn.execute(
            """
            SELECT mean_confidence, sample_count
            FROM agent_metrics_daily
            WHERE day = ? AND agent_id = ?
            """,
            (day, agent_id),
        ).fetchone()
        if row:
            current_mean = float(row["mean_confidence"] or 0.0)
            sample_count = int(row["sample_count"] or 0)
            new_count = sample_count + 1
            new_mean = ((current_mean * sample_count) + float(confidence)) / new_count
            conn.execute(
                """
                UPDATE agent_metrics_daily
                SET mean_confidence = ?, sample_count = ?
                WHERE day = ? AND agent_id = ?
                """,
                (new_mean, new_count, day, agent_id),
            )


def fetch_agent_metrics(conn: sqlite3.Connection, day: str | None = None) -> list[dict[str, Any]]:
    if day:
        rows = conn.execute(
            """
            SELECT day, agent_id, jobs_processed, escalations, approvals, denials, replans,
                   mean_confidence, sample_count
            FROM agent_metrics_daily
            WHERE day = ?
            ORDER BY agent_id ASC
            """,
            (day,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT day, agent_id, jobs_processed, escalations, approvals, denials, replans,
                   mean_confidence, sample_count
            FROM agent_metrics_daily
            ORDER BY day DESC, agent_id ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]
