from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time, timezone
from typing import Any, Optional, Tuple

from sqlmodel import Session, select

from config import settings
from models import AuditLog, CallLog, Campaign, DNCEntry, Lead, Notification, Pathway, UsageEvent, Workspace
from twilio_voice import place_outbound_call

PHONE_PROVIDER = os.getenv("PHONE_PROVIDER", "telnyx")

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _weekday_short(dt: datetime) -> str:
    return dt.strftime("%a")


def _parse_hhmm(value: str) -> time:
    parts = (value or "09:00").split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    return time(hour=hour, minute=minute)


def campaign_is_within_window(campaign: Campaign, workspace: Workspace) -> bool:
    now = utcnow()

    allowed_days = [
        x.strip()
        for x in (campaign.allowed_days_csv or workspace.call_days_csv or ",".join(settings.call_days)).split(",")
        if x.strip()
    ]
    today_short = _weekday_short(now)
    if allowed_days and today_short not in allowed_days:
        return False

    start_value = campaign.start_hour_local or workspace.call_window_start or settings.call_window_start
    end_value = campaign.end_hour_local or workspace.call_window_end or settings.call_window_end

    now_local_time = now.astimezone().time().replace(tzinfo=None)
    start_t = _parse_hhmm(start_value)
    end_t = _parse_hhmm(end_value)

    return start_t <= now_local_time <= end_t


def _lead_is_callable(lead: Lead, campaign: Campaign) -> bool:
    if lead.status in {"do_not_call", "booked", "completed", "bad_number"}:
        return False
    if lead.attempts >= campaign.attempt_limit_per_lead:
        return False
    if lead.next_call_at and lead.next_call_at > utcnow():
        return False
    return True


def pick_next_lead(session: Session, campaign: Campaign) -> Optional[Lead]:
    stmt = (
        select(Lead)
        .where(Lead.workspace_id == campaign.workspace_id)
        .where(Lead.lead_list_id == campaign.lead_list_id)
        .order_by(Lead.priority.desc(), Lead.created_at.asc())
    )
    leads = session.exec(stmt).all()
    for lead in leads:
        if _lead_is_callable(lead, campaign):
            return lead
    return None


def create_calllog_for_attempt(
    session: Session,
    workspace: Workspace,
    campaign: Campaign,
    lead: Lead,
    pathway: Pathway,
) -> CallLog:
    calllog = CallLog(
        workspace_id=workspace.id,
        campaign_id=campaign.id,
        lead_id=lead.id,
        pathway_id=pathway.id,
        from_number=workspace.twilio_from_number or settings.twilio_from_number,
        to_number=lead.phone,
        status="queued",
        disposition=None,
        direction="outbound",
        current_node=None,
        transcript="",
        route_trace="[]",
        extracted_json="{}",
        notes=None,
        provider_json="{}",
        latency_json="{}",
    )
    session.add(calllog)
    session.commit()
    session.refresh(calllog)
    return calllog


def record_usage_event(
    session: Session,
    workspace_id: int,
    event_type: str,
    quantity: int = 1,
    reference_type: Optional[str] = None,
    reference_id: Optional[int] = None,
    metadata_json: Optional[str] = None,
) -> None:
    event = UsageEvent(
        workspace_id=workspace_id,
        event_type=event_type,
        quantity=quantity,
        reference_type=reference_type,
        reference_id=reference_id,
        metadata_json=metadata_json,
    )
    session.add(event)


def _record_audit(
    session: Session,
    workspace_id: int,
    action: str,
    entity_type: str,
    entity_id: Optional[int] = None,
    details: Optional[dict] = None,
) -> None:
    """Write an AuditLog row."""
    entry = AuditLog(
        workspace_id=workspace_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details_json=json.dumps(details) if details else None,
    )
    session.add(entry)


