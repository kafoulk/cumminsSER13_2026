from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.agents import (
    email_agent,
    gathering_agent,
    parts_agent,
    quote_agent,
    repair_agent,
    scheduler_agent,
    triage_agent,
)
from backend.local_db import db


WORKFLOW_MODE_INVESTIGATION_ONLY = "INVESTIGATION_ONLY"
WORKFLOW_MODE_FIX_PLAN = "FIX_PLAN"
STATUS_PENDING_APPROVAL = "PENDING_APPROVAL"
STATUS_TIMEOUT_HOLD = "TIMEOUT_HOLD"
STATUS_READY = "READY"
STATUS_DIAGNOSTIC_IN_PROGRESS = "DIAGNOSTIC_IN_PROGRESS"
STATUS_DENIED = "DENIED"
STATUS_PENDING_QUOTE_APPROVAL = "PENDING_QUOTE_APPROVAL"
STATUS_AWAITING_CUSTOMER_APPROVAL = "AWAITING_CUSTOMER_APPROVAL"
STATUS_QUOTE_REWORK_REQUIRED = "QUOTE_REWORK_REQUIRED"
STATUS_CUSTOMER_DECLINED = "CUSTOMER_DECLINED"
STATUS_REPAIR_POOL_OPEN = "REPAIR_POOL_OPEN"
STATUS_REPAIR_IN_PROGRESS = "REPAIR_IN_PROGRESS"
STATUS_REPAIR_COMPLETED = "REPAIR_COMPLETED"
NO_SUPERVISOR_REPAIR_STATUSES = {STATUS_REPAIR_POOL_OPEN, STATUS_REPAIR_IN_PROGRESS}
PARTS_ACTIVE_REPAIR_STATUSES = {STATUS_REPAIR_POOL_OPEN, STATUS_REPAIR_IN_PROGRESS}
ESCALATION_POLICY_PATH = Path(__file__).resolve().parent / "config" / "escalation_policy.yaml"
WORKFLOW_ALLOWED_ACTIONS = {
    WORKFLOW_MODE_INVESTIGATION_ONLY: [
        "capture_observation",
        "capture_measurement",
        "attach_evidence",
        "request_supervisor_review",
    ],
    WORKFLOW_MODE_FIX_PLAN: [
        "diagnose",
        "replace_part",
        "repair",
        "verify_fix",
        "closeout",
    ],
}
DEFAULT_ESCALATION_POLICY = {
    "policy_version": "hybrid_risk_policy_v2",
    "governance_policy_version": "apex_governance_v2",
    "approval_threshold": 0.70,
    "safety_keywords": [
        "unsafe",
        "fire",
        "smoke",
        "brake",
        "hazard",
        "injury",
        "critical",
    ],
    "warranty_keywords": ["warranty", "authorization", "coverage", "claim"],
    "safety_semantic_weights": {
        "danger": 0.30,
        "dangerous": 0.35,
        "unsafe": 0.35,
        "hazard": 0.30,
        "risk": 0.12,
        "smoke": 0.25,
        "fire": 0.45,
        "injury": 0.45,
        "critical": 0.22,
        "brake": 0.20,
        "explosion": 0.55,
        "toxic": 0.30,
        "leak": 0.15,
        "leaking": 0.15,
        "overheat": 0.20,
        "stop operation": 0.40,
        "do not operate": 0.45,
        "operator danger": 0.45,
    },
    "warranty_semantic_weights": {
        "warranty": 0.50,
        "coverage": 0.35,
        "authorization": 0.35,
        "approved": 0.20,
        "claim": 0.25,
        "void": 0.30,
        "billable": 0.20,
        "oem": 0.15,
    },
}
ONLINE_MODEL_DEFAULT = "llama3.1:8b"
OFFLINE_MODEL_DEFAULT = "llama3.2:3b"
MODEL_SIZE_1_3B_PATTERN = re.compile(r"(?<!\d)(?:1|2|3)(?:\.\d+)?b(?!\d)", re.IGNORECASE)
MODEL_SIZE_8B_PATTERN = re.compile(r"(?<!\d)8(?:\.\d+)?b(?!\d)", re.IGNORECASE)
EQUIPMENT_ID_PATTERN = re.compile(r"\b(?:EQ[-_ ]?\d{2,}|[A-Z]{2,4}-\d{3,})\b", re.IGNORECASE)
FAULT_CODE_PATTERN = re.compile(r"\b(?:[PBCU]\d{4}|[A-Z]{2,5}-\d{2,5}|[A-Z]\d{3,4})\b", re.IGNORECASE)
NEGATION_TOKENS = {"no", "not", "never", "without", "none", "denies"}
ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_ATTACHMENT_BYTES = 3 * 1024 * 1024
MAX_ATTACHMENTS_PER_STEP = 5
ISSUE_SEARCH_LIMIT_MAX = 100
SIMILAR_ISSUE_MATCH_LIMIT = 5
SIMILARITY_MIN_SCORE = 0.14
SIMILARITY_STOPWORDS = {
    "and",
    "the",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "near",
    "under",
    "over",
    "when",
    "where",
    "while",
    "issue",
    "fault",
    "code",
    "notes",
    "symptoms",
    "operator",
    "technician",
    "tech",
}
WORKFLOW_STEP_ID_ALIASES = {
    "offline-context-observation": "step-context-observation",
    "offline-schedule-checkpoint": "step-schedule-checkpoint",
    "offline-supervisor-handoff": "step-supervisor-evidence-handoff",
    "offline-final-handoff": "step-final-handoff",
}


