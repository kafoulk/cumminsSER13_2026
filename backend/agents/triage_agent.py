from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import requests
import yaml


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "ollama_config.yaml"

TRIAGE_PROMPT_TEMPLATE = """You are a diesel service triage assistant.
Given the context JSON, return STRICT JSON only with keys:
- summary (string)
- likely_causes (array of strings)
- next_steps (array of strings)
- safety_flag (boolean)
- confidence (number from 0 to 1)

Context JSON:
{context_json}
"""

GUIDED_QUESTION_PROMPT_TEMPLATE = """You are a field diagnostics coach.
Given the context JSON, return STRICT JSON only with keys:
- question (string)
- rationale (string)
- confidence (number from 0 to 1)

Generate exactly one concrete question a junior technician can answer on site before final diagnosis.

Context JSON:
{context_json}
"""

UNSAFE_KEYWORDS = {
    "unsafe",
    "fire",
    "smoke",
    "brake",
    "injury",
    "critical",
    "hazard",
}


def load_ollama_config() -> dict[str, Any]:
    default = {
        "enabled": True,
        "base_url": "http://localhost:11434",
        "model": "llama3.1:8b",
        "online_model": "llama3.1:8b",
        "offline_model": "llama3.2:3b",
        "timeout_sec": 20,
    }
    if not CONFIG_PATH.exists():
        return default
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    loaded = raw.get("ollama", {})
    return {
        "enabled": bool(loaded.get("enabled", default["enabled"])),
        "base_url": str(loaded.get("base_url", default["base_url"])).rstrip("/"),
        "model": str(loaded.get("model", default["model"])),
        "online_model": str(loaded.get("online_model", loaded.get("model", default["online_model"]))),
        "offline_model": str(loaded.get("offline_model", default["offline_model"])),
        "timeout_sec": int(loaded.get("timeout_sec", default["timeout_sec"])),
    }


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _extract_json_object(raw_text: str) -> dict[str, Any] | None:
    if not raw_text:
        return None
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", raw_text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _to_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        parts = [line.strip("- ").strip() for line in value.splitlines() if line.strip()]
        if not parts:
            return [value.strip()]
        return parts
    return []


def run_ollama_prompt(
    prompt: str,
    expect_json: bool = False,
    offline_mode: bool = False,
    temperature: float = 0.2,
) -> tuple[Any | None, dict[str, Any]]:
    config = load_ollama_config()
    mode_effective = "offline" if offline_mode else "online"
    model_selected = config["offline_model"] if offline_mode else config["online_model"]
    metadata = {
        "provider": "ollama",
        "enabled": config["enabled"],
        "model": model_selected,
        "model_online": config["online_model"],
        "model_offline": config["offline_model"],
        "mode_effective": mode_effective,
        "base_url": config["base_url"],
    }
    if not config["enabled"]:
        metadata["used_fallback"] = True
        metadata["fallback_reason"] = "ollama_disabled_in_config"
        return None, metadata

    try:
        response = requests.post(
            f"{config['base_url']}/api/generate",
            json={
                "model": model_selected,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": max(0.0, min(1.0, float(temperature)))},
            },
            timeout=config["timeout_sec"],
        )
        response.raise_for_status()
        body = response.json()
        text = str(body.get("response", "")).strip()
        if expect_json:
            parsed = _extract_json_object(text)
            if parsed is None:
                raise ValueError("Model response was not valid JSON")
            metadata["used_fallback"] = False
            return parsed, metadata
        metadata["used_fallback"] = False
        return text, metadata
    except Exception as exc:  # noqa: BLE001
        metadata["used_fallback"] = True
        metadata["fallback_reason"] = str(exc)
        return None, metadata


