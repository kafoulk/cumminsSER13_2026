from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


MANUALS_DIR = Path(__file__).resolve().parents[1] / "knowledge_base" / "manuals"
INVENTORY_PATH = Path(__file__).resolve().parents[1] / "knowledge_base" / "synthetic" / "inventory.json"

PARTS_BY_KEYWORD = {
    "coolant": ["Water pump", "Thermostat", "Coolant hose set"],
    "overheat": ["Radiator", "Fan clutch", "Coolant temperature sensor"],
    "oil": ["Oil pressure sensor", "Oil filter", "Oil pump"],
    "fuel": ["Fuel filter", "High-pressure fuel pump", "Injector set"],
    "injector": ["Injector set", "Injector harness", "Fuel rail"],
    "brake": ["Brake line kit", "Brake pressure sensor", "ABS module"],
    "battery": ["Battery", "Starter relay", "Alternator belt"],
    "turbo": ["Turbocharger assembly", "Charge-air hose", "Boost pressure sensor"],
}


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-zA-Z0-9_]+", text.lower()) if len(token) >= 3}


def _load_chunks() -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    if not MANUALS_DIR.exists():
        return chunks

    for path in sorted(MANUALS_DIR.glob("*.txt")):
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            continue
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", raw) if part.strip()]
        if not paragraphs:
            paragraphs = [raw]
        for idx, paragraph in enumerate(paragraphs):
            chunks.append(
                {
                    "chunk_id": f"{path.name}:{idx}",
                    "title": path.stem.replace("_", " ").title(),
                    "path": str(path.relative_to(Path(__file__).resolve().parents[2])),
                    "text": paragraph,
                }
            )
    return chunks


def _load_inventory() -> dict[str, dict[str, int]]:
    if not INVENTORY_PATH.exists():
        return {}
    try:
        raw = INVENTORY_PATH.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
    inventory = parsed.get("inventory_by_location", {}) if isinstance(parsed, dict) else {}
    if not isinstance(inventory, dict):
        return {}
    normalized: dict[str, dict[str, int]] = {}
    for location, items in inventory.items():
        if not isinstance(items, dict):
            continue
        normalized[str(location)] = {
            str(part): int(qty) for part, qty in items.items() if isinstance(qty, int) or str(qty).isdigit()
        }
    return normalized


def _pick_inventory_location(payload_location: str, inventory: dict[str, dict[str, int]]) -> str:
    if not inventory:
        return payload_location or "Unknown"
    if payload_location and payload_location in inventory:
        return payload_location
    lowered_location = payload_location.lower()
    for known_location in inventory:
        if known_location.lower() in lowered_location or lowered_location in known_location.lower():
            return known_location
    return sorted(inventory.keys())[0]


def collect_evidence(payload: dict[str, Any], triage: dict[str, Any]) -> dict[str, Any]:
    query_text = " ".join(
        [
            str(payload.get("fault_code", "")),
            str(payload.get("symptoms", "")),
            str(payload.get("notes", "")),
            " ".join(str(item) for item in triage.get("likely_causes", [])),
        ]
    )
    query_terms = _tokenize(query_text)

    scored: list[tuple[int, dict[str, str]]] = []
    for chunk in _load_chunks():
        text_lower = chunk["text"].lower()
        score = sum(1 for term in query_terms if term in text_lower)
        scored.append((score, chunk))
    scored.sort(key=lambda item: item[0], reverse=True)

    top_hits = [item for item in scored if item[0] > 0][:3]
    if not top_hits and scored:
        top_hits = scored[:2]

    manual_refs: list[dict[str, Any]] = []
    source_chunks_used: list[str] = []
    for score, chunk in top_hits:
        manual_refs.append(
            {
                "title": chunk["title"],
                "path": chunk["path"],
                "snippet": chunk["text"][:220],
                "score": score,
            }
        )
        source_chunks_used.append(chunk["chunk_id"])

    lower_query = query_text.lower()
    parts_candidates: list[str] = []
    for keyword, parts in PARTS_BY_KEYWORD.items():
        if keyword in lower_query:
            parts_candidates.extend(parts)

    deduped_parts: list[str] = []
    seen: set[str] = set()
    for part in parts_candidates:
        if part not in seen:
            seen.add(part)
            deduped_parts.append(part)
    if not deduped_parts:
        deduped_parts = ["Diagnostic harness kit", "General sensor service kit"]

    inventory = _load_inventory()
    payload_location = str(payload.get("location", "")).strip()
    selected_location = _pick_inventory_location(payload_location, inventory)
    location_inventory = inventory.get(selected_location, {})
    parts_availability: list[dict[str, Any]] = []
    missing_critical_parts: list[str] = []
    for idx, part in enumerate(deduped_parts[:6]):
        qty = int(location_inventory.get(part, 0))
        if qty <= 0:
            status = "OUT_OF_STOCK"
        elif qty == 1:
            status = "LOW_STOCK"
        else:
            status = "IN_STOCK"
        parts_availability.append(
            {
                "part_name": part,
                "quantity": qty,
                "status": status,
            }
        )
        if idx < 3 and status == "OUT_OF_STOCK":
            missing_critical_parts.append(part)

    confidence = 0.45 + (0.1 * min(len(manual_refs), 3))
    if deduped_parts:
        confidence += 0.1
    confidence = _clamp(confidence)

    evidence_notes = (
        "Evidence selected via local keyword matching from knowledge_base/manuals and "
        "heuristic parts mapping from symptom/fault indicators."
    )

    triage_steps = triage.get("next_steps", [])
    parts_by_step = []
    for index, step in enumerate(triage_steps[:3], start=1):
        parts_by_step.append(
            {
                "step_id_hint": f"step-{index}",
                "step_instruction": step,
                "recommended_parts": deduped_parts[:3],
            }
        )

    return {
        "manual_refs": manual_refs,
        "parts_candidates": deduped_parts[:6],
        "parts_by_step": parts_by_step,
        "inventory_location": selected_location,
        "parts_availability": parts_availability,
        "missing_critical_parts": missing_critical_parts,
        "evidence_notes": evidence_notes,
        "source_chunks_used": source_chunks_used,
        "confidence": confidence,
    }
