from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROSTER_PATH = Path(__file__).resolve().parents[1] / "knowledge_base" / "synthetic" / "technicians.json"


def _load_roster() -> list[dict[str, Any]]:
    if not ROSTER_PATH.exists():
        return []
    try:
        parsed = json.loads(ROSTER_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []
    roster = parsed.get("technicians", []) if isinstance(parsed, dict) else []
    return roster if isinstance(roster, list) else []


def _required_certifications(text: str) -> list[str]:
    required: list[str] = []
    if any(token in text for token in {"fuel", "injector", "rail"}):
        required.append("FuelSystem")
    if any(token in text for token in {"brake", "abs"}):
        required.append("Brake")
    if any(token in text for token in {"high-voltage", "battery", "starter"}):
        required.append("Electrical")
    if any(token in text for token in {"overheat", "coolant", "radiator"}):
        required.append("Cooling")
    return required


def _infer_region(location: str) -> str:
    lowered = location.lower()
    if any(token in lowered for token in {"indy", "indianapolis"}):
        return "IN-CENTRAL"
    if any(token in lowered for token in {"columbus"}):
        return "OH-CENTRAL"
    if any(token in lowered for token in {"quarry", "remote", "tunnel"}):
        return "REMOTE"
    return "IN-CENTRAL"


def _score_technician(
    tech: dict[str, Any],
    *,
    required_certs: list[str],
    region: str,
    priority: str,
) -> tuple[float, str]:
    if str(tech.get("availability_status", "")).lower() != "available":
        return -1.0, "Technician not currently available"

    score = 0.0
    rationale_parts: list[str] = []

    if str(tech.get("region", "")) == region:
        score += 1.0
        rationale_parts.append("region match")
    elif region == "REMOTE":
        score += 0.3
        rationale_parts.append("remote-capable fallback")

    skill_level = str(tech.get("skill_level", "junior")).lower()
    if skill_level == "senior":
        score += 1.2
        rationale_parts.append("senior skill")
    elif skill_level == "mid":
        score += 0.8
        rationale_parts.append("mid skill")
    else:
        score += 0.4
        rationale_parts.append("junior skill")

    current_load = int(tech.get("current_load", 0))
    score += max(0.0, 1.0 - (0.2 * current_load))
    rationale_parts.append(f"load={current_load}")

    certs = {str(item) for item in tech.get("certifications", [])}
    matched_certs = [cert for cert in required_certs if cert in certs]
    score += 0.9 * len(matched_certs)
    if matched_certs:
        rationale_parts.append(f"certs={','.join(matched_certs)}")
    elif required_certs:
        score -= 0.6
        rationale_parts.append("missing preferred cert")

    if priority == "HIGH" and skill_level == "junior":
        score -= 0.4
        rationale_parts.append("high-priority penalty for junior")

    return score, "; ".join(rationale_parts)


def forecast(job_payload: dict[str, Any]) -> dict[str, Any]:
    text = " ".join(
        [
            str(job_payload.get("fault_code", "")),
            str(job_payload.get("symptoms", "")),
            str(job_payload.get("notes", "")),
        ]
    ).lower()

    if any(token in text for token in {"fire", "unsafe", "brake", "injury", "smoke"}):
        priority_hint = "HIGH"
        eta_bucket = "0-4h"
        checkpoints = ["Immediate safety review", "Supervisor confirmation", "Rapid diagnostics"]
    elif any(token in text for token in {"no-start", "derate", "shutdown", "critical"}):
        priority_hint = "MEDIUM"
        eta_bucket = "4-12h"
        checkpoints = ["Electrical baseline", "Fuel/air verification", "Targeted fault isolation"]
    else:
        priority_hint = "NORMAL"
        eta_bucket = "12-24h"
        checkpoints = ["Routine diagnostics", "Parts verification", "Repair scheduling"]

    required_certs = _required_certifications(text)
    region = _infer_region(str(job_payload.get("location", "")))
    roster = _load_roster()
    scored: list[tuple[float, dict[str, Any], str]] = []
    for tech in roster:
        score, rationale = _score_technician(
            tech,
            required_certs=required_certs,
            region=region,
            priority=priority_hint,
        )
        if score >= 0:
            scored.append((score, tech, rationale))
    scored.sort(key=lambda item: item[0], reverse=True)

    assignment_recommendation: dict[str, Any] | None = None
    alternates: list[dict[str, Any]] = []
    if scored:
        best_score, best_tech, best_rationale = scored[0]
        assignment_recommendation = {
            "tech_id": best_tech.get("tech_id"),
            "tech_name": best_tech.get("name"),
            "region": best_tech.get("region"),
            "required_certifications": required_certs,
            "confidence": max(0.3, min(0.95, 0.45 + (0.1 * best_score))),
            "rationale": best_rationale,
        }
        for score, tech, rationale in scored[1:3]:
            alternates.append(
                {
                    "tech_id": tech.get("tech_id"),
                    "tech_name": tech.get("name"),
                    "confidence": max(0.25, min(0.9, 0.4 + (0.08 * score))),
                    "rationale": rationale,
                }
            )

    if any(token in text for token in {"fire", "unsafe", "brake", "injury", "smoke"}):
        escalation_suggestion = "Use supervisor-assisted dispatch for safety-sensitive work."
    elif priority_hint == "MEDIUM":
        escalation_suggestion = "Prefer certified tech if available within ETA window."
    else:
        escalation_suggestion = "Standard dispatch path is acceptable."

    return {
        "priority_hint": priority_hint,
        "eta_bucket": eta_bucket,
        "checkpoints": checkpoints,
        "required_certifications": required_certs,
        "dispatch_region": region,
        "assignment_recommendation": assignment_recommendation,
        "alternates": alternates,
        "dispatch_notes": escalation_suggestion,
    }