def _fallback_triage(payload: dict[str, Any]) -> dict[str, Any]:
    fault_code = str(payload.get("fault_code", "")).lower()
    symptoms = str(payload.get("symptoms", "")).lower()
    notes = str(payload.get("notes", "")).lower()
    text = f"{fault_code} {symptoms} {notes}"

    likely_causes: list[str]
    if "overheat" in text or "temp" in text:
        likely_causes = ["Coolant flow restriction", "Thermostat or fan fault"]
        next_steps = [
            "Check coolant level and pressure test cooling system",
            "Inspect fan drive and thermostat operation",
            "Scan ECU for thermal derate events",
        ]
    elif "oil" in text and "pressure" in text:
        likely_causes = ["Low oil level or viscosity issue", "Pressure sensor or pump fault"]
        next_steps = [
            "Verify oil level and specification",
            "Inspect for leaks and check oil filter condition",
            "Validate oil pressure with a mechanical gauge",
        ]
    elif "start" in text or "crank" in text:
        likely_causes = ["Battery or starter circuit issue", "Fuel delivery interruption"]
        next_steps = [
            "Run battery and starter voltage-drop checks",
            "Confirm fuel pressure and injector command",
            "Inspect for fault codes related to crank/no-start",
        ]
    else:
        likely_causes = ["Sensor fault", "Intermittent electrical issue"]
        next_steps = [
            "Capture fault freeze-frame and active DTC data",
            "Inspect connectors and harness at affected subsystem",
            "Perform targeted functional tests per manual",
        ]

    safety_flag = any(keyword in text for keyword in UNSAFE_KEYWORDS)
    confidence = 0.55
    if fault_code and fault_code not in {"unknown", "na", "none"}:
        confidence += 0.1
    if len(symptoms) < 20:
        confidence -= 0.1
    if safety_flag:
        confidence -= 0.05

    summary = (
        f"Initial triage for fault '{payload.get('fault_code', 'N/A')}' suggests "
        f"{likely_causes[0].lower()} as the most probable issue."
    )

    return {
        "summary": summary,
        "likely_causes": likely_causes,
        "next_steps": next_steps,
        "safety_flag": safety_flag,
        "confidence": _clamp(confidence),
    }