def _load_escalation_policy() -> dict[str, Any]:
    policy = json.loads(json.dumps(DEFAULT_ESCALATION_POLICY))
    if ESCALATION_POLICY_PATH.exists():
        with ESCALATION_POLICY_PATH.open("r", encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
        file_policy = loaded.get("escalation_policy", loaded)
        if isinstance(file_policy, dict):
            for key, value in file_policy.items():
                if key in {"safety_semantic_weights", "warranty_semantic_weights"} and isinstance(value, dict):
                    current = policy.get(key, {})
                    if isinstance(current, dict):
                        current.update(value)
                        policy[key] = current
                    continue
                policy[key] = value
    threshold_env = os.getenv("APPROVAL_THRESHOLD")
    if threshold_env:
        try:
            policy["approval_threshold"] = float(threshold_env)
        except ValueError:
            pass
    return policy


ESCALATION_POLICY = _load_escalation_policy()
APPROVAL_THRESHOLD = float(ESCALATION_POLICY.get("approval_threshold", 0.70))
GOVERNANCE_POLICY_VERSION = str(ESCALATION_POLICY.get("governance_policy_version", "apex_governance_v2"))
RISK_SIGNAL_POLICY_VERSION = str(ESCALATION_POLICY.get("policy_version", "hybrid_risk_policy_v2"))
SAFETY_KEYWORDS = {str(item).lower() for item in ESCALATION_POLICY.get("safety_keywords", [])}
WARRANTY_KEYWORDS = {str(item).lower() for item in ESCALATION_POLICY.get("warranty_keywords", [])}
SAFETY_SEMANTIC_WEIGHTS = {
    str(term).lower(): float(weight)
    for term, weight in dict(ESCALATION_POLICY.get("safety_semantic_weights", {})).items()
}
WARRANTY_SEMANTIC_WEIGHTS = {
    str(term).lower(): float(weight)
    for term, weight in dict(ESCALATION_POLICY.get("warranty_semantic_weights", {})).items()
}
ESCALATION_POLICY_HASH = hashlib.sha256(json.dumps(ESCALATION_POLICY, sort_keys=True).encode("utf-8")).hexdigest()

SERVICE_REPORT_PROMPT_TEMPLATE = """You are a service report assistant.
Produce a concise field-to-back-office service report with EXACT headings:
- Customer complaint
- Observations
- Diagnostics performed
- Manual references used
- Parts considered
- Actions taken (proposed)
- Safety/warranty notes
- Next steps

Use this context JSON:
{context_json}
"""

RISK_CLASSIFIER_PROMPT_TEMPLATE = """You are a service safety classifier.
Return STRICT JSON only with keys:
- safety_signal (boolean)
- warranty_signal (boolean)
- rationale (string)
- confidence (number from 0 to 1)

Classify using semantic meaning, not exact keyword matching.
If technician notes imply user/operator danger or unsafe operation, set safety_signal=true.

Context JSON:
{context_json}
"""

DEMO_SCENARIOS = [
    {
        "id": "normal_ready",
        "label": "Normal: READY (no escalation)",
        "payload": {
            "issue_text": "Engine temp rising under load with coolant smell near radiator.",
            "equipment_id": "EQ-1001",
            "fault_code": "P0217",
            "symptoms": "Engine temp rising under load",
            "notes": "Coolant smell near radiator",
            "location": "Indy Yard",
            "is_offline": False,
            "request_supervisor_review": False,
        },
    },
    {
        "id": "safety_escalation",
        "label": "Safety: auto escalation",
        "payload": {
            "issue_text": "Coolant leaking and smoke near manifold. Very dangerous for operator.",
            "equipment_id": "EQ-3003",
            "fault_code": "P0217",
            "symptoms": "Coolant leaking and smoke near manifold",
            "notes": "Very dangerous for operator, stop operation immediately.",
            "location": "Remote Quarry",
            "is_offline": False,
            "request_supervisor_review": False,
        },
    },
    {
        "id": "manual_supervisor",
        "label": "Manual: force supervisor review",
        "payload": {
            "issue_text": "Brake warning intermittent. Field tech requests supervisor sign-off.",
            "equipment_id": "EQ-2002",
            "fault_code": "BRK-404",
            "symptoms": "Brake warning intermittent",
            "notes": "Field tech requests explicit supervisor sign-off.",
            "location": "Columbus Depot",
            "is_offline": False,
            "request_supervisor_review": True,
        },
    },
    {
        "id": "offline_sync_demo",
        "label": "Offline: queue then sync",
        "payload": {
            "issue_text": "Loss of power under load at tunnel station; no network available.",
            "equipment_id": "EQ-5005",
            "fault_code": "FUEL-117",
            "symptoms": "Loss of power under load",
            "notes": "No network at site. Capture locally and sync later.",
            "location": "Tunnel Station",
            "is_offline": True,
            "request_supervisor_review": False,
        },
    },
]

SIMILARITY_DEMO_SCENARIOS = [
    {
        "id": "history_match_cooling_a",
        "label": "History Match: Cooling A",
        "payload": {
            "issue_text": "Engine temp climbs fast and coolant smell is strong by the radiator.",
            "equipment_id": "EQ-4201",
            "fault_code": "P0217",
            "symptoms": "Temp rises under load and coolant odor",
            "notes": "Needs cooling system diagnosis",
            "location": "North Yard",
            "is_offline": False,
        },
        "expected_history_match": True,
    },
    {
        "id": "history_match_cooling_b",
        "label": "History Match: Cooling B",
        "payload": {
            "issue_text": "Coolant leak near hose clamp and overheating after 20 minutes.",
            "equipment_id": "EQ-4202",
            "fault_code": "P0217",
            "symptoms": "Coolant leak with over-temp warning",
            "notes": "Likely cooling loop issue",
            "location": "South Yard",
            "is_offline": False,
        },
        "expected_history_match": True,
    },
    {
        "id": "history_no_match",
        "label": "History No Match: Electronics",
        "payload": {
            "issue_text": "Touchscreen reboots and GPS loses route every few minutes.",
            "equipment_id": "EQ-9301",
            "fault_code": "ELEC-771",
            "symptoms": "Display reboot loop and navigation failure",
            "notes": "No driveline faults observed",
            "location": "Downtown Fleet",
            "is_offline": False,
        },
        "expected_history_match": False,
    },
]

HISTORY_SEED_JOBS = [
    {
        "job_id": "hist-cooling-001",
        "created_ts": "2026-02-11T14:00:00Z",
        "updated_ts": "2026-02-11T18:40:00Z",
        "status": STATUS_REPAIR_COMPLETED,
        "workflow_mode": WORKFLOW_MODE_FIX_PLAN,
        "field_payload_json": {
            "issue_text": "Engine overheating under load with coolant smell near radiator.",
            "equipment_id": "EQ-1101",
            "fault_code": "P0217",
            "symptoms": "Temp rise under load, coolant odor",
            "notes": "Found weak thermostat response and fan clutch lag.",
            "location": "Indy Yard",
            "customer_name": "Indy Fleet Services",
        },
        "final_response_json": {
            "job_id": "hist-cooling-001",
            "status": STATUS_REPAIR_COMPLETED,
            "workflow_mode": WORKFLOW_MODE_FIX_PLAN,
            "quote_stage": "CUSTOMER_APPROVED",
            "service_report": "Historical cooling repair completed.",
            "escalation_reasons": [],
            "customer_decision": {"decision": "approve", "ts": "2026-02-11T15:10:00Z"},
            "repair_completion": {"ts": "2026-02-11T18:40:00Z"},
        },
    },
    {
        "job_id": "hist-cooling-002",
        "created_ts": "2026-02-19T09:10:00Z",
        "updated_ts": "2026-02-19T13:25:00Z",
        "status": STATUS_REPAIR_COMPLETED,
        "workflow_mode": WORKFLOW_MODE_FIX_PLAN,
        "field_payload_json": {
            "issue_text": "Coolant leak and over-temperature warning on hill climbs.",
            "equipment_id": "EQ-1188",
            "fault_code": "P0217",
            "symptoms": "Coolant leak at upper hose and over-temp warning",
            "notes": "Replaced hose set and verified fan engagement.",
            "location": "Columbus Depot",
            "customer_name": "Midwest Haul Co",
        },
        "final_response_json": {
            "job_id": "hist-cooling-002",
            "status": STATUS_REPAIR_COMPLETED,
            "workflow_mode": WORKFLOW_MODE_FIX_PLAN,
            "quote_stage": "CUSTOMER_APPROVED",
            "service_report": "Historical cooling leak repair completed.",
            "escalation_reasons": [],
            "customer_decision": {"decision": "approve", "ts": "2026-02-19T10:00:00Z"},
            "repair_completion": {"ts": "2026-02-19T13:25:00Z"},
        },
    },
    {
        "job_id": "hist-brake-001",
        "created_ts": "2026-02-23T07:20:00Z",
        "updated_ts": "2026-02-23T12:15:00Z",
        "status": STATUS_REPAIR_COMPLETED,
        "workflow_mode": WORKFLOW_MODE_FIX_PLAN,
        "field_payload_json": {
            "issue_text": "Brake warning with light smoke at rear axle.",
            "equipment_id": "EQ-2205",
            "fault_code": "BRK-404",
            "symptoms": "Brake warning and smoke near rear axle",
            "notes": "Replaced pressure sensor and bled brake lines.",
            "location": "Remote Quarry",
            "customer_name": "StoneWorks Logistics",
        },
        "final_response_json": {
            "job_id": "hist-brake-001",
            "status": STATUS_REPAIR_COMPLETED,
            "workflow_mode": WORKFLOW_MODE_FIX_PLAN,
            "quote_stage": "CUSTOMER_APPROVED",
            "service_report": "Historical brake safety repair completed.",
            "escalation_reasons": [],
            "customer_decision": {"decision": "approve", "ts": "2026-02-23T08:20:00Z"},
            "repair_completion": {"ts": "2026-02-23T12:15:00Z"},
        },
    },
]


class JobSubmitRequest(BaseModel):
    job_id: str | None = None
    issue_text: str | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    customer_email: str | None = None
    equipment_id: str | None = None
    fault_code: str | None = None
    symptoms: str | None = None
    notes: str | None = None
    location: str | None = None
    is_offline: bool | None = False
    request_supervisor_review: bool | None = False
    guided_answer: str | None = None


class JobIntakeRequest(BaseModel):
    job_id: str | None = None
    issue_text: str | None = None
    customer_name: str | None = None
    customer_phone: str | None = None
    customer_email: str | None = None
    equipment_id: str | None = None
    fault_code: str | None = None
    symptoms: str | None = None
    notes: str | None = None
    location: str | None = None
    is_offline: bool | None = False
    request_supervisor_review: bool | None = False


class GuidedAnswerRequest(BaseModel):
    answer_text: str
    actor_id: str = "field_technician"


class TimeoutCheckRequest(BaseModel):
    now_ts: str | None = None


class SupervisorApproveRequest(BaseModel):
    job_id: str
    approver_name: str
    decision: Literal["approve", "deny"]
    notes: str | None = None


class WorkflowStepUpdateRequest(BaseModel):
    step_id: str
    status: Literal["done", "blocked", "failed"]
    measurement_json: dict[str, Any] | None = None
    notes: str | None = None
    actor_id: str = "field_technician"
    request_supervisor_review: bool | None = False


class AttachmentUploadRequest(BaseModel):
    step_id: str
    source: Literal["camera", "gallery"]
    filename: str
    mime_type: str
    image_base64: str
    caption: str | None = None
    captured_ts: str | None = None


class QuoteEmailDraftRequest(BaseModel):
    recipient_name: str | None = None
    recipient_email: str | None = None
    additional_notes: str | None = None


class CustomerApprovalRequest(BaseModel):
    decision: Literal["approve", "deny"]
    actor_id: str = "field_technician"
    notes: str | None = None


class RepairClaimRequest(BaseModel):
    technician_id: str
    technician_name: str | None = None


class RepairCompleteRequest(BaseModel):
    technician_id: str
    notes: str | None = None


class DemoHistoryResetRequest(BaseModel):
    clear_server: bool = True


class PartsUseRequest(BaseModel):
    job_id: str
    step_id: str
    part_id: str
    quantity_used: int = 1
    actor_id: str = "field_technician"
    actor_role: Literal["technician", "supervisor"] = "technician"
    notes: str | None = None


class PartsCatalogUpsertRequest(BaseModel):
    part_name: str
    category: str = "general"
    unit: str = "each"
    location: str | None = None
    initial_quantity: int | None = None
    actor_id: str = "Supervisor"
    actor_role: Literal["technician", "supervisor"] = "supervisor"


class PartsReplenishRequest(BaseModel):
    part_id: str
    location: str
    quantity_add: int
    actor_id: str = "Supervisor"
    actor_role: Literal["technician", "supervisor"] = "supervisor"
    notes: str | None = None
    request_id: str | None = None


class PartsAdjustRequest(BaseModel):
    part_id: str
    location: str
    quantity_delta: int
    actor_id: str = "Supervisor"
    actor_role: Literal["technician", "supervisor"] = "supervisor"
    notes: str | None = None


app = FastAPI(title="Cummins Service Reboot Backend", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    db.init_db()
    _ensure_evidence_dirs()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_step_id(step_id: str) -> str:
    raw = str(step_id or "").strip()
    return WORKFLOW_STEP_ID_ALIASES.get(raw, raw)


def _stock_status(quantity_on_hand: int, reorder_level: int) -> str:
    qty = int(quantity_on_hand)
    reorder = int(reorder_level)
    if qty <= 0:
        return "OUT_OF_STOCK"
    if qty <= max(1, reorder):
        return "LOW_STOCK"
    return "IN_STOCK"


def _require_supervisor_role(actor_role: str) -> None:
    if str(actor_role or "").strip().lower() != db.SUPERVISOR_ROLE:
        raise HTTPException(status_code=403, detail="Supervisor role required for this action.")


def _inventory_location_for_job(job: dict[str, Any]) -> str:
    payload = job.get("field_payload_json") or {}
    location = str(payload.get("location", "")).strip()
    return location or "Unknown"


def _parts_usage_enabled_for_status(status: str) -> bool:
    return str(status or "").upper() in PARTS_ACTIVE_REPAIR_STATUSES


def _apply_part_sync_entity(server_conn: Any, entity: str, payload: dict[str, Any]) -> None:
    if entity == "parts_catalog_upsert":
        db.upsert_part_catalog(server_conn, payload)
        return
    if entity == "parts_inventory_upsert":
        db.upsert_part_inventory_row(
            server_conn,
            part_id=str(payload.get("part_id", "")),
            location=str(payload.get("location", "")),
            quantity_on_hand=int(payload.get("quantity_on_hand", 0)),
            reorder_level=int(payload.get("reorder_level", 2)),
            updated_ts=payload.get("updated_ts"),
        )
        return
    if entity == "parts_usage":
        db.insert_parts_usage_log(server_conn, payload)
        return
    if entity == "parts_restock_request":
        existing = db.get_restock_request(server_conn, str(payload.get("request_id", "")))
        if existing:
            db.update_restock_request_status(
                server_conn,
                request_id=str(payload.get("request_id", "")),
                status=str(payload.get("status", db.RESTOCK_STATUS_PENDING)),
                fulfilled_by=payload.get("fulfilled_by"),
                fulfilled_ts=payload.get("fulfilled_ts"),
            )
        else:
            db.insert_restock_request(server_conn, payload)
        return
    if entity == "parts_restock_status":
        db.update_restock_request_status(
            server_conn,
            request_id=str(payload.get("request_id", "")),
            status=str(payload.get("status", db.RESTOCK_STATUS_PENDING)),
            fulfilled_by=payload.get("fulfilled_by"),
            fulfilled_ts=payload.get("fulfilled_ts"),
        )
        return
    raise ValueError(f"Unsupported part sync entity '{entity}'")


def _utc_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _approval_due_ts(base_ts: str, minutes: int = 30) -> str:
    return (_parse_utc(base_ts) + timedelta(minutes=minutes)).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _local_evidence_root() -> Path:
    return db.LOCAL_DB_PATH.parent / "evidence" / "local"


def _server_evidence_root() -> Path:
    return db.SERVER_DB_PATH.parent / "evidence" / "server"


def _ensure_evidence_dirs() -> None:
    _local_evidence_root().mkdir(parents=True, exist_ok=True)
    _server_evidence_root().mkdir(parents=True, exist_ok=True)


def _clean_attachment_filename(filename: str, extension: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(filename or "").strip())
    if not name:
        name = f"attachment{extension}"
    if "." not in name:
        name = f"{name}{extension}"
    return name


def _attachment_local_path(job_id: str, attachment_id: str, extension: str) -> tuple[Path, str]:
    rel = Path("evidence") / "local" / job_id / f"{attachment_id}{extension}"
    abs_path = db.LOCAL_DB_PATH.parent / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    return abs_path, str(rel)


def _attachment_server_path(job_id: str, attachment_id: str, extension: str) -> tuple[Path, str]:
    rel = Path("evidence") / "server" / job_id / f"{attachment_id}{extension}"
    abs_path = db.SERVER_DB_PATH.parent / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    return abs_path, str(rel)


def _decode_image_payload(image_base64: str) -> bytes:
    value = str(image_base64 or "").strip()
    if "," in value and value.lower().startswith("data:"):
        value = value.split(",", 1)[1]
    try:
        return base64.b64decode(value, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Invalid image_base64 payload: {exc}") from exc


def _attachment_public_payload(item: dict[str, Any]) -> dict[str, Any]:
    payload = dict(item)
    payload["content_url"] = f"/api/attachments/{item['attachment_id']}/content"
    payload.pop("sync_error", None)
    return payload


def _copy_attachment_local_to_server(attachment: dict[str, Any]) -> dict[str, Any]:
    attachment_id = str(attachment.get("attachment_id", "")).strip()
    job_id = str(attachment.get("job_id", "")).strip()
    local_rel_path = str(attachment.get("local_rel_path", "")).strip()
    if not attachment_id or not job_id or not local_rel_path:
        raise ValueError("Attachment payload missing attachment_id/job_id/local_rel_path")
    local_abs = db.LOCAL_DB_PATH.parent / local_rel_path
    if not local_abs.exists():
        raise FileNotFoundError(f"Attachment file not found: {local_abs}")
    suffix = local_abs.suffix or ".jpg"
    server_abs, server_rel = _attachment_server_path(job_id, attachment_id, suffix)
    shutil.copy2(local_abs, server_abs)
    updated = dict(attachment)
    updated["server_rel_path"] = server_rel
    updated["sync_state"] = "synced"
    updated["sync_error"] = ""
    return updated


def _issue_similarity_score(anchor_tokens: set[str], candidate_tokens: set[str]) -> float:
    if not anchor_tokens or not candidate_tokens:
        return 0.0
    intersection = len(anchor_tokens.intersection(candidate_tokens))
    union = len(anchor_tokens.union(candidate_tokens))
    if union <= 0:
        return 0.0
    return intersection / union


def _tokenize_similarity_inputs(*values: Any) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        for token in re.findall(r"[a-zA-Z0-9_-]+", str(value or "").lower()):
            if len(token) < 3:
                continue
            if token in SIMILARITY_STOPWORDS:
                continue
            tokens.add(token)
    return tokens


def _score_similar_issue_records(
    conn: Any,
    *,
    anchor_tokens: set[str],
    anchor_fault_code: str | None = None,
    anchor_equipment_id: str | None = None,
    exclude_job_id: str | None = None,
    limit: int = SIMILAR_ISSUE_MATCH_LIMIT,
    min_score: float = SIMILARITY_MIN_SCORE,
) -> list[dict[str, Any]]:
    candidates = db.search_issue_records(conn, limit=ISSUE_SEARCH_LIMIT_MAX, offset=0)
    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_job_id = str(candidate.get("job_id", ""))
        if exclude_job_id and candidate_job_id == exclude_job_id:
            continue
        candidate_tokens = set(str(token) for token in candidate.get("tags_json", []))
        base_score = _issue_similarity_score(anchor_tokens, candidate_tokens)
        fault_bonus = 0.0
        equipment_bonus = 0.0
        if anchor_fault_code:
            candidate_fault = str(candidate.get("fault_code", "")).strip().upper()
            if candidate_fault and candidate_fault == anchor_fault_code:
                fault_bonus = 0.12
        if anchor_equipment_id:
            candidate_equipment = str(candidate.get("equipment_id", "")).strip().upper()
            if candidate_equipment and candidate_equipment == anchor_equipment_id:
                equipment_bonus = 0.05
        score = _clamp(base_score + fault_bonus + equipment_bonus)
        if score < float(min_score):
            continue
        scored.append(
            {
                "score": round(score, 4),
                "base_score": round(base_score, 4),
                "fault_bonus": round(fault_bonus, 4),
                "equipment_bonus": round(equipment_bonus, 4),
                "job_id": candidate.get("job_id"),
                "status": candidate.get("status"),
                "workflow_mode": candidate.get("workflow_mode"),
                "equipment_id": candidate.get("equipment_id"),
                "fault_code": candidate.get("fault_code"),
                "issue_text": candidate.get("issue_text"),
                "updated_ts": candidate.get("updated_ts"),
                "attachment_count": int(candidate.get("attachment_count", 0)),
            }
        )
    scored.sort(key=lambda item: (float(item["score"]), str(item.get("updated_ts", ""))), reverse=True)
    return scored[: max(1, int(limit))]


def _similar_issues_for_payload(
    conn: Any,
    *,
    payload: dict[str, Any],
    limit: int = SIMILAR_ISSUE_MATCH_LIMIT,
) -> list[dict[str, Any]]:
    fault_code = str(payload.get("fault_code", "")).strip().upper()
    equipment_id = str(payload.get("equipment_id", "")).strip().upper()
    anchor_tokens = _tokenize_similarity_inputs(
        fault_code,
        equipment_id,
        payload.get("issue_text"),
        payload.get("symptoms"),
        payload.get("notes"),
    )
    if not anchor_tokens:
        return []
    return _score_similar_issue_records(
        conn,
        anchor_tokens=anchor_tokens,
        anchor_fault_code=fault_code if fault_code and not fault_code.startswith("UNKNOWN") else None,
        anchor_equipment_id=equipment_id if equipment_id and not equipment_id.startswith("UNKNOWN") else None,
        limit=limit,
    )


def _similar_issues_for_job(
    conn: Any,
    *,
    job_id: str,
    limit: int = SIMILAR_ISSUE_MATCH_LIMIT,
) -> list[dict[str, Any]]:
    anchor = db.get_issue_record(conn, job_id)
    if not anchor:
        return []
    anchor_tokens = set(str(token) for token in anchor.get("tags_json", []))
    if not anchor_tokens:
        return []
    anchor_fault_code = str(anchor.get("fault_code", "")).strip().upper()
    anchor_equipment_id = str(anchor.get("equipment_id", "")).strip().upper()
    return _score_similar_issue_records(
        conn,
        anchor_tokens=anchor_tokens,
        anchor_fault_code=anchor_fault_code if anchor_fault_code and not anchor_fault_code.startswith("UNKNOWN") else None,
        anchor_equipment_id=(
            anchor_equipment_id if anchor_equipment_id and not anchor_equipment_id.startswith("UNKNOWN") else None
        ),
        exclude_job_id=job_id,
        limit=limit,
    )


def _build_seed_job_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_id": str(item["job_id"]),
        "created_ts": str(item["created_ts"]),
        "updated_ts": str(item["updated_ts"]),
        "status": str(item["status"]),
        "field_payload_json": dict(item.get("field_payload_json") or {}),
        "final_response_json": dict(item.get("final_response_json") or {}),
        "requires_approval": 0,
        "approved_by": None,
        "approved_ts": None,
        "guided_question": "What checks did you run and what did you observe?",
        "guided_answer": "Historical seeded case.",
        "approval_due_ts": None,
        "timed_out": 0,
        "first_occurrence_fault": 0,
        "assigned_tech_id": None,
        "workflow_mode": str(item.get("workflow_mode") or WORKFLOW_MODE_FIX_PLAN),
    }


def _seed_issue_history(conn: Any) -> list[dict[str, Any]]:
    inserted: list[dict[str, Any]] = []
    for item in HISTORY_SEED_JOBS:
        row = _build_seed_job_row(item)
        db.upsert_job(conn, row)
        db.insert_decision_log(
            conn,
            _build_log_entry(
                ts=row["updated_ts"],
                job_id=row["job_id"],
                agent_id="seed_loader",
                action="SEED_JOB_IMPORTED",
                input_json={"source": "history_seed_v1"},
                output_json={"status": row["status"], "workflow_mode": row["workflow_mode"]},
                confidence=1.0,
            ),
        )
        inserted.append(
            {
                "job_id": row["job_id"],
                "status": row["status"],
                "equipment_id": row["field_payload_json"].get("equipment_id"),
                "fault_code": row["field_payload_json"].get("fault_code"),
                "issue_text": row["field_payload_json"].get("issue_text"),
            }
        )
    return inserted


def _reset_history_demo_data(clear_server: bool) -> dict[str, Any]:
    for evidence_dir in [_local_evidence_root(), _server_evidence_root()]:
        if evidence_dir.exists():
            shutil.rmtree(evidence_dir)
    _ensure_evidence_dirs()

    with db.open_local_connection() as local_conn:
        db.clear_runtime_data(local_conn)
        db.create_schema(local_conn)
        seeded_local = _seed_issue_history(local_conn)
        local_conn.commit()
        local_history_count = len(
            db.search_issue_records(
                local_conn,
                limit=ISSUE_SEARCH_LIMIT_MAX,
                offset=0,
            )
        )

    seeded_server: list[dict[str, Any]] = []
    server_history_count = 0
    if clear_server:
        with db.open_server_connection() as server_conn:
            db.clear_runtime_data(server_conn)
            db.create_schema(server_conn)
            seeded_server = _seed_issue_history(server_conn)
            server_conn.commit()
            server_history_count = len(
                db.search_issue_records(
                    server_conn,
                    limit=ISSUE_SEARCH_LIMIT_MAX,
                    offset=0,
                )
            )

    return {
        "seeded_local_jobs": seeded_local,
        "seeded_server_jobs": seeded_server,
        "local_history_count": local_history_count,
        "server_history_count": server_history_count,
    }


def _default_guided_answer(payload: dict[str, Any]) -> str:
    symptoms = str(payload.get("symptoms", "")).strip()
    notes = str(payload.get("notes", "")).strip()
    fault_code = str(payload.get("fault_code", "")).strip()
    compact = " ".join(part for part in [symptoms, notes] if part).strip()
    if compact:
        return f"Observed during guided step for {fault_code}: {compact[:180]}"
    return f"Observed during guided step for {fault_code}: no additional field notes provided."


def _first_regex_match(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    if not match:
        return ""
    return str(match.group(0)).strip().upper().replace(" ", "-").replace("_", "-")


def _should_check_first_occurrence(payload: dict[str, Any]) -> bool:
    equipment_id = str(payload.get("equipment_id", "") or "").strip().upper()
    fault_code = str(payload.get("fault_code", "") or "").strip().upper()
    if not equipment_id or not fault_code:
        return False
    if equipment_id.startswith("UNKNOWN") or fault_code.startswith("UNKNOWN"):
        return False
    return True


def _should_enforce_parts_availability(payload: dict[str, Any]) -> bool:
    location = str(payload.get("location", "") or "").strip().lower()
    if not location:
        return False
    return location not in {"unknown", "n/a", "na", "none"}


def _normalize_issue_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized = dict(payload)
    issue_text = str(normalized.get("issue_text", "") or "").strip()
    equipment_id_raw = str(normalized.get("equipment_id", "") or "").strip()
    fault_code_raw = str(normalized.get("fault_code", "") or "").strip()
    symptoms_raw = str(normalized.get("symptoms", "") or "").strip()
    notes_raw = str(normalized.get("notes", "") or "").strip()
    customer_name = str(normalized.get("customer_name", "") or "").strip()
    customer_phone = str(normalized.get("customer_phone", "") or "").strip()
    customer_email = str(normalized.get("customer_email", "") or "").strip()

    merged_text = " ".join(part for part in [issue_text, symptoms_raw, notes_raw] if part).strip()
    if not merged_text and not equipment_id_raw and not fault_code_raw:
        raise HTTPException(
            status_code=422,
            detail="Provide issue_text or structured issue details (equipment_id, fault_code, symptoms, or notes).",
        )

    inferred_equipment = ""
    if not equipment_id_raw:
        inferred_equipment = _first_regex_match(EQUIPMENT_ID_PATTERN, merged_text)
    inferred_fault = ""
    if not fault_code_raw:
        inferred_fault = _first_regex_match(FAULT_CODE_PATTERN, merged_text)
        if inferred_fault.startswith("EQ-"):
            inferred_fault = ""

    equipment_id = (equipment_id_raw or inferred_equipment or "UNKNOWN_EQUIPMENT").strip().upper()
    fault_code = (fault_code_raw or inferred_fault or "UNKNOWN_FAULT").strip().upper()
    symptoms = symptoms_raw or issue_text or notes_raw or "No symptom details provided."
    notes = notes_raw or issue_text or symptoms_raw or "No technician notes provided."

    normalized["issue_text"] = issue_text or merged_text
    normalized["equipment_id"] = equipment_id
    normalized["fault_code"] = fault_code
    normalized["symptoms"] = symptoms
    normalized["notes"] = notes
    normalized["customer_name"] = customer_name
    normalized["customer_phone"] = customer_phone
    normalized["customer_email"] = customer_email

    completeness_hits = 0
    if issue_text:
        completeness_hits += 1
    if equipment_id_raw:
        completeness_hits += 1
    if fault_code_raw:
        completeness_hits += 1
    if symptoms_raw:
        completeness_hits += 1
    if notes_raw:
        completeness_hits += 1
    confidence = _clamp(0.35 + (0.13 * completeness_hits))

    meta = {
        "issue_text_present": bool(issue_text),
        "inferred_equipment_id": bool(inferred_equipment and not equipment_id_raw),
        "inferred_fault_code": bool(inferred_fault and not fault_code_raw),
        "used_unknown_equipment": equipment_id == "UNKNOWN_EQUIPMENT",
        "used_unknown_fault": fault_code == "UNKNOWN_FAULT",
        "normalization_confidence": confidence,
    }
    return normalized, meta


def _extract_job_id_from_sync_event(event: dict[str, Any]) -> str | None:
    payload = event.get("payload_json") or {}
    if isinstance(payload, dict) and payload.get("job_id"):
        return str(payload["job_id"])
    entity_id = str(event.get("entity_id", ""))
    if ":" in entity_id:
        return entity_id.split(":", 1)[0]
    return entity_id or None


def _is_offline(request_offline: bool | None = False) -> bool:
    env_offline = os.getenv("OFFLINE", "0") == "1"
    return env_offline or bool(request_offline)


def _mode_effective(request_offline: bool | None = False) -> str:
    return "offline" if _is_offline(request_offline) else "online"


def _is_offline_model_allowed(model_name: str) -> bool:
    return bool(MODEL_SIZE_1_3B_PATTERN.search(str(model_name).lower()))


def _is_online_model_allowed(model_name: str) -> bool:
    return bool(MODEL_SIZE_8B_PATTERN.search(str(model_name).lower()))


def _runtime_model_config(mode_effective: str) -> dict[str, Any]:
    config = triage_agent.load_ollama_config()
    model_online = str(config.get("online_model", config.get("model", ONLINE_MODEL_DEFAULT)))
    model_offline = str(config.get("offline_model", OFFLINE_MODEL_DEFAULT))
    policy_notes: list[str] = []

    if not _is_online_model_allowed(model_online):
        policy_notes.append(f"online_model '{model_online}' did not match 8B policy; fallback applied.")
        model_online = ONLINE_MODEL_DEFAULT
    if not _is_offline_model_allowed(model_offline):
        policy_notes.append(f"offline_model '{model_offline}' did not match 1-3B policy; fallback applied.")
        model_offline = OFFLINE_MODEL_DEFAULT

    model_selected = model_offline if mode_effective == "offline" else model_online
    model_tier = "offline_1_3b" if mode_effective == "offline" else "online_8b"
    return {
        "mode_effective": mode_effective,
        "model_online": model_online,
        "model_offline": model_offline,
        "model_selected": model_selected,
        "model_tier": model_tier,
        "model_policy_valid": len(policy_notes) == 0,
        "model_policy_notes": policy_notes,
    }


def _is_escalation_log_entry(entry: dict[str, Any]) -> bool:
    action = str(entry.get("action", "")).upper()
    agent_id = str(entry.get("agent_id", ""))
    if int(entry.get("requires_human", 0)) == 1:
        return True
    if "ESCALATION" in action or "APPROVAL" in action:
        return True
    if action in {"MANUAL_ESCALATION_REQUESTED", "SUPERVISOR_DECISION", "APPROVAL_TIMEOUT_FAILSAFE"}:
        return True
    if action == "SYNC_RETRY_THRESHOLD_EXCEEDED":
        return True
    return agent_id in {"human_supervisor", "approval_logic", "sync_engine"}


def _contains_keywords(text: str, keywords: set[str]) -> bool:
    lowered = text.lower()
    return any(keyword in lowered for keyword in keywords)


def _tokenize_text(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _build_risk_input_text(payload: dict[str, Any], triage_output: dict[str, Any]) -> str:
    return " ".join(
        [
            str(payload.get("fault_code", "")),
            str(payload.get("symptoms", "")),
            str(payload.get("notes", "")),
            str(triage_output.get("summary", "")),
            " ".join(str(item) for item in triage_output.get("likely_causes", [])),
        ]
    ).lower()


def _contains_negated_phrase(words: list[str], index: int) -> bool:
    window = words[max(0, index - 3) : index]
    return any(token in NEGATION_TOKENS for token in window)


def _score_semantic_terms(text: str, weighted_terms: dict[str, float]) -> tuple[float, list[str]]:
    words = _tokenize_text(text)
    score = 0.0
    matched: list[str] = []

    for term, weight in weighted_terms.items():
        if " " in term:
            if term in text:
                score += weight
                matched.append(term)
            continue

        for idx, token in enumerate(words):
            if not token.startswith(term):
                continue
            if _contains_negated_phrase(words, idx):
                continue
            score += weight
            matched.append(token)
            break

    deduped: list[str] = []
    seen: set[str] = set()
    for item in matched:
        if item not in seen:
            seen.add(item)
            deduped.append(item)

    return _clamp(score), deduped


def _fallback_semantic_risk_signals(payload: dict[str, Any], triage_output: dict[str, Any]) -> dict[str, Any]:
    risk_text = _build_risk_input_text(payload, triage_output)
    safety_score, safety_terms = _score_semantic_terms(risk_text, SAFETY_SEMANTIC_WEIGHTS)
    warranty_score, warranty_terms = _score_semantic_terms(risk_text, WARRANTY_SEMANTIC_WEIGHTS)
    safety_signal = safety_score >= 0.30
    warranty_signal = warranty_score >= 0.30

    if safety_signal or warranty_signal:
        rationale = "Fallback semantic scorer detected risk-aligned language."
    else:
        rationale = "Fallback semantic scorer found no significant safety/warranty language."

    confidence = 0.25 + min(0.65, max(safety_score, warranty_score))
    return {
        "safety_signal": safety_signal,
        "warranty_signal": warranty_signal,
        "rationale": rationale,
        "confidence": _clamp(confidence),
        "source": "fallback_semantic",
        "matched_terms": {
            "safety": safety_terms,
            "warranty": warranty_terms,
        },
    }


def _normalize_risk_signals(result: dict[str, Any]) -> dict[str, Any]:
    matched_terms = result.get("matched_terms", {})
    safety_terms = matched_terms.get("safety", []) if isinstance(matched_terms, dict) else []
    warranty_terms = matched_terms.get("warranty", []) if isinstance(matched_terms, dict) else []
    return {
        "safety_signal": bool(result.get("safety_signal", False)),
        "warranty_signal": bool(result.get("warranty_signal", False)),
        "rationale": str(result.get("rationale", "")).strip(),
        "confidence": _clamp(float(result.get("confidence", 0.0))),
        "source": result.get("source", "unknown"),
        "matched_terms": {
            "safety": safety_terms if isinstance(safety_terms, list) else [],
            "warranty": warranty_terms if isinstance(warranty_terms, list) else [],
        },
    }


def _merge_keyword_risk_hits(
    risk_signals: dict[str, Any],
    *,
    keyword_safety_hit: bool,
    keyword_warranty_hit: bool,
) -> dict[str, Any]:
    merged = _normalize_risk_signals(risk_signals)

    if keyword_safety_hit and not merged["safety_signal"]:
        merged["safety_signal"] = True
        merged["confidence"] = max(float(merged.get("confidence", 0.0)), 0.5)
        merged["matched_terms"]["safety"] = list(merged["matched_terms"]["safety"]) + ["keyword_match"]

    if keyword_warranty_hit and not merged["warranty_signal"]:
        merged["warranty_signal"] = True
        merged["confidence"] = max(float(merged.get("confidence", 0.0)), 0.5)
        merged["matched_terms"]["warranty"] = list(merged["matched_terms"]["warranty"]) + ["keyword_match"]

    if keyword_safety_hit or keyword_warranty_hit:
        merged["rationale"] = f"{merged['rationale']} Keyword-based risk signals were also detected.".strip()

    return merged


def _build_log_entry(
    *,
    ts: str,
    job_id: str,
    agent_id: str,
    action: str,
    input_json: dict[str, Any],
    output_json: dict[str, Any],
    confidence: float,
    requires_human: int = 0,
) -> dict[str, Any]:
    return {
        "ts": ts,
        "job_id": job_id,
        "agent_id": agent_id,
        "action": action,
        "input_json": input_json,
        "output_json": output_json,
        "confidence": _clamp(confidence),
        "requires_human": int(requires_human),
    }


def _build_model_route_log_entry(
    *,
    ts: str,
    job_id: str,
    mode_effective: str,
    runtime_models: dict[str, Any],
) -> dict[str, Any]:
    return _build_log_entry(
        ts=ts,
        job_id=job_id,
        agent_id="orchestrator",
        action="MODEL_ROUTE_SELECTED",
        input_json={"mode_effective": mode_effective},
        output_json={
            "model_selected": runtime_models.get("model_selected"),
            "model_online": runtime_models.get("model_online"),
            "model_offline": runtime_models.get("model_offline"),
            "model_tier": runtime_models.get("model_tier"),
            "model_policy_valid": runtime_models.get("model_policy_valid"),
            "model_policy_notes": runtime_models.get("model_policy_notes"),
        },
        confidence=1.0,
    )


def _strip_triage_meta(triage_result: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(triage_result)
    cleaned.pop("llm_metadata", None)
    return cleaned


def _normalize_final_response(job_row: dict[str, Any]) -> dict[str, Any]:
    final = job_row.get("final_response_json")
    if not isinstance(final, dict):
        return job_row
    mode_effective = str(final.get("mode_effective", "online"))
    final["status"] = job_row.get("status")
    final["requires_approval"] = bool(job_row.get("requires_approval", 0))
    final.setdefault("escalation_reasons", [])
    final.setdefault("risk_signals", {})
    final.setdefault("escalation_policy_version", RISK_SIGNAL_POLICY_VERSION)
    final.setdefault("governance_policy_version", GOVERNANCE_POLICY_VERSION)
    final.setdefault("policy_config_hash", ESCALATION_POLICY_HASH)
    final.setdefault("guided_question", job_row.get("guided_question"))
    final.setdefault("guided_answer", job_row.get("guided_answer"))
    final.setdefault("approval_due_ts", job_row.get("approval_due_ts"))
    final.setdefault("timed_out", bool(job_row.get("timed_out", 0)))
    final.setdefault("first_occurrence_fault", bool(job_row.get("first_occurrence_fault", 0)))
    final.setdefault("model_tier", "offline_1_3b" if mode_effective == "offline" else "online_8b")
    final.setdefault("model_policy_valid", True)
    final.setdefault("model_policy_notes", [])
    workflow_mode = str(
        final.get("workflow_mode")
        or job_row.get("workflow_mode")
        or _derive_workflow_mode(
            status=str(job_row.get("status", "")),
            requires_approval=bool(job_row.get("requires_approval", 0)),
            supervisor_decision=final.get("supervisor_decision"),
        )
    )
    workflow_meta = _workflow_mode_metadata(workflow_mode)
    final["workflow_mode"] = workflow_mode
    final.setdefault("workflow_intent", workflow_meta["workflow_intent"])
    final.setdefault("allowed_actions", workflow_meta["allowed_actions"])
    final.setdefault("suppressed_guidance", workflow_meta["suppressed_guidance"])
    job_row["final_response_json"] = final
    job_row["workflow_mode"] = workflow_mode
    return job_row


def _workflow_mode_metadata(workflow_mode: str) -> dict[str, Any]:
    if workflow_mode == WORKFLOW_MODE_FIX_PLAN:
        return {
            "workflow_mode": WORKFLOW_MODE_FIX_PLAN,
            "workflow_intent": "Execute repair plan and verify fix.",
            "allowed_actions": list(WORKFLOW_ALLOWED_ACTIONS[WORKFLOW_MODE_FIX_PLAN]),
            "suppressed_guidance": False,
        }
    return {
        "workflow_mode": WORKFLOW_MODE_INVESTIGATION_ONLY,
        "workflow_intent": "Run diagnostic checklist and prepare a quote. Repair guidance is suppressed until customer approval.",
        "allowed_actions": list(WORKFLOW_ALLOWED_ACTIONS[WORKFLOW_MODE_INVESTIGATION_ONLY]),
        "suppressed_guidance": True,
    }


def _workflow_generation_agent_id(workflow_mode: str) -> str:
    if workflow_mode == WORKFLOW_MODE_INVESTIGATION_ONLY:
        return "gathering_agent"
    return "repair_agent"


def _derive_workflow_mode(
    *,
    status: str,
    requires_approval: bool,
    supervisor_decision: dict[str, Any] | None = None,
) -> str:
    normalized_status = str(status or "").upper()
    if requires_approval or normalized_status in {
        STATUS_PENDING_APPROVAL,
        STATUS_TIMEOUT_HOLD,
        STATUS_DENIED,
        STATUS_DIAGNOSTIC_IN_PROGRESS,
        STATUS_PENDING_QUOTE_APPROVAL,
        STATUS_AWAITING_CUSTOMER_APPROVAL,
        STATUS_QUOTE_REWORK_REQUIRED,
    }:
        return WORKFLOW_MODE_INVESTIGATION_ONLY
    if isinstance(supervisor_decision, dict) and str(supervisor_decision.get("decision", "")).lower() == "approve":
        return WORKFLOW_MODE_FIX_PLAN
    return WORKFLOW_MODE_FIX_PLAN


def _evaluate_escalation_reasons(
    *,
    combined_confidence: float | None = None,
    safety_hit: bool = False,
    warranty_hit: bool = False,
    triage_unsafe: bool = False,
    manual_request: bool = False,
    high_risk_step_failure: bool = False,
    first_occurrence_fault: bool = False,
    parts_unconfirmed: bool = False,
) -> list[str]:
    reasons: list[str] = []
    if manual_request:
        reasons.append("manual_request")
    if combined_confidence is not None and combined_confidence < APPROVAL_THRESHOLD:
        reasons.append("low_confidence")
    if safety_hit:
        reasons.append("safety_signal")
    if warranty_hit:
        reasons.append("warranty_signal")
    if triage_unsafe:
        reasons.append("triage_unsafe")
    if high_risk_step_failure:
        reasons.append("high_risk_step_failed_or_blocked")
    if first_occurrence_fault:
        reasons.append("first_occurrence_fault")
    if parts_unconfirmed:
        reasons.append("parts_unconfirmed")
    return reasons


def _evaluate_llm_risk_signals(
    payload: dict[str, Any],
    triage_output: dict[str, Any],
    offline_mode: bool = False,
) -> dict[str, Any]:
    context = {
        "equipment_id": payload.get("equipment_id"),
        "fault_code": payload.get("fault_code"),
        "symptoms": payload.get("symptoms"),
        "notes": payload.get("notes"),
        "triage_summary": triage_output.get("summary"),
        "triage_safety_flag": triage_output.get("safety_flag"),
    }
    context_json = json.dumps(context, sort_keys=True)
    prompt = RISK_CLASSIFIER_PROMPT_TEMPLATE.format(context_json=context_json)
    prompt_hash = _hash_text(prompt)
    parsed, meta = triage_agent.run_ollama_prompt(prompt, expect_json=True, offline_mode=offline_mode)

    if not isinstance(parsed, dict):
        fallback = _fallback_semantic_risk_signals(payload, triage_output)
        return {
            **fallback,
            "llm_used": False,
            "prompt_hash": prompt_hash,
            "context_hash": _hash_text(context_json),
            "policy_version": RISK_SIGNAL_POLICY_VERSION,
            **meta,
        }

    llm_result = _normalize_risk_signals(
        {
            "safety_signal": parsed.get("safety_signal", False),
            "warranty_signal": parsed.get("warranty_signal", False),
            "rationale": parsed.get("rationale", ""),
            "confidence": parsed.get("confidence", 0.0),
            "source": "ollama_llm",
            "matched_terms": parsed.get("matched_terms", {"safety": [], "warranty": []}),
        }
    )
    return {
        **llm_result,
        "safety_signal": bool(parsed.get("safety_signal", False)),
        "warranty_signal": bool(parsed.get("warranty_signal", False)),
        "llm_used": not bool(meta.get("used_fallback", False)),
        "prompt_hash": prompt_hash,
        "context_hash": _hash_text(context_json),
        "policy_version": RISK_SIGNAL_POLICY_VERSION,
        **meta,
    }


def _metric_event(
    *,
    agent_id: str,
    counter: str | None = None,
    confidence: float | None = None,
    day: str | None = None,
) -> dict[str, Any]:
    return {
        "day": day or _utc_day(),
        "agent_id": agent_id,
        "counter": counter,
        "confidence": confidence,
    }


def _normalize_string_list(value: Any, fallback: list[str]) -> list[str]:
    if isinstance(value, list):
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if cleaned:
            return cleaned
    return list(fallback)


def _suggest_step_title(instructions: str, index: int) -> str:
    lowered = instructions.lower()
    if any(token in lowered for token in {"safety", "hazard", "isolate", "brake"}):
        return "Safety Containment Check"
    if any(token in lowered for token in {"coolant", "overheat", "thermostat", "fan"}):
        return "Cooling System Validation"
    if any(token in lowered for token in {"fuel", "injector", "rail"}):
        return "Fuel System Validation"
    if any(token in lowered for token in {"electrical", "harness", "connector", "sensor"}):
        return "Electrical Integrity Check"
    if any(token in lowered for token in {"scan", "dtc", "freeze-frame"}):
        return "ECU Diagnostic Scan"
    return f"Diagnostic Step {index}"


def _infer_risk_from_instruction(instructions: str, safety_flag: bool = False) -> str:
    lowered = instructions.lower()
    if safety_flag and any(token in lowered for token in {"isolate", "hazard", "brake", "smoke", "fire"}):
        return "CRITICAL"
    if any(token in lowered for token in {"pressure", "fuel rail", "brake", "high-voltage"}):
        return "HIGH"
    if any(token in lowered for token in {"inspect", "scan", "verify", "measure"}):
        return "MEDIUM"
    return "LOW"


def _friendly_input_label(token: str) -> str:
    raw = str(token or "").strip().lower()
    if not raw:
        return "notes"
    exact_map = {
        "active_dtcs": "active fault codes",
        "abs_dtcs": "brake module fault codes",
        "freeze_frame": "snapshot data",
        "operating_context": "when the issue happens",
        "connector_notes": "connector condition notes",
        "harness_notes": "wire harness notes",
        "component_visual_notes": "visual condition notes",
        "test_procedure": "test procedure used",
        "observed_result": "observed result",
        "measurement_value": "measured value",
        "engine_temp": "engine temperature",
        "ambient_temp": "outside temperature",
        "engine_load_pct": "engine load percent",
        "cooling_pressure_psi": "cooling pressure",
        "coolant_level_state": "coolant level state",
        "leak_inspection_notes": "leak inspection notes",
        "fan_engagement_state": "fan engagement state",
        "airflow_obstruction_notes": "airflow obstruction notes",
        "radiator_condition": "radiator condition",
        "thermostat_observation": "thermostat behavior",
        "pump_flow_assessment": "pump flow assessment",
        "coolant_return_temp": "coolant return temperature",
        "lockout_status": "lockout status",
        "hazard_assessment": "hazard assessment",
        "supervisor_notification": "supervisor notified status",
        "line_pressure": "line pressure",
        "pressure_drop_test": "pressure drop test result",
        "sensor_signal_check": "sensor signal check",
        "module_comm_status": "module communication status",
        "fuel_rail_pressure": "fuel rail pressure",
        "load_condition": "load condition",
        "throttle_response_notes": "throttle response notes",
        "filter_condition": "filter condition",
        "line_restriction_notes": "line restriction notes",
        "flow_assessment": "flow assessment",
        "injector_command_state": "injector command state",
        "harness_continuity": "wire continuity",
        "connector_condition": "connector condition",
        "sensor_pressure": "sensor pressure",
        "mechanical_gauge_pressure": "manual gauge pressure",
        "engine_state": "engine state",
        "oil_level": "oil level",
        "oil_grade": "oil grade",
        "leak_notes": "leak notes",
        "observation_confirmation": "observation confirmation",
        "variance_notes": "difference notes",
        "checkpoint_confirmation": "checkpoint confirmation",
        "evidence_summary": "evidence summary",
        "open_questions": "open questions",
        "handoff_notes": "handoff notes",
        "repair_plan_summary": "repair plan summary",
        "parts_confirmation": "parts confirmation",
    }
    if raw in exact_map:
        return exact_map[raw]
    part_map = {
        "dtcs": "fault codes",
        "ecu": "computer data",
        "psi": "pressure",
        "pct": "percent",
        "abs": "brake system module",
    }
    parts = [part_map.get(part, part) for part in raw.split("_")]
    deduped_parts: list[str] = []
    for part in parts:
        if not deduped_parts or deduped_parts[-1] != part:
            deduped_parts.append(part)
    return " ".join(deduped_parts).strip()


def _compose_actionable_instruction(
    instructions: str,
    required_inputs: list[str],
    pass_criteria: list[str],
    recommended_parts: list[str],
    risk_level: str,
    suppress_repair_guidance: bool = False,
) -> str:
    base = instructions.strip() or "Do this check."
    if base and not base.endswith("."):
        base = f"{base}."
    capture_items = [item for item in required_inputs if str(item).strip()]
    capture_text = ", ".join(_friendly_input_label(item) for item in capture_items[:3]) or "what you see"
    pass_items = [str(item).strip() for item in pass_criteria if str(item).strip()]
    pass_text = ", ".join(pass_items[:2]) or "the result is clearly recorded"
    if suppress_repair_guidance:
        parts_line = "Do not repair yet."
        escalation = "If this fails, stop and add notes for review."
    else:
        parts_line = (
            f"Check these parts if needed: {', '.join(recommended_parts[:3])}."
            if recommended_parts
            else "No parts listed yet."
        )
        escalation = (
            "If this fails and the machine is unsafe, stop work and make it safe."
            if risk_level in {"HIGH", "CRITICAL"}
            else "If this fails, add notes and move to the next check."
        )
    return f"{base} Record: {capture_text}. Done when: {pass_text}. {parts_line} {escalation}"


def _context_observation_text(triage: dict[str, Any]) -> str:
    guided_answer = str(triage.get("guided_answer", "")).strip()
    summary = str(triage.get("summary", "")).strip()
    likely = ", ".join(str(item) for item in triage.get("likely_causes", []))
    if guided_answer:
        return guided_answer[:240]
    if summary and likely:
        return f"{summary} Likely causes: {likely}"[:240]
    return summary[:240]


def _detect_workflow_domain(triage: dict[str, Any], evidence: dict[str, Any]) -> str:
    text = " ".join(
        [
            str(triage.get("summary", "")),
            " ".join(str(item) for item in triage.get("likely_causes", [])),
            " ".join(str(item) for item in evidence.get("parts_candidates", [])),
        ]
    ).lower()
    if any(token in text for token in {"brake", "abs", "hydraulic"}):
        return "brake"
    if any(token in text for token in {"coolant", "overheat", "thermostat", "radiator", "fan"}):
        return "cooling"
    if any(token in text for token in {"fuel", "injector", "rail", "power loss"}):
        return "fuel"
    if any(token in text for token in {"oil", "pressure", "pump"}):
        return "lubrication"
    return "general"


def _domain_playbook_steps(domain: str, safety_flag: bool) -> list[dict[str, Any]]:
    playbooks: dict[str, list[dict[str, Any]]] = {
        "cooling": [
            {
                "step_id": "cooling-baseline",
                "title": "Baseline Thermal Snapshot",
                "instructions": "Capture engine temp trend under load and idle to establish baseline behavior.",
                "required_inputs": ["engine_temp", "ambient_temp", "engine_load_pct"],
                "pass_criteria": ["Baseline trend logged", "No uncontrolled temperature rise"],
                "risk_level": "HIGH" if safety_flag else "MEDIUM",
            },
            {
                "step_id": "cooling-integrity",
                "title": "Cooling Circuit Integrity",
                "instructions": "Pressure test cooling loop and inspect for leaks, coolant level issues, and hose degradation.",
                "required_inputs": ["cooling_pressure_psi", "coolant_level_state", "leak_inspection_notes"],
                "pass_criteria": ["Pressure stable within spec", "No active leak source"],
                "risk_level": "HIGH",
            },
            {
                "step_id": "cooling-airflow",
                "title": "Airflow and Fan Operation",
                "instructions": "Verify radiator airflow path, fan engagement logic, and shroud condition.",
                "required_inputs": ["fan_engagement_state", "airflow_obstruction_notes", "radiator_condition"],
                "pass_criteria": ["Fan engages as expected", "Airflow path clear"],
                "risk_level": "MEDIUM",
            },
            {
                "step_id": "cooling-control",
                "title": "Thermostat and Pump Function",
                "instructions": "Validate thermostat opening behavior and coolant pump circulation.",
                "required_inputs": ["thermostat_observation", "pump_flow_assessment", "coolant_return_temp"],
                "pass_criteria": ["Thermostat response confirmed", "Pump flow normal"],
                "risk_level": "MEDIUM",
            },
        ],
        "brake": [
            {
                "step_id": "brake-safety",
                "title": "Immediate Safety Isolation",
                "instructions": "Isolate unit, lockout operation, and confirm hazard controls before diagnosis.",
                "required_inputs": ["lockout_status", "hazard_assessment", "supervisor_notification"],
                "pass_criteria": ["Isolation confirmed", "Supervisor notified"],
                "risk_level": "CRITICAL",
            },
            {
                "step_id": "brake-pressure",
                "title": "Brake Pressure Verification",
                "instructions": "Measure line pressure stability and inspect for pressure drop/leak.",
                "required_inputs": ["line_pressure", "pressure_drop_test", "leak_inspection_notes"],
                "pass_criteria": ["Pressure stable", "No critical leak found"],
                "risk_level": "HIGH",
            },
            {
                "step_id": "brake-controls",
                "title": "ABS/Control Module Diagnostics",
                "instructions": "Read active faults and validate sensor/module communication.",
                "required_inputs": ["abs_dtcs", "sensor_signal_check", "module_comm_status"],
                "pass_criteria": ["Fault source localized", "Communication verified"],
                "risk_level": "HIGH",
            },
        ],
        "fuel": [
            {
                "step_id": "fuel-baseline",
                "title": "Fuel Delivery Baseline",
                "instructions": "Capture rail pressure and response during idle and loaded conditions.",
                "required_inputs": ["fuel_rail_pressure", "load_condition", "throttle_response_notes"],
                "pass_criteria": ["Pressure trend recorded", "No unstable delivery behavior"],
                "risk_level": "MEDIUM",
            },
            {
                "step_id": "fuel-restriction",
                "title": "Restriction and Filter Check",
                "instructions": "Inspect filters/lines for blockage or flow restriction.",
                "required_inputs": ["filter_condition", "line_restriction_notes", "flow_assessment"],
                "pass_criteria": ["No severe restriction", "Flow path validated"],
                "risk_level": "MEDIUM",
            },
            {
                "step_id": "fuel-electrical",
                "title": "Injector and Harness Verification",
                "instructions": "Validate injector command quality and harness continuity.",
                "required_inputs": ["injector_command_state", "harness_continuity", "connector_condition"],
                "pass_criteria": ["Injector command valid", "Harness integrity confirmed"],
                "risk_level": "HIGH",
            },
        ],
        "lubrication": [
            {
                "step_id": "lube-baseline",
                "title": "Oil Pressure Baseline",
                "instructions": "Compare sensor-reported oil pressure against mechanical reference.",
                "required_inputs": ["sensor_pressure", "mechanical_gauge_pressure", "engine_state"],
                "pass_criteria": ["Readings correlated", "Pressure within spec"],
                "risk_level": "HIGH" if safety_flag else "MEDIUM",
            },
            {
                "step_id": "lube-integrity",
                "title": "Oil Circuit Integrity",
                "instructions": "Inspect oil level, grade, filter condition, and external leaks.",
                "required_inputs": ["oil_level", "oil_grade", "filter_condition", "leak_notes"],
                "pass_criteria": ["Oil system integrity verified", "No critical leak"],
                "risk_level": "MEDIUM",
            },
        ],
        "general": [
            {
                "step_id": "general-scan",
                "title": "ECU Fault Baseline",
                "instructions": "Capture active DTCs, freeze-frame, and system context.",
                "required_inputs": ["active_dtcs", "freeze_frame", "operating_context"],
                "pass_criteria": ["Fault context captured", "Baseline established"],
                "risk_level": "MEDIUM",
            },
            {
                "step_id": "general-physical",
                "title": "Physical and Harness Inspection",
                "instructions": "Inspect connectors, harness routing, and visible component condition.",
                "required_inputs": ["connector_notes", "harness_notes", "component_visual_notes"],
                "pass_criteria": ["Visual evidence captured", "Likely fault area narrowed"],
                "risk_level": "MEDIUM",
            },
            {
                "step_id": "general-functional",
                "title": "Targeted Functional Test",
                "instructions": "Run subsystem test to validate suspected root cause.",
                "required_inputs": ["test_procedure", "observed_result", "measurement_value"],
                "pass_criteria": ["Test outcome recorded", "Root-cause confidence increased"],
                "risk_level": "MEDIUM",
            },
        ],
    }
    return [dict(item) for item in playbooks.get(domain, playbooks["general"])]


def _build_actionable_workflow_core(
    triage: dict[str, Any],
    evidence: dict[str, Any],
    scheduler: dict[str, Any],
    workflow_mode: str = WORKFLOW_MODE_FIX_PLAN,
) -> list[dict[str, Any]]:
    workflow: list[dict[str, Any]] = []
    source_steps = triage.get("workflow_steps", [])
    base_steps: list[dict[str, Any]] = []
    safety_flag = bool(triage.get("safety_flag", False))
    suppress_repair_guidance = workflow_mode == WORKFLOW_MODE_INVESTIGATION_ONLY
    default_step_kind = "investigate" if suppress_repair_guidance else "diagnose"
    domain = _detect_workflow_domain(triage, evidence)

    if not suppress_repair_guidance and not source_steps:
        for item in _domain_playbook_steps(domain, safety_flag):
            base_steps.append(
                {
                    "step_id": item.get("step_id"),
                    "step_order": len(base_steps) + 1,
                    "title": item.get("title"),
                    "instructions": item.get("instructions"),
                    "required_inputs": item.get("required_inputs", []),
                    "pass_criteria": item.get("pass_criteria", []),
                    "risk_level": item.get("risk_level", "MEDIUM"),
                    "status": "pending",
                    "agent_id": "triage_agent",
                    "step_kind": default_step_kind,
                }
            )

    for index, step in enumerate(source_steps, start=1):
        base_steps.append(
            {
                "step_id": step.get("step_id", f"step-{index}"),
                "step_order": int(step.get("step_order", index)),
                "title": step.get("title", f"Diagnostic step {index}"),
                "instructions": step.get("instructions", ""),
                "required_inputs": step.get("required_inputs", []),
                "pass_criteria": step.get("pass_criteria", []),
                "risk_level": step.get("risk_level", "MEDIUM"),
                "status": step.get("status", "pending"),
                "agent_id": "triage_agent",
                "step_kind": step.get("step_kind", default_step_kind),
            }
        )

    if suppress_repair_guidance and not base_steps:
        for item in _domain_playbook_steps(domain, safety_flag):
            base_steps.append(
                {
                    "step_id": item.get("step_id"),
                    "step_order": len(base_steps) + 1,
                    "title": item.get("title"),
                    "instructions": item.get("instructions"),
                    "required_inputs": item.get("required_inputs", []),
                    "pass_criteria": item.get("pass_criteria", []),
                    "risk_level": item.get("risk_level", "MEDIUM"),
                    "status": "pending",
                    "agent_id": "triage_agent",
                    "step_kind": default_step_kind,
                }
            )

    if not base_steps:
        for index, step_text in enumerate(triage.get("next_steps", []), start=1):
            base_steps.append(
                {
                    "step_id": f"step-{index}",
                    "step_order": index,
                    "title": f"Diagnostic step {index}",
                    "instructions": step_text,
                    "required_inputs": ["observation_notes"],
                    "pass_criteria": ["Result captured"],
                    "risk_level": "MEDIUM",
                    "status": "pending",
                    "agent_id": "triage_agent",
                    "step_kind": default_step_kind,
                }
            )
    else:
        for item in triage.get("next_steps", []):
            instruction = str(item).strip()
            if not instruction:
                continue
            if any(instruction.lower() in str(step.get("instructions", "")).lower() for step in base_steps):
                continue
            next_id = len(base_steps) + 1
            base_steps.append(
                {
                    "step_id": f"triage-guidance-{next_id}",
                    "step_order": next_id,
                    "title": f"Triage Directed Check {next_id}",
                    "instructions": instruction,
                    "required_inputs": ["observation_notes", "measurement_value"],
                    "pass_criteria": ["Guidance check completed", "Outcome recorded"],
                    "risk_level": _infer_risk_from_instruction(instruction, safety_flag),
                    "status": "pending",
                    "agent_id": "triage_agent",
                    "step_kind": default_step_kind,
                }
            )

    parts_by_step = evidence.get("parts_by_step", [])
    step_to_parts = {item.get("step_id_hint"): item.get("recommended_parts", []) for item in parts_by_step}
    fallback_parts = evidence.get("parts_candidates", [])[:3]
    sorted_steps = sorted(base_steps, key=lambda item: int(item.get("step_order", 999)))
    for index, step in enumerate(sorted_steps, start=1):
        raw_risk = str(step.get("risk_level", "MEDIUM")).upper()
        risk_level = raw_risk if raw_risk in {"LOW", "MEDIUM", "HIGH", "CRITICAL"} else "MEDIUM"
        required_inputs = _normalize_string_list(
            step.get("required_inputs"),
            ["observation_notes", "measurement_value"],
        )
        pass_criteria = _normalize_string_list(
            step.get("pass_criteria"),
            ["Evidence captured", "Result recorded"],
        )
        recommended_parts = []
        if not suppress_repair_guidance:
            recommended_parts = _normalize_string_list(
                step_to_parts.get(step.get("step_id")) or step.get("recommended_parts"),
                fallback_parts,
            )
        base_instruction = str(step.get("instructions", "")).strip()
        title = str(step.get("title", "")).strip()
        if not title or title.lower().startswith("diagnostic step"):
            title = _suggest_step_title(base_instruction, index)

        workflow.append(
            {
                "step_id": str(step.get("step_id", f"step-{index}")),
                "step_order": index,
                "title": title,
                "instructions": _compose_actionable_instruction(
                    base_instruction,
                    required_inputs,
                    pass_criteria,
                    recommended_parts,
                    risk_level,
                    suppress_repair_guidance=suppress_repair_guidance,
                ),
                "required_inputs": required_inputs,
                "pass_criteria": pass_criteria,
                "risk_level": risk_level,
                "status": str(step.get("status", "pending")),
                "agent_id": str(step.get("agent_id", "triage_agent")),
                "recommended_parts": recommended_parts,
                "step_kind": str(step.get("step_kind", default_step_kind)),
                "suppressed": int(suppress_repair_guidance),
                "workflow_mode": workflow_mode,
            }
        )

    observation_text = _context_observation_text(triage)
    if observation_text:
        observation_parts = [] if suppress_repair_guidance else fallback_parts
        workflow.append(
            {
                "step_id": "step-context-observation",
                "step_order": len(workflow) + 1,
                "title": "Technician Observation Validation",
                "instructions": _compose_actionable_instruction(
                    f'Validate this field observation against collected diagnostics: "{observation_text}".',
                    ["observation_confirmation", "variance_notes"],
                    ["Observation reconciled with measurements", "Any variance documented"],
                    observation_parts,
                    _infer_risk_from_instruction(observation_text, safety_flag=bool(triage.get("safety_flag", False))),
                    suppress_repair_guidance=suppress_repair_guidance,
                ),
                "required_inputs": ["observation_confirmation", "variance_notes"],
                "pass_criteria": ["Observation reconciled with measurements", "Any variance documented"],
                "risk_level": _infer_risk_from_instruction(
                    observation_text,
                    safety_flag=bool(triage.get("safety_flag", False)),
                ),
                "status": "pending",
                "agent_id": "triage_agent",
                "recommended_parts": observation_parts,
                "step_kind": "investigate",
                "suppressed": int(suppress_repair_guidance),
                "workflow_mode": workflow_mode,
            }
        )

    checkpoints = scheduler.get("checkpoints", [])
    if checkpoints:
        priority_hint = str(scheduler.get("priority_hint", "NORMAL")).upper()
        workflow.append(
            {
                "step_id": "step-schedule-checkpoint",
                "step_order": len(workflow) + 1,
                "title": "Scheduling and Dispatch Checkpoint",
                "instructions": _compose_actionable_instruction(
                    "; ".join(checkpoints),
                    ["dispatch_confirmation", "eta_commitment"],
                    [f"Dispatch confirmed at priority {priority_hint}", "ETA communicated"],
                    [],
                    "LOW",
                    suppress_repair_guidance=suppress_repair_guidance,
                ),
                "required_inputs": ["checkpoint_confirmation"],
                "pass_criteria": ["Checkpoint acknowledged"],
                "risk_level": "LOW",
                "status": "pending",
                "agent_id": "scheduler_agent",
                "recommended_parts": [],
                "step_kind": "handoff",
                "suppressed": int(suppress_repair_guidance),
                "workflow_mode": workflow_mode,
            }
        )

    if suppress_repair_guidance:
        workflow.append(
            {
                "step_id": "step-supervisor-evidence-handoff",
                "step_order": len(workflow) + 1,
                "title": "Quote Preparation Handoff",
                "instructions": _compose_actionable_instruction(
                    "Finalize evidence summary and unresolved questions for quote preparation. Do not perform repair actions until customer approval.",
                    ["evidence_summary", "open_questions", "handoff_notes"],
                    ["Evidence package complete", "Quote package ready for customer"],
                    [],
                    "MEDIUM",
                    suppress_repair_guidance=True,
                ),
                "required_inputs": ["evidence_summary", "open_questions", "handoff_notes"],
                "pass_criteria": ["Evidence package complete", "Quote package ready for customer"],
                "risk_level": "MEDIUM",
                "status": "pending",
                "agent_id": "orchestrator",
                "recommended_parts": [],
                "step_kind": "handoff",
                "suppressed": 1,
                "workflow_mode": workflow_mode,
            }
        )
    else:
        workflow.append(
            {
                "step_id": "step-final-handoff",
                "step_order": len(workflow) + 1,
                "title": "Final Handoff and Report Lock",
                "instructions": _compose_actionable_instruction(
                    "Finalize diagnosis summary, confirm parts readiness, and prepare back-office handoff.",
                    ["repair_plan_summary", "parts_confirmation", "handoff_notes"],
                    ["Service report finalized", "Handoff complete"],
                    fallback_parts,
                    "MEDIUM",
                    suppress_repair_guidance=False,
                ),
                "required_inputs": ["repair_plan_summary", "parts_confirmation", "handoff_notes"],
                "pass_criteria": ["Service report finalized", "Handoff complete"],
                "risk_level": "MEDIUM",
                "status": "pending",
                "agent_id": "orchestrator",
                "recommended_parts": fallback_parts,
                "step_kind": "handoff",
                "suppressed": 0,
                "workflow_mode": workflow_mode,
            }
        )

    return workflow


def _build_actionable_workflow(
    triage: dict[str, Any],
    evidence: dict[str, Any],
    scheduler: dict[str, Any],
    workflow_mode: str = WORKFLOW_MODE_FIX_PLAN,
    offline_mode: bool = False,
) -> list[dict[str, Any]]:
    if workflow_mode == WORKFLOW_MODE_INVESTIGATION_ONLY:
        checklist = gathering_agent.build_checklist(
            triage=triage,
            evidence=evidence,
            scheduler=scheduler,
            workflow_builder=_build_actionable_workflow_core,
            offline_mode=offline_mode,
        )
        return checklist.get("workflow_steps", [])

    repair_plan = repair_agent.build_repair_plan(
        triage=triage,
        evidence=evidence,
        scheduler=scheduler,
        workflow_builder=_build_actionable_workflow_core,
        offline_mode=offline_mode,
    )
    return repair_plan.get("workflow_steps", [])


def _build_service_report_template(
    payload: dict[str, Any],
    triage: dict[str, Any],
    evidence: dict[str, Any],
    scheduler: dict[str, Any],
    requires_approval: bool,
    workflow_mode: str,
) -> str:
    manual_refs = ", ".join(ref.get("title", "N/A") for ref in evidence.get("manual_refs", [])) or "None"
    suppress_repair_guidance = workflow_mode == WORKFLOW_MODE_INVESTIGATION_ONLY
    parts = "Suppressed pending customer approval." if suppress_repair_guidance else (
        ", ".join(evidence.get("parts_candidates", [])) or "None"
    )
    safety_notes = []
    if triage.get("safety_flag"):
        safety_notes.append("Triage flagged potential unsafe condition.")
    if requires_approval:
        safety_notes.append("Supervisor approval required before release.")
    if not safety_notes:
        safety_notes.append("No immediate safety or warranty escalation detected.")

    lines = [
        "Customer complaint",
        f"- Customer: {payload.get('customer_name', 'N/A') or 'N/A'}",
        f"- Contact: {payload.get('customer_phone', 'N/A') or payload.get('customer_email', 'N/A') or 'N/A'}",
        f"- Equipment: {payload.get('equipment_id', 'N/A')}",
        f"- Fault code: {payload.get('fault_code', 'N/A')}",
        f"- Symptoms: {payload.get('symptoms', 'N/A')}",
        "",
        "Observations",
        f"- {triage.get('summary', 'No summary available.')}",
        "",
        "Diagnostics performed",
        *[f"- {step}" for step in triage.get("next_steps", [])],
        "",
        "Manual references used",
        f"- {manual_refs}",
        "",
        "Parts considered",
        f"- {parts}",
        "",
        "Actions taken (proposed)",
    ]
    if suppress_repair_guidance:
        lines.extend(
            [
                "- Gather diagnostic evidence requested in checklist.",
                "- Capture unresolved questions for quote creation.",
                "- Repair guidance intentionally suppressed until customer approval.",
            ]
        )
    else:
        lines.extend(
            [
                f"- Priority hint: {scheduler.get('priority_hint', 'NORMAL')}",
                f"- ETA bucket: {scheduler.get('eta_bucket', '12-24h')}",
            ]
        )
    lines.extend(
        [
            "",
            "Safety/warranty notes",
            *[f"- {note}" for note in safety_notes],
            "",
            "Next steps",
        ]
    )
    if suppress_repair_guidance:
        lines.extend(
            [
                "- Complete investigation checklist and evidence package.",
                "- Generate quote and draft customer communication.",
                "- Wait for customer approval before attempting fixes.",
            ]
        )
    else:
        lines.extend(
            [
                "- Execute diagnostic checks and confirm root cause.",
                "- Update customer approval/repair pool status as needed.",
                "- Proceed with repair plan after authorization.",
            ]
        )
    return "\n".join(lines)


def _generate_service_report(
    payload: dict[str, Any],
    triage: dict[str, Any],
    evidence: dict[str, Any],
    scheduler: dict[str, Any],
    requires_approval: bool,
    workflow_mode: str,
    offline_mode: bool = False,
) -> tuple[str, dict[str, Any]]:
    context = {
        "payload": payload,
        "triage": triage,
        "evidence": evidence,
        "scheduler": scheduler,
        "requires_approval": requires_approval,
        "workflow_mode": workflow_mode,
        "suppressed_guidance": workflow_mode == WORKFLOW_MODE_INVESTIGATION_ONLY,
    }
    context_json = json.dumps(context, sort_keys=True)
    prompt = SERVICE_REPORT_PROMPT_TEMPLATE.format(context_json=context_json)
    prompt_hash = _hash_text(prompt)
    llm_text, llm_meta = triage_agent.run_ollama_prompt(prompt, expect_json=False, offline_mode=offline_mode)

    required_headings = [
        "customer complaint",
        "observations",
        "diagnostics performed",
        "manual references used",
        "parts considered",
        "actions taken (proposed)",
        "safety/warranty notes",
        "next steps",
    ]
    if isinstance(llm_text, str):
        lower_text = llm_text.lower()
        if all(heading in lower_text for heading in required_headings):
            if workflow_mode == WORKFLOW_MODE_INVESTIGATION_ONLY:
                blocked_fix_terms = {"replace", "repair", "install", "swap", "torque"}
                if not any(term in lower_text for term in blocked_fix_terms):
                    return llm_text.strip(), {
                        "prompt_template_id": "service_report_v1",
                        "prompt_hash": prompt_hash,
                        "context_hash": _hash_text(context_json),
                        **llm_meta,
                    }
            else:
                return llm_text.strip(), {
                    "prompt_template_id": "service_report_v1",
                    "prompt_hash": prompt_hash,
                    "context_hash": _hash_text(context_json),
                    **llm_meta,
                }

    fallback = _build_service_report_template(
        payload,
        triage,
        evidence,
        scheduler,
        requires_approval,
        workflow_mode,
    )
    return fallback, {
        "prompt_template_id": "service_report_v1",
        "prompt_hash": prompt_hash,
        "context_hash": _hash_text(context_json),
        **llm_meta,
        "used_fallback_template": True,
    }


def _extract_triage_replan_payload(job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    final = job.get("final_response_json") or {}
    triage_data = final.get("triage") if isinstance(final.get("triage"), dict) else {}
    evidence_data = final.get("evidence") if isinstance(final.get("evidence"), dict) else {}
    schedule_data = final.get("schedule_hint") if isinstance(final.get("schedule_hint"), dict) else {}
    return triage_data, evidence_data, schedule_data


def _collect_diagnostic_context(local_conn: Any, job_id: str) -> dict[str, Any]:
    events = db.fetch_workflow_events(local_conn, job_id)
    step_results: list[dict[str, Any]] = []
    notes: list[str] = []
    completed = 0
    blocked_or_failed = 0

    for event in events:
        if str(event.get("event_type", "")).upper() != "STEP_RESULT":
            continue
        input_json = event.get("input_json") or {}
        status = str(input_json.get("status", "")).lower()
        step_id = str(event.get("step_id") or input_json.get("step_id") or "")
        step_note = str(input_json.get("notes") or "").strip()
        measurement_json = input_json.get("measurement_json") or {}
        measurement_value = ""
        if isinstance(measurement_json, dict):
            measurement_value = str(measurement_json.get("value") or "").strip()

        if status == "done":
            completed += 1
        if status in {"blocked", "failed"}:
            blocked_or_failed += 1

        summary_line = " ".join(
            part
            for part in [
                f"Step {step_id}" if step_id else "",
                f"status={status}" if status else "",
                f"measurement={measurement_value}" if measurement_value else "",
                f"note={step_note}" if step_note else "",
            ]
            if part
        ).strip()
        if summary_line:
            notes.append(summary_line)

        step_results.append(
            {
                "step_id": step_id,
                "status": status,
                "measurement_value": measurement_value,
                "notes": step_note,
                "ts": event.get("ts"),
            }
        )

    notes = notes[-8:]
    context_text = " | ".join(notes)
    return {
        "step_results": step_results,
        "step_result_count": len(step_results),
        "completed_count": completed,
        "blocked_or_failed_count": blocked_or_failed,
        "summary_lines": notes,
        "context_text": context_text,
    }


def _build_updated_job_row(
    job: dict[str, Any],
    *,
    now: str,
    status: str,
    final_response: dict[str, Any],
    requires_approval: bool,
    approved_by: str | None = None,
    approved_ts: str | None = None,
    approval_due_ts: str | None = None,
    timed_out: int | None = None,
    assigned_tech_id: str | None = None,
    workflow_mode: str | None = None,
) -> dict[str, Any]:
    return {
        "job_id": job["job_id"],
        "created_ts": job["created_ts"],
        "updated_ts": now,
        "status": status,
        "field_payload_json": job.get("field_payload_json") or {},
        "final_response_json": final_response,
        "requires_approval": int(bool(requires_approval)),
        "approved_by": approved_by,
        "approved_ts": approved_ts,
        "guided_question": job.get("guided_question"),
        "guided_answer": job.get("guided_answer"),
        "approval_due_ts": approval_due_ts,
        "timed_out": int(job.get("timed_out", 0) if timed_out is None else timed_out),
        "first_occurrence_fault": int(job.get("first_occurrence_fault", 0)),
        "assigned_tech_id": assigned_tech_id if assigned_tech_id is not None else job.get("assigned_tech_id"),
        "workflow_mode": workflow_mode if workflow_mode is not None else job.get("workflow_mode"),
    }


def _quote_agent_inputs(job: dict[str, Any], *, offline_mode: bool) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    payload = dict(job.get("field_payload_json") or {})
    triage_data, evidence_data, schedule_data = _extract_triage_replan_payload(job)
    if not triage_data:
        triage_data = _strip_triage_meta(triage_agent.analyze(payload, offline_mode=offline_mode))
    if not evidence_data:
        evidence_data = parts_agent.collect_evidence(payload, triage_data)
    if not schedule_data:
        schedule_data = scheduler_agent.forecast(payload)
    return payload, triage_data, evidence_data, schedule_data


def _queue_offline_events(
    local_conn: Any,
    job_row: dict[str, Any],
    log_entries: list[dict[str, Any]],
    workflow_steps: list[dict[str, Any]] | None = None,
    workflow_events: list[dict[str, Any]] | None = None,
    metric_events: list[dict[str, Any]] | None = None,
    alert_events: list[dict[str, Any]] | None = None,
    issue_attachments: list[dict[str, Any]] | None = None,
) -> None:
    for entry in log_entries:
        if not _is_escalation_log_entry(entry):
            continue
        db.enqueue_sync_event(
            local_conn,
            ts=entry["ts"],
            entity="decision_log",
            entity_id=f"{entry['job_id']}:{entry['agent_id']}:{entry['action']}",
            payload=entry,
        )
    db.enqueue_sync_event(
        local_conn,
        ts=job_row["updated_ts"],
        entity="job",
        entity_id=job_row["job_id"],
        payload=job_row,
    )
    if workflow_steps is not None:
        db.enqueue_sync_event(
            local_conn,
            ts=job_row["updated_ts"],
            entity="workflow_steps_replace",
            entity_id=job_row["job_id"],
            payload={
                "job_id": job_row["job_id"],
                "steps": workflow_steps,
                "ts": job_row["updated_ts"],
                "agent_id": "orchestrator",
            },
        )
    for event in workflow_events or []:
        db.enqueue_sync_event(
            local_conn,
            ts=event.get("ts", job_row["updated_ts"]),
            entity="workflow_event",
            entity_id=f"{event.get('job_id')}:{event.get('event_type')}:{event.get('step_id')}",
            payload=event,
        )
    for metric in metric_events or []:
        db.enqueue_sync_event(
            local_conn,
            ts=job_row["updated_ts"],
            entity="agent_metric",
            entity_id=f"{metric.get('day')}:{metric.get('agent_id')}:{metric.get('counter')}",
            payload=metric,
        )
    for alert in alert_events or []:
        db.enqueue_sync_event(
            local_conn,
            ts=alert["ts"],
            entity="supervisor_alert",
            entity_id=f"{alert.get('job_id', 'global')}:{alert['alert_type']}",
            payload=alert,
        )
    for attachment in issue_attachments or []:
        db.enqueue_sync_event(
            local_conn,
            ts=str(attachment.get("created_ts", job_row["updated_ts"])),
            entity="attachment_upsert",
            entity_id=str(attachment.get("attachment_id")),
            payload=attachment,
        )
        db.enqueue_sync_event(
            local_conn,
            ts=str(attachment.get("created_ts", job_row["updated_ts"])),
            entity="attachment_file_copy",
            entity_id=str(attachment.get("attachment_id")),
            payload={
                "attachment_id": attachment.get("attachment_id"),
                "job_id": attachment.get("job_id"),
                "local_rel_path": attachment.get("local_rel_path"),
            },
        )


def _mirror_online_to_server(
    job_row: dict[str, Any],
    log_entries: list[dict[str, Any]],
    workflow_steps: list[dict[str, Any]] | None = None,
    workflow_events: list[dict[str, Any]] | None = None,
    metric_events: list[dict[str, Any]] | None = None,
    alert_events: list[dict[str, Any]] | None = None,
    issue_attachments: list[dict[str, Any]] | None = None,
) -> None:
    with db.open_server_connection() as server_conn:
        for entry in log_entries:
            db.insert_decision_log(server_conn, entry)
        db.upsert_job_lww(server_conn, job_row)
        if workflow_steps is not None:
            db.replace_workflow_steps(
                server_conn,
                job_id=job_row["job_id"],
                steps=workflow_steps,
                ts=job_row["updated_ts"],
                agent_id="orchestrator",
            )
        for event in workflow_events or []:
            db.insert_workflow_event(server_conn, event)
        for metric_event in metric_events or []:
            db.apply_metric_event(server_conn, metric_event)
        for alert in alert_events or []:
            db.insert_supervisor_alert(server_conn, alert)
        for attachment in issue_attachments or []:
            copied = _copy_attachment_local_to_server(attachment)
            db.upsert_issue_attachment(server_conn, copied)
            db.refresh_issue_attachment_summary(server_conn, str(copied.get("job_id")))
        server_conn.commit()


@app.get("/api/demo/scenarios")
def get_demo_scenarios() -> dict[str, Any]:
    scenarios = [*DEMO_SCENARIOS, *SIMILARITY_DEMO_SCENARIOS]
    return {
        "count": len(scenarios),
        "scenarios": scenarios,
        "history_seed_job_ids": [item["job_id"] for item in HISTORY_SEED_JOBS],
    }


@app.post("/api/demo/history/reset")
def reset_demo_history(request: DemoHistoryResetRequest | None = None) -> dict[str, Any]:
    clear_server = True if request is None else bool(request.clear_server)
    seeded = _reset_history_demo_data(clear_server=clear_server)
    return {
        "status": "ok",
        "message": "Demo history reset completed.",
        **seeded,
        "similarity_demo_scenarios": SIMILARITY_DEMO_SCENARIOS,
    }


@app.post("/api/job/intake")
def intake_job(request: JobIntakeRequest) -> dict[str, Any]:
    now = _utc_now()
    payload = request.model_dump()
    payload["job_id"] = payload.get("job_id") or str(uuid.uuid4())
    payload["is_offline"] = bool(payload.get("is_offline", False))
    payload["request_supervisor_review"] = bool(payload.get("request_supervisor_review", False))
    payload, normalization_meta = _normalize_issue_payload(payload)
    if str(payload.get("issue_text", "")).strip():
        return create_job(JobSubmitRequest(**payload))
    job_id = payload["job_id"]
    offline_mode = _is_offline(request.is_offline)
    mode_effective = _mode_effective(request.is_offline)
    runtime_models = _runtime_model_config(mode_effective)

    with db.open_local_connection() as local_conn:
        existing_job = db.get_job(local_conn, job_id)
        log_entries: list[dict[str, Any]] = []

        guided = triage_agent.generate_guided_question(payload, offline_mode=offline_mode)
        guided_question = str(guided.get("question", "")).strip()
        occurrence_check_applied = _should_check_first_occurrence(payload)
        first_occurrence_fault = (
            db.is_first_occurrence_fault(
                local_conn,
                equipment_id=str(payload.get("equipment_id", "")),
                fault_code=str(payload.get("fault_code", "")),
                current_job_id=job_id,
            )
            if occurrence_check_applied
            else False
        )
        precheck_reasons = _evaluate_escalation_reasons(
            manual_request=bool(payload.get("request_supervisor_review")),
            first_occurrence_fault=first_occurrence_fault,
        )
        requires_approval_precheck = bool(precheck_reasons)

        received_entry = _build_log_entry(
            ts=now,
            job_id=job_id,
            agent_id="orchestrator",
            action="JOB_RECEIVED_INTAKE",
            input_json={
                "payload": payload,
                "normalization_meta": normalization_meta,
            },
            output_json={"accepted": True, "offline_mode": offline_mode},
            confidence=1.0,
        )
        model_route_entry = _build_model_route_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            mode_effective=mode_effective,
            runtime_models=runtime_models,
        )
        question_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="triage_agent",
            action="GUIDED_QUESTION_GENERATED",
            input_json={"payload": payload},
            output_json={
                "guided_question": guided_question,
                "question_rationale": guided.get("rationale", ""),
                "question_confidence": guided.get("confidence", 0.0),
                "mode_effective": mode_effective,
                "model_selected": runtime_models["model_selected"],
                "model_tier": runtime_models["model_tier"],
            },
            confidence=float(guided.get("confidence", 0.0)),
        )
        occurrence_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="approval_logic",
            action="FIRST_OCCURRENCE_CHECK",
            input_json={
                "equipment_id": payload.get("equipment_id"),
                "fault_code": payload.get("fault_code"),
            },
            output_json={
                "first_occurrence_fault": first_occurrence_fault,
                "check_applied": occurrence_check_applied,
                "precheck_reasons": precheck_reasons,
            },
            confidence=1.0,
            requires_human=int(requires_approval_precheck),
        )
        for entry in [received_entry, model_route_entry, question_entry, occurrence_entry]:
            db.insert_decision_log(local_conn, entry)
            log_entries.append(entry)

        workflow_mode = _derive_workflow_mode(
            status="AWAITING_GUIDED_ANSWER",
            requires_approval=requires_approval_precheck,
        )
        workflow_meta = _workflow_mode_metadata(workflow_mode)
        final_response = {
            "job_id": job_id,
            "status": "AWAITING_GUIDED_ANSWER",
            "guided_question": guided_question,
            "requires_approval_precheck": requires_approval_precheck,
            "precheck_reasons": precheck_reasons,
            "governance_policy_version": GOVERNANCE_POLICY_VERSION,
            "escalation_policy_version": RISK_SIGNAL_POLICY_VERSION,
            "policy_config_hash": ESCALATION_POLICY_HASH,
            "mode_effective": mode_effective,
            "model_selected": runtime_models["model_selected"],
            "model_online": runtime_models["model_online"],
            "model_offline": runtime_models["model_offline"],
            "model_tier": runtime_models["model_tier"],
            "model_policy_valid": runtime_models["model_policy_valid"],
            "model_policy_notes": runtime_models["model_policy_notes"],
            "workflow_mode": workflow_mode,
            "workflow_intent": workflow_meta["workflow_intent"],
            "allowed_actions": workflow_meta["allowed_actions"],
            "suppressed_guidance": workflow_meta["suppressed_guidance"],
        }
        job_row = {
            "job_id": job_id,
            "created_ts": existing_job["created_ts"] if existing_job else now,
            "updated_ts": _utc_now(),
            "status": "AWAITING_GUIDED_ANSWER",
            "field_payload_json": payload,
            "final_response_json": final_response,
            "requires_approval": int(requires_approval_precheck),
            "approved_by": None,
            "approved_ts": None,
            "guided_question": guided_question,
            "guided_answer": None,
            "approval_due_ts": None,
            "timed_out": 0,
            "first_occurrence_fault": int(first_occurrence_fault),
            "assigned_tech_id": None,
            "workflow_mode": workflow_mode,
        }
        db.upsert_job(local_conn, job_row)

        if offline_mode:
            _queue_offline_events(local_conn, job_row, log_entries)
        else:
            _mirror_online_to_server(job_row, log_entries)
        local_conn.commit()
        return final_response


@app.post("/api/job/{job_id}/guided-answer")
def submit_guided_answer(job_id: str, request: GuidedAnswerRequest) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        payload = dict(job.get("field_payload_json") or {})
    payload["job_id"] = job_id
    payload["guided_answer"] = request.answer_text
    return create_job(JobSubmitRequest(**payload))


@app.post("/api/job")
def create_job(request: JobSubmitRequest) -> dict[str, Any]:
    now = _utc_now()
    payload = request.model_dump()
    payload["job_id"] = payload.get("job_id") or str(uuid.uuid4())
    payload["is_offline"] = bool(payload.get("is_offline", False))
    payload["request_supervisor_review"] = bool(payload.get("request_supervisor_review", False))
    payload["guided_answer"] = str(payload.get("guided_answer") or "").strip()
    raw_payload = dict(payload)
    payload, normalization_meta = _normalize_issue_payload(payload)
    payload["guided_answer"] = str(payload.get("guided_answer") or "").strip()
    job_id = payload["job_id"]
    offline_mode = _is_offline(request.is_offline)
    mode_effective = _mode_effective(request.is_offline)
    runtime_models = _runtime_model_config(mode_effective)

    with db.open_local_connection() as local_conn:
        existing_job = db.get_job(local_conn, job_id)
        log_entries: list[dict[str, Any]] = []
        workflow_events: list[dict[str, Any]] = []
        metric_events: list[dict[str, Any]] = []

        received_entry = _build_log_entry(
            ts=now,
            job_id=job_id,
            agent_id="orchestrator",
            action="JOB_RECEIVED",
            input_json=raw_payload,
            output_json={"accepted": True, "offline_mode": offline_mode},
            confidence=1.0,
        )
        db.insert_decision_log(local_conn, received_entry)
        log_entries.append(received_entry)

        normalization_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="orchestrator",
            action="FREE_TEXT_NORMALIZED",
            input_json={
                "issue_text": raw_payload.get("issue_text"),
                "equipment_id": raw_payload.get("equipment_id"),
                "fault_code": raw_payload.get("fault_code"),
                "symptoms": raw_payload.get("symptoms"),
                "notes": raw_payload.get("notes"),
            },
            output_json={
                "equipment_id": payload.get("equipment_id"),
                "fault_code": payload.get("fault_code"),
                "symptoms": payload.get("symptoms"),
                "notes": payload.get("notes"),
                "normalization_meta": normalization_meta,
            },
            confidence=float(normalization_meta.get("normalization_confidence", 0.0)),
        )
        db.insert_decision_log(local_conn, normalization_entry)
        log_entries.append(normalization_entry)

        model_route_entry = _build_model_route_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            mode_effective=mode_effective,
            runtime_models=runtime_models,
        )
        db.insert_decision_log(local_conn, model_route_entry)
        log_entries.append(model_route_entry)

        existing_final = (existing_job or {}).get("final_response_json") or {}
        existing_guided_question = ""
        if existing_job:
            existing_guided_question = str(existing_job.get("guided_question") or "").strip()
        guided_question = str(existing_final.get("guided_question") or existing_guided_question).strip()
        if not guided_question:
            guided_question_obj = triage_agent.generate_guided_question(payload, offline_mode=offline_mode)
            guided_question = str(guided_question_obj.get("question", "")).strip()
            question_entry = _build_log_entry(
                ts=_utc_now(),
                job_id=job_id,
                agent_id="triage_agent",
                action="GUIDED_QUESTION_GENERATED",
                input_json={"payload": payload},
                output_json={
                    "guided_question": guided_question,
                    "question_rationale": guided_question_obj.get("rationale", ""),
                    "question_confidence": guided_question_obj.get("confidence", 0.0),
                    "mode_effective": mode_effective,
                    "model_selected": runtime_models["model_selected"],
                    "model_tier": runtime_models["model_tier"],
                },
                confidence=float(guided_question_obj.get("confidence", 0.0)),
            )
            db.insert_decision_log(local_conn, question_entry)
            log_entries.append(question_entry)

        guided_answer = str(payload.get("guided_answer", "")).strip()
        if not guided_answer:
            guided_answer = _default_guided_answer(payload)
            compat_entry = _build_log_entry(
                ts=_utc_now(),
                job_id=job_id,
                agent_id="orchestrator",
                action="GUIDED_COMPAT_FALLBACK_USED",
                input_json={"has_guided_answer": False},
                output_json={"guided_answer": guided_answer},
                confidence=0.6,
            )
            db.insert_decision_log(local_conn, compat_entry)
            log_entries.append(compat_entry)

        guided_answer_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="field_technician",
            action="GUIDED_ANSWER_RECEIVED",
            input_json={"guided_question": guided_question},
            output_json={"guided_answer": guided_answer},
            confidence=1.0,
        )
        db.insert_decision_log(local_conn, guided_answer_entry)
        log_entries.append(guided_answer_entry)
        payload["guided_question"] = guided_question
        payload["guided_answer"] = guided_answer

        occurrence_check_applied = _should_check_first_occurrence(payload)
        first_occurrence_fault = (
            db.is_first_occurrence_fault(
                local_conn,
                equipment_id=str(payload.get("equipment_id", "")),
                fault_code=str(payload.get("fault_code", "")),
                current_job_id=job_id,
            )
            if occurrence_check_applied
            else False
        )
        occurrence_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="approval_logic",
            action="FIRST_OCCURRENCE_CHECK",
            input_json={
                "equipment_id": payload.get("equipment_id"),
                "fault_code": payload.get("fault_code"),
            },
            output_json={
                "first_occurrence_fault": first_occurrence_fault,
                "check_applied": occurrence_check_applied,
            },
            confidence=1.0,
            requires_human=int(first_occurrence_fault),
        )
        db.insert_decision_log(local_conn, occurrence_entry)
        log_entries.append(occurrence_entry)

        triage_result = triage_agent.analyze(payload, offline_mode=offline_mode)
        triage_output = _strip_triage_meta(triage_result)
        triage_meta = triage_result.get("llm_metadata", {})
        triage_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="triage_agent",
            action="TRIAGE_ANALYZE",
            input_json={
                "payload": payload,
                "prompt_template_id": triage_meta.get("prompt_template_id"),
                "prompt_hash": triage_meta.get("prompt_hash"),
                "context_hash": triage_meta.get("context_hash"),
            },
            output_json=triage_output,
            confidence=float(triage_output.get("confidence", 0.0)),
        )
        db.insert_decision_log(local_conn, triage_entry)
        log_entries.append(triage_entry)
        metric_events.append(
            _metric_event(
                agent_id="triage_agent",
                counter="jobs_processed",
                confidence=float(triage_output.get("confidence", 0.0)),
            )
        )

        evidence_result = parts_agent.collect_evidence(payload, triage_output)
        evidence_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="parts_agent",
            action="COLLECT_EVIDENCE",
            input_json={
                "payload": payload,
                "triage_summary": triage_output.get("summary"),
                "triage_likely_causes": triage_output.get("likely_causes", []),
            },
            output_json=evidence_result,
            confidence=float(evidence_result.get("confidence", 0.0)),
        )
        db.insert_decision_log(local_conn, evidence_entry)
        log_entries.append(evidence_entry)
        metric_events.append(
            _metric_event(
                agent_id="parts_agent",
                counter="jobs_processed",
                confidence=float(evidence_result.get("confidence", 0.0)),
            )
        )

        schedule_hint = scheduler_agent.forecast(payload)
        schedule_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="scheduler_agent",
            action="FORECAST_SCHEDULE",
            input_json={"payload": payload},
            output_json=schedule_hint,
            confidence=0.65,
        )
        db.insert_decision_log(local_conn, schedule_entry)
        log_entries.append(schedule_entry)
        metric_events.append(
            _metric_event(
                agent_id="scheduler_agent",
                counter="jobs_processed",
                confidence=0.65,
            )
        )

        combined_confidence = _clamp(
            (float(triage_output.get("confidence", 0.0)) + float(evidence_result.get("confidence", 0.0)))
            / 2
        )
        keyword_text = " ".join(
            [
                str(payload.get("fault_code", "")),
                str(payload.get("symptoms", "")),
                str(payload.get("notes", "")),
                str(triage_output.get("summary", "")),
            ]
        )
        keyword_safety_hit = _contains_keywords(keyword_text, SAFETY_KEYWORDS)
        keyword_warranty_hit = _contains_keywords(keyword_text, WARRANTY_KEYWORDS)
        llm_risk = _evaluate_llm_risk_signals(
            payload=payload,
            triage_output=triage_output,
            offline_mode=offline_mode,
        )
        risk_signals = _merge_keyword_risk_hits(
            llm_risk,
            keyword_safety_hit=keyword_safety_hit,
            keyword_warranty_hit=keyword_warranty_hit,
        )
        safety_hit = bool(risk_signals.get("safety_signal", False))
        warranty_hit = bool(risk_signals.get("warranty_signal", False))
        triage_unsafe = bool(triage_output.get("safety_flag", False))
        parts_unconfirmed = bool(
            _should_enforce_parts_availability(payload) and evidence_result.get("missing_critical_parts")
        )
        escalation_reasons = _evaluate_escalation_reasons(
            combined_confidence=combined_confidence,
            safety_hit=safety_hit,
            warranty_hit=warranty_hit,
            triage_unsafe=triage_unsafe,
            manual_request=bool(payload.get("request_supervisor_review")),
            first_occurrence_fault=first_occurrence_fault,
            parts_unconfirmed=parts_unconfirmed,
        )
        requires_approval = False
        status = STATUS_DIAGNOSTIC_IN_PROGRESS
        workflow_mode = WORKFLOW_MODE_INVESTIGATION_ONLY
        workflow_meta = _workflow_mode_metadata(workflow_mode)
        approval_due_ts = None
        escalation_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="approval_logic",
            action="ESCALATION_CHECK",
            input_json={
                "threshold": APPROVAL_THRESHOLD,
                "combined_confidence": combined_confidence,
                "safety_keyword_hit": keyword_safety_hit,
                "warranty_keyword_hit": keyword_warranty_hit,
                "safety_llm_hit": llm_risk.get("safety_signal", False),
                "warranty_llm_hit": llm_risk.get("warranty_signal", False),
                "llm_risk_confidence": llm_risk.get("confidence", 0.0),
                "risk_source": llm_risk.get("source", "unknown"),
                "risk_matched_terms": llm_risk.get("matched_terms", {}),
                "policy_version": RISK_SIGNAL_POLICY_VERSION,
                "policy_config_hash": ESCALATION_POLICY_HASH,
                "triage_unsafe": triage_unsafe,
                "manual_request": bool(payload.get("request_supervisor_review")),
                "first_occurrence_fault": first_occurrence_fault,
                "parts_unconfirmed": parts_unconfirmed,
            },
            output_json={
                "requires_approval": requires_approval,
                "status": status,
                "escalation_reasons": escalation_reasons,
                "risk_signals": risk_signals,
                "governance_policy_version": GOVERNANCE_POLICY_VERSION,
                "workflow_mode": workflow_mode,
                "supervisor_routing": "disabled_for_diagnostic_phase",
            },
            confidence=combined_confidence,
            requires_human=0,
        )
        db.insert_decision_log(local_conn, escalation_entry)
        log_entries.append(escalation_entry)

        if "manual_request" in escalation_reasons:
            bypass_entry = _build_log_entry(
                ts=_utc_now(),
                job_id=job_id,
                agent_id="approval_logic",
                action="MANUAL_ESCALATION_IGNORED",
                input_json={"request_supervisor_review": True},
                output_json={"status": status, "reason": "supervisor_disabled_in_this_flow"},
                confidence=1.0,
                requires_human=0,
            )
            db.insert_decision_log(local_conn, bypass_entry)
            log_entries.append(bypass_entry)

        service_report, report_meta = _generate_service_report(
            payload=payload,
            triage=triage_output,
            evidence=evidence_result,
            scheduler=schedule_hint,
            requires_approval=requires_approval,
            workflow_mode=workflow_mode,
            offline_mode=offline_mode,
        )
        report_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="orchestrator",
            action="SERVICE_REPORT_GENERATED",
            input_json={
                "prompt_template_id": report_meta.get("prompt_template_id"),
                "prompt_hash": report_meta.get("prompt_hash"),
                "context_hash": report_meta.get("context_hash"),
                "workflow_mode": workflow_mode,
            },
            output_json={
                "used_fallback_template": report_meta.get("used_fallback_template", False),
                "suppressed_guidance": workflow_meta["suppressed_guidance"],
            },
            confidence=0.7,
        )
        db.insert_decision_log(local_conn, report_entry)
        log_entries.append(report_entry)

        actionable_workflow = _build_actionable_workflow(
            triage=triage_output,
            evidence=evidence_result,
            scheduler=schedule_hint,
            workflow_mode=workflow_mode,
            offline_mode=offline_mode,
        )
        workflow_generation_agent = _workflow_generation_agent_id(workflow_mode)
        db.replace_workflow_steps(
            local_conn,
            job_id=job_id,
            steps=actionable_workflow,
            ts=_utc_now(),
            agent_id=workflow_generation_agent,
        )
        workflow_created_event = {
            "ts": _utc_now(),
            "job_id": job_id,
            "step_id": None,
            "actor_id": workflow_generation_agent,
            "event_type": "WORKFLOW_CREATED",
            "input_json": {"step_count": len(actionable_workflow)},
            "output_json": {"status": "created"},
        }
        db.insert_workflow_event(local_conn, workflow_created_event)
        workflow_events.append(workflow_created_event)
        workflow_log_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id=workflow_generation_agent,
            action="WORKFLOW_GENERATED",
            input_json={"job_id": job_id, "workflow_mode": workflow_mode},
            output_json={
                "step_count": len(actionable_workflow),
                "suppressed_guidance": workflow_meta["suppressed_guidance"],
                "workflow_generation_agent": workflow_generation_agent,
            },
            confidence=0.8,
        )
        db.insert_decision_log(local_conn, workflow_log_entry)
        log_entries.append(workflow_log_entry)

        similar_issue_matches = _similar_issues_for_payload(
            local_conn,
            payload=payload,
            limit=SIMILAR_ISSUE_MATCH_LIMIT,
        )
        history_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="orchestrator",
            action="SIMILAR_ISSUES_LOOKUP",
            input_json={
                "fault_code": payload.get("fault_code"),
                "equipment_id": payload.get("equipment_id"),
                "issue_text": payload.get("issue_text"),
                "similarity_min_score": SIMILARITY_MIN_SCORE,
            },
            output_json={
                "match_count": len(similar_issue_matches),
                "top_match_job_ids": [item.get("job_id") for item in similar_issue_matches[:3]],
            },
            confidence=1.0 if similar_issue_matches else 0.9,
        )
        db.insert_decision_log(local_conn, history_entry)
        log_entries.append(history_entry)

        response_payload = {
            "job_id": job_id,
            "status": status,
            "requires_approval": requires_approval,
            "approval_due_ts": approval_due_ts,
            "timed_out": False,
            "service_report": service_report,
            "triage": triage_output,
            "evidence": evidence_result,
            "schedule_hint": schedule_hint,
            "assignment_recommendation": schedule_hint.get("assignment_recommendation"),
            "initial_workflow": actionable_workflow,
            "escalation_reasons": escalation_reasons,
            "risk_signals": risk_signals,
            "escalation_policy_version": RISK_SIGNAL_POLICY_VERSION,
            "governance_policy_version": GOVERNANCE_POLICY_VERSION,
            "policy_config_hash": ESCALATION_POLICY_HASH,
            "mode_effective": mode_effective,
            "model_selected": runtime_models["model_selected"],
            "model_online": runtime_models["model_online"],
            "model_offline": runtime_models["model_offline"],
            "model_tier": runtime_models["model_tier"],
            "model_policy_valid": runtime_models["model_policy_valid"],
            "model_policy_notes": runtime_models["model_policy_notes"],
            "workflow_mode": workflow_mode,
            "workflow_intent": workflow_meta["workflow_intent"],
            "allowed_actions": workflow_meta["allowed_actions"],
            "suppressed_guidance": workflow_meta["suppressed_guidance"],
            "workflow_generation_agent": workflow_generation_agent,
            "quote_stage": "DIAGNOSTIC_IN_PROGRESS",
            "approval_stage": "CUSTOMER_DECISION",
            "issue_text": payload.get("issue_text"),
            "customer_info": {
                "name": payload.get("customer_name"),
                "phone": payload.get("customer_phone"),
                "email": payload.get("customer_email"),
            },
            "normalization_meta": normalization_meta,
            "guided_question": guided_question,
            "guided_answer": guided_answer,
            "first_occurrence_fault": first_occurrence_fault,
            "similar_issue_matches": similar_issue_matches,
            "similarity_min_score": SIMILARITY_MIN_SCORE,
        }

        job_row = {
            "job_id": job_id,
            "created_ts": existing_job["created_ts"] if existing_job else now,
            "updated_ts": _utc_now(),
            "status": status,
            "field_payload_json": payload,
            "final_response_json": response_payload,
            "requires_approval": int(requires_approval),
            "approved_by": existing_job["approved_by"] if existing_job else None,
            "approved_ts": existing_job["approved_ts"] if existing_job else None,
            "guided_question": guided_question,
            "guided_answer": guided_answer,
            "approval_due_ts": approval_due_ts,
            "timed_out": 0,
            "first_occurrence_fault": int(first_occurrence_fault),
            "assigned_tech_id": (schedule_hint.get("assignment_recommendation") or {}).get("tech_id"),
            "workflow_mode": workflow_mode,
        }

        db.upsert_job(local_conn, job_row)
        for metric_event in metric_events:
            db.apply_metric_event(local_conn, metric_event)
        if offline_mode:
            _queue_offline_events(
                local_conn,
                job_row,
                log_entries,
                workflow_steps=actionable_workflow,
                workflow_events=workflow_events,
                metric_events=metric_events,
            )
        else:
            _mirror_online_to_server(
                job_row,
                log_entries,
                workflow_steps=actionable_workflow,
                workflow_events=workflow_events,
                metric_events=metric_events,
            )

        local_conn.commit()
        return response_payload


