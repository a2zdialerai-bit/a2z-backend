from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Timestamped(SQLModel):
    created_at: datetime = Field(default_factory=utcnow, nullable=False)
    updated_at: datetime = Field(default_factory=utcnow, nullable=False)


class Workspace(Timestamped, table=True):
    __tablename__ = "workspaces"

    id: Optional[int] = Field(default=None, primary_key=True)

    name: str = Field(index=True, max_length=200)
    slug: str = Field(index=True, unique=True, max_length=200)

    brand_name: Optional[str] = Field(default=None, max_length=255)
    default_agent_name: Optional[str] = Field(default=None, max_length=255)
    default_brokerage_name: Optional[str] = Field(default=None, max_length=255)
    default_caller_title: Optional[str] = Field(default=None, max_length=120)

    plan: str = Field(default="starter", max_length=50)
    subscription_status: str = Field(default="trial", max_length=50)
    monthly_call_limit: int = Field(default=1000)
    monthly_calls_used: int = Field(default=0)

    voice_mode: str = Field(default="realtime", max_length=50)
    appointment_mode: str = Field(default="google", max_length=50)
    timezone: str = Field(default="America/New_York", max_length=80)

    twilio_account_sid: Optional[str] = Field(default=None, max_length=255)
    twilio_auth_token: Optional[str] = Field(default=None, max_length=255)
    twilio_from_number: Optional[str] = Field(default=None, max_length=40)

    openai_api_key: Optional[str] = Field(default=None, max_length=255)
    elevenlabs_api_key: Optional[str] = Field(default=None, max_length=255)
    elevenlabs_voice_id: Optional[str] = Field(default=None, max_length=255)
    deepgram_api_key: Optional[str] = Field(default=None, max_length=255)

    google_refresh_token: Optional[str] = Field(default=None, max_length=2000)
    google_calendar_id: Optional[str] = Field(default=None, max_length=255)
    calendly_link: Optional[str] = Field(default=None, max_length=500)

    sms_confirmation_enabled: bool = Field(default=True)
    email_confirmation_enabled: bool = Field(default=False)

    call_window_start: str = Field(default="09:00", max_length=10)
    call_window_end: str = Field(default="19:00", max_length=10)
    call_days_csv: str = Field(default="Mon,Tue,Wed,Thu,Fri,Sat", max_length=100)

    dnc_enabled: bool = Field(default=True)
    recording_enabled: bool = Field(default=False)
    strict_pathway_mode: bool = Field(default=False)


