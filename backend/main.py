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

from backend.agents import parts_agent, scheduler_agent, triage_agent
from backend.local_db import db


WORKFLOW_MODE_INVESTIGATION_ONLY = "INVESTIGATION_ONLY"
WORKFLOW_MODE_FIX_PLAN = "FIX_PLAN"
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


class JobSubmitRequest(BaseModel):
    job_id: str | None = None
    issue_text: str | None = None
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
        "workflow_intent": "Collect additional evidence for supervisor decision. Repair guidance suppressed.",
        "allowed_actions": list(WORKFLOW_ALLOWED_ACTIONS[WORKFLOW_MODE_INVESTIGATION_ONLY]),
        "suppressed_guidance": True,
    }


def _derive_workflow_mode(
    *,
    status: str,
    requires_approval: bool,
    supervisor_decision: dict[str, Any] | None = None,
) -> str:
    normalized_status = str(status or "").upper()
    if requires_approval or normalized_status in {"PENDING_APPROVAL", "TIMEOUT_HOLD", "DENIED"}:
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


def _compose_actionable_instruction(
    instructions: str,
    required_inputs: list[str],
    pass_criteria: list[str],
    recommended_parts: list[str],
    risk_level: str,
    suppress_repair_guidance: bool = False,
) -> str:
    base = instructions.strip() or "Perform this diagnostic check and capture evidence."
    capture = f"Capture: {', '.join(required_inputs)}."
    passed = f"Pass when: {', '.join(pass_criteria)}."
    if suppress_repair_guidance:
        parts_line = "Repair and parts guidance suppressed pending supervisor decision."
        escalation = "If blocked/failed: capture evidence and escalate to supervisor."
    else:
        parts_line = (
            f"Parts to validate if failed: {', '.join(recommended_parts)}."
            if recommended_parts
            else "Parts to validate if failed: none listed."
        )
        escalation = (
            "If blocked/failed: stop release and escalate to supervisor immediately."
            if risk_level in {"HIGH", "CRITICAL"}
            else "If blocked/failed: record notes and trigger workflow replan or supervisor review."
        )
    return f"{base} {capture} {passed} {parts_line} {escalation}"


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


def _build_actionable_workflow(
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
                "title": "Supervisor Evidence Handoff",
                "instructions": _compose_actionable_instruction(
                    "Finalize evidence summary and unresolved questions for supervisor review. Do not perform repair actions until approved.",
                    ["evidence_summary", "open_questions", "handoff_notes"],
                    ["Evidence package complete", "Supervisor package submitted"],
                    [],
                    "MEDIUM",
                    suppress_repair_guidance=True,
                ),
                "required_inputs": ["evidence_summary", "open_questions", "handoff_notes"],
                "pass_criteria": ["Evidence package complete", "Supervisor package submitted"],
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
    parts = "Suppressed pending supervisor decision." if suppress_repair_guidance else (
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
                "- Capture unresolved questions for supervisor review.",
                "- Repair guidance intentionally suppressed until approval.",
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
                "- Submit supervisor review package.",
                "- Wait for supervisor decision before attempting fixes.",
            ]
        )
    else:
        lines.extend(
            [
                "- Execute diagnostic checks and confirm root cause.",
                "- Update supervisor queue if approval is required.",
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
    return {
        "count": len(DEMO_SCENARIOS),
        "scenarios": DEMO_SCENARIOS,
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
        requires_approval = bool(escalation_reasons)
        status = "PENDING_APPROVAL" if requires_approval else "READY"
        workflow_mode = _derive_workflow_mode(status=status, requires_approval=requires_approval)
        workflow_meta = _workflow_mode_metadata(workflow_mode)
        approval_due_ts = _approval_due_ts(now) if requires_approval else None
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
            },
            confidence=combined_confidence,
            requires_human=int(requires_approval),
        )
        db.insert_decision_log(local_conn, escalation_entry)
        log_entries.append(escalation_entry)
        if requires_approval:
            metric_events.append(_metric_event(agent_id="approval_logic", counter="escalations"))

        if "manual_request" in escalation_reasons:
            manual_entry = _build_log_entry(
                ts=_utc_now(),
                job_id=job_id,
                agent_id="field_technician",
                action="MANUAL_ESCALATION_REQUESTED",
                input_json={"request_supervisor_review": True},
                output_json={"status": "PENDING_APPROVAL"},
                confidence=1.0,
                requires_human=1,
            )
            db.insert_decision_log(local_conn, manual_entry)
            log_entries.append(manual_entry)

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
        )
        db.replace_workflow_steps(
            local_conn,
            job_id=job_id,
            steps=actionable_workflow,
            ts=_utc_now(),
            agent_id="orchestrator",
        )
        workflow_created_event = {
            "ts": _utc_now(),
            "job_id": job_id,
            "step_id": None,
            "actor_id": "orchestrator",
            "event_type": "WORKFLOW_CREATED",
            "input_json": {"step_count": len(actionable_workflow)},
            "output_json": {"status": "created"},
        }
        db.insert_workflow_event(local_conn, workflow_created_event)
        workflow_events.append(workflow_created_event)
        workflow_log_entry = _build_log_entry(
            ts=_utc_now(),
            job_id=job_id,
            agent_id="orchestrator",
            action="WORKFLOW_GENERATED",
            input_json={"job_id": job_id, "workflow_mode": workflow_mode},
            output_json={
                "step_count": len(actionable_workflow),
                "suppressed_guidance": workflow_meta["suppressed_guidance"],
            },
            confidence=0.8,
        )
        db.insert_decision_log(local_conn, workflow_log_entry)
        log_entries.append(workflow_log_entry)

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
            "issue_text": payload.get("issue_text"),
            "normalization_meta": normalization_meta,
            "guided_question": guided_question,
            "guided_answer": guided_answer,
            "first_occurrence_fault": first_occurrence_fault,
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
        if requires_approval:
            job_row["approved_by"] = None
            job_row["approved_ts"] = None

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


