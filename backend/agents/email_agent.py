from __future__ import annotations

from typing import Any


def draft_quote_email(
    *,
    payload: dict[str, Any],
    triage: dict[str, Any],
    schedule: dict[str, Any],
    quote: dict[str, Any],
) -> dict[str, Any]:
    equipment_id = str(payload.get("equipment_id") or "Unknown equipment")
    fault_code = str(payload.get("fault_code") or "N/A")
    customer_name = str(payload.get("customer_name") or "").strip()
    symptoms = str(payload.get("symptoms") or payload.get("issue_text") or "Not provided")
    eta_bucket = str(schedule.get("eta_bucket") or "TBD")
    priority = str(schedule.get("priority_hint") or "NORMAL")
    likely_causes = triage.get("likely_causes", [])
    if not isinstance(likely_causes, list):
        likely_causes = []
    likely_cause_line = ", ".join(str(item) for item in likely_causes[:2]) or "Pending confirmation"

    total = float(quote.get("total_usd", 0.0))
    subtotal = float(quote.get("subtotal_usd", 0.0))
    tax = float(quote.get("tax_usd", 0.0))
    quote_id = str(quote.get("quote_id") or "Q-PENDING")
    line_items = quote.get("line_items", [])
    if not isinstance(line_items, list):
        line_items = []

    lines = []
    for item in line_items[:8]:
        item_type = str(item.get("type", "item")).title()
        description = str(item.get("description", "Service item"))
        line_total = float(item.get("line_total_usd", 0.0))
        lines.append(f"- {item_type}: {description} (${line_total:,.2f})")
    line_items_text = "\n".join(lines) if lines else "- Estimate details pending."

    subject = f"Service Quote {quote_id} | {equipment_id} {fault_code}".strip()
    body = "\n".join(
        [
            f"Hello {customer_name}," if customer_name else "Hello,",
            "",
            f"We completed initial diagnostics for equipment {equipment_id} (fault {fault_code}).",
            f"Reported issue: {symptoms}",
            "",
            "Preliminary findings:",
            f"- Likely cause(s): {likely_cause_line}",
            f"- Priority: {priority}",
            f"- Estimated schedule window: {eta_bucket}",
            "",
            "Proposed quote:",
            line_items_text,
            f"- Subtotal: ${subtotal:,.2f}",
            f"- Tax: ${tax:,.2f}",
            f"- Total estimate: ${total:,.2f}",
            "",
            "Please confirm approval to proceed. We will not start repair work until customer approval is recorded.",
            "",
            "Thanks,",
            "Service Team",
        ]
    )

    call_script = (
        f"Hi, this is the service team calling about {equipment_id}. "
        f"We found likely cause '{likely_cause_line}'. The current estimate is ${total:,.2f} with ETA {eta_bucket}. "
        "Do you approve us to proceed with the repair?"
    )

    return {
        "subject": subject,
        "body_text": body,
        "call_script": call_script,
        "confidence": 0.8,
    }