@app.post("/api/job/{job_id}/quote")
def generate_quote(job_id: str) -> dict[str, Any]:
    now = _utc_now()
    offline_mode = _is_offline(False)
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        if str(job.get("status", "")).upper() in {STATUS_PENDING_APPROVAL, STATUS_TIMEOUT_HOLD, STATUS_DENIED}:
            raise HTTPException(status_code=409, detail="Resolve supervisor technical approval before quote generation.")

        payload, triage_data, evidence_data, schedule_data = _quote_agent_inputs(job, offline_mode=offline_mode)
        quote_package = quote_agent.build_quote(
            job_id=job_id,
            payload=payload,
            triage=triage_data,
            evidence=evidence_data,
            schedule=schedule_data,
        )
        final_response = dict(job.get("final_response_json") or {})
        final_response["quote_package"] = quote_package
        final_response["quote_stage"] = "QUOTE_READY"
        final_response["status"] = job.get("status", STATUS_READY)

        log_entry = _build_log_entry(
            ts=now,
            job_id=job_id,
            agent_id="quote_agent",
            action="QUOTE_GENERATED",
            input_json={
                "equipment_id": payload.get("equipment_id"),
                "fault_code": payload.get("fault_code"),
                "triage_summary": triage_data.get("summary"),
                "parts_considered": evidence_data.get("parts_candidates", []),
                "eta_bucket": schedule_data.get("eta_bucket"),
            },
            output_json={
                "quote_id": quote_package.get("quote_id"),
                "total_usd": quote_package.get("total_usd"),
                "line_item_count": len(quote_package.get("line_items", [])),
            },
            confidence=float(quote_package.get("confidence", 0.0)),
        )
        db.insert_decision_log(local_conn, log_entry)

        job_row = _build_updated_job_row(
            job,
            now=now,
            status=str(job.get("status", STATUS_READY)),
            final_response=final_response,
            requires_approval=bool(job.get("requires_approval", 0)),
            approved_by=job.get("approved_by"),
            approved_ts=job.get("approved_ts"),
            approval_due_ts=job.get("approval_due_ts"),
        )
        db.upsert_job(local_conn, job_row)

        if offline_mode:
            _queue_offline_events(local_conn, job_row, [log_entry])
        else:
            _mirror_online_to_server(job_row, [log_entry])

        local_conn.commit()
        return {
            "job_id": job_id,
            "status": job_row["status"],
            "quote_package": quote_package,
            "quote_stage": final_response.get("quote_stage"),
        }


