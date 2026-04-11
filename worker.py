from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional, Tuple

import pytz
from sqlmodel import Session, func, select

from config import settings
from models import AuditLog, CallLog, Campaign, DNCEntry, Lead, Notification, Pathway, UsageEvent, Workspace
from twilio_voice import place_outbound_call

PHONE_PROVIDER = os.getenv("PHONE_PROVIDER", "telnyx")

# Anti-spam: max outbound calls per number per day
MAX_CALLS_PER_NUMBER_PER_DAY = 100

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


def is_within_call_window(workspace: Workspace, campaign: Optional[Campaign] = None) -> bool:
    """Check if it's within legal calling hours using workspace timezone (pytz-aware).

    Hours: 9AM-7PM Mon-Sat in workspace local time.
    """
    tz_name = workspace.timezone or "America/New_York"
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone("America/New_York")

    now_local = datetime.now(tz)

    # Determine allowed days
    if campaign and campaign.allowed_days_csv:
        allowed_days = [d.strip() for d in campaign.allowed_days_csv.split(",") if d.strip()]
    elif workspace.call_days_csv:
        allowed_days = [d.strip() for d in workspace.call_days_csv.split(",") if d.strip()]
    else:
        allowed_days = list(settings.call_days)

    today_short = now_local.strftime("%a")
    if allowed_days and today_short not in allowed_days:
        return False

    # Determine call window
    if campaign and campaign.start_hour_local:
        start_value = campaign.start_hour_local
    else:
        start_value = workspace.call_window_start or settings.call_window_start

    if campaign and campaign.end_hour_local:
        end_value = campaign.end_hour_local
    else:
        end_value = workspace.call_window_end or settings.call_window_end

    now_time = now_local.time().replace(tzinfo=None)
    start_t = _parse_hhmm(start_value)
    end_t = _parse_hhmm(end_value)

    return start_t <= now_time <= end_t


def campaign_is_within_window(campaign: Campaign, workspace: Workspace) -> bool:
    """Legacy wrapper used by run_campaign_tick — delegates to is_within_call_window."""
    return is_within_call_window(workspace, campaign)


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