def pre_call_checks(
    lead: Lead,
    campaign: Campaign,
    settings_obj: Any,
    session: Session,
) -> Tuple[bool, str]:
    """Run pre-call quality checks before placing an outbound call.

    Args:
        lead: The Lead to be called.
        campaign: The active Campaign.
        settings_obj: The workspace object (has API keys, call window, etc.)
                      or falls back to global settings.
        session: Active SQLModel session.

    Returns:
        (True, "ok") if all checks pass, (False, reason_string) on first failure.
    """
    ws: Optional[Workspace] = session.get(Workspace, campaign.workspace_id)

    # 1. Twilio credentials
    twilio_sid = (ws.twilio_account_sid if ws else None) or settings.twilio_account_sid
    twilio_token = (ws.twilio_auth_token if ws else None) or settings.twilio_auth_token
    if not twilio_sid or not twilio_token:
        return False, "missing_twilio_credentials"

    # 2. OpenAI key
    openai_key = (ws.openai_api_key if ws else None) or settings.openai_api_key
    if not openai_key:
        return False, "missing_openai_api_key"

    # 3. ElevenLabs key if voice_mode == "elevenlabs"
    voice_mode = campaign.voice_mode or (ws.voice_mode if ws else "realtime")
    if voice_mode == "elevenlabs":
        el_key = (ws.elevenlabs_api_key if ws else None)
        if not el_key:
            return False, "missing_elevenlabs_api_key"

    # 4. Pathway JSON valid
    pathway = session.get(Pathway, campaign.pathway_id)
    if not pathway:
        return False, "pathway_not_found"
    try:
        json.loads(pathway.json_def or "{}")
    except (json.JSONDecodeError, TypeError):
        return False, "pathway_json_invalid"

    # 5. Lead phone not in DNC
    phone_normalized = (lead.phone or "").strip()
    dnc_stmt = (
        select(DNCEntry)
        .where(DNCEntry.workspace_id == campaign.workspace_id)
        .where(DNCEntry.phone == phone_normalized)
    )
    dnc_hit = session.exec(dnc_stmt).first()
    if dnc_hit:
        return False, "phone_on_dnc_list"

    # 6. Call window check
    if ws:
        ok_window = campaign_is_within_window(campaign, ws)
        if not ok_window:
            return False, "outside_call_window"

    # 7. Attempt count < campaign limit
    if lead.attempts >= campaign.attempt_limit_per_lead:
        return False, "attempt_limit_reached"

    return True, "ok"


