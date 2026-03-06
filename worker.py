from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Optional

from sqlmodel import Session, select

from .config import settings
from .models import CallLog, Campaign, Lead, Pathway, UsageEvent, Workspace
from .twilio_voice import place_outbound_call

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

    calllog = create_calllog_for_attempt(session, workspace, campaign, lead, pathway)

    result = place_outbound_call(
        workspace=workspace,
        lead=lead,
        campaign=campaign,
        pathway=pathway,
        calllog_id=calllog.id,
    )

    if not result.get("ok"):
        calllog.status = "failed"
        calllog.error_message = result.get("error", "Call placement failed")
        lead.attempts += 1
        lead.last_called_at = utcnow()
        session.add(calllog)
        session.add(lead)
        session.commit()
        return result

    calllog.twilio_call_sid = result.get("call_sid")
    calllog.status = result.get("status", "queued")

    lead.attempts += 1
    lead.last_called_at = utcnow()

    campaign.total_dials += 1
    campaign.last_run_at = utcnow()

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