class User(Timestamped, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")

    email: str = Field(index=True, unique=True, max_length=255)
    full_name: str = Field(default="", max_length=255)
    password_hash: str = Field(max_length=255)

    role: str = Field(default="owner", max_length=50)
    is_active: bool = Field(default=True)
    is_verified: bool = Field(default=False)
    last_login_at: Optional[datetime] = Field(default=None)


class LeadList(Timestamped, table=True):
    __tablename__ = "leadlists"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")

    name: str = Field(max_length=255)
    source: str = Field(default="csv", max_length=100)
    description: Optional[str] = Field(default=None, max_length=1000)

    total_records: int = Field(default=0)
    active_records: int = Field(default=0)


class Lead(Timestamped, table=True):
    __tablename__ = "leads"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")
    lead_list_id: int = Field(index=True, foreign_key="leadlists.id")

    homeowner_name: Optional[str] = Field(default=None, max_length=255)
    first_name: Optional[str] = Field(default=None, max_length=120)
    last_name: Optional[str] = Field(default=None, max_length=120)

    phone: str = Field(index=True, max_length=40)
    email: Optional[str] = Field(default=None, max_length=255)

    property_address: Optional[str] = Field(default=None, max_length=500)
    city: Optional[str] = Field(default=None, max_length=120)
    state: Optional[str] = Field(default=None, max_length=50)
    postal_code: Optional[str] = Field(default=None, max_length=20)

    lead_source: str = Field(default="expired_listing", max_length=100)
    listing_status: Optional[str] = Field(default=None, max_length=120)
    listing_status_raw: Optional[str] = Field(default=None, max_length=500)

    status: str = Field(default="new", index=True, max_length=80)
    disposition: Optional[str] = Field(default=None, max_length=80)

    last_called_at: Optional[datetime] = Field(default=None)
    next_call_at: Optional[datetime] = Field(default=None)

    attempts: int = Field(default=0)
    priority: int = Field(default=0)

    notes: Optional[str] = Field(default=None, max_length=5000)
    raw_data_json: Optional[str] = Field(default=None)
    extracted_json: Optional[str] = Field(default=None)


class Pathway(Timestamped, table=True):
    __tablename__ = "pathways"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")

    name: str = Field(max_length=255)
    description: Optional[str] = Field(default=None, max_length=1000)

    is_active: bool = Field(default=True)
    version: int = Field(default=1)

    json_def: str = Field(default="{}")
    validation_errors: Optional[str] = Field(default=None)
    tags_csv: Optional[str] = Field(default=None, max_length=1000)


class Campaign(Timestamped, table=True):
    __tablename__ = "campaigns"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")

    name: str = Field(max_length=255)
    lead_list_id: int = Field(index=True, foreign_key="leadlists.id")
    pathway_id: int = Field(index=True, foreign_key="pathways.id")

    caller_name: Optional[str] = Field(default=None, max_length=255)
    caller_title: Optional[str] = Field(default=None, max_length=120)
    brokerage_name: Optional[str] = Field(default=None, max_length=255)

    status: str = Field(default="draft", index=True, max_length=80)
    voice_mode: str = Field(default="realtime", max_length=50)
    appointment_mode: str = Field(default="google", max_length=50)

    concurrency: int = Field(default=1)
    daily_cap: int = Field(default=100)
    attempt_limit_per_lead: int = Field(default=6)

    start_hour_local: str = Field(default="09:00", max_length=10)
    end_hour_local: str = Field(default="19:00", max_length=10)
    allowed_days_csv: str = Field(default="Mon,Tue,Wed,Thu,Fri,Sat", max_length=100)

    timezone: str = Field(default="America/New_York", max_length=80)
    autopilot_enabled: bool = Field(default=True)

    last_run_at: Optional[datetime] = Field(default=None)
    next_run_at: Optional[datetime] = Field(default=None)

    total_dials: int = Field(default=0)
    total_connected: int = Field(default=0)
    total_booked: int = Field(default=0)
    total_opt_out: int = Field(default=0)


class CallLog(Timestamped, table=True):
    __tablename__ = "calllogs"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")

    campaign_id: Optional[int] = Field(default=None, index=True, foreign_key="campaigns.id")
    lead_id: Optional[int] = Field(default=None, index=True, foreign_key="leads.id")
    pathway_id: Optional[int] = Field(default=None, index=True, foreign_key="pathways.id")

    twilio_call_sid: Optional[str] = Field(default=None, index=True, max_length=100)
    stream_sid: Optional[str] = Field(default=None, index=True, max_length=100)

    from_number: Optional[str] = Field(default=None, max_length=40)
    to_number: Optional[str] = Field(default=None, max_length=40)

    status: str = Field(default="queued", index=True, max_length=80)
    disposition: Optional[str] = Field(default=None, index=True, max_length=80)
    direction: str = Field(default="outbound", max_length=20)

    current_node: Optional[str] = Field(default=None, max_length=200)
    transcript: Optional[str] = Field(default=None)
    route_trace: Optional[str] = Field(default=None)
    extracted_json: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)

    latency_json: Optional[str] = Field(default=None)
    provider_json: Optional[str] = Field(default=None)
    error_message: Optional[str] = Field(default=None)

    started_at: Optional[datetime] = Field(default=None)
    answered_at: Optional[datetime] = Field(default=None)
    ended_at: Optional[datetime] = Field(default=None)
    duration_seconds: Optional[int] = Field(default=None)


class Appointment(Timestamped, table=True):
    __tablename__ = "appointments"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")

    campaign_id: Optional[int] = Field(default=None, index=True, foreign_key="campaigns.id")
    lead_id: Optional[int] = Field(default=None, index=True, foreign_key="leads.id")
    calllog_id: Optional[int] = Field(default=None, index=True, foreign_key="calllogs.id")

    status: str = Field(default="pending", index=True, max_length=80)
    source: str = Field(default="phone_call", max_length=80)

    homeowner_name: Optional[str] = Field(default=None, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=40)
    email: Optional[str] = Field(default=None, max_length=255)
    property_address: Optional[str] = Field(default=None, max_length=500)

    appointment_time_iso: Optional[str] = Field(default=None, max_length=100)
    timezone: str = Field(default="America/New_York", max_length=80)
    confirmed: bool = Field(default=False)

    google_event_id: Optional[str] = Field(default=None, max_length=255)
    calendly_event_uri: Optional[str] = Field(default=None, max_length=500)

    confirmation_sent_sms: bool = Field(default=False)
    confirmation_sent_email: bool = Field(default=False)

    notes: Optional[str] = Field(default=None)


class DNCEntry(Timestamped, table=True):
    __tablename__ = "dnc"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")

    phone: str = Field(index=True, max_length=40)
    reason: str = Field(default="manual", max_length=120)
    source: str = Field(default="workspace", max_length=120)


class IntegrationSetting(Timestamped, table=True):
    __tablename__ = "integration_settings"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")

    provider: str = Field(index=True, max_length=80)
    key: str = Field(index=True, max_length=120)
    value: str = Field(default="")
    is_enabled: bool = Field(default=True)


class UsageEvent(Timestamped, table=True):
    __tablename__ = "usage_events"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")

    event_type: str = Field(index=True, max_length=100)
    quantity: int = Field(default=1)

    reference_type: Optional[str] = Field(default=None, max_length=80)
    reference_id: Optional[int] = Field(default=None)
    metadata_json: Optional[str] = Field(default=None)


class AuditLog(Timestamped, table=True):
    __tablename__ = "audit_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")

    user_id: Optional[int] = Field(default=None, index=True, foreign_key="users.id")
    action: str = Field(index=True, max_length=120)
    entity_type: str = Field(index=True, max_length=80)
    entity_id: Optional[int] = Field(default=None, index=True)
    details_json: Optional[str] = Field(default=None)