"""Post-call scoring and auto-listing processor."""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Any

from sqlmodel import Session, select

from models import AgentProfile, AgentVoiceClone, CallLog, Campaign, Lead, MarketplaceListing, PartnerPayout, Workspace

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


def get_auto_list_threshold(campaign: Any = None) -> int:
    """Admin campaigns list more inventory at score >= 75; agents at >= 80."""
    if campaign and getattr(campaign, "is_admin_campaign", False):
        return 75
    return 80


def get_platform_fee_pct(campaign: Any = None) -> int:
    """Admin campaigns keep 100% (0% fee); agent campaigns pay 40%."""
    if campaign and getattr(campaign, "is_admin_campaign", False):
        return 0
    return 40


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

    # Determine pricing by readiness score
    def _price_by_score(score: int) -> int:
        if score >= 93:
            return 12500  # $125
        elif score >= 86:
            return 10000  # $100
        else:
            return 8500   # $85 (for 80-85)

    # Determine campaign early (needed for thresholds)
    campaign = session.get(Campaign, call_log.campaign_id) if call_log.campaign_id else None
    is_admin_campaign = bool(campaign and getattr(campaign, "is_admin_campaign", False))

    # Dynamic threshold: 75 for admin, 80 for agents
    auto_list_threshold = get_auto_list_threshold(campaign)
    platform_fee_pct = get_platform_fee_pct(campaign)
    seller_payout_pct = 100 - platform_fee_pct

    # Update lead
    if lead:
        lead.readiness_score = readiness_score
        lead.marketplace_eligible = (
            readiness_score >= auto_list_threshold
            and disposition not in ("opted_out", "wrong_number")
        )
        session.add(lead)

    # Update call log with final disposition
    call_log.disposition = disposition
    session.add(call_log)

    # Auto-create marketplace listing if eligible
    if lead and lead.marketplace_eligible:
        listing_type = "booked_appointment" if disposition == "booked_appointment" else "qualified_lead"
        base_price = _price_by_score(readiness_score)

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
        logger.info(
            f"Auto-created MarketplaceListing for lead {lead.id} with readiness {readiness_score} "
            f"price={base_price} platform_fee_pct={platform_fee_pct} admin={is_admin_campaign}"
        )

        # Update campaign stats
        if campaign:
            campaign.marketplace_listings_count = getattr(campaign, "marketplace_listings_count", 0) + 1
            session.add(campaign)

    # Voice royalty tracking
    if campaign and getattr(campaign, "agent_voice_clone_id", None):
        try:
            shared_voice = session.exec(
                select(AgentVoiceClone).where(
                    AgentVoiceClone.id == campaign.agent_voice_clone_id,
                    AgentVoiceClone.is_shared == True,
                )
            ).first()
            if shared_voice and shared_voice.workspace_id != call_log.workspace_id:
                duration_minutes = (call_log.duration_seconds or 0) / 60
                rate = getattr(shared_voice, "royalty_rate_cents_per_min", 1)
                royalty_cents = int(duration_minutes * rate)
                if royalty_cents > 0:
                    shared_voice.total_royalties_earned_cents = getattr(shared_voice, "total_royalties_earned_cents", 0) + royalty_cents
                    shared_voice.total_minutes_used = getattr(shared_voice, "total_minutes_used", 0) + int(duration_minutes)
                    session.add(shared_voice)
                    payout = PartnerPayout(
                        workspace_id=shared_voice.workspace_id,
                        payout_type="voice_royalty",
                        gross_amount_cents=royalty_cents,
                        net_amount_cents=royalty_cents,
                        platform_fee_cents=0,
                        status="pending",
                        currency="usd",
                    )
                    session.add(payout)
                    logger.info(f"Voice royalty: {royalty_cents}¢ for clone {shared_voice.id}")
        except Exception as _re:
            logger.warning(f"Royalty tracking failed: {_re}")

    session.commit()
    logger.info(
        f"Post-call processing complete for CallLog {call_log_id}: "
        f"disposition={disposition}, readiness={readiness_score}"
    )

    # Top Agent SMS: send profile link to homeowner 10 min after non-booked calls
    from config import settings as _settings
    if (
        _settings.top_agent_sms_follow_up_enabled
        and disposition != "booked_appointment"
        and disposition not in ("opted_out", "wrong_number")
        and lead
        and lead.phone
        and call_log.workspace_id
    ):
        delay_seconds = _settings.top_agent_sms_delay_minutes * 60

        def _send_top_agent_sms() -> None:
            time.sleep(delay_seconds)
            try:
                from db import session_scope
                from notifications import send_sms
                with session_scope() as _sess:
                    workspace = _sess.get(Workspace, call_log.workspace_id)
                    if not workspace:
                        return
                    agent_profile = _sess.exec(
                        select(AgentProfile).where(AgentProfile.workspace_id == call_log.workspace_id)
                    ).first()
                    if not agent_profile:
                        return
                    agent_name = agent_profile.full_name or workspace.name or "your agent"
                    brokerage = agent_profile.brokerage or workspace.name or "A2Z Realty"
                    slug = (agent_profile.primary_territory_key or "").strip("/")
                    profile_url = f"a2zdialer.com/agents/{slug}" if slug else "a2zdialer.com"
                    homeowner_first = (lead.first_name or lead.homeowner_name or "there").split()[0]
                    body = (
                        f"Hi {homeowner_first} — this is {agent_name} from {brokerage}. "
                        f"Here is my profile when the timing is right: {profile_url}"
                    )
                    result = send_sms(workspace, lead.phone, body)
                    logger.info(f"Top Agent SMS to lead {lead.id}: {result}")
            except Exception:
                logger.exception(f"Top Agent SMS failed for lead {lead.id}")

        threading.Thread(target=_send_top_agent_sms, daemon=True).start()


def process_admin_callback_result(call_log_id: int, new_score: int, session: Session) -> None:
    """After AI handles an admin callback, update score and send admin SMS summary."""
    call_log = session.get(CallLog, call_log_id)
    if not call_log:
        return
    lead = session.get(Lead, call_log.lead_id) if call_log.lead_id else None
    workspace = session.get(Workspace, call_log.workspace_id) if call_log.workspace_id else None

    if lead:
        lead.readiness_score = new_score
        lead.marketplace_eligible = new_score >= 75
        session.add(lead)
        session.commit()
        logger.info(f"Admin callback: lead {lead.id} updated score={new_score}")

    if workspace and getattr(workspace, "agent_callback_number", None):
        try:
            from notifications import send_sms
            first = lead.first_name or "" if lead else ""
            last = lead.last_name or "" if lead else ""
            address = (lead.property_address or "") if lead else ""
            listed = "✅ Listed on marketplace" if new_score >= 75 else "❌ Score too low — not listed"
            send_sms(workspace, workspace.agent_callback_number, (
                f"📋 Callback handled by AI\n"
                f"Homeowner: {first} {last}\n"
                f"Property: {address}\n"
                f"New score: {new_score}/100\n"
                f"{listed}"
            ))
        except Exception:
            logger.exception("Admin callback SMS summary failed")