@app.post("/api/job/{job_id}/quote/email-draft")
def draft_quote_email(job_id: str, request: QuoteEmailDraftRequest) -> dict[str, Any]:
    now = _utc_now()
    offline_mode = _is_offline(False)
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")

        current_status = str(job.get("status", "")).upper()
        if current_status in {STATUS_PENDING_APPROVAL, STATUS_TIMEOUT_HOLD, STATUS_DENIED}:
            raise HTTPException(status_code=409, detail="Resolve technical supervisor approval before drafting quote email.")

        payload, triage_data, evidence_data, schedule_data = _quote_agent_inputs(job, offline_mode=offline_mode)
        final_response = dict(job.get("final_response_json") or {})
        quote_package = final_response.get("quote_package")
        if not isinstance(quote_package, dict) or not quote_package:
            quote_package = quote_agent.build_quote(
                job_id=job_id,
                payload=payload,
                triage=triage_data,
                evidence=evidence_data,
                schedule=schedule_data,
            )
            final_response["quote_package"] = quote_package

        quote_email = email_agent.draft_quote_email(
            payload=payload,
            triage=triage_data,
            schedule=schedule_data,
            quote=quote_package,
        )
        quote_email["recipient_name"] = (
            str(request.recipient_name or "").strip()
            or str(payload.get("customer_name") or "").strip()
            or None
        )
        quote_email["recipient_email"] = (
            str(request.recipient_email or "").strip()
            or str(payload.get("customer_email") or "").strip()
            or None
        )
        quote_email["additional_notes"] = str(request.additional_notes or "").strip() or None

        workflow_mode = WORKFLOW_MODE_INVESTIGATION_ONLY
        workflow_meta = _workflow_mode_metadata(workflow_mode)
        approval_due_ts = None
        final_response["quote_email_draft"] = quote_email
        final_response["quote_stage"] = "AWAITING_CUSTOMER_APPROVAL"
        final_response["approval_stage"] = "CUSTOMER_DECISION"
        final_response["status"] = STATUS_AWAITING_CUSTOMER_APPROVAL
        final_response["requires_approval"] = False
        final_response["approval_due_ts"] = approval_due_ts
        final_response["workflow_mode"] = workflow_mode
        final_response["workflow_intent"] = workflow_meta["workflow_intent"]
        final_response["allowed_actions"] = workflow_meta["allowed_actions"]
        final_response["suppressed_guidance"] = workflow_meta["suppressed_guidance"]
        final_response["workflow_generation_agent"] = _workflow_generation_agent_id(workflow_mode)

        investigation_steps = _build_actionable_workflow(
            triage=triage_data,
            evidence=evidence_data,
            scheduler=schedule_data,
            workflow_mode=WORKFLOW_MODE_INVESTIGATION_ONLY,
            offline_mode=offline_mode,
        )
        workflow_generation_agent = _workflow_generation_agent_id(WORKFLOW_MODE_INVESTIGATION_ONLY)
        db.replace_workflow_steps(
            local_conn,
            job_id=job_id,
            steps=investigation_steps,
            ts=now,
            agent_id=workflow_generation_agent,
        )
        workflow_event = {
            "ts": now,
            "job_id": job_id,
            "step_id": None,
            "actor_id": workflow_generation_agent,
            "event_type": "WORKFLOW_MODE_SWITCHED",
            "input_json": {"previous_mode": job.get("workflow_mode"), "reason": "quote_email_ready_for_customer"},
            "output_json": {
                "workflow_mode": WORKFLOW_MODE_INVESTIGATION_ONLY,
                "step_count": len(investigation_steps),
                "workflow_generation_agent": workflow_generation_agent,
            },
        }
        db.insert_workflow_event(local_conn, workflow_event)

        draft_entry = _build_log_entry(
            ts=now,
            job_id=job_id,
            agent_id="email_agent",
            action="QUOTE_EMAIL_DRAFTED",
            input_json={
                "quote_id": quote_package.get("quote_id"),
                "total_usd": quote_package.get("total_usd"),
                "recipient_email": quote_email.get("recipient_email"),
                "recipient_name": quote_email.get("recipient_name"),
            },
            output_json={
                "subject": quote_email.get("subject"),
                "approval_stage": "CUSTOMER_DECISION",
            },
            confidence=float(quote_email.get("confidence", 0.0)),
            requires_human=0,
        )
        approval_entry = _build_log_entry(
            ts=now,
            job_id=job_id,
            agent_id="approval_logic",
            action="CUSTOMER_APPROVAL_REQUESTED",
            input_json={"approval_stage": "CUSTOMER_DECISION"},
            output_json={"status": STATUS_AWAITING_CUSTOMER_APPROVAL, "approval_due_ts": approval_due_ts},
            confidence=1.0,
            requires_human=0,
        )
        db.insert_decision_log(local_conn, draft_entry)
        db.insert_decision_log(local_conn, approval_entry)

        job_row = _build_updated_job_row(
            job,
            now=now,
            status=STATUS_AWAITING_CUSTOMER_APPROVAL,
            final_response=final_response,
            requires_approval=False,
            approved_by=job.get("approved_by"),
            approved_ts=job.get("approved_ts"),
            approval_due_ts=approval_due_ts,
            timed_out=0,
            workflow_mode=workflow_mode,
        )
        db.upsert_job(local_conn, job_row)

        if offline_mode:
            _queue_offline_events(
                local_conn,
                job_row,
                [draft_entry, approval_entry],
                workflow_steps=investigation_steps,
                workflow_events=[workflow_event],
            )
        else:
            _mirror_online_to_server(
                job_row,
                [draft_entry, approval_entry],
                workflow_steps=investigation_steps,
                workflow_events=[workflow_event],
            )

        local_conn.commit()
        return {
            "job_id": job_id,
            "status": STATUS_AWAITING_CUSTOMER_APPROVAL,
            "approval_due_ts": approval_due_ts,
            "quote_stage": final_response.get("quote_stage"),
            "quote_email_draft": quote_email,
        }