def _fallback_guided_question(payload: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(
        [
            str(payload.get("fault_code", "")),
            str(payload.get("symptoms", "")),
            str(payload.get("notes", "")),
        ]
    ).lower()
    if any(token in text for token in {"coolant", "overheat", "temp"}):
        question = "Have you checked coolant level and confirmed active radiator fan engagement under load?"
    elif any(token in text for token in {"brake", "smoke", "unsafe"}):
        question = "Before proceeding, can you confirm hazard isolation and whether braking pressure is stable?"
    elif any(token in text for token in {"fuel", "injector", "no-start", "crank"}):
        question = "Can you verify fuel pressure at spec and check injector harness continuity at idle?"
    else:
        question = "Can you confirm the key measurement that changed when the fault first appeared?"
    return {
        "question": question,
        "rationale": "Deterministic guided-learning fallback based on fault and symptom indicators.",
        "confidence": 0.72,
    }


def generate_guided_question(payload: dict[str, Any], offline_mode: bool = False) -> dict[str, Any]:
    context = {
        "equipment_id": payload.get("equipment_id"),
        "fault_code": payload.get("fault_code"),
        "symptoms": payload.get("symptoms"),
        "notes": payload.get("notes"),
        "location": payload.get("location"),
    }
    context_json = json.dumps(context, sort_keys=True)
    prompt = GUIDED_QUESTION_PROMPT_TEMPLATE.format(context_json=context_json)
    parsed, meta = run_ollama_prompt(prompt, expect_json=True, offline_mode=offline_mode)

    fallback = _fallback_guided_question(payload)
    if not isinstance(parsed, dict):
        fallback["metadata"] = {"source": "fallback", **meta}
        return fallback

    question = str(parsed.get("question", "")).strip() or fallback["question"]
    rationale = str(parsed.get("rationale", "")).strip() or fallback["rationale"]
    confidence = _clamp(float(parsed.get("confidence", fallback["confidence"])))
    return {
        "question": question,
        "rationale": rationale,
        "confidence": confidence,
        "metadata": {"source": "ollama", **meta},
    }


def _step_risk_level(step_text: str, safety_flag: bool) -> str:
    lowered = step_text.lower()
    if safety_flag and any(token in lowered for token in {"isolate", "safety", "brake", "smoke", "fire"}):
        return "CRITICAL"
    if any(token in lowered for token in {"pressure", "high-voltage", "fuel rail", "brake"}):
        return "HIGH"
    if any(token in lowered for token in {"inspect", "scan", "verify"}):
        return "MEDIUM"
    return "LOW"


def _build_workflow_steps(
    payload: dict[str, Any],
    next_steps: list[str],
    safety_flag: bool,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []

    if safety_flag:
        steps.append(
            {
                "step_id": "step-0-safety",
                "step_order": 1,
                "title": "Immediate safety containment",
                "instructions": "Secure equipment, isolate hazards, and confirm supervisor awareness before further diagnostics.",
                "required_inputs": ["hazard_assessment", "site_isolation_status"],
                "pass_criteria": ["Hazard containment confirmed", "Supervisor notified"],
                "risk_level": "CRITICAL",
                "status": "pending",
            }
        )

    for idx, item in enumerate(next_steps, start=1 if not safety_flag else 2):
        step_id = f"step-{idx}"
        steps.append(
            {
                "step_id": step_id,
                "step_order": idx,
                "title": f"Diagnostic step {idx}",
                "instructions": item,
                "required_inputs": ["observation_notes", "measurement_value"],
                "pass_criteria": ["Evidence captured", "Result recorded"],
                "risk_level": _step_risk_level(item, safety_flag),
                "status": "pending",
            }
        )

    if not steps:
        steps.append(
            {
                "step_id": "step-1",
                "step_order": 1,
                "title": "Collect baseline diagnostics",
                "instructions": "Capture fault and freeze-frame data for initial assessment.",
                "required_inputs": ["fault_snapshot"],
                "pass_criteria": ["Snapshot collected"],
                "risk_level": "MEDIUM",
                "status": "pending",
            }
        )
    return steps


def analyze(payload: dict[str, Any], offline_mode: bool = False) -> dict[str, Any]:
    guided_answer = str(payload.get("guided_answer", "")).strip()
    guided_question = str(payload.get("guided_question", "")).strip()
    if not guided_question:
        guided_question = generate_guided_question(payload, offline_mode=offline_mode).get("question", "")

    context = {
        "equipment_id": payload.get("equipment_id"),
        "fault_code": payload.get("fault_code"),
        "symptoms": payload.get("symptoms"),
        "notes": payload.get("notes"),
        "location": payload.get("location"),
        "guided_question": guided_question,
        "guided_answer": guided_answer,
    }
    context_json = json.dumps(context, sort_keys=True)
    prompt = TRIAGE_PROMPT_TEMPLATE.format(context_json=context_json)

    llm_result, llm_meta = run_ollama_prompt(prompt, expect_json=True, offline_mode=offline_mode)
    prompt_hash = _hash_text(prompt)
    context_hash = _hash_text(context_json)

    if llm_result is None:
        fallback = _fallback_triage(payload)
        if guided_answer:
            fallback["summary"] = f"{fallback['summary']} Guided observation: {guided_answer}."
        fallback["workflow_steps"] = _build_workflow_steps(
            payload=payload,
            next_steps=fallback["next_steps"],
            safety_flag=bool(fallback["safety_flag"]),
        )
        fallback["guided_question"] = guided_question
        fallback["guided_answer"] = guided_answer
        fallback["llm_metadata"] = {
            "prompt_template_id": "triage_v1",
            "prompt_hash": prompt_hash,
            "context_hash": context_hash,
            "context": context,
            **llm_meta,
        }
        return fallback

    result = {
        "summary": str(llm_result.get("summary", "")).strip(),
        "likely_causes": _to_list(llm_result.get("likely_causes")),
        "next_steps": _to_list(llm_result.get("next_steps")),
        "safety_flag": bool(llm_result.get("safety_flag", False)),
        "confidence": _clamp(float(llm_result.get("confidence", 0.6))),
        "guided_question": guided_question,
        "guided_answer": guided_answer,
        "llm_metadata": {
            "prompt_template_id": "triage_v1",
            "prompt_hash": prompt_hash,
            "context_hash": context_hash,
            "context": context,
            **llm_meta,
        },
    }
    if not result["summary"]:
        result["summary"] = _fallback_triage(payload)["summary"]
    if not result["likely_causes"]:
        result["likely_causes"] = _fallback_triage(payload)["likely_causes"]
    if not result["next_steps"]:
        result["next_steps"] = _fallback_triage(payload)["next_steps"]
    if not guided_answer:
        result["confidence"] = _clamp(result["confidence"] - 0.12)
    if not result["safety_flag"]:
        text = " ".join(
            [
                str(payload.get("symptoms", "")),
                str(payload.get("notes", "")),
                result["summary"],
            ]
        ).lower()
        result["safety_flag"] = any(keyword in text for keyword in UNSAFE_KEYWORDS)
    result["workflow_steps"] = _build_workflow_steps(
        payload=payload,
        next_steps=result["next_steps"],
        safety_flag=bool(result["safety_flag"]),
    )

    return result
