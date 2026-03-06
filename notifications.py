from __future__ import annotations

import logging
from typing import Optional

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

from .config import settings
from .models import Appointment, Workspace

logger = logging.getLogger(__name__)


def _workspace_twilio_sid(workspace: Workspace) -> str:
    return workspace.twilio_account_sid or settings.twilio_account_sid


def _workspace_twilio_token(workspace: Workspace) -> str:
    return workspace.twilio_auth_token or settings.twilio_auth_token


def _workspace_sms_from(workspace: Workspace) -> str:
    return workspace.twilio_from_number or settings.twilio_sms_from_number or settings.twilio_from_number


def get_twilio_client(workspace: Workspace) -> Optional[TwilioClient]:
    sid = _workspace_twilio_sid(workspace)
    token = _workspace_twilio_token(workspace)
    if not sid or not token:
        return None
    return TwilioClient(sid, token)


def build_appointment_sms(appointment: Appointment) -> str:
    name = appointment.homeowner_name or "there"
    when = appointment.appointment_time_iso or "the scheduled time"
    return (
        f"Hi {name}, your appointment is confirmed for {when}. "
        f"If you need to reschedule, please reply to this message."
    )


def send_sms(workspace: Workspace, to_number: str, body: str) -> dict:
    client = get_twilio_client(workspace)
    if not client:
        return {"ok": False, "error": "Twilio client not configured"}

    from_number = _workspace_sms_from(workspace)
    if not from_number:
        return {"ok": False, "error": "SMS from number not configured"}

    try:
        msg = client.messages.create(
            from_=from_number,
            to=to_number,
            body=body,
        )
        return {
            "ok": True,
            "sid": msg.sid,
            "status": getattr(msg, "status", "queued"),
        }
    except TwilioRestException as exc:
        logger.exception("Failed sending SMS")
        return {"ok": False, "error": str(exc)}


def send_appointment_confirmation_sms(workspace: Workspace, appointment: Appointment) -> dict:
    if not workspace.sms_confirmation_enabled and not settings.enable_sms_confirmation:
        return {"ok": False, "error": "SMS confirmations disabled"}

    if not appointment.phone:
        return {"ok": False, "error": "Appointment missing phone"}

    body = build_appointment_sms(appointment)
    return send_sms(workspace, appointment.phone, body)