@app.post("/api/job/{job_id}/customer-approval")
def record_customer_approval(job_id: str, request: CustomerApprovalRequest) -> dict[str, Any]:
    now = _utc_now()
    offline_mode = _is_offline(False)
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")

        final_response = dict(job.get("final_response_json") or {})
        current_status = str(job.get("status", "")).upper()
        if current_status not in {STATUS_AWAITING_CUSTOMER_APPROVAL, STATUS_QUOTE_REWORK_REQUIRED}:
            raise HTTPException(status_code=409, detail="Job is not in customer-approval stage.")

        approved = request.decision == "approve"
        new_status = STATUS_REPAIR_POOL_OPEN if approved else STATUS_CUSTOMER_DECLINED
        workflow_mode = WORKFLOW_MODE_FIX_PLAN if approved else WORKFLOW_MODE_INVESTIGATION_ONLY
        workflow_meta = _workflow_mode_metadata(workflow_mode)
        final_response["customer_decision"] = {
            "decision": request.decision,
            "actor_id": request.actor_id,
            "notes": request.notes,
            "ts": now,
        }
        final_response["status"] = new_status
        final_response["requires_approval"] = False
        final_response["approval_due_ts"] = None
        final_response["quote_stage"] = "CUSTOMER_APPROVED" if approved else "CUSTOMER_DECLINED"
        final_response["workflow_mode"] = workflow_mode
        final_response["workflow_intent"] = workflow_meta["workflow_intent"]
        final_response["allowed_actions"] = workflow_meta["allowed_actions"]
        final_response["suppressed_guidance"] = workflow_meta["suppressed_guidance"]
        final_response["workflow_generation_agent"] = _workflow_generation_agent_id(workflow_mode)

        log_entries: list[dict[str, Any]] = []
        customer_entry = _build_log_entry(
            ts=now,
            job_id=job_id,
            agent_id=request.actor_id,
            action="CUSTOMER_DECISION_RECORDED",
            input_json=request.model_dump(),
            output_json={"status": new_status},
            confidence=1.0,
        )
        db.insert_decision_log(local_conn, customer_entry)
        log_entries.append(customer_entry)

        workflow_steps_for_sync: list[dict[str, Any]] | None = None
        workflow_events_for_sync: list[dict[str, Any]] | None = None
        if approved:
            payload, triage_data, evidence_data, schedule_data = _quote_agent_inputs(job, offline_mode=offline_mode)
            diagnostic_context = _collect_diagnostic_context(local_conn, job_id)
            repair_payload = dict(payload)
            if diagnostic_context.get("context_text"):
                existing_notes = str(repair_payload.get("notes", "")).strip()
                repair_payload["notes"] = (
                    f"{existing_notes} Diagnostic findings: {diagnostic_context['context_text']}".strip()
                )
            repair_payload["diagnostic_context"] = diagnostic_context.get("step_results", [])
            triage_data = _strip_triage_meta(triage_agent.analyze(repair_payload, offline_mode=offline_mode))
            evidence_data = parts_agent.collect_evidence(repair_payload, triage_data)
            schedule_data = scheduler_agent.forecast(repair_payload)
            final_response["triage"] = triage_data
            final_response["evidence"] = evidence_data
            final_response["schedule_hint"] = schedule_data
            final_response["assignment_recommendation"] = schedule_data.get("assignment_recommendation")
            final_response["repair_planning_context"] = {
                "diagnostic_step_result_count": diagnostic_context.get("step_result_count", 0),
                "diagnostic_completed_count": diagnostic_context.get("completed_count", 0),
                "diagnostic_blocked_or_failed_count": diagnostic_context.get("blocked_or_failed_count", 0),
                "summary_lines": diagnostic_context.get("summary_lines", []),
            }

            context_entry = _build_log_entry(
                ts=now,
                job_id=job_id,
                agent_id="repair_agent",
                action="REPAIR_PLAN_CONTEXT_COMPILED",
                input_json={
                    "diagnostic_step_result_count": diagnostic_context.get("step_result_count", 0),
                    "completed_count": diagnostic_context.get("completed_count", 0),
                    "blocked_or_failed_count": diagnostic_context.get("blocked_or_failed_count", 0),
                },
                output_json={
                    "context_text_present": bool(diagnostic_context.get("context_text")),
                    "summary_lines": diagnostic_context.get("summary_lines", []),
                },
                confidence=0.85,
            )
            db.insert_decision_log(local_conn, context_entry)
            log_entries.append(context_entry)

            workflow_steps = _build_actionable_workflow(
                triage=triage_data,
                evidence=evidence_data,
                scheduler=schedule_data,
                workflow_mode=WORKFLOW_MODE_FIX_PLAN,
                offline_mode=offline_mode,
            )
            final_response["initial_workflow"] = workflow_steps
            workflow_generation_agent = _workflow_generation_agent_id(WORKFLOW_MODE_FIX_PLAN)
            db.replace_workflow_steps(
                local_conn,
                job_id=job_id,
                steps=workflow_steps,
                ts=now,
                agent_id=workflow_generation_agent,
            )
            workflow_event = {
                "ts": now,
                "job_id": job_id,
                "step_id": None,
                "actor_id": workflow_generation_agent,
                "event_type": "CUSTOMER_APPROVAL_REPAIR_POOL_OPENED",
                "input_json": {"decision": request.decision},
                "output_json": {
                    "status": STATUS_REPAIR_POOL_OPEN,
                    "step_count": len(workflow_steps),
                    "workflow_generation_agent": workflow_generation_agent,
                },
            }
            db.insert_workflow_event(local_conn, workflow_event)
            workflow_steps_for_sync = workflow_steps
            workflow_events_for_sync = [workflow_event]

            promote_entry = _build_log_entry(
                ts=now,
                job_id=job_id,
                agent_id=workflow_generation_agent,
                action="REPAIR_POOL_OPENED",
                input_json={"decision": request.decision},
                output_json={
                    "status": STATUS_REPAIR_POOL_OPEN,
                    "workflow_mode": WORKFLOW_MODE_FIX_PLAN,
                    "workflow_generation_agent": workflow_generation_agent,
                },
                confidence=0.95,
            )
            db.insert_decision_log(local_conn, promote_entry)
            log_entries.append(promote_entry)

        job_row = _build_updated_job_row(
            job,
            now=now,
            status=new_status,
            final_response=final_response,
            requires_approval=False,
            approved_by=job.get("approved_by"),
            approved_ts=job.get("approved_ts"),
            approval_due_ts=None,
            timed_out=0,
            workflow_mode=workflow_mode,
        )
        db.upsert_job(local_conn, job_row)

        if offline_mode:
            _queue_offline_events(
                local_conn,
                job_row,
                log_entries,
                workflow_steps=workflow_steps_for_sync,
                workflow_events=workflow_events_for_sync,
            )
        else:
            _mirror_online_to_server(
                job_row,
                log_entries,
                workflow_steps=workflow_steps_for_sync,
                workflow_events=workflow_events_for_sync,
            )
        local_conn.commit()
        updated = db.get_job(local_conn, job_id)
        if updated:
            updated = _normalize_final_response(updated)
        return updated or job_row


