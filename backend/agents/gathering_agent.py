from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from . import triage_agent


WORKFLOW_MODE_INVESTIGATION_ONLY = "INVESTIGATION_ONLY"
VALID_RISK_LEVELS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}

DIAGNOSTIC_CHECKLIST_PROMPT_TEMPLATE = """You are the gathering_agent for a diesel service workflow.
Generate a concise STEP-BY-STEP DIAGNOSTIC checklist in simple English.

Rules:
- Return STRICT JSON only.
- Output keys:
  - steps: array of 4 to 8 objects.
- Each step object must include:
  - title (string)
  - instructions (string, one sentence, plain language)
  - required_inputs (array of short snake_case strings)
  - pass_criteria (array of short strings)
  - risk_level (LOW|MEDIUM|HIGH|CRITICAL)
- Do NOT include repair/fix actions. Focus on evidence gathering and validation.

Context JSON:
{context_json}
"""


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _to_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_step(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    title = str(raw.get("title", "")).strip() or f"Diagnostic check {index}"
    instructions = str(raw.get("instructions", "")).strip() or "Collect and record this diagnostic evidence."
    required_inputs = _to_text_list(raw.get("required_inputs")) or [
        "observation_notes",
        "measurement_value",
    ]
    pass_criteria = _to_text_list(raw.get("pass_criteria")) or [
        "Evidence captured",
        "Result recorded",
    ]
    risk_level = str(raw.get("risk_level", "MEDIUM")).upper()
    if risk_level not in VALID_RISK_LEVELS:
        risk_level = "MEDIUM"
    return {
        "step_id": f"diag-agent-{index}",
        "step_order": index,
        "title": title,
        "instructions": instructions,
        "required_inputs": required_inputs,
        "pass_criteria": pass_criteria,
        "risk_level": risk_level,
        "status": "pending",
        "step_kind": "investigate",
    }


def _fallback_steps(triage: dict[str, Any]) -> list[dict[str, Any]]:
    next_steps = _to_text_list(triage.get("next_steps"))
    if not next_steps:
        next_steps = [
            "Capture fault code snapshot and current operating condition.",
            "Inspect the affected area and record what you can see or smell.",
            "Run the basic system check and write down measured values.",
        ]
    steps: list[dict[str, Any]] = []
    for index, instruction in enumerate(next_steps[:6], start=1):
        steps.append(
            {
                "step_id": f"diag-fallback-{index}",
                "step_order": index,
                "title": f"Diagnostic check {index}",
                "instructions": instruction,
                "required_inputs": ["observation_notes", "measurement_value"],
                "pass_criteria": ["Evidence captured", "Result recorded"],
                "risk_level": "MEDIUM",
                "status": "pending",
                "step_kind": "investigate",
            }
        )
    return steps


def _generate_diagnostic_steps(
    triage: dict[str, Any],
    evidence: dict[str, Any],
    scheduler: dict[str, Any],
    offline_mode: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    manual_refs = []
    for item in evidence.get("manual_refs", []) or []:
        if isinstance(item, dict):
            title = str(item.get("title", "")).strip()
            if title:
                manual_refs.append(title)
    context = {
        "summary": triage.get("summary"),
        "likely_causes": _to_text_list(triage.get("likely_causes"))[:4],
        "next_steps": _to_text_list(triage.get("next_steps"))[:6],
        "safety_flag": bool(triage.get("safety_flag", False)),
        "manual_refs": manual_refs[:5],
        "parts_candidates": _to_text_list(evidence.get("parts_candidates"))[:5],
        "priority_hint": scheduler.get("priority_hint"),
        "eta_bucket": scheduler.get("eta_bucket"),
    }
    context_json = json.dumps(context, sort_keys=True)
    prompt = DIAGNOSTIC_CHECKLIST_PROMPT_TEMPLATE.format(context_json=context_json)
    parsed, llm_meta = triage_agent.run_ollama_prompt(
        prompt,
        expect_json=True,
        offline_mode=offline_mode,
        temperature=0.45,
    )

    steps_raw = parsed.get("steps") if isinstance(parsed, dict) else None
    normalized: list[dict[str, Any]] = []
    if isinstance(steps_raw, list):
        for idx, item in enumerate(steps_raw[:8], start=1):
            normalized.append(_normalize_step(item, idx))

    meta = {
        "source": "ollama" if normalized else "fallback",
        "prompt_template_id": "gathering_checklist_v1",
        "prompt_hash": _hash_text(prompt),
        "context_hash": _hash_text(context_json),
        "context": context,
        **llm_meta,
    }
    return normalized, meta


def build_checklist(
    triage: dict[str, Any],
    evidence: dict[str, Any],
    scheduler: dict[str, Any],
    workflow_builder: Callable[..., list[dict[str, Any]]],
    offline_mode: bool = False,
) -> dict[str, Any]:
    """Build investigation-only checklist steps using gathering_agent prompt generation."""
    generated_steps, generation_meta = _generate_diagnostic_steps(
        triage=triage,
        evidence=evidence,
        scheduler=scheduler,
        offline_mode=offline_mode,
    )
    if not generated_steps:
        generated_steps = _fallback_steps(triage)

    triage_for_workflow = dict(triage)
    triage_for_workflow["workflow_steps"] = generated_steps
    triage_for_workflow["next_steps"] = [
        str(item.get("instructions", "")).strip()
        for item in generated_steps
        if str(item.get("instructions", "")).strip()
    ]

    workflow_steps = workflow_builder(
        triage=triage_for_workflow,
        evidence=evidence,
        scheduler=scheduler,
        workflow_mode=WORKFLOW_MODE_INVESTIGATION_ONLY,
    )
    for step in workflow_steps:
        step["agent_id"] = "gathering_agent"
    return {
        "agent_id": "gathering_agent",
        "workflow_mode": WORKFLOW_MODE_INVESTIGATION_ONLY,
        "suppressed_guidance": True,
        "workflow_steps": workflow_steps,
        "generation_meta": generation_meta,
    }