@app.post("/api/job/{job_id}/attachments")
def upload_job_attachment(job_id: str, request: AttachmentUploadRequest) -> dict[str, Any]:
    now = _utc_now()
    mime_type = str(request.mime_type or "").strip().lower()
    extension = ALLOWED_IMAGE_MIME_TYPES.get(mime_type)
    if not extension:
        raise HTTPException(status_code=422, detail="Unsupported mime_type. Allowed: image/jpeg, image/png, image/webp")

    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")
        step = db.get_workflow_step(local_conn, job_id, request.step_id)
        if not step:
            raise HTTPException(status_code=404, detail="step_id not found")
        attachment_count = db.count_job_step_attachments(local_conn, job_id, request.step_id)
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
            "step_id": request.step_id,
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
                "step_id": request.step_id,
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
            "step_id": request.step_id,
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
        anchor = db.get_issue_record(local_conn, job_id)
        if not anchor:
            raise HTTPException(status_code=404, detail="job_id not found in issue history")
        candidates = db.search_issue_records(local_conn, limit=ISSUE_SEARCH_LIMIT_MAX, offset=0)
        anchor_tokens = set(str(token) for token in anchor.get("tags_json", []))
        scored: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate.get("job_id") == job_id:
                continue
            candidate_tokens = set(str(token) for token in candidate.get("tags_json", []))
            score = _issue_similarity_score(anchor_tokens, candidate_tokens)
            if score <= 0:
                continue
            scored.append(
                {
                    "score": round(score, 4),
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
        return {"job_id": job_id, "count": len(scored[:limit]), "similar_issues": scored[:limit]}


@app.get("/api/supervisor/queue")
def get_supervisor_queue() -> dict[str, Any]:
    with db.open_local_connection() as local_conn:
        queue = db.fetch_pending_approval_jobs(local_conn)
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

        new_status = "READY" if request.decision == "approve" else "DENIED"
        workflow_mode = (
            WORKFLOW_MODE_FIX_PLAN if request.decision == "approve" else WORKFLOW_MODE_INVESTIGATION_ONLY
        )
        workflow_meta = _workflow_mode_metadata(workflow_mode)

        final_response = job.get("final_response_json") or {}
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
        log_entries_for_sync: list[dict[str, Any]] = []
        workflow_steps_for_sync: list[dict[str, Any]] | None = None
        workflow_events_for_sync: list[dict[str, Any]] | None = None

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
            )
            db.replace_workflow_steps(
                local_conn,
                job_id=request.job_id,
                steps=workflow_steps,
                ts=now,
                agent_id="orchestrator",
            )
            workflow_event = {
                "ts": now,
                "job_id": request.job_id,
                "step_id": None,
                "actor_id": "orchestrator",
                "event_type": "WORKFLOW_APPROVAL_PROMOTION",
                "input_json": {"decision": request.decision},
                "output_json": {"step_count": len(workflow_steps), "workflow_mode": WORKFLOW_MODE_FIX_PLAN},
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
                agent_id="orchestrator",
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
                },
                confidence=0.8,
            )
            db.insert_decision_log(local_conn, promotion_entry)
            log_entries_for_sync.append(promotion_entry)
        else:
            workflow_steps_for_sync = db.get_workflow_steps(local_conn, request.job_id)

        job_row = {
            "job_id": job["job_id"],
            "created_ts": job["created_ts"],
            "updated_ts": now,
            "status": new_status,
            "field_payload_json": job.get("field_payload_json") or {},
            "final_response_json": final_response,
            "requires_approval": 0,
            "approved_by": request.approver_name,
            "approved_ts": now,
            "guided_question": job.get("guided_question"),
            "guided_answer": job.get("guided_answer"),
            "approval_due_ts": None,
            "timed_out": 0,
            "first_occurrence_fault": int(job.get("first_occurrence_fault", 0)),
            "assigned_tech_id": job.get("assigned_tech_id"),
            "workflow_mode": workflow_mode,
        }
        db.upsert_job(local_conn, job_row)

        approval_entry = _build_log_entry(
            ts=now,
            job_id=request.job_id,
            agent_id="human_supervisor",
            action="SUPERVISOR_DECISION",
            input_json=request.model_dump(),
            output_json={"status": new_status, "job_id": request.job_id},
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
    with db.open_local_connection() as local_conn:
        job = db.get_job(local_conn, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_id not found")

        step = db.get_workflow_step(local_conn, job_id, request.step_id)
        if not step:
            raise HTTPException(status_code=404, detail="step_id not found")

        db.update_workflow_step_status(local_conn, job_id, request.step_id, request.status)
        updated_step = db.get_workflow_step(local_conn, job_id, request.step_id) or step

        workflow_event = {
            "ts": now,
            "job_id": job_id,
            "step_id": request.step_id,
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
                "step_id": request.step_id,
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

        if bool(request.request_supervisor_review):
            manual_entry = _build_log_entry(
                ts=now,
                job_id=job_id,
                agent_id=request.actor_id,
                action="MANUAL_ESCALATION_REQUESTED",
                input_json={
                    "source": "workflow_step_update",
                    "step_id": request.step_id,
                    "request_supervisor_review": True,
                },
                output_json={"status": "PENDING_APPROVAL"},
                confidence=1.0,
                requires_human=1,
            )
            db.insert_decision_log(local_conn, manual_entry)
            log_entries_for_sync.append(manual_entry)

        requires_approval = bool(job["requires_approval"])
        new_status = job["status"]
        high_risk_step_failure = bool(
            request.status in {"failed", "blocked"} and updated_step.get("risk_level") in {"HIGH", "CRITICAL"}
        )
        new_reasons = _evaluate_escalation_reasons(
            manual_request=bool(request.request_supervisor_review),
            high_risk_step_failure=high_risk_step_failure,
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
        final_response["policy_config_hash"] = ESCALATION_POLICY_HASH
        final_response["last_workflow_update"] = {
            "ts": now,
            "step_id": request.step_id,
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
                )
                db.replace_workflow_steps(
                    local_conn,
                    job_id=job_id,
                    steps=regenerated_steps,
                    ts=now,
                    agent_id="orchestrator",
                )
                transition_event = {
                    "ts": now,
                    "job_id": job_id,
                    "step_id": None,
                    "actor_id": "orchestrator",
                    "event_type": "WORKFLOW_MODE_SWITCHED",
                    "input_json": {"previous_mode": previous_workflow_mode},
                    "output_json": {"workflow_mode": workflow_mode, "step_count": len(regenerated_steps)},
                }
                db.insert_workflow_event(local_conn, transition_event)
                workflow_events_for_sync.append(transition_event)
                mode_switch_entry = _build_log_entry(
                    ts=now,
                    job_id=job_id,
                    agent_id="orchestrator",
                    action="WORKFLOW_MODE_SWITCHED",
                    input_json={"previous_mode": previous_workflow_mode, "new_mode": workflow_mode},
                    output_json={"step_count": len(regenerated_steps)},
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
        requires_approval = bool(escalation_reasons)
        new_status = "PENDING_APPROVAL" if requires_approval else "READY"
        workflow_mode = _derive_workflow_mode(status=new_status, requires_approval=requires_approval)
        workflow_meta = _workflow_mode_metadata(workflow_mode)
        approval_due_ts = _approval_due_ts(now) if requires_approval else None
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
            },
            confidence=combined_confidence,
            requires_human=int(requires_approval),
        )
        db.insert_decision_log(local_conn, escalation_entry)
        log_entries.append(escalation_entry)
        if requires_approval:
            metric_events.append(_metric_event(agent_id="approval_logic", counter="escalations"))

        workflow_steps = _build_actionable_workflow(
            triage=triage_output,
            evidence=evidence_result,
            scheduler=schedule_hint,
            workflow_mode=workflow_mode,
        )
        db.replace_workflow_steps(local_conn, job_id=job_id, steps=workflow_steps, ts=_utc_now())
        workflow_event = {
            "ts": _utc_now(),
            "job_id": job_id,
            "step_id": None,
            "actor_id": "orchestrator",
            "event_type": "WORKFLOW_REPLANNED",
            "input_json": {"recent_events_considered": len(recent_events)},
            "output_json": {"step_count": len(workflow_steps)},
        }
        db.insert_workflow_event(local_conn, workflow_event)
        workflow_events.append(workflow_event)

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
            }
        )
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