@app.get("/api/repair/pool")
def get_repair_pool(include_claimed: bool = True, limit: int = 100) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        jobs = db.fetch_repair_pool_jobs(local_conn, include_claimed=include_claimed, limit=min(max(limit, 1), 200))
        return {"count": len(jobs), "jobs": jobs}


@app.post("/api/repair/pool/{job_id}/claim")
def claim_repair_job(job_id: str, request: RepairClaimRequest) -> dict[str, Any]:
    now = _utc_now()
    offline_mode = _is_offline(False)
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        if str(job.get("status", "")).upper() not in {STATUS_REPAIR_POOL_OPEN, STATUS_REPAIR_IN_PROGRESS}:
            raise HTTPException(status_code=409, detail="Job is not available in the repair pool.")

        final_response = dict(job.get("final_response_json") or {})
        final_response["repair_assignment"] = {
            "technician_id": request.technician_id,
            "technician_name": request.technician_name,
            "assigned_ts": now,
        }
        final_response["status"] = STATUS_REPAIR_IN_PROGRESS
        final_response["requires_approval"] = False

        claim_entry = _build_log_entry(
            ts=now,
            job_id=job_id,
            agent_id="scheduler_agent",
            action="REPAIR_POOL_CLAIMED",
            input_json=request.model_dump(),
            output_json={"status": STATUS_REPAIR_IN_PROGRESS},
            confidence=0.9,
        )
        db.insert_decision_log(local_conn, claim_entry)
        metric_event = _metric_event(agent_id="scheduler_agent", counter="jobs_processed", confidence=0.9)
        db.apply_metric_event(local_conn, metric_event)

        job_row = _build_updated_job_row(
            job,
            now=now,
            status=STATUS_REPAIR_IN_PROGRESS,
            final_response=final_response,
            requires_approval=False,
            approved_by=job.get("approved_by"),
            approved_ts=job.get("approved_ts"),
            approval_due_ts=None,
            timed_out=0,
            assigned_tech_id=request.technician_id,
            workflow_mode=WORKFLOW_MODE_FIX_PLAN,
        )
        db.upsert_job(local_conn, job_row)

        if offline_mode:
            _queue_offline_events(local_conn, job_row, [claim_entry], metric_events=[metric_event])
        else:
            _mirror_online_to_server(job_row, [claim_entry], metric_events=[metric_event])
        local_conn.commit()
        updated = db.get_job(local_conn, job_id)
        if updated:
            updated = _normalize_final_response(updated)
        return updated or job_row


@app.post("/api/repair/pool/{job_id}/complete")
def complete_repair_job(job_id: str, request: RepairCompleteRequest) -> dict[str, Any]:
    now = _utc_now()
    offline_mode = _is_offline(False)
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        if str(job.get("status", "")).upper() not in {STATUS_REPAIR_IN_PROGRESS, STATUS_REPAIR_POOL_OPEN}:
            raise HTTPException(status_code=409, detail="Job is not active for repair completion.")

        final_response = dict(job.get("final_response_json") or {})
        payload = dict(job.get("field_payload_json") or {})
        completion_notes = str(request.notes or "").strip()
        # Product decision: repair-pool completion stays in technician flow and does not route to supervisor.
        needs_supervisor = False
        new_status = STATUS_REPAIR_COMPLETED
        workflow_mode = WORKFLOW_MODE_FIX_PLAN
        workflow_meta = _workflow_mode_metadata(workflow_mode)
        approval_due_ts = None

        final_response["repair_completion"] = {
            "technician_id": request.technician_id,
            "notes": completion_notes,
            "ts": now,
        }
        final_response["status"] = new_status
        final_response["requires_approval"] = needs_supervisor
        final_response["approval_due_ts"] = approval_due_ts
        final_response["workflow_mode"] = workflow_mode
        final_response["workflow_intent"] = workflow_meta["workflow_intent"]
        final_response["allowed_actions"] = workflow_meta["allowed_actions"]
        final_response["suppressed_guidance"] = workflow_meta["suppressed_guidance"]
        final_response["escalation_reasons"] = final_response.get("escalation_reasons", [])
        final_response.pop("approval_stage", None)

        completion_entry = _build_log_entry(
            ts=now,
            job_id=job_id,
            agent_id=request.technician_id,
            action="REPAIR_COMPLETION_RECORDED",
            input_json={"notes": completion_notes, "equipment_id": payload.get("equipment_id")},
            output_json={"status": new_status, "needs_supervisor": needs_supervisor},
            confidence=0.9,
            requires_human=int(needs_supervisor),
        )
        db.insert_decision_log(local_conn, completion_entry)
        log_entries = [completion_entry]
        metric_events = [_metric_event(agent_id="orchestrator", counter="jobs_processed", confidence=0.9)]

        for metric_event in metric_events:
            db.apply_metric_event(local_conn, metric_event)

        job_row = _build_updated_job_row(
            job,
            now=now,
            status=new_status,
            final_response=final_response,
            requires_approval=needs_supervisor,
            approved_by=None if needs_supervisor else job.get("approved_by"),
            approved_ts=None if needs_supervisor else job.get("approved_ts"),
            approval_due_ts=approval_due_ts,
            timed_out=0,
            assigned_tech_id=request.technician_id,
            workflow_mode=workflow_mode,
        )
        db.upsert_job(local_conn, job_row)

        if offline_mode:
            _queue_offline_events(local_conn, job_row, log_entries, metric_events=metric_events)
        else:
            _mirror_online_to_server(job_row, log_entries, metric_events=metric_events)
        local_conn.commit()
        updated = db.get_job(local_conn, job_id)
        if updated:
            updated = _normalize_final_response(updated)
        return updated or job_row


@app.post("/api/job/{job_id}/attachments")
def upload_job_attachment(job_id: str, request: AttachmentUploadRequest) -> dict[str, Any]:
    now = _utc_now()
    mime_type = str(request.mime_type or "").strip().lower()
    requested_step_id = str(request.step_id or "").strip()
    canonical_step_id = _canonical_step_id(requested_step_id)
    extension = ALLOWED_IMAGE_MIME_TYPES.get(mime_type)
    if not extension:
        raise HTTPException(status_code=422, detail="Unsupported mime_type. Allowed: image/jpeg, image/png, image/webp")

    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        step = db.get_workflow_step(local_conn, job_id, canonical_step_id)
        if not step:
            raise HTTPException(status_code=404, detail="step_id not found")
        attachment_count = db.count_job_step_attachments(local_conn, job_id, canonical_step_id)
        if attachment_count >= MAX_ATTACHMENTS_PER_STEP:
            raise HTTPException(
                status_code=409,
                detail=f"Step already has {MAX_ATTACHMENTS_PER_STEP} images. Remove one before uploading more.",
            )

        image_bytes = _decode_image_payload(request.image_base64)
        if not image_bytes:
            raise HTTPException(status_code=422, detail="image_base64 decoded to empty payload")
        if len(image_bytes) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Image too large. Max size is {MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB.",
            )

        attachment_id = str(uuid.uuid4())
        file_abs_path, local_rel_path = _attachment_local_path(job_id, attachment_id, extension)
        file_abs_path.write_bytes(image_bytes)
        sha256 = hashlib.sha256(image_bytes).hexdigest()

        attachment = {
            "attachment_id": attachment_id,
            "job_id": job_id,
            "step_id": canonical_step_id,
            "created_ts": now,
            "captured_ts": request.captured_ts or now,
            "source": request.source,
            "filename": _clean_attachment_filename(request.filename, extension),
            "mime_type": mime_type,
            "byte_size": len(image_bytes),
            "sha256": sha256,
            "caption": str(request.caption or "").strip() or None,
            "local_rel_path": local_rel_path,
            "server_rel_path": None,
            "sync_state": "pending",
            "sync_error": None,
            "vision_features_json": {},
        }
        db.insert_issue_attachment(local_conn, attachment)
        db.refresh_issue_attachment_summary(local_conn, job_id)

        log_entry = _build_log_entry(
            ts=now,
            job_id=job_id,
            agent_id="field_technician",
            action="ATTACHMENT_ADDED",
            input_json={
                "step_id": canonical_step_id,
                "requested_step_id": requested_step_id,
                "source": request.source,
                "filename": request.filename,
                "mime_type": mime_type,
                "byte_size": len(image_bytes),
            },
            output_json={
                "attachment_id": attachment_id,
                "sha256": sha256,
                "local_rel_path": local_rel_path,
            },
            confidence=1.0,
        )
        db.insert_decision_log(local_conn, log_entry)

        if _is_offline(False):
            db.enqueue_sync_event(
                local_conn,
                ts=now,
                entity="decision_log",
                entity_id=f"{job_id}:field_technician:ATTACHMENT_ADDED:{attachment_id}",
                payload=log_entry,
            )
            db.enqueue_sync_event(
                local_conn,
                ts=now,
                entity="attachment_upsert",
                entity_id=attachment_id,
                payload=attachment,
            )
            db.enqueue_sync_event(
                local_conn,
                ts=now,
                entity="attachment_file_copy",
                entity_id=attachment_id,
                payload={
                    "attachment_id": attachment_id,
                    "job_id": job_id,
                    "local_rel_path": local_rel_path,
                },
            )
        else:
            copied = _copy_attachment_local_to_server(attachment)
            db.upsert_issue_attachment(local_conn, copied)
            db.refresh_issue_attachment_summary(local_conn, job_id)
            with db.open_server_connection() as server_conn:
                db.insert_decision_log(server_conn, log_entry)
                db.upsert_issue_attachment(server_conn, copied)
                db.refresh_issue_attachment_summary(server_conn, job_id)
                server_conn.commit()
            attachment = copied

        local_conn.commit()
        return {
            "job_id": job_id,
            "step_id": canonical_step_id,
            "attachment": _attachment_public_payload(attachment),
        }


@app.get("/api/job/{job_id}/attachments")
def get_job_attachments(job_id: str) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        attachments = [_attachment_public_payload(item) for item in db.get_job_attachments(local_conn, job_id)]
        return {"job_id": job_id, "count": len(attachments), "attachments": attachments}


@app.get("/api/attachments/{attachment_id}/content")
def get_attachment_content(attachment_id: str) -> FileResponse:
    with db.open_local_connection() as local_conn:
        attachment = db.get_attachment(local_conn, attachment_id)
        if not attachment:
            raise HTTPException(status_code=404, detail="attachment_id not found")
    local_rel_path = str(attachment.get("local_rel_path", "")).strip()
    if not local_rel_path:
        raise HTTPException(status_code=404, detail="attachment missing local file path")
    abs_path = db.LOCAL_DB_PATH.parent / local_rel_path
    if not abs_path.exists():
        raise HTTPException(status_code=404, detail="attachment file not found")
    return FileResponse(
        path=str(abs_path),
        media_type=str(attachment.get("mime_type") or "application/octet-stream"),
        filename=str(attachment.get("filename") or abs_path.name),
    )