def count_calls_today_from_number(session: Session, from_number: str) -> int:
    """Count outbound calls placed today from a given phone number (anti-spam)."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    count = session.exec(
        select(func.count(CallLog.id)).where(
            CallLog.from_number == from_number,
            CallLog.direction == "outbound",
            CallLog.created_at >= today_start,
        )
    ).one()
    return count or 0


def passes_compliance_check(lead: Lead, workspace: Workspace, campaign: Campaign, session: Session) -> Tuple[bool, str]:
    """Full compliance check before dialing a lead.

    Returns (True, "ok") if all checks pass, (False, reason) otherwise.
    """
    # 1. Campaign must still be running
    if campaign.status != "running":
        return False, "campaign_not_running"

    # 2. Lead not flagged as invalid or DNC
    if lead.status in {"do_not_call", "bad_number"}:
        return False, "lead_flagged"

    # 3. Not on workspace DNC list
    phone_normalized = (lead.phone or "").strip()
    dnc_hit = session.exec(
        select(DNCEntry)
        .where(DNCEntry.workspace_id == campaign.workspace_id)
        .where(DNCEntry.phone == phone_normalized)
    ).first()
    if dnc_hit:
        return False, "phone_on_dnc_list"

    # 4. Within calling hours (9AM-7PM workspace timezone, Mon-Sat)
    if not is_within_call_window(workspace, campaign):
        return False, "outside_call_window"

    # 5. Not called in last 24 hours
    if lead.last_called_at:
        hours_since = (utcnow() - lead.last_called_at).total_seconds() / 3600
        if hours_since < 24:
            return False, "called_within_24h"

    # 6. Workspace has minutes remaining
    minutes_used = getattr(workspace, 'minutes_used_this_month', 0) or 0
    minutes_limit = getattr(workspace, 'minutes_limit', 200) or 200
    if minutes_used >= minutes_limit:
        return False, "minute_limit_reached"

    # 7. Attempt limit not reached
    if lead.attempts >= campaign.attempt_limit_per_lead:
        return False, "attempt_limit_reached"

    return True, "ok"


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

    # Full compliance check (includes DNC, hours, 24h cooldown, minutes remaining)
    compliance_ok, compliance_reason = passes_compliance_check(lead, workspace, campaign, session)
    if not compliance_ok:
        _record_audit(
            session,
            workspace_id=workspace.id,
            action="compliance_check_failed",
            entity_type="lead",
            entity_id=lead.id,
            details={"reason": compliance_reason, "campaign_id": campaign.id},
        )
        if compliance_reason in ("phone_on_dnc_list", "lead_flagged"):
            lead.status = "do_not_call"
            session.add(lead)
            session.commit()
        elif compliance_reason in ("attempt_limit_reached",):
            lead.status = "skipped"
            session.add(lead)
            session.commit()
        elif compliance_reason == "minute_limit_reached":
            # Pause all running campaigns
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
        return {"ok": False, "error": f"compliance_check_failed: {compliance_reason}"}

    # Pre-call quality checks (credentials, pathway validity)
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

    # Anti-spam: number rotation / pause if daily call cap exceeded
    outbound_number = workspace.twilio_from_number or settings.twilio_from_number
    if outbound_number:
        calls_today = count_calls_today_from_number(session, outbound_number)
        if calls_today >= MAX_CALLS_PER_NUMBER_PER_DAY:
            logger.warning(
                f"Outbound number {outbound_number} has reached {MAX_CALLS_PER_NUMBER_PER_DAY} calls today. "
                f"Pausing campaign {campaign.id} until tomorrow."
            )
            campaign.status = "paused"
            campaign.pause_reason = "daily_number_cap_reached"
            session.add(campaign)
            session.commit()
            return {"ok": False, "error": "daily_number_cap_reached"}

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

    # Determine caller ID
    from_number = _get_outbound_caller_id(workspace)

    if PHONE_PROVIDER == "telnyx":
        from telnyx_voice import place_telnyx_call  # type: ignore
        result = place_telnyx_call(
            to_number=lead.phone,
            calllog_id=calllog.id,
            campaign_id=campaign.id,
            lead_id=lead.id,
            pathway_id=pathway.id,
            workspace_id=workspace.id,
            from_number=from_number,
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


def _get_outbound_caller_id(workspace: Workspace) -> str:
    """Determine the outbound caller ID for a workspace.

    Preference order:
    1. Agent's verified callback number (if set)
    2. Workspace Telnyx/Twilio provisioned number
    3. Global fallback from settings
    """
    agent_callback = getattr(workspace, 'agent_callback_number', None)
    if agent_callback:
        return agent_callback
    return workspace.twilio_from_number or settings.twilio_from_number


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


async def run_campaign_loop(campaign_id: int) -> None:
    """Async campaign calling loop with Mojo-style speed.

    - No artificial pauses between leads
    - Only 1.5s line reset between calls
    - Tracks consecutive quick hangups (<3s); pauses 5 min after 5 consecutive
    - Post-call processing runs async/non-blocking
    - Auto-pauses at 7PM, waits until 9AM if outside window
    """
    from db import session_scope

    consecutive_quick_hangups = 0

    logger.info(f"Campaign loop started for campaign_id={campaign_id}")

    while True:
        async with asyncio.timeout(30):
            pass  # placeholder for any setup

        with session_scope() as session:
            campaign = session.get(Campaign, campaign_id)
            if not campaign or campaign.status != "running":
                logger.info(f"Campaign {campaign_id} no longer running — loop exiting")
                break

            workspace = session.get(Workspace, campaign.workspace_id)
            if not workspace:
                logger.error(f"Workspace not found for campaign {campaign_id}")
                break

            # Check calling hours — auto-pause at 7PM, wait at 9AM
            if not is_within_call_window(workspace, campaign):
                tz_name = workspace.timezone or "America/New_York"
                try:
                    tz = pytz.timezone(tz_name)
                except Exception:
                    tz = pytz.timezone("America/New_York")
                now_local = datetime.now(tz)
                end_t = _parse_hhmm(campaign.end_hour_local or workspace.call_window_end or "19:00")

                # After 7PM — pause campaign
                if now_local.time().replace(tzinfo=None) > end_t:
                    logger.info(f"Campaign {campaign_id} auto-paused at end of calling window")
                    campaign.status = "paused"
                    campaign.pause_reason = "after_hours_auto_pause"
                    session.add(campaign)
                    session.commit()
                    break

                # Before calling window — sleep until window opens
                start_t = _parse_hhmm(campaign.start_hour_local or workspace.call_window_start or "09:00")
                now_time = now_local.time().replace(tzinfo=None)
                if now_time < start_t:
                    # Calculate seconds until window opens
                    now_dt = datetime.now(tz)
                    target_dt = now_dt.replace(
                        hour=start_t.hour,
                        minute=start_t.minute,
                        second=0,
                        microsecond=0
                    )
                    wait_seconds = max((target_dt - now_dt).total_seconds(), 60)
                    logger.info(f"Campaign {campaign_id} sleeping {wait_seconds:.0f}s until calling window")
                    await asyncio.sleep(min(wait_seconds, 300))  # check every 5 min max
                    continue
                else:
                    # Weekend or disallowed day
                    await asyncio.sleep(300)
                    continue

            # Get next lead
            pathway = session.get(Pathway, campaign.pathway_id)
            if not pathway:
                logger.error(f"Pathway not found for campaign {campaign_id}")
                break

            lead = pick_next_lead(session, campaign)
            if not lead:
                logger.info(f"Campaign {campaign_id} — no more callable leads, marking complete")
                campaign.status = "completed"
                session.add(campaign)
                session.commit()
                break

            # Compliance check — skip immediately if fails (no pause)
            compliance_ok, compliance_reason = passes_compliance_check(lead, workspace, campaign, session)
            if not compliance_ok:
                logger.info(f"Skipping lead {lead.id}: {compliance_reason}")
                if compliance_reason in ("phone_on_dnc_list", "lead_flagged"):
                    lead.status = "do_not_call"
                elif compliance_reason == "attempt_limit_reached":
                    lead.status = "skipped"
                elif compliance_reason in ("outside_call_window", "called_within_24h"):
                    # Skip this lead for now, move to next
                    lead.next_call_at = utcnow() + timedelta(hours=1)
                elif compliance_reason == "minute_limit_reached":
                    campaign.status = "paused"
                    campaign.pause_reason = "minute_limit_reached"
                    session.add(campaign)
                    session.commit()
                    break
                elif compliance_reason == "campaign_not_running":
                    break
                session.add(lead)
                session.commit()
                # No pause — immediately continue to next lead
                continue

            # Place the call
            calllog = create_calllog_for_attempt(session, workspace, campaign, lead, pathway)
            from_number = _get_outbound_caller_id(workspace)

            if PHONE_PROVIDER == "telnyx":
                from telnyx_voice import place_telnyx_call  # type: ignore
                result = place_telnyx_call(
                    to_number=lead.phone,
                    calllog_id=calllog.id,
                    campaign_id=campaign.id,
                    lead_id=lead.id,
                    pathway_id=pathway.id,
                    workspace_id=workspace.id,
                    from_number=from_number,
                )
            else:
                result = place_outbound_call(
                    workspace=workspace,
                    lead=lead,
                    campaign=campaign,
                    pathway=pathway,
                    calllog_id=calllog.id,
                )

            if not result.get("ok"):
                calllog.status = "failed"
                lead.attempts += 1
                lead.last_called_at = utcnow()
                session.add(calllog)
                session.add(lead)
                session.commit()
                # Brief line reset even on failure
                await asyncio.sleep(1.5)
                continue

            call_control_id = result.get("call_control_id") or result.get("call_sid")
            calllog.twilio_call_sid = call_control_id
            calllog.status = result.get("status", "initiating")
            lead.attempts += 1
            lead.last_called_at = utcnow()
            campaign.total_dials += 1
            campaign.last_run_at = utcnow()
            workspace.calls_this_month += 1

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
            session.add(workspace)
            session.commit()

            calllog_id_for_wait = calllog.id

        # Wait for call to complete (poll calllog status)
        call_start_time = utcnow()
        call_completed = False
        duration_seconds = 0

        for _ in range(120):  # max 2 minutes polling
            await asyncio.sleep(2)
            with session_scope() as poll_session:
                updated_calllog = poll_session.get(CallLog, calllog_id_for_wait)
                if updated_calllog and updated_calllog.status in ("completed", "failed", "no_answer", "busy", "cancelled"):
                    call_completed = True
                    duration_seconds = updated_calllog.duration_seconds or 0
                    break

        if not call_completed:
            logger.warning(f"Call {calllog_id_for_wait} did not complete within polling window")

        # Track consecutive quick hangups
        if duration_seconds < 3:
            consecutive_quick_hangups += 1
            logger.info(
                f"Quick hangup detected (duration={duration_seconds}s). "
                f"Consecutive count: {consecutive_quick_hangups}"
            )
            if consecutive_quick_hangups >= 5:
                logger.warning(
                    f"Campaign {campaign_id}: 5 consecutive quick hangups. "
                    f"Pausing 5 minutes to avoid spam flagging."
                )
                await asyncio.sleep(300)  # 5 min pause
                consecutive_quick_hangups = 0
        else:
            consecutive_quick_hangups = 0

        # Post-call processing (async, non-blocking)
        asyncio.create_task(_post_call_processing(calllog_id_for_wait))

        # 1.5 second line reset — only pause between calls
        await asyncio.sleep(1.5)

    logger.info(f"Campaign loop finished for campaign_id={campaign_id}")


async def _post_call_processing(calllog_id: int) -> None:
    """Non-blocking post-call cleanup: update stats, send notifications, etc."""
    try:
        from db import session_scope
        with session_scope() as session:
            calllog = session.get(CallLog, calllog_id)
            if not calllog:
                return

            lead = session.get(Lead, calllog.lead_id) if calllog.lead_id else None
            campaign = session.get(Campaign, calllog.campaign_id) if calllog.campaign_id else None

            if calllog.disposition == "booked" and campaign:
                campaign.total_booked = (campaign.total_booked or 0) + 1
                session.add(campaign)

            if lead and calllog.disposition in ("not_interested", "dnc"):
                lead.status = "do_not_call"
                session.add(lead)

            session.commit()
    except Exception as exc:
        logger.error(f"Post-call processing error for calllog {calllog_id}: {exc}")
