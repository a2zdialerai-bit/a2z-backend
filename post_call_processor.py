"""Post-call scoring and auto-listing processor."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional, Any

from sqlmodel import Session, select

from models import CallLog, Lead, MarketplaceListing

logger = logging.getLogger("a2z.post_call")


def calculate_readiness_score(extracted_fields: dict, disposition: str) -> int:
    """Calculate 0-100 readiness score from extracted fields and disposition."""
    if disposition in ("opted_out", "wrong_number"):
        return 0

    score = 0

    # Seller motivation present
    motivation = extracted_fields.get("seller_motivation") or extracted_fields.get("why_not_sold")
    if motivation and str(motivation).strip():
        score += 20

    # Timeline present and near
    timeline = extracted_fields.get("planned_move") or extracted_fields.get("seller_timeline")
    if timeline and str(timeline).strip():
        score += 20

    # Openness to agent
    openness = extracted_fields.get("stay_put_option") or extracted_fields.get("seller_openness")
    if openness is not None:
        score += 15

    # Price realism
    price = extracted_fields.get("price_realism") or extracted_fields.get("listing_status")
    if price and str(price).strip():
        score += 15

    # Appointment booked
    if disposition == "booked_appointment":
        score += 30

    return min(100, score)


def get_disposition_from_transcript(transcript: dict) -> str:
    """Infer disposition from transcript data."""
    turns = transcript.get("turns", [])
    nodes_visited = transcript.get("nodes_visited", [])

    # Check nodes visited for exit types
    if any("opt_out" in n for n in nodes_visited):
        return "opted_out"
    if any("wrong_number" in n for n in nodes_visited):
        return "wrong_number"
    if any("callback" in n for n in nodes_visited):
        return "callback_scheduled"
    if any("booked" in n or "close" in n for n in nodes_visited):
        return "booked_appointment"
    if any("polite_no" in n or "not_interested" in n for n in nodes_visited):
        return "not_interested"
    if any("sold" in n for n in nodes_visited):
        return "not_interested"

    return "not_interested"


def process_completed_call(
    call_log_id: int,
    session: Session,
) -> None:
    """Process a completed call: score it and potentially create marketplace listing."""
    call_log = session.get(CallLog, call_log_id)
    if not call_log:
        logger.warning(f"CallLog {call_log_id} not found for post-call processing")
        return

    lead = session.get(Lead, call_log.lead_id) if call_log.lead_id else None

    # Parse transcript — stored in the `transcript` text field or `provider_json`
    transcript_data: dict = {}
    transcript_raw = call_log.transcript or call_log.provider_json or ""
    if transcript_raw:
        try:
            parsed = json.loads(transcript_raw)
            if isinstance(parsed, dict) and "turns" in parsed:
                transcript_data = parsed
        except (json.JSONDecodeError, AttributeError):
            pass

    # Parse extracted fields
    extracted_fields: dict = {}
    if call_log.extracted_json:
        try:
            extracted_fields = json.loads(call_log.extracted_json)
        except (json.JSONDecodeError, AttributeError):
            pass

    # Determine disposition
    disposition = call_log.disposition or get_disposition_from_transcript(transcript_data)

    # Calculate readiness score
    readiness_score = calculate_readiness_score(extracted_fields, disposition)

    # Update lead
    if lead:
        lead.readiness_score = readiness_score
        lead.marketplace_eligible = (
            readiness_score >= 60
            and disposition not in ("opted_out", "wrong_number")
            and bool(extracted_fields.get("seller_motivation") or extracted_fields.get("why_not_sold"))
        )
        session.add(lead)

    # Update call log with final disposition
    call_log.disposition = disposition
    session.add(call_log)

    # Auto-create marketplace listing if eligible
    if lead and lead.marketplace_eligible:
        listing_type = "booked_appointment" if disposition == "booked_appointment" else "qualified_lead"
        base_price = 9500 if listing_type == "booked_appointment" else 4500  # cents

        # Derive territory from zip/city (Lead uses city/state/postal_code)
        territory_key = "unknown"
        if lead.postal_code:
            territory_key = f"zip_{lead.postal_code}"
        elif lead.city and lead.state:
            city_slug = lead.city.lower().replace(" ", "_")
            state_slug = lead.state.lower()
            territory_key = f"{city_slug}_{state_slug}"

        seller_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Seller"
        motivation_text = str(extracted_fields.get("why_not_sold", ""))
        appt_time_raw = str(extracted_fields.get("appointment_time", "")) if listing_type == "booked_appointment" else None

        listing = MarketplaceListing(
            workspace_id=call_log.workspace_id,
            lead_id=lead.id,
            listing_type=listing_type,
            status="pending_review",
            readiness_score=readiness_score,
            territory_key=territory_key,
            base_price_cents=base_price,
            final_price_cents=base_price,
            is_featured=False,
            homeowner_name=seller_name,
            property_address=lead.property_address or "",
            city=lead.city or "",
            state=lead.state or "",
            postal_code=lead.postal_code or "",
            summary=motivation_text,
            appointment_time_iso=appt_time_raw,
        )
        session.add(listing)
        logger.info(f"Auto-created MarketplaceListing for lead {lead.id} with readiness {readiness_score}")

    session.commit()
    logger.info(
        f"Post-call processing complete for CallLog {call_log_id}: "
        f"disposition={disposition}, readiness={readiness_score}"
    )
