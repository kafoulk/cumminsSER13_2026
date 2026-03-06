from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from . import triage_agent


WORKFLOW_MODE_FIX_PLAN = "FIX_PLAN"
VALID_RISK_LEVELS = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}

REPAIR_PLAN_PROMPT_TEMPLATE = """You are the repair_agent for a diesel service workflow.
Generate a STEP-BY-STEP REPAIR checklist in simple English.

Rules:
- Return STRICT JSON only.
- Output keys:
  - steps: array of 5 to 10 objects.
- Each step object must include:
  - title (string)
  - instructions (string, one sentence, plain language)
  - required_inputs (array of short snake_case strings)
  - pass_criteria (array of short strings)
  - risk_level (LOW|MEDIUM|HIGH|CRITICAL)
  - recommended_parts (array of part names from the context list when relevant)
- Use the provided parts list across the checklist where appropriate.
- Include a final verification step.

Context JSON:
{context_json}
"""


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _to_text_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _normalize_step(raw: Any, index: int, fallback_parts: list[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    title = str(raw.get("title", "")).strip() or f"Repair step {index}"
    instructions = str(raw.get("instructions", "")).strip() or "Perform the repair action and record results."
    required_inputs = _to_text_list(raw.get("required_inputs")) or [
        "observation_notes",
        "measurement_value",
    ]
    pass_criteria = _to_text_list(raw.get("pass_criteria")) or [
        "Action completed",
        "Outcome recorded",
    ]
    risk_level = str(raw.get("risk_level", "MEDIUM")).upper()
    if risk_level not in VALID_RISK_LEVELS:
        risk_level = "MEDIUM"
    recommended_parts = _to_text_list(raw.get("recommended_parts")) or fallback_parts[:2]
    return {
        "step_id": f"repair-agent-{index}",
        "step_order": index,
        "title": title,
        "instructions": instructions,
        "required_inputs": required_inputs,
        "pass_criteria": pass_criteria,
        "risk_level": risk_level,
        "status": "pending",
        "step_kind": "repair",
        "recommended_parts": recommended_parts,
    }


def _fallback_steps(triage: dict[str, Any], parts_candidates: list[str]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {
            "step_id": "repair-fallback-1",
            "step_order": 1,
            "title": "Confirm Repair Baseline",
            "instructions": "Reconfirm the fault and baseline readings before replacing parts.",
            "required_inputs": ["fault_confirmation", "baseline_measurements"],
            "pass_criteria": ["Fault reconfirmed", "Baseline captured"],
            "risk_level": "MEDIUM",
            "status": "pending",
            "step_kind": "repair",
            "recommended_parts": [],
        }
    ]
    for index, part_name in enumerate(parts_candidates[:4], start=2):
        steps.append(
            {
                "step_id": f"repair-fallback-part-{index}",
                "step_order": index,
                "title": f"Repair {part_name}",
                "instructions": f"Inspect and service {part_name}; replace if out of spec.",
                "required_inputs": ["inspection_notes", "measurement_value", "part_action_taken"],
                "pass_criteria": [f"{part_name} action completed", "Outcome documented"],
                "risk_level": "MEDIUM",
                "status": "pending",
                "step_kind": "repair",
                "recommended_parts": [part_name],
            }
        )
    final_order = len(steps) + 1
    steps.append(
        {
            "step_id": f"repair-fallback-{final_order}",
            "step_order": final_order,
            "title": "Final Verification Road Test",
            "instructions": "Run verification checks to confirm the original issue is resolved.",
            "required_inputs": ["verification_notes", "post_repair_measurements"],
            "pass_criteria": ["No active fault", "Repair outcome confirmed"],
            "risk_level": "MEDIUM",
            "status": "pending",
            "step_kind": "verify_fix",
            "recommended_parts": parts_candidates[:3],
        }
    )
    if triage.get("safety_flag"):
        steps[0]["risk_level"] = "HIGH"
        steps[-1]["risk_level"] = "HIGH"
    return steps


def _generate_repair_steps(
    triage: dict[str, Any],
    evidence: dict[str, Any],
    scheduler: dict[str, Any],
    offline_mode: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    parts_candidates = _to_text_list(evidence.get("parts_candidates"))[:6]
    context = {
        "summary": triage.get("summary"),
        "likely_causes": _to_text_list(triage.get("likely_causes"))[:4],
        "diagnostic_next_steps": _to_text_list(triage.get("next_steps"))[:6],
        "safety_flag": bool(triage.get("safety_flag", False)),
        "parts_candidates": parts_candidates,
        "priority_hint": scheduler.get("priority_hint"),
        "eta_bucket": scheduler.get("eta_bucket"),
    }
    context_json = json.dumps(context, sort_keys=True)
    prompt = REPAIR_PLAN_PROMPT_TEMPLATE.format(context_json=context_json)
    parsed, llm_meta = triage_agent.run_ollama_prompt(
        prompt,
        expect_json=True,
        offline_mode=offline_mode,
        temperature=0.4,
    )

    steps_raw = parsed.get("steps") if isinstance(parsed, dict) else None
    normalized: list[dict[str, Any]] = []
    if isinstance(steps_raw, list):
        for idx, item in enumerate(steps_raw[:10], start=1):
            normalized.append(_normalize_step(item, idx, parts_candidates))

    meta = {
        "source": "ollama" if normalized else "fallback",
        "prompt_template_id": "repair_plan_v1",
        "prompt_hash": _hash_text(prompt),
        "context_hash": _hash_text(context_json),
        "context": context,
        **llm_meta,
    }
    return normalized, meta


def build_repair_plan(
    triage: dict[str, Any],
    evidence: dict[str, Any],
    scheduler: dict[str, Any],
    workflow_builder: Callable[..., list[dict[str, Any]]],
    offline_mode: bool = False,
) -> dict[str, Any]:
    """Build repair-oriented checklist after customer approval clears."""
    parts_candidates = _to_text_list(evidence.get("parts_candidates"))
    generated_steps, generation_meta = _generate_repair_steps(
        triage=triage,
        evidence=evidence,
        scheduler=scheduler,
        offline_mode=offline_mode,
    )
    if not generated_steps:
        generated_steps = _fallback_steps(triage, parts_candidates)

    triage_for_workflow = dict(triage)
    triage_for_workflow["workflow_steps"] = generated_steps
    triage_for_workflow["next_steps"] = [
        str(item.get("instructions", "")).strip()
        for item in generated_steps
        if str(item.get("instructions", "")).strip()
    ]

    step_to_parts = []
    for step in generated_steps:
        step_to_parts.append(
            {
                "step_id_hint": step.get("step_id"),
                "recommended_parts": step.get("recommended_parts", []),
            }
        )
    evidence_for_workflow = dict(evidence)
    evidence_for_workflow["parts_by_step"] = step_to_parts

    workflow_steps = workflow_builder(
        triage=triage_for_workflow,
        evidence=evidence_for_workflow,
        scheduler=scheduler,
        workflow_mode=WORKFLOW_MODE_FIX_PLAN,
    )
    for step in workflow_steps:
        step["agent_id"] = "repair_agent"
    return {
        "agent_id": "repair_agent",
        "workflow_mode": WORKFLOW_MODE_FIX_PLAN,
        "suppressed_guidance": False,
        "workflow_steps": workflow_steps,
        "generation_meta": generation_meta,
    }