def run_campaign_tick(session: Session, campaign: Campaign) -> dict:
    workspace = session.get(Workspace, campaign.workspace_id)
    if not workspace:
        return {"ok": False, "error": "Workspace not found"}

    if campaign.status != "running":
        return {"ok": False, "error": f"Campaign not running: {campaign.status}"}

    if not campaign.autopilot_enabled:
        return {"ok": False, "error": "Autopilot disabled"}

    if not campaign_is_within_window(campaign, workspace):
        return {"ok": False, "error": "Outside calling window"}

    pathway = session.get(Pathway, campaign.pathway_id)
    if not pathway:
        return {"ok": False, "error": "Pathway not found"}

    lead = pick_next_lead(session, campaign)
    if not lead:
        return {"ok": True, "status": "idle", "message": "No callable leads available"}

    # Pre-call quality checks
    checks_ok, checks_reason = pre_call_checks(lead, campaign, workspace, session)
    if not checks_ok:
        _record_audit(
            session,
            workspace_id=workspace.id,
            action="pre_call_check_failed",
            entity_type="lead",
            entity_id=lead.id,
            details={"reason": checks_reason, "campaign_id": campaign.id},
        )
        lead.status = "skipped"
        session.add(lead)
        session.commit()
        return {"ok": False, "error": f"pre_call_check_failed: {checks_reason}"}

    # Usage metering check
    if workspace.calls_this_month >= workspace.calls_limit:
        logger.warning(f"Workspace {workspace.id} has reached call limit {workspace.calls_limit}")
        return {"ok": False, "error": "workspace_call_limit_reached"}

    # Hard minute limit check
    minutes_used = getattr(workspace, 'minutes_used_this_month', 0) or 0
    minutes_limit = getattr(workspace, 'minutes_limit', 200) or 200

    if minutes_used >= minutes_limit:
        running_campaigns = session.exec(
            select(Campaign).where(
                Campaign.workspace_id == workspace.id,
                Campaign.status == "running"
            )
        ).all()
        for camp in running_campaigns:
            camp.status = "paused"
            camp.pause_reason = "minute_limit_reached"
            session.add(camp)
        session.commit()
        return {"ok": False, "reason": "minute_limit_reached"}

    # 80% warning check — create notification if not already sent
    warning_threshold = minutes_limit * 0.80
    if minutes_used >= warning_threshold:
        existing_warning = session.exec(
            select(Notification).where(
                Notification.workspace_id == workspace.id,
                Notification.type == "minutes_80_percent_warning",
            )
        ).first()
        if not existing_warning:
            warning_notif = Notification(
                workspace_id=workspace.id,
                user_id=None,
                type="minutes_80_percent_warning",
                title="You are almost out of call minutes",
                body=f"You have used {int(minutes_used)} of your {minutes_limit} included minutes this month. Upgrade now to avoid interruption.",
                link="/app/billing",
                read=False,
            )
            session.add(warning_notif)
            session.commit()

    # DNC permanent suppression check
    dnc = session.exec(select(DNCEntry).where(DNCEntry.phone == lead.phone, DNCEntry.workspace_id == workspace.id)).first()
    if dnc:
        logger.info(f"Skipping lead {lead.id} — on DNC list")
        lead.status = "do_not_call"
        session.add(lead)
        session.commit()
        return {"ok": False, "error": "lead_on_dnc_list"}

    calllog = create_calllog_for_attempt(session, workspace, campaign, lead, pathway)

    # Pre-warming audit event
    _record_audit(
        session,
        workspace_id=workspace.id,
        action="pre_warming",
        entity_type="calllog",
        entity_id=calllog.id,
        details={"lead_id": lead.id, "campaign_id": campaign.id},
    )
    session.commit()

    if PHONE_PROVIDER == "telnyx":
        from telnyx_voice import place_telnyx_call  # type: ignore
        result = place_telnyx_call(
            to_number=lead.phone,
            calllog_id=calllog.id,
            campaign_id=campaign.id,
            lead_id=lead.id,
            pathway_id=pathway.id,
            workspace_id=workspace.id,
            from_number=workspace.twilio_from_number or settings.twilio_from_number,
        )
        call_sid_key = "call_control_id"
        default_status = "initiating"
    else:
        result = place_outbound_call(
            workspace=workspace,
            lead=lead,
            campaign=campaign,
            pathway=pathway,
            calllog_id=calllog.id,
        )
        call_sid_key = "call_sid"
        default_status = "queued"

    if not result.get("ok"):
        calllog.status = "failed"
        calllog.error_message = result.get("error", "Call placement failed")
        lead.attempts += 1
        lead.last_called_at = utcnow()
        session.add(calllog)
        session.add(lead)
        session.commit()
        return result

    calllog.twilio_call_sid = result.get(call_sid_key)
    calllog.status = result.get("status", default_status)

    lead.attempts += 1
    lead.last_called_at = utcnow()

    campaign.total_dials += 1
    campaign.last_run_at = utcnow()

    workspace.calls_this_month += 1
    session.add(workspace)

    record_usage_event(
        session,
        workspace_id=workspace.id,
        event_type="outbound_call_queued",
        quantity=1,
        reference_type="campaign",
        reference_id=campaign.id,
    )

    session.add(calllog)
    session.add(lead)
    session.add(campaign)
    session.commit()

    return {
        "ok": True,
        "status": "dialed",
        "campaign_id": campaign.id,
        "lead_id": lead.id,
        "calllog_id": calllog.id,
        "call_sid": calllog.twilio_call_sid,
    }


def run_worker_once(session: Session) -> dict:
    stmt = (
        select(Campaign)
        .where(Campaign.status == "running")
        .where(Campaign.autopilot_enabled == True)  # noqa: E712
        .order_by(Campaign.updated_at.asc())
    )
    campaigns = session.exec(stmt).all()

    processed = 0
    results: list[dict] = []

    for campaign in campaigns[: settings.worker_max_concurrency]:
        result = run_campaign_tick(session, campaign)
        results.append(result)
        processed += 1

    return {
        "ok": True,
        "processed": processed,
        "results": results,
    }