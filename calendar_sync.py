from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from config import settings
from models import Appointment, Workspace


GOOGLE_AUTH_BASE = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


def get_google_oauth_start_url(state: str) -> str:
    params = {
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": settings.google_scopes,
        "state": state,
    }
    return f"{GOOGLE_AUTH_BASE}?{urlencode(params)}"


def exchange_google_code_for_tokens(code: str) -> Dict[str, Any]:
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "redirect_uri": settings.google_redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def refresh_google_access_token(refresh_token: str) -> Dict[str, Any]:
    resp = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def build_google_credentials(workspace: Workspace) -> Optional[Credentials]:
    if not workspace.google_refresh_token:
        return None

    token_data = refresh_google_access_token(workspace.google_refresh_token)
    access_token = token_data.get("access_token")
    if not access_token:
        return None

    return Credentials(
        token=access_token,
        refresh_token=workspace.google_refresh_token,
        token_uri=GOOGLE_TOKEN_URL,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=[settings.google_scopes],
    )


def create_google_calendar_event(workspace: Workspace, appointment: Appointment) -> Dict[str, Any]:
    creds = build_google_credentials(workspace)
    if not creds:
        return {"ok": False, "error": "Google Calendar not connected"}

    if not appointment.appointment_time_iso:
        return {"ok": False, "error": "Appointment time missing"}

    start_dt = datetime.fromisoformat(appointment.appointment_time_iso.replace("Z", "+00:00"))
    end_dt = start_dt + timedelta(minutes=30)

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    calendar_id = workspace.google_calendar_id or "primary"

    event = {
        "summary": f"Listing Appointment - {appointment.homeowner_name or 'Lead'}",
        "description": appointment.notes or "Scheduled by A2Z Dialer",
        "start": {
            "dateTime": start_dt.isoformat(),
            "timeZone": appointment.timezone or workspace.timezone or settings.default_timezone,
        },
        "end": {
            "dateTime": end_dt.isoformat(),
            "timeZone": appointment.timezone or workspace.timezone or settings.default_timezone,
        },
        "attendees": [{"email": appointment.email}] if appointment.email else [],
        "location": appointment.property_address or "",
    }

    created = service.events().insert(calendarId=calendar_id, body=event).execute()
    return {
        "ok": True,
        "event_id": created.get("id"),
        "html_link": created.get("htmlLink"),
    }


def get_calendly_booking_link(workspace: Workspace) -> str:
    return workspace.calendly_link or settings.calendly_link_default


def create_calendly_placeholder(workspace: Workspace, appointment: Appointment) -> Dict[str, Any]:
    return {
        "ok": True,
        "status": "pending-confirmation",
        "booking_link": get_calendly_booking_link(workspace),
        "source": "calendly_link_fallback",
        "lead": appointment.homeowner_name,
    }