@app.get("/api/issues")
def get_issue_history(
    q: str | None = None,
    equipment_id: str | None = None,
    fault_code: str | None = None,
    location: str | None = None,
    status: str | None = None,
    workflow_mode: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> dict[str, Any]:
    limit = max(1, min(ISSUE_SEARCH_LIMIT_MAX, int(limit)))
    offset = max(0, int(offset))
    with db.open_local_connection() as local_conn:
        issues = db.search_issue_records(
            local_conn,
            q=q,
            equipment_id=equipment_id,
            fault_code=fault_code,
            location=location,
            status=status,
            workflow_mode=workflow_mode,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
        return {"count": len(issues), "issues": issues, "limit": limit, "offset": offset}


@app.get("/api/issues/{job_id}/similar")
def get_similar_issues(job_id: str, limit: int = 5) -> dict[str, Any]:
    limit = max(1, min(20, int(limit)))
    with db.open_local_connection() as local_conn:
        if not db.get_issue_record(local_conn, job_id):
            raise HTTPException(status_code=404, detail="job_id not found in issue history")
        scored = _similar_issues_for_job(
            local_conn,
            job_id=job_id,
            limit=limit,
        )
        return {
            "job_id": job_id,
            "count": len(scored),
            "similar_issues": scored,
            "similarity_min_score": SIMILARITY_MIN_SCORE,
        }


@app.get("/api/parts")
def get_parts_inventory(
    location: str | None = None,
    q: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    limit = max(1, min(1000, int(limit)))
    with db.open_local_connection() as local_conn:
        items = db.list_parts_inventory(
            local_conn,
            location=location,
            q=q,
            limit=limit,
        )
    for item in items:
        item["stock_status"] = _stock_status(
            int(item.get("quantity_on_hand", 0)),
            int(item.get("reorder_level", 0)),
        )
    return {
        "count": len(items),
        "location": location,
        "items": items,
    }


@app.get("/api/job/{job_id}/workflow/parts")
def get_job_workflow_parts(job_id: str) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        steps = db.get_workflow_steps(local_conn, job_id)
        location = _inventory_location_for_job(job)
        status = str(job.get("status", "")).upper()
        parts_enabled = _parts_usage_enabled_for_status(status)
        step_parts: list[dict[str, Any]] = []
        for step in steps:
            step_part_names: list[str] = []
            for value in step.get("recommended_parts", []) or []:
                name = str(value or "").strip()
                if not name or name in step_part_names:
                    continue
                step_part_names.append(name)
                if len(step_part_names) >= 3:
                    break
            parts_source = "step_recommended" if parts_enabled else "locked_until_repair"
            step_items: list[dict[str, Any]] = []
            for part_name in (step_part_names if parts_enabled else []):
                catalog = db.get_part_catalog_by_name(local_conn, str(part_name))
                if not catalog:
                    step_items.append(
                        {
                            "part_id": None,
                            "part_name": str(part_name),
                            "category": "unknown",
                            "location": location,
                            "quantity_on_hand": 0,
                            "reorder_level": 0,
                            "stock_status": "UNKNOWN",
                        }
                    )
                    continue
                inventory = db.get_part_inventory(local_conn, str(catalog.get("part_id")), location)
                quantity = int((inventory or {}).get("quantity_on_hand", 0))
                reorder_level = int((inventory or {}).get("reorder_level", 2))
                step_items.append(
                    {
                        "part_id": catalog.get("part_id"),
                        "part_name": catalog.get("part_name"),
                        "category": catalog.get("category"),
                        "location": location,
                        "quantity_on_hand": quantity,
                        "reorder_level": reorder_level,
                        "stock_status": _stock_status(quantity, reorder_level),
                    }
                )
            step_parts.append(
                {
                    "step_id": step.get("step_id"),
                    "step_order": step.get("step_order"),
                    "title": step.get("title"),
                    "parts_source": parts_source,
                    "parts": step_items,
                }
            )
        return {
            "job_id": job_id,
            "location": location,
            "status": status,
            "parts_enabled": parts_enabled,
            "steps": step_parts,
        }


@app.get("/api/job/{job_id}/parts-usage")
def get_job_parts_usage(job_id: str) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        usage_rows = db.list_job_parts_usage(local_conn, job_id)
        return {"job_id": job_id, "count": len(usage_rows), "usage": usage_rows}


@app.post("/api/parts/use")
def use_part_for_step(request: PartsUseRequest) -> dict[str, Any]:
    now = _utc_now()
    offline_mode = _is_offline(False)
    quantity_used = max(1, int(request.quantity_used))
    actor_role = str(request.actor_role).strip().lower()
    actor_id = str(request.actor_id or "").strip() or "field_technician"
    canonical_step_id = _canonical_step_id(request.step_id)
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, request.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        current_status = str(job.get("status", "")).upper()
        if not _parts_usage_enabled_for_status(current_status):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Parts usage is disabled until repair begins. "
                    "Current status must be REPAIR_POOL_OPEN or REPAIR_IN_PROGRESS."
                ),
            )
        step = db.get_workflow_step(local_conn, request.job_id, canonical_step_id)
        if not step:
            raise HTTPException(status_code=404, detail="step_id not found")

        location = _inventory_location_for_job(job)
        inventory_before = db.get_part_inventory(local_conn, request.part_id, location)
        if not inventory_before:
            raise HTTPException(status_code=404, detail="part_id not found in location inventory")

        part_name = str(inventory_before.get("part_name", request.part_id))
        usage_entry = {
            "ts": now,
            "job_id": request.job_id,
            "step_id": canonical_step_id,
            "part_id": request.part_id,
            "part_name_snapshot": part_name,
            "location": location,
            "quantity_used": quantity_used,
            "actor_id": actor_id,
            "actor_role": actor_role,
            "notes": request.notes,
        }

        sync_entities: list[tuple[str, dict[str, Any]]] = []
        log_entries: list[dict[str, Any]] = []

        decremented = db.decrement_part_inventory_atomic(
            local_conn,
            part_id=request.part_id,
            location=location,
            quantity_use=quantity_used,
            updated_ts=now,
        )
        if not decremented:
            restock_request = {
                "request_id": str(uuid.uuid4()),
                "ts": now,
                "job_id": request.job_id,
                "step_id": canonical_step_id,
                "part_id": request.part_id,
                "part_name_snapshot": part_name,
                "location": location,
                "requested_qty": quantity_used,
                "status": db.RESTOCK_STATUS_PENDING,
                "requested_by": actor_id,
                "requested_role": actor_role,
                "fulfilled_by": None,
                "fulfilled_ts": None,
                "notes": request.notes,
            }
            db.insert_restock_request(local_conn, restock_request)
            blocked_entry = _build_log_entry(
                ts=now,
                job_id=request.job_id,
                agent_id=actor_id,
                action="PART_USE_BLOCKED_OUT_OF_STOCK",
                input_json={
                    "step_id": canonical_step_id,
                    "part_id": request.part_id,
                    "part_name": part_name,
                    "location": location,
                    "quantity_requested": quantity_used,
                },
                output_json={
                    "restock_request_id": restock_request["request_id"],
                    "status": "blocked",
                },
                confidence=1.0,
            )
            db.insert_decision_log(local_conn, blocked_entry)
            log_entries.append(blocked_entry)
            sync_entities.append(("parts_restock_request", restock_request))
            inventory_after = db.get_part_inventory(local_conn, request.part_id, location) or inventory_before
            inventory_after["stock_status"] = _stock_status(
                int(inventory_after.get("quantity_on_hand", 0)),
                int(inventory_after.get("reorder_level", 0)),
            )

            if offline_mode:
                for entry in log_entries:
                    db.enqueue_sync_event(
                        local_conn,
                        ts=entry["ts"],
                        entity="decision_log",
                        entity_id=f"{entry['job_id']}:{entry['agent_id']}:{entry['action']}:{entry['ts']}",
                        payload=entry,
                    )
                for entity, payload in sync_entities:
                    db.enqueue_sync_event(
                        local_conn,
                        ts=now,
                        entity=entity,
                        entity_id=str(payload.get("request_id") or payload.get("part_id") or uuid.uuid4()),
                        payload=payload,
                    )
            else:
                with db.open_server_connection() as server_conn:
                    for entry in log_entries:
                        db.insert_decision_log(server_conn, entry)
                    for entity, payload in sync_entities:
                        _apply_part_sync_entity(server_conn, entity, payload)
                    server_conn.commit()
            local_conn.commit()
            return {
                "ok": False,
                "blocked_out_of_stock": True,
                "message": "Part is out of stock. Restock request created.",
                "restock_request": restock_request,
                "inventory": inventory_after,
            }

        usage_id = db.insert_parts_usage_log(local_conn, usage_entry)
        inventory_after = db.get_part_inventory(local_conn, request.part_id, location) or inventory_before
        inventory_after["stock_status"] = _stock_status(
            int(inventory_after.get("quantity_on_hand", 0)),
            int(inventory_after.get("reorder_level", 0)),
        )
        usage_event = dict(usage_entry)
        usage_event["id"] = usage_id

        workflow_event = {
            "ts": now,
            "job_id": request.job_id,
            "step_id": canonical_step_id,
            "actor_id": actor_id,
            "event_type": "PART_USED",
            "input_json": {
                "part_id": request.part_id,
                "part_name": part_name,
                "quantity_used": quantity_used,
                "location": location,
            },
            "output_json": {
                "quantity_on_hand_after": inventory_after.get("quantity_on_hand", 0),
            },
        }
        db.insert_workflow_event(local_conn, workflow_event)
        part_used_entry = _build_log_entry(
            ts=now,
            job_id=request.job_id,
            agent_id=actor_id,
            action="PART_USED",
            input_json={
                "step_id": canonical_step_id,
                "part_id": request.part_id,
                "part_name": part_name,
                "quantity_used": quantity_used,
                "location": location,
            },
            output_json={
                "quantity_on_hand_after": inventory_after.get("quantity_on_hand", 0),
            },
            confidence=1.0,
        )
        db.insert_decision_log(local_conn, part_used_entry)
        log_entries.extend([part_used_entry])
        sync_entities.append(("parts_usage", usage_entry))
        sync_entities.append(
            (
                "parts_inventory_upsert",
                {
                    "part_id": inventory_after.get("part_id"),
                    "location": inventory_after.get("location"),
                    "quantity_on_hand": inventory_after.get("quantity_on_hand"),
                    "reorder_level": inventory_after.get("reorder_level"),
                    "updated_ts": inventory_after.get("updated_ts"),
                },
            )
        )

        if offline_mode:
            db.enqueue_sync_event(
                local_conn,
                ts=workflow_event["ts"],
                entity="workflow_event",
                entity_id=f"{request.job_id}:{canonical_step_id}:PART_USED:{workflow_event['ts']}",
                payload=workflow_event,
            )
            for entry in log_entries:
                db.enqueue_sync_event(
                    local_conn,
                    ts=entry["ts"],
                    entity="decision_log",
                    entity_id=f"{entry['job_id']}:{entry['agent_id']}:{entry['action']}:{entry['ts']}",
                    payload=entry,
                )
            for entity, payload in sync_entities:
                db.enqueue_sync_event(
                    local_conn,
                    ts=now,
                    entity=entity,
                    entity_id=str(payload.get("id") or payload.get("part_id") or uuid.uuid4()),
                    payload=payload,
                )
        else:
            with db.open_server_connection() as server_conn:
                db.insert_workflow_event(server_conn, workflow_event)
                for entry in log_entries:
                    db.insert_decision_log(server_conn, entry)
                for entity, payload in sync_entities:
                    _apply_part_sync_entity(server_conn, entity, payload)
                server_conn.commit()

        local_conn.commit()
        return {
            "ok": True,
            "blocked_out_of_stock": False,
            "message": "Part usage recorded.",
            "usage": usage_event,
            "inventory": inventory_after,
        }


@app.get("/api/parts/restock-requests")
def get_parts_restock_requests(
    status: Literal["PENDING", "FULFILLED"] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        requests = db.list_restock_requests(
            local_conn,
            status=status,
            limit=max(1, min(500, int(limit))),
        )
        return {"count": len(requests), "status": status, "requests": requests}


@app.post("/api/parts/catalog")
def upsert_parts_catalog_item(request: PartsCatalogUpsertRequest) -> dict[str, Any]:
    _require_supervisor_role(request.actor_role)
    now = _utc_now()
    offline_mode = _is_offline(False)
    with db.open_local_connection() as local_conn:
        part = db.upsert_part_catalog(
            local_conn,
            {
                "part_name": request.part_name,
                "category": request.category,
                "unit": request.unit,
                "active": 1,
                "updated_ts": now,
                "created_ts": now,
            },
        )
        inventory_snapshot = None
        if request.location and request.initial_quantity is not None:
            inventory_snapshot = db.add_part_inventory_quantity(
                local_conn,
                part_id=str(part.get("part_id")),
                location=str(request.location),
                quantity_add=max(0, int(request.initial_quantity)),
                updated_ts=now,
            )
        log_entry = _build_log_entry(
            ts=now,
            job_id="inventory-global",
            agent_id=request.actor_id,
            action="PART_CATALOG_UPSERT",
            input_json={
                "part_name": request.part_name,
                "category": request.category,
                "unit": request.unit,
                "location": request.location,
                "initial_quantity": request.initial_quantity,
            },
            output_json={"part_id": part.get("part_id")},
            confidence=1.0,
        )
        db.insert_decision_log(local_conn, log_entry)

        if offline_mode:
            db.enqueue_sync_event(
                local_conn,
                ts=now,
                entity="decision_log",
                entity_id=f"inventory:{request.actor_id}:PART_CATALOG_UPSERT:{now}",
                payload=log_entry,
            )
            db.enqueue_sync_event(
                local_conn,
                ts=now,
                entity="parts_catalog_upsert",
                entity_id=str(part.get("part_id")),
                payload=part,
            )
            if inventory_snapshot:
                db.enqueue_sync_event(
                    local_conn,
                    ts=now,
                    entity="parts_inventory_upsert",
                    entity_id=f"{inventory_snapshot.get('part_id')}:{inventory_snapshot.get('location')}",
                    payload=inventory_snapshot,
                )
        else:
            with db.open_server_connection() as server_conn:
                db.insert_decision_log(server_conn, log_entry)
                _apply_part_sync_entity(server_conn, "parts_catalog_upsert", part)
                if inventory_snapshot:
                    _apply_part_sync_entity(server_conn, "parts_inventory_upsert", inventory_snapshot)
                server_conn.commit()
        local_conn.commit()
        return {
            "part": part,
            "inventory": inventory_snapshot,
        }


@app.post("/api/parts/replenish")
def replenish_parts_inventory(request: PartsReplenishRequest) -> dict[str, Any]:
    _require_supervisor_role(request.actor_role)
    now = _utc_now()
    offline_mode = _is_offline(False)
    quantity_add = max(1, int(request.quantity_add))
    with db.open_local_connection() as local_conn:
        inventory = db.add_part_inventory_quantity(
            local_conn,
            part_id=request.part_id,
            location=str(request.location or "").strip() or "Unknown",
            quantity_add=quantity_add,
            updated_ts=now,
        )
        if not inventory:
            raise HTTPException(status_code=404, detail="part_id not found")
        restock_status_payload = None
        if request.request_id:
            updated = db.update_restock_request_status(
                local_conn,
                request_id=request.request_id,
                status=db.RESTOCK_STATUS_FULFILLED,
                fulfilled_by=request.actor_id,
                fulfilled_ts=now,
            )
            if updated:
                restock_status_payload = {
                    "request_id": request.request_id,
                    "status": db.RESTOCK_STATUS_FULFILLED,
                    "fulfilled_by": request.actor_id,
                    "fulfilled_ts": now,
                }
        log_entry = _build_log_entry(
            ts=now,
            job_id="inventory-global",
            agent_id=request.actor_id,
            action="PART_REPLENISHED",
            input_json={
                "part_id": request.part_id,
                "location": request.location,
                "quantity_add": quantity_add,
                "request_id": request.request_id,
            },
            output_json={
                "quantity_on_hand": inventory.get("quantity_on_hand"),
            },
            confidence=1.0,
        )
        db.insert_decision_log(local_conn, log_entry)

        if offline_mode:
            db.enqueue_sync_event(
                local_conn,
                ts=now,
                entity="decision_log",
                entity_id=f"inventory:{request.actor_id}:PART_REPLENISHED:{now}",
                payload=log_entry,
            )
            db.enqueue_sync_event(
                local_conn,
                ts=now,
                entity="parts_inventory_upsert",
                entity_id=f"{inventory.get('part_id')}:{inventory.get('location')}",
                payload=inventory,
            )
            if restock_status_payload:
                db.enqueue_sync_event(
                    local_conn,
                    ts=now,
                    entity="parts_restock_status",
                    entity_id=str(request.request_id),
                    payload=restock_status_payload,
                )
        else:
            with db.open_server_connection() as server_conn:
                db.insert_decision_log(server_conn, log_entry)
                _apply_part_sync_entity(server_conn, "parts_inventory_upsert", inventory)
                if restock_status_payload:
                    _apply_part_sync_entity(server_conn, "parts_restock_status", restock_status_payload)
                server_conn.commit()

        local_conn.commit()
        inventory["stock_status"] = _stock_status(
            int(inventory.get("quantity_on_hand", 0)),
            int(inventory.get("reorder_level", 0)),
        )
        return {
            "inventory": inventory,
            "restock_status": restock_status_payload,
        }


@app.post("/api/parts/adjust")
def adjust_parts_inventory(request: PartsAdjustRequest) -> dict[str, Any]:
    _require_supervisor_role(request.actor_role)
    now = _utc_now()
    offline_mode = _is_offline(False)
    quantity_delta = int(request.quantity_delta)
    if quantity_delta == 0:
        raise HTTPException(status_code=422, detail="quantity_delta must be non-zero")
    location = str(request.location or "").strip() or "Unknown"

    with db.open_local_connection() as local_conn:
        existing = db.get_part_inventory(local_conn, request.part_id, location)
        if not existing:
            raise HTTPException(status_code=404, detail="part_id not found")
        previous_qty = int(existing.get("quantity_on_hand", 0))
        next_qty = max(0, previous_qty + quantity_delta)

        inventory = db.upsert_part_inventory_row(
            local_conn,
            part_id=request.part_id,
            location=location,
            quantity_on_hand=next_qty,
            reorder_level=int(existing.get("reorder_level", 2)),
            updated_ts=now,
        )
        log_entry = _build_log_entry(
            ts=now,
            job_id="inventory-global",
            agent_id=request.actor_id,
            action="PART_INVENTORY_ADJUSTED",
            input_json={
                "part_id": request.part_id,
                "location": location,
                "quantity_delta": quantity_delta,
                "previous_qty": previous_qty,
                "notes": request.notes,
            },
            output_json={"quantity_on_hand": inventory.get("quantity_on_hand")},
            confidence=1.0,
        )
        db.insert_decision_log(local_conn, log_entry)

        if offline_mode:
            db.enqueue_sync_event(
                local_conn,
                ts=now,
                entity="decision_log",
                entity_id=f"inventory:{request.actor_id}:PART_INVENTORY_ADJUSTED:{now}",
                payload=log_entry,
            )
            db.enqueue_sync_event(
                local_conn,
                ts=now,
                entity="parts_inventory_upsert",
                entity_id=f"{inventory.get('part_id')}:{inventory.get('location')}",
                payload=inventory,
            )
        else:
            with db.open_server_connection() as server_conn:
                db.insert_decision_log(server_conn, log_entry)
                _apply_part_sync_entity(server_conn, "parts_inventory_upsert", inventory)
                server_conn.commit()

        local_conn.commit()
        inventory["stock_status"] = _stock_status(
            int(inventory.get("quantity_on_hand", 0)),
            int(inventory.get("reorder_level", 0)),
        )
        return {"inventory": inventory, "quantity_delta": quantity_delta}


@app.get("/api/supervisor/queue")
def get_supervisor_queue() -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        queue = db.fetch_pending_approval_jobs(local_conn)
        return {"count": len(queue), "jobs": queue}


@app.get("/api/supervisor/tickets")
def get_supervisor_tickets(ticket_state: Literal["ALL", "OPEN", "CLOSED"] = "ALL", limit: int = 200) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        tickets = db.fetch_supervisor_ticket_ledger(
            local_conn,
            ticket_state=None if ticket_state == "ALL" else ticket_state,
            limit=min(max(limit, 1), 500),
        )
        open_count = sum(1 for item in tickets if str(item.get("ticket_state", "")).upper() == "OPEN")
        closed_count = sum(1 for item in tickets if str(item.get("ticket_state", "")).upper() == "CLOSED")
        return {
            "count": len(tickets),
            "open_count": open_count,
            "closed_count": closed_count,
            "ticket_state": ticket_state,
            "tickets": tickets,
        }


@app.get("/api/customer/queue")
def get_customer_approval_queue(include_rework: bool = True, limit: int = 100) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        queue = db.fetch_customer_approval_jobs(
            local_conn,
            include_rework=include_rework,
            limit=min(max(limit, 1), 200),
        )
        return {"count": len(queue), "jobs": queue}


@app.get("/api/supervisor/alerts")
def get_supervisor_alerts(include_acknowledged: bool = False) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        alerts = db.fetch_supervisor_alerts(local_conn, include_acknowledged=include_acknowledged)
        return {"count": len(alerts), "alerts": alerts}


@app.post("/api/jobs/check-timeouts")
def check_approval_timeouts(request: TimeoutCheckRequest | None = None) -> dict[str, Any]:
    now = request.now_ts if request and request.now_ts else _utc_now()
    offline_mode = _is_offline(False)
    processed = 0
    affected_job_ids: list[str] = []

    with db.open_local_connection() as local_conn:
        overdue_jobs = db.fetch_overdue_pending_jobs(local_conn, now)
        for job in overdue_jobs:
            processed += 1
            job_id = str(job["job_id"])
            final_response = job.get("final_response_json") or {}
            workflow_mode = _derive_workflow_mode(status="TIMEOUT_HOLD", requires_approval=True)
            workflow_meta = _workflow_mode_metadata(workflow_mode)
            final_response["status"] = "TIMEOUT_HOLD"
            final_response["requires_approval"] = True
            final_response["timed_out"] = True
            final_response["timeout_ts"] = now
            final_response["workflow_mode"] = workflow_mode
            final_response["workflow_intent"] = workflow_meta["workflow_intent"]
            final_response["allowed_actions"] = workflow_meta["allowed_actions"]
            final_response["suppressed_guidance"] = workflow_meta["suppressed_guidance"]
            reasons = set(final_response.get("escalation_reasons", []))
            reasons.add("approval_timeout")
            final_response["escalation_reasons"] = sorted(reasons)

            log_entry = _build_log_entry(
                ts=now,
                job_id=job_id,
                agent_id="approval_logic",
                action="APPROVAL_TIMEOUT_FAILSAFE",
                input_json={
                    "approval_due_ts": job.get("approval_due_ts"),
                    "status_before": job.get("status"),
                },
                output_json={"status": "TIMEOUT_HOLD"},
                confidence=1.0,
                requires_human=1,
            )
            db.insert_decision_log(local_conn, log_entry)

            alert = {
                "ts": now,
                "job_id": job_id,
                "alert_type": "APPROVAL_TIMEOUT",
                "payload_json": {
                    "approval_due_ts": job.get("approval_due_ts"),
                    "message": "Approval timeout exceeded 30-minute hold window.",
                },
                "acknowledged": 0,
            }
            db.insert_supervisor_alert(local_conn, alert)

            job_row = {
                "job_id": job_id,
                "created_ts": job["created_ts"],
                "updated_ts": now,
                "status": "TIMEOUT_HOLD",
                "field_payload_json": job.get("field_payload_json") or {},
                "final_response_json": final_response,
                "requires_approval": 1,
                "approved_by": None,
                "approved_ts": None,
                "guided_question": job.get("guided_question"),
                "guided_answer": job.get("guided_answer"),
                "approval_due_ts": job.get("approval_due_ts"),
                "timed_out": 1,
                "first_occurrence_fault": int(job.get("first_occurrence_fault", 0)),
                "assigned_tech_id": job.get("assigned_tech_id"),
                "workflow_mode": workflow_mode,
            }
            db.upsert_job(local_conn, job_row)

            if offline_mode:
                _queue_offline_events(local_conn, job_row, [log_entry], alert_events=[alert])
            else:
                _mirror_online_to_server(job_row, [log_entry], alert_events=[alert])
            affected_job_ids.append(job_id)

        local_conn.commit()

    return {
        "processed": processed,
        "timed_out_count": len(affected_job_ids),
        "affected_job_ids": affected_job_ids,
        "checked_at": now,
    }


@app.post("/api/supervisor/approve")
def supervisor_approve(request: SupervisorApproveRequest) -> dict[str, Any]:
    now = _utc_now()
    offline_mode = _is_offline(False)
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, request.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        final_response = dict(job.get("final_response_json") or {})
        log_entries_for_sync: list[dict[str, Any]] = []
        workflow_steps_for_sync: list[dict[str, Any]] | None = None
        workflow_events_for_sync: list[dict[str, Any]] | None = None

        approval_stage = str(final_response.get("approval_stage", "")).upper()
        current_status = str(job.get("status", "")).upper()
        repair_release_approval = approval_stage == "REPAIR_RELEASE"
        quote_approval = current_status == STATUS_PENDING_QUOTE_APPROVAL or approval_stage == "QUOTE_EMAIL"

        if repair_release_approval:
            new_status = STATUS_REPAIR_COMPLETED if request.decision == "approve" else STATUS_REPAIR_IN_PROGRESS
            workflow_mode = WORKFLOW_MODE_FIX_PLAN
            workflow_meta = _workflow_mode_metadata(workflow_mode)
            final_response["status"] = new_status
            final_response["requires_approval"] = False
            final_response["approval_due_ts"] = None
            final_response["timed_out"] = False
            final_response["approval_stage"] = "NONE"
            final_response["workflow_mode"] = workflow_mode
            final_response["workflow_intent"] = workflow_meta["workflow_intent"]
            final_response["allowed_actions"] = workflow_meta["allowed_actions"]
            final_response["suppressed_guidance"] = workflow_meta["suppressed_guidance"]
            final_response["repair_supervisor_decision"] = {
                "decision": request.decision,
                "approver_name": request.approver_name,
                "notes": request.notes,
                "ts": now,
            }
            workflow_steps_for_sync = db.get_workflow_steps(local_conn, request.job_id)
        elif quote_approval:
            new_status = (
                STATUS_AWAITING_CUSTOMER_APPROVAL if request.decision == "approve" else STATUS_QUOTE_REWORK_REQUIRED
            )
            workflow_mode = WORKFLOW_MODE_INVESTIGATION_ONLY
            workflow_meta = _workflow_mode_metadata(workflow_mode)
            final_response["status"] = new_status
            final_response["requires_approval"] = False
            final_response["approval_due_ts"] = None
            final_response["timed_out"] = False
            final_response["approval_stage"] = "CUSTOMER_DECISION"
            final_response["quote_stage"] = (
                "AWAITING_CUSTOMER_APPROVAL" if request.decision == "approve" else "QUOTE_REWORK_REQUIRED"
            )
            final_response["workflow_mode"] = workflow_mode
            final_response["workflow_intent"] = workflow_meta["workflow_intent"]
            final_response["allowed_actions"] = workflow_meta["allowed_actions"]
            final_response["suppressed_guidance"] = workflow_meta["suppressed_guidance"]
            final_response["quote_supervisor_decision"] = {
                "decision": request.decision,
                "approver_name": request.approver_name,
                "notes": request.notes,
                "ts": now,
            }
            workflow_steps_for_sync = db.get_workflow_steps(local_conn, request.job_id)
        else:
            new_status = STATUS_READY if request.decision == "approve" else STATUS_DENIED
            workflow_mode = WORKFLOW_MODE_FIX_PLAN if request.decision == "approve" else WORKFLOW_MODE_INVESTIGATION_ONLY
            workflow_meta = _workflow_mode_metadata(workflow_mode)
            final_response["status"] = new_status
            final_response["requires_approval"] = False
            final_response["approval_due_ts"] = None
            final_response["timed_out"] = False
            final_response["workflow_mode"] = workflow_mode
            final_response["workflow_intent"] = workflow_meta["workflow_intent"]
            final_response["allowed_actions"] = workflow_meta["allowed_actions"]
            final_response["suppressed_guidance"] = workflow_meta["suppressed_guidance"]
            final_response["supervisor_decision"] = {
                "decision": request.decision,
                "approver_name": request.approver_name,
                "notes": request.notes,
                "ts": now,
            }

            if request.decision == "approve":
                payload = dict(job.get("field_payload_json") or {})
                triage_data, evidence_data, schedule_data = _extract_triage_replan_payload(job)
                if not triage_data:
                    triage_data = _strip_triage_meta(triage_agent.analyze(payload, offline_mode=offline_mode))
                if not evidence_data:
                    evidence_data = parts_agent.collect_evidence(payload, triage_data)
                if not schedule_data:
                    schedule_data = scheduler_agent.forecast(payload)

                workflow_steps = _build_actionable_workflow(
                    triage=triage_data,
                    evidence=evidence_data,
                    scheduler=schedule_data,
                    workflow_mode=WORKFLOW_MODE_FIX_PLAN,
                    offline_mode=offline_mode,
                )
                workflow_generation_agent = _workflow_generation_agent_id(WORKFLOW_MODE_FIX_PLAN)
                db.replace_workflow_steps(
                    local_conn,
                    job_id=request.job_id,
                    steps=workflow_steps,
                    ts=now,
                    agent_id=workflow_generation_agent,
                )
                workflow_event = {
                    "ts": now,
                    "job_id": request.job_id,
                    "step_id": None,
                    "actor_id": workflow_generation_agent,
                    "event_type": "WORKFLOW_APPROVAL_PROMOTION",
                    "input_json": {"decision": request.decision},
                    "output_json": {
                        "step_count": len(workflow_steps),
                        "workflow_mode": WORKFLOW_MODE_FIX_PLAN,
                        "workflow_generation_agent": workflow_generation_agent,
                    },
                }
                db.insert_workflow_event(local_conn, workflow_event)
                workflow_events_for_sync = [workflow_event]
                workflow_steps_for_sync = workflow_steps

                report_text, report_meta = _generate_service_report(
                    payload=payload,
                    triage=triage_data,
                    evidence=evidence_data,
                    scheduler=schedule_data,
                    requires_approval=False,
                    workflow_mode=WORKFLOW_MODE_FIX_PLAN,
                    offline_mode=offline_mode,
                )
                final_response["service_report"] = report_text
                final_response["triage"] = triage_data
                final_response["evidence"] = evidence_data
                final_response["schedule_hint"] = schedule_data
                final_response["initial_workflow"] = workflow_steps
                final_response["assignment_recommendation"] = schedule_data.get("assignment_recommendation")

                promotion_entry = _build_log_entry(
                    ts=now,
                    job_id=request.job_id,
                    agent_id=workflow_generation_agent,
                    action="POST_APPROVAL_FIX_PLAN_GENERATED",
                    input_json={
                        "workflow_mode": WORKFLOW_MODE_FIX_PLAN,
                        "prompt_template_id": report_meta.get("prompt_template_id"),
                        "prompt_hash": report_meta.get("prompt_hash"),
                        "context_hash": report_meta.get("context_hash"),
                    },
                    output_json={
                        "step_count": len(workflow_steps),
                        "used_fallback_template": report_meta.get("used_fallback_template", False),
                        "workflow_generation_agent": workflow_generation_agent,
                    },
                    confidence=0.8,
                )
                db.insert_decision_log(local_conn, promotion_entry)
                log_entries_for_sync.append(promotion_entry)
            else:
                workflow_steps_for_sync = db.get_workflow_steps(local_conn, request.job_id)

        job_row = _build_updated_job_row(
            job,
            now=now,
            status=new_status,
            final_response=final_response,
            requires_approval=False,
            approved_by=request.approver_name,
            approved_ts=now,
            approval_due_ts=None,
            timed_out=0,
            workflow_mode=workflow_mode,
        )
        db.upsert_job(local_conn, job_row)

        approval_entry = _build_log_entry(
            ts=now,
            job_id=request.job_id,
            agent_id="human_supervisor",
            action="SUPERVISOR_DECISION",
            input_json=request.model_dump(),
            output_json={
                "status": new_status,
                "job_id": request.job_id,
                "approval_stage": (
                    "REPAIR_RELEASE"
                    if repair_release_approval
                    else ("QUOTE_EMAIL" if quote_approval else "TECHNICAL_WORKFLOW")
                ),
            },
            confidence=1.0,
            requires_human=0,
        )
        db.insert_decision_log(local_conn, approval_entry)
        log_entries_for_sync.append(approval_entry)
        metric_event = _metric_event(
            agent_id="human_supervisor",
            counter="approvals" if request.decision == "approve" else "denials",
            confidence=1.0,
        )
        db.apply_metric_event(local_conn, metric_event)

        if offline_mode:
            _queue_offline_events(
                local_conn,
                job_row,
                log_entries_for_sync,
                workflow_steps=workflow_steps_for_sync,
                workflow_events=workflow_events_for_sync,
                metric_events=[metric_event],
            )
        else:
            _mirror_online_to_server(
                job_row,
                log_entries_for_sync,
                workflow_steps=workflow_steps_for_sync,
                workflow_events=workflow_events_for_sync,
                metric_events=[metric_event],
            )

        local_conn.commit()
        updated = db.get_job(local_conn, request.job_id)
        if updated:
            updated = _normalize_final_response(updated)
        return updated or job_row


@app.post("/api/sync")
def sync_to_server() -> dict[str, Any]:
    processed = 0
    synced = 0
    failed = 0
    alerts_triggered = 0
    last_sync_ts: str | None = None
    processed_by_entity: dict[str, int] = {}
    synced_by_entity: dict[str, int] = {}
    failed_by_entity: dict[str, int] = {}

    with db.open_local_connection() as local_conn, db.open_server_connection() as server_conn:
        events = db.get_unsynced_events(local_conn)
        for event in events:
            processed += 1
            entity = str(event.get("entity", "unknown"))
            processed_by_entity[entity] = processed_by_entity.get(entity, 0) + 1
            try:
                payload = event.get("payload_json") or {}
                if entity == "job":
                    db.upsert_job_lww(server_conn, payload)
                elif entity == "decision_log":
                    db.insert_decision_log(server_conn, payload)
                elif entity == "workflow_steps_replace":
                    db.replace_workflow_steps(
                        server_conn,
                        job_id=payload["job_id"],
                        steps=payload.get("steps", []),
                        ts=payload.get("ts", _utc_now()),
                        agent_id=payload.get("agent_id", "orchestrator"),
                    )
                elif entity == "workflow_event":
                    db.insert_workflow_event(server_conn, payload)
                elif entity in {"metric_event", "agent_metric"}:
                    db.apply_metric_event(server_conn, payload)
                elif entity == "supervisor_alert":
                    db.insert_supervisor_alert(server_conn, payload)
                elif entity == "attachment_upsert":
                    db.upsert_issue_attachment(server_conn, payload)
                    db.refresh_issue_attachment_summary(server_conn, str(payload.get("job_id", "")))
                elif entity == "attachment_file_copy":
                    attachment_id = str(payload.get("attachment_id", ""))
                    if not attachment_id:
                        raise ValueError("attachment_file_copy missing attachment_id")
                    attachment = db.get_attachment(local_conn, attachment_id)
                    if not attachment:
                        raise ValueError(f"attachment_id not found: {attachment_id}")
                    copied = _copy_attachment_local_to_server(attachment)
                    db.upsert_issue_attachment(local_conn, copied)
                    db.refresh_issue_attachment_summary(local_conn, str(copied.get("job_id", "")))
                    db.upsert_issue_attachment(server_conn, copied)
                    db.refresh_issue_attachment_summary(server_conn, str(copied.get("job_id", "")))
                elif entity in {
                    "parts_catalog_upsert",
                    "parts_inventory_upsert",
                    "parts_usage",
                    "parts_restock_request",
                    "parts_restock_status",
                }:
                    _apply_part_sync_entity(server_conn, entity, payload)
                else:
                    raise ValueError(f"Unsupported sync entity '{entity}'")
                db.mark_sync_event_synced(local_conn, int(event["id"]))
                synced += 1
                synced_by_entity[entity] = synced_by_entity.get(entity, 0) + 1
                last_sync_ts = _utc_now()
            except Exception as exc:  # noqa: BLE001
                failed += 1
                failed_by_entity[entity] = failed_by_entity.get(entity, 0) + 1
                failed_event = db.mark_sync_event_failed(local_conn, int(event["id"]), str(exc))
                retry_count = int((failed_event or {}).get("retry_count", 0))
                if retry_count > 3:
                    job_id = _extract_job_id_from_sync_event(event)
                    alert = {
                        "ts": _utc_now(),
                        "job_id": job_id,
                        "alert_type": "SYNC_FAILURE",
                        "payload_json": {
                            "sync_event_id": event.get("id"),
                            "entity": event.get("entity"),
                            "retry_count": retry_count,
                            "last_error": str(exc),
                        },
                        "acknowledged": 0,
                    }
                    db.insert_supervisor_alert(local_conn, alert)
                    if job_id:
                        sync_log = _build_log_entry(
                            ts=_utc_now(),
                            job_id=job_id,
                            agent_id="sync_engine",
                            action="SYNC_RETRY_THRESHOLD_EXCEEDED",
                            input_json={
                                "sync_event_id": event.get("id"),
                                "entity": event.get("entity"),
                                "retry_count": retry_count,
                            },
                            output_json={"alert_type": "SYNC_FAILURE"},
                            confidence=1.0,
                            requires_human=1,
                        )
                        db.insert_decision_log(local_conn, sync_log)
                        try:
                            db.insert_decision_log(server_conn, sync_log)
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        db.insert_supervisor_alert(server_conn, alert)
                    except Exception:  # noqa: BLE001
                        pass
                    alerts_triggered += 1

        local_conn.commit()
        server_conn.commit()

    return {
        "processed": processed,
        "synced": synced,
        "failed": failed,
        "alerts_triggered": alerts_triggered,
        "processed_by_entity": processed_by_entity,
        "synced_by_entity": synced_by_entity,
        "failed_by_entity": failed_by_entity,
        "last_sync_time": last_sync_ts,
    }


@app.get("/api/job/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        data = db.fetch_job_with_logs(local_conn, job_id)
        if not data:
            raise HTTPException(status_code=404, detail="job_id not found")
        data["job"] = _normalize_final_response(data["job"])
        data["attachments"] = [_attachment_public_payload(item) for item in data.get("attachments", [])]
        return data


@app.get("/api/job/{job_id}/timeline")
def get_job_timeline(job_id: str) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        timeline = db.fetch_job_timeline(local_conn, job_id)
        return {
            "job_id": job_id,
            "status": job["status"],
            "requires_approval": bool(job["requires_approval"]),
            "timeline": timeline,
        }


@app.get("/api/job/{job_id}/workflow")
def get_job_workflow(job_id: str) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        final_response = (job.get("final_response_json") or {}) if isinstance(job, dict) else {}
        workflow_mode = str(
            final_response.get("workflow_mode")
            or job.get("workflow_mode")
            or _derive_workflow_mode(
                status=str(job.get("status", "")),
                requires_approval=bool(job.get("requires_approval", 0)),
                supervisor_decision=final_response.get("supervisor_decision"),
            )
        )
        workflow_meta = _workflow_mode_metadata(workflow_mode)
        return {
            "job_id": job_id,
            "status": job["status"],
            "requires_approval": bool(job["requires_approval"]),
            "approval_due_ts": job.get("approval_due_ts"),
            "timed_out": bool(job.get("timed_out", 0)),
            "workflow_mode": workflow_mode,
            "workflow_intent": workflow_meta["workflow_intent"],
            "allowed_actions": workflow_meta["allowed_actions"],
            "suppressed_guidance": workflow_meta["suppressed_guidance"],
            "workflow_generation_agent": final_response.get(
                "workflow_generation_agent",
                _workflow_generation_agent_id(workflow_mode),
            ),
            "escalation_reasons": final_response.get("escalation_reasons", []),
            "risk_signals": final_response.get("risk_signals", {}),
            "escalation_policy_version": final_response.get(
                "escalation_policy_version",
                RISK_SIGNAL_POLICY_VERSION,
            ),
            "policy_config_hash": final_response.get("policy_config_hash", ESCALATION_POLICY_HASH),
            "workflow_steps": db.get_workflow_steps(local_conn, job_id),
            "workflow_events": db.fetch_workflow_events(local_conn, job_id),
        }


@app.post("/api/job/{job_id}/workflow/step")
def update_workflow_step(job_id: str, request: WorkflowStepUpdateRequest) -> dict[str, Any]:
    now = _utc_now()
    offline_mode = _is_offline(False)
    requested_step_id = str(request.step_id or "").strip()
    canonical_step_id = _canonical_step_id(requested_step_id)
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")

        step = db.get_workflow_step(local_conn, job_id, canonical_step_id)
        if not step:
            raise HTTPException(status_code=404, detail="step_id not found")

        db.update_workflow_step_status(local_conn, job_id, canonical_step_id, request.status)
        updated_step = db.get_workflow_step(local_conn, job_id, canonical_step_id) or step

        workflow_event = {
            "ts": now,
            "job_id": job_id,
            "step_id": canonical_step_id,
            "actor_id": request.actor_id,
            "event_type": "STEP_RESULT",
            "input_json": {
                "measurement_json": request.measurement_json or {},
                "notes": request.notes,
            },
            "output_json": {"status": request.status},
        }
        db.insert_workflow_event(local_conn, workflow_event)

        log_entry = _build_log_entry(
            ts=now,
            job_id=job_id,
            agent_id=request.actor_id,
            action="WORKFLOW_STEP_UPDATE",
            input_json={
                "step_id": canonical_step_id,
                "requested_step_id": requested_step_id,
                "status": request.status,
                "measurement_json": request.measurement_json or {},
                "notes": request.notes,
                "request_supervisor_review": bool(request.request_supervisor_review),
            },
            output_json={"step_status": request.status},
            confidence=0.9 if request.status == "done" else 0.6,
        )
        db.insert_decision_log(local_conn, log_entry)
        log_entries_for_sync = [log_entry]

        current_status = str(job.get("status", "")).upper()
        allow_supervisor_escalation = False

        if bool(request.request_supervisor_review) and allow_supervisor_escalation:
            manual_entry = _build_log_entry(
                ts=now,
                job_id=job_id,
                agent_id=request.actor_id,
                action="MANUAL_ESCALATION_REQUESTED",
                input_json={
                    "source": "workflow_step_update",
                    "step_id": canonical_step_id,
                    "requested_step_id": requested_step_id,
                    "request_supervisor_review": True,
                },
                output_json={"status": "PENDING_APPROVAL"},
                confidence=1.0,
                requires_human=1,
            )
            db.insert_decision_log(local_conn, manual_entry)
            log_entries_for_sync.append(manual_entry)
        elif bool(request.request_supervisor_review) and not allow_supervisor_escalation:
            bypass_entry = _build_log_entry(
                ts=now,
                job_id=job_id,
                agent_id="orchestrator",
                action="SUPERVISOR_ESCALATION_BYPASSED",
                input_json={
                    "step_id": canonical_step_id,
                    "status": current_status,
                    "request_supervisor_review": True,
                },
                output_json={"reason": "supervisor_disabled_in_this_flow"},
                confidence=1.0,
            )
            db.insert_decision_log(local_conn, bypass_entry)
            log_entries_for_sync.append(bypass_entry)

        requires_approval = bool(job["requires_approval"])
        new_status = job["status"]
        high_risk_step_failure = bool(
            request.status in {"failed", "blocked"} and updated_step.get("risk_level") in {"HIGH", "CRITICAL"}
        )
        new_reasons = _evaluate_escalation_reasons(
            manual_request=False,
            high_risk_step_failure=False,
        )

        final_response = job.get("final_response_json") or {}
        previous_workflow_mode = str(
            final_response.get("workflow_mode")
            or job.get("workflow_mode")
            or _derive_workflow_mode(
                status=str(job.get("status", "")),
                requires_approval=bool(job.get("requires_approval", 0)),
                supervisor_decision=final_response.get("supervisor_decision"),
            )
        )
        escalation_reasons = set(final_response.get("escalation_reasons", []))
        escalation_reasons.update(new_reasons)
        if new_reasons:
            requires_approval = True
            new_status = "PENDING_APPROVAL"
            final_response["approval_due_ts"] = _approval_due_ts(now)
        workflow_mode = _derive_workflow_mode(status=new_status, requires_approval=requires_approval)
        workflow_meta = _workflow_mode_metadata(workflow_mode)
        final_response["status"] = new_status
        final_response["requires_approval"] = requires_approval
        final_response["escalation_reasons"] = sorted(escalation_reasons)
        final_response.setdefault("risk_signals", {})
        final_response.setdefault("escalation_policy_version", RISK_SIGNAL_POLICY_VERSION)
        final_response.setdefault("governance_policy_version", GOVERNANCE_POLICY_VERSION)
        final_response["workflow_mode"] = workflow_mode
        final_response["workflow_intent"] = workflow_meta["workflow_intent"]
        final_response["allowed_actions"] = workflow_meta["allowed_actions"]
        final_response["suppressed_guidance"] = workflow_meta["suppressed_guidance"]
        final_response["workflow_generation_agent"] = _workflow_generation_agent_id(workflow_mode)
        final_response["policy_config_hash"] = ESCALATION_POLICY_HASH
        final_response["last_workflow_update"] = {
            "ts": now,
            "step_id": canonical_step_id,
            "requested_step_id": requested_step_id,
            "status": request.status,
        }

        job_row = {
            "job_id": job["job_id"],
            "created_ts": job["created_ts"],
            "updated_ts": now,
            "status": new_status,
            "field_payload_json": job.get("field_payload_json") or {},
            "final_response_json": final_response,
            "requires_approval": int(requires_approval),
            "approved_by": job.get("approved_by"),
            "approved_ts": job.get("approved_ts"),
            "guided_question": job.get("guided_question"),
            "guided_answer": job.get("guided_answer"),
            "approval_due_ts": final_response.get("approval_due_ts"),
            "timed_out": int(final_response.get("timed_out", 0)),
            "first_occurrence_fault": int(job.get("first_occurrence_fault", 0)),
            "assigned_tech_id": job.get("assigned_tech_id"),
            "workflow_mode": workflow_mode,
        }
        if requires_approval:
            job_row["approved_by"] = None
            job_row["approved_ts"] = None
        db.upsert_job(local_conn, job_row)

        metric_events: list[dict[str, Any]] = []
        if new_reasons:
            escalation_metric = _metric_event(agent_id="approval_logic", counter="escalations")
            db.apply_metric_event(local_conn, escalation_metric)
            metric_events.append(escalation_metric)

        workflow_events_for_sync = [workflow_event]
        if workflow_mode != previous_workflow_mode:
            triage_data, evidence_data, schedule_data = _extract_triage_replan_payload(
                {"final_response_json": final_response}
            )
            if triage_data and evidence_data and schedule_data:
                regenerated_steps = _build_actionable_workflow(
                    triage=triage_data,
                    evidence=evidence_data,
                    scheduler=schedule_data,
                    workflow_mode=workflow_mode,
                    offline_mode=offline_mode,
                )
                workflow_generation_agent = _workflow_generation_agent_id(workflow_mode)
                db.replace_workflow_steps(
                    local_conn,
                    job_id=job_id,
                    steps=regenerated_steps,
                    ts=now,
                    agent_id=workflow_generation_agent,
                )
                transition_event = {
                    "ts": now,
                    "job_id": job_id,
                    "step_id": None,
                    "actor_id": workflow_generation_agent,
                    "event_type": "WORKFLOW_MODE_SWITCHED",
                    "input_json": {"previous_mode": previous_workflow_mode},
                    "output_json": {
                        "workflow_mode": workflow_mode,
                        "step_count": len(regenerated_steps),
                        "workflow_generation_agent": workflow_generation_agent,
                    },
                }
                db.insert_workflow_event(local_conn, transition_event)
                workflow_events_for_sync.append(transition_event)
                mode_switch_entry = _build_log_entry(
                    ts=now,
                    job_id=job_id,
                    agent_id=workflow_generation_agent,
                    action="WORKFLOW_MODE_SWITCHED",
                    input_json={"previous_mode": previous_workflow_mode, "new_mode": workflow_mode},
                    output_json={
                        "step_count": len(regenerated_steps),
                        "workflow_generation_agent": workflow_generation_agent,
                    },
                    confidence=0.85,
                )
                db.insert_decision_log(local_conn, mode_switch_entry)
                log_entries_for_sync.append(mode_switch_entry)

        workflow_steps = db.get_workflow_steps(local_conn, job_id)
        if offline_mode:
            _queue_offline_events(
                local_conn,
                job_row,
                log_entries_for_sync,
                workflow_steps=workflow_steps,
                workflow_events=workflow_events_for_sync,
                metric_events=metric_events,
            )
        else:
            _mirror_online_to_server(
                job_row,
                log_entries_for_sync,
                workflow_steps=workflow_steps,
                workflow_events=workflow_events_for_sync,
                metric_events=metric_events,
            )

        local_conn.commit()
        return {
            "job_id": job_id,
            "status": new_status,
            "requires_approval": requires_approval,
            "workflow_mode": workflow_mode,
            "workflow_intent": workflow_meta["workflow_intent"],
            "allowed_actions": workflow_meta["allowed_actions"],
            "suppressed_guidance": workflow_meta["suppressed_guidance"],
            "escalation_reasons": sorted(escalation_reasons),
            "risk_signals": final_response.get("risk_signals", {}),
            "escalation_policy_version": final_response.get(
                "escalation_policy_version",
                RISK_SIGNAL_POLICY_VERSION,
            ),
            "policy_config_hash": final_response.get("policy_config_hash", ESCALATION_POLICY_HASH),
            "updated_step": updated_step,
            "workflow_steps": workflow_steps,
        }


@app.post("/api/job/{job_id}/replan")
def replan_job(job_id: str) -> dict[str, Any]:
    now = _utc_now()
    offline_mode = _is_offline(False)
    mode_effective = _mode_effective(False)
    runtime_models = _runtime_model_config(mode_effective)

    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")

        payload = dict(job.get("field_payload_json") or {})
        payload.setdefault("guided_question", job.get("guided_question"))
        payload.setdefault("guided_answer", job.get("guided_answer"))
        if not str(payload.get("guided_answer") or "").strip():
            payload["guided_answer"] = _default_guided_answer(payload)
        recent_events = db.fetch_workflow_events(local_conn, job_id)[-3:]
        if recent_events:
            event_notes = " ".join(
                str(item.get("input_json", {}).get("notes", "")) for item in recent_events
            ).strip()
            if event_notes:
                payload["notes"] = f"{payload.get('notes', '')} {event_notes}".strip()
        payload, normalization_meta = _normalize_issue_payload(payload)

        log_entries: list[dict[str, Any]] = []
        workflow_events: list[dict[str, Any]] = []
        metric_events: list[dict[str, Any]] = []
        model_route_entry = _build_model_route_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            mode_effective=mode_effective,
            runtime_models=runtime_models,
        )
        db.insert_decision_log(local_conn, model_route_entry)
        log_entries.append(model_route_entry)

        normalization_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="orchestrator",
            action="FREE_TEXT_NORMALIZED_REPLAN",
            input_json={
                "issue_text": payload.get("issue_text"),
                "equipment_id": payload.get("equipment_id"),
                "fault_code": payload.get("fault_code"),
                "symptoms": payload.get("symptoms"),
                "notes": payload.get("notes"),
            },
            output_json={"normalization_meta": normalization_meta},
            confidence=float(normalization_meta.get("normalization_confidence", 0.0)),
        )
        db.insert_decision_log(local_conn, normalization_entry)
        log_entries.append(normalization_entry)

        triage_result = triage_agent.analyze(payload, offline_mode=offline_mode)
        triage_output = _strip_triage_meta(triage_result)
        triage_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="triage_agent",
            action="TRIAGE_REPLAN",
            input_json={"payload": payload},
            output_json=triage_output,
            confidence=float(triage_output.get("confidence", 0.0)),
        )
        db.insert_decision_log(local_conn, triage_entry)
        log_entries.append(triage_entry)
        metric_events.append(
            _metric_event(
                agent_id="triage_agent",
                counter="replans",
                confidence=float(triage_output.get("confidence", 0.0)),
            )
        )

        evidence_result = parts_agent.collect_evidence(payload, triage_output)
        evidence_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="parts_agent",
            action="EVIDENCE_REPLAN",
            input_json={"payload": payload, "triage_summary": triage_output.get("summary")},
            output_json=evidence_result,
            confidence=float(evidence_result.get("confidence", 0.0)),
        )
        db.insert_decision_log(local_conn, evidence_entry)
        log_entries.append(evidence_entry)
        metric_events.append(
            _metric_event(
                agent_id="parts_agent",
                counter="replans",
                confidence=float(evidence_result.get("confidence", 0.0)),
            )
        )

        schedule_hint = scheduler_agent.forecast(payload)
        schedule_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="scheduler_agent",
            action="SCHEDULE_REPLAN",
            input_json={"payload": payload},
            output_json=schedule_hint,
            confidence=0.65,
        )
        db.insert_decision_log(local_conn, schedule_entry)
        log_entries.append(schedule_entry)
        metric_events.append(_metric_event(agent_id="scheduler_agent", counter="replans", confidence=0.65))

        occurrence_check_applied = _should_check_first_occurrence(payload)
        first_occurrence_fault = (
            db.is_first_occurrence_fault(
                local_conn,
                equipment_id=str(payload.get("equipment_id", "")),
                fault_code=str(payload.get("fault_code", "")),
                current_job_id=job_id,
            )
            if occurrence_check_applied
            else False
        )
        occurrence_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="approval_logic",
            action="FIRST_OCCURRENCE_CHECK",
            input_json={
                "equipment_id": payload.get("equipment_id"),
                "fault_code": payload.get("fault_code"),
            },
            output_json={
                "first_occurrence_fault": first_occurrence_fault,
                "check_applied": occurrence_check_applied,
            },
            confidence=1.0,
            requires_human=int(first_occurrence_fault),
        )
        db.insert_decision_log(local_conn, occurrence_entry)
        log_entries.append(occurrence_entry)

        combined_confidence = _clamp(
            (float(triage_output.get("confidence", 0.0)) + float(evidence_result.get("confidence", 0.0)))
            / 2
        )
        keyword_text = " ".join(
            [
                str(payload.get("fault_code", "")),
                str(payload.get("symptoms", "")),
                str(payload.get("notes", "")),
                str(triage_output.get("summary", "")),
            ]
        )
        keyword_safety_hit = _contains_keywords(keyword_text, SAFETY_KEYWORDS)
        keyword_warranty_hit = _contains_keywords(keyword_text, WARRANTY_KEYWORDS)
        llm_risk = _evaluate_llm_risk_signals(
            payload=payload,
            triage_output=triage_output,
            offline_mode=offline_mode,
        )
        risk_signals = _merge_keyword_risk_hits(
            llm_risk,
            keyword_safety_hit=keyword_safety_hit,
            keyword_warranty_hit=keyword_warranty_hit,
        )
        safety_hit = bool(risk_signals.get("safety_signal", False))
        warranty_hit = bool(risk_signals.get("warranty_signal", False))
        triage_unsafe = bool(triage_output.get("safety_flag", False))
        parts_unconfirmed = bool(
            _should_enforce_parts_availability(payload) and evidence_result.get("missing_critical_parts")
        )
        escalation_reasons = _evaluate_escalation_reasons(
            combined_confidence=combined_confidence,
            safety_hit=safety_hit,
            warranty_hit=warranty_hit,
            triage_unsafe=triage_unsafe,
            first_occurrence_fault=first_occurrence_fault,
            parts_unconfirmed=parts_unconfirmed,
        )
        current_status = str(job.get("status", "")).upper()
        in_repair_phase = current_status in {STATUS_REPAIR_POOL_OPEN, STATUS_REPAIR_IN_PROGRESS, STATUS_REPAIR_COMPLETED}
        requires_approval = False
        new_status = current_status if in_repair_phase else STATUS_DIAGNOSTIC_IN_PROGRESS
        workflow_mode = WORKFLOW_MODE_FIX_PLAN if in_repair_phase else WORKFLOW_MODE_INVESTIGATION_ONLY
        workflow_meta = _workflow_mode_metadata(workflow_mode)
        approval_due_ts = None
        escalation_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="approval_logic",
            action="ESCALATION_REPLAN_CHECK",
            input_json={
                "threshold": APPROVAL_THRESHOLD,
                "combined_confidence": combined_confidence,
                "safety_keyword_hit": keyword_safety_hit,
                "warranty_keyword_hit": keyword_warranty_hit,
                "safety_llm_hit": llm_risk.get("safety_signal", False),
                "warranty_llm_hit": llm_risk.get("warranty_signal", False),
                "llm_risk_confidence": llm_risk.get("confidence", 0.0),
                "risk_source": llm_risk.get("source", "unknown"),
                "risk_matched_terms": llm_risk.get("matched_terms", {}),
                "policy_version": RISK_SIGNAL_POLICY_VERSION,
                "policy_config_hash": ESCALATION_POLICY_HASH,
                "triage_unsafe": triage_unsafe,
                "first_occurrence_fault": first_occurrence_fault,
                "parts_unconfirmed": parts_unconfirmed,
            },
            output_json={
                "requires_approval": requires_approval,
                "status": new_status,
                "escalation_reasons": escalation_reasons,
                "risk_signals": risk_signals,
                "governance_policy_version": GOVERNANCE_POLICY_VERSION,
                "workflow_mode": workflow_mode,
                "supervisor_routing": "disabled_for_diagnostic_phase",
            },
            confidence=combined_confidence,
            requires_human=0,
        )
        db.insert_decision_log(local_conn, escalation_entry)
        log_entries.append(escalation_entry)

        workflow_steps = _build_actionable_workflow(
            triage=triage_output,
            evidence=evidence_result,
            scheduler=schedule_hint,
            workflow_mode=workflow_mode,
            offline_mode=offline_mode,
        )
        workflow_generation_agent = _workflow_generation_agent_id(workflow_mode)
        db.replace_workflow_steps(
            local_conn,
            job_id=job_id,
            steps=workflow_steps,
            ts=_utc_now(),
            agent_id=workflow_generation_agent,
        )
        workflow_event = {
            "ts": _utc_now(),
            "job_id": job_id,
            "step_id": None,
            "actor_id": workflow_generation_agent,
            "event_type": "WORKFLOW_REPLANNED",
            "input_json": {"recent_events_considered": len(recent_events)},
            "output_json": {
                "step_count": len(workflow_steps),
                "workflow_generation_agent": workflow_generation_agent,
            },
        }
        db.insert_workflow_event(local_conn, workflow_event)
        workflow_events.append(workflow_event)
        workflow_log_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id=workflow_generation_agent,
            action="WORKFLOW_REPLANNED",
            input_json={"job_id": job_id, "workflow_mode": workflow_mode},
            output_json={
                "step_count": len(workflow_steps),
                "suppressed_guidance": workflow_meta["suppressed_guidance"],
                "workflow_generation_agent": workflow_generation_agent,
            },
            confidence=0.8,
        )
        db.insert_decision_log(local_conn, workflow_log_entry)
        log_entries.append(workflow_log_entry)

        report_text, _ = _generate_service_report(
            payload=payload,
            triage=triage_output,
            evidence=evidence_result,
            scheduler=schedule_hint,
            requires_approval=requires_approval,
            workflow_mode=workflow_mode,
            offline_mode=offline_mode,
        )
        final_response = job.get("final_response_json") or {}
        final_response.update(
            {
                "status": new_status,
                "requires_approval": requires_approval,
                "approval_due_ts": approval_due_ts,
                "timed_out": False,
                "escalation_reasons": escalation_reasons,
                "risk_signals": risk_signals,
                "escalation_policy_version": RISK_SIGNAL_POLICY_VERSION,
                "governance_policy_version": GOVERNANCE_POLICY_VERSION,
                "policy_config_hash": ESCALATION_POLICY_HASH,
                "guided_question": payload.get("guided_question"),
                "guided_answer": payload.get("guided_answer"),
                "issue_text": payload.get("issue_text"),
                "normalization_meta": normalization_meta,
                "first_occurrence_fault": first_occurrence_fault,
                "service_report": report_text,
                "triage": triage_output,
                "evidence": evidence_result,
                "schedule_hint": schedule_hint,
                "assignment_recommendation": schedule_hint.get("assignment_recommendation"),
                "initial_workflow": workflow_steps,
                "mode_effective": mode_effective,
                "model_selected": runtime_models["model_selected"],
                "model_online": runtime_models["model_online"],
                "model_offline": runtime_models["model_offline"],
                "model_tier": runtime_models["model_tier"],
                "model_policy_valid": runtime_models["model_policy_valid"],
                "model_policy_notes": runtime_models["model_policy_notes"],
                "workflow_mode": workflow_mode,
                "workflow_intent": workflow_meta["workflow_intent"],
                "allowed_actions": workflow_meta["allowed_actions"],
                "suppressed_guidance": workflow_meta["suppressed_guidance"],
                "workflow_generation_agent": workflow_generation_agent,
            }
        )
        if workflow_mode == WORKFLOW_MODE_INVESTIGATION_ONLY and new_status == STATUS_DIAGNOSTIC_IN_PROGRESS:
            final_response["quote_stage"] = "DIAGNOSTIC_IN_PROGRESS"
            final_response["approval_stage"] = "CUSTOMER_DECISION"
        job_row = {
            "job_id": job["job_id"],
            "created_ts": job["created_ts"],
            "updated_ts": now,
            "status": new_status,
            "field_payload_json": payload,
            "final_response_json": final_response,
            "requires_approval": int(requires_approval),
            "approved_by": None if requires_approval else job.get("approved_by"),
            "approved_ts": None if requires_approval else job.get("approved_ts"),
            "guided_question": payload.get("guided_question"),
            "guided_answer": payload.get("guided_answer"),
            "approval_due_ts": approval_due_ts,
            "timed_out": 0,
            "first_occurrence_fault": int(first_occurrence_fault),
            "assigned_tech_id": (schedule_hint.get("assignment_recommendation") or {}).get("tech_id"),
            "workflow_mode": workflow_mode,
        }
        db.upsert_job(local_conn, job_row)

        for metric_event in metric_events:
            db.apply_metric_event(local_conn, metric_event)

        if offline_mode:
            _queue_offline_events(
                local_conn,
                job_row,
                log_entries,
                workflow_steps=workflow_steps,
                workflow_events=workflow_events,
                metric_events=metric_events,
            )
        else:
            _mirror_online_to_server(
                job_row,
                log_entries,
                workflow_steps=workflow_steps,
                workflow_events=workflow_events,
                metric_events=metric_events,
            )

        local_conn.commit()
        return {
            "job_id": job_id,
            "status": new_status,
            "requires_approval": requires_approval,
            "approval_due_ts": approval_due_ts,
            "timed_out": False,
            "escalation_reasons": escalation_reasons,
            "risk_signals": risk_signals,
            "escalation_policy_version": RISK_SIGNAL_POLICY_VERSION,
            "governance_policy_version": GOVERNANCE_POLICY_VERSION,
            "guided_question": payload.get("guided_question"),
            "guided_answer": payload.get("guided_answer"),
            "issue_text": payload.get("issue_text"),
            "normalization_meta": normalization_meta,
            "first_occurrence_fault": first_occurrence_fault,
            "triage": triage_output,
            "evidence": evidence_result,
            "schedule_hint": schedule_hint,
            "assignment_recommendation": schedule_hint.get("assignment_recommendation"),
            "updated_workflow": workflow_steps,
            "mode_effective": mode_effective,
            "model_selected": runtime_models["model_selected"],
            "model_online": runtime_models["model_online"],
            "model_offline": runtime_models["model_offline"],
            "model_tier": runtime_models["model_tier"],
            "model_policy_valid": runtime_models["model_policy_valid"],
            "model_policy_notes": runtime_models["model_policy_notes"],
            "workflow_mode": workflow_mode,
            "workflow_intent": workflow_meta["workflow_intent"],
            "allowed_actions": workflow_meta["allowed_actions"],
            "suppressed_guidance": workflow_meta["suppressed_guidance"],
            "workflow_generation_agent": workflow_generation_agent,
        }


@app.get("/api/metrics/agent-performance")
def get_agent_performance(day: str | None = None) -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        metrics = db.fetch_agent_metrics(local_conn, day=day)
        return {"day_filter": day, "count": len(metrics), "metrics": metrics}


@app.get("/api/config/runtime")
def get_runtime_config(is_offline: bool = False) -> dict[str, Any]:
    mode_effective = _mode_effective(is_offline)
    return _runtime_model_config(mode_effective)


@app.get("/api/health")
def health(is_offline: bool = False) -> dict[str, Any]:
    mode_effective = _mode_effective(is_offline)
    runtime = _runtime_model_config(mode_effective)
    return {
        "status": "ok",
        "ts": _utc_now(),
        **runtime,
    }
