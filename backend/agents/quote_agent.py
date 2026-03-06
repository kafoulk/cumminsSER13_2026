from __future__ import annotations

from typing import Any


PART_UNIT_PRICE_USD = {
    "Water pump": 325.0,
    "Thermostat": 85.0,
    "Coolant hose set": 140.0,
    "Radiator": 620.0,
    "Fan clutch": 410.0,
    "Coolant temperature sensor": 72.0,
    "Brake line kit": 380.0,
    "Brake pressure sensor": 145.0,
    "ABS module": 950.0,
    "Fuel filter": 68.0,
    "High-pressure fuel pump": 1200.0,
    "Injector set": 980.0,
    "Injector harness": 220.0,
    "Fuel rail": 540.0,
    "Diagnostic harness kit": 190.0,
    "General sensor service kit": 130.0,
}
DEFAULT_PART_PRICE_USD = 150.0
TAX_RATE = 0.07


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _labor_rate_for_location(location: str) -> float:
    lowered = str(location or "").lower()
    if any(token in lowered for token in {"remote", "quarry", "tunnel"}):
        return 175.0
    if "columbus" in lowered:
        return 160.0
    return 155.0


def _labor_hours(triage: dict[str, Any], schedule: dict[str, Any]) -> float:
    steps = triage.get("next_steps", [])
    step_count = len(steps) if isinstance(steps, list) else 0
    base = 1.5 + (0.6 * min(step_count, 4))
    priority = str(schedule.get("priority_hint", "NORMAL")).upper()
    if priority == "HIGH":
        base += 0.8
    elif priority == "MEDIUM":
        base += 0.3
    if bool(triage.get("safety_flag")):
        base += 0.7
    return round(_clamp(base, 1.0, 10.0), 1)


def build_quote(
    *,
    job_id: str,
    payload: dict[str, Any],
    triage: dict[str, Any],
    evidence: dict[str, Any],
    schedule: dict[str, Any],
) -> dict[str, Any]:
    parts = evidence.get("parts_candidates", [])
    if not isinstance(parts, list):
        parts = []
    selected_parts = [str(part).strip() for part in parts if str(part).strip()][:4]
    if not selected_parts:
        selected_parts = ["Diagnostic harness kit"]

    part_lines: list[dict[str, Any]] = []
    part_subtotal = 0.0
    for part_name in selected_parts:
        unit_price = float(PART_UNIT_PRICE_USD.get(part_name, DEFAULT_PART_PRICE_USD))
        quantity = 1
        line_total = round(unit_price * quantity, 2)
        part_subtotal += line_total
        part_lines.append(
            {
                "type": "part",
                "description": part_name,
                "quantity": quantity,
                "unit_price_usd": round(unit_price, 2),
                "line_total_usd": line_total,
            }
        )

    labor_hours = _labor_hours(triage, schedule)
    labor_rate = _labor_rate_for_location(str(payload.get("location", "")))
    labor_total = round(labor_hours * labor_rate, 2)
    labor_line = {
        "type": "labor",
        "description": "Diagnostic + repair labor estimate",
        "hours": labor_hours,
        "rate_per_hour_usd": round(labor_rate, 2),
        "line_total_usd": labor_total,
    }

    subtotal = round(part_subtotal + labor_total, 2)
    tax = round(subtotal * TAX_RATE, 2)
    total = round(subtotal + tax, 2)

    confidence = 0.7
    confidence = (confidence + float(triage.get("confidence", 0.0)) + float(evidence.get("confidence", 0.0))) / 3

    return {
        "quote_id": f"Q-{job_id[:8]}",
        "currency": "USD",
        "line_items": [labor_line, *part_lines],
        "labor": {
            "hours": labor_hours,
            "rate_per_hour_usd": round(labor_rate, 2),
            "line_total_usd": labor_total,
        },
        "parts_subtotal_usd": round(part_subtotal, 2),
        "subtotal_usd": subtotal,
        "tax_rate": TAX_RATE,
        "tax_usd": tax,
        "total_usd": total,
        "assumptions": [
            "Synthetic estimate generated from triage, parts evidence, and dispatch hints.",
            "Final pricing may change after teardown or additional diagnostics.",
        ],
        "confidence": _clamp(confidence, 0.0, 1.0),
    }
