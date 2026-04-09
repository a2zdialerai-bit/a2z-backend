from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, EmailStr, Field


class AuthRegisterIn(BaseModel):
    workspace_name: str = Field(min_length=2, max_length=200)
    full_name: str = Field(min_length=2, max_length=200)
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class AuthLoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserMeOut(BaseModel):
    id: int
    workspace_id: int
    email: EmailStr
    full_name: str
    role: str
    is_active: bool
    plan: str
    subscription_status: str


class LeadListCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    source: str = Field(default="csv", max_length=100)
    description: Optional[str] = Field(default=None, max_length=1000)


class PathwayCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=1000)
    json_def: Any


class PathwayUpdateIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=1000)
    json_def: Optional[Any] = None
    is_active: Optional[bool] = None


class PathwaySimulateIn(BaseModel):
    current_node: Optional[str] = None
    user_reply: str = ""
    flags: dict[str, Any] = Field(default_factory=dict)


class CampaignCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    lead_list_id: int
    pathway_id: int
    concurrency: int = Field(default=1, ge=1, le=20)
    daily_cap: int = Field(default=100, ge=1, le=10000)
    attempt_limit_per_lead: int = Field(default=6, ge=1, le=20)
    voice_mode: str = Field(default="realtime", max_length=50)
    appointment_mode: str = Field(default="google", max_length=50)
    timezone: str = Field(default="America/New_York", max_length=80)
    start_hour_local: str = Field(default="09:00", max_length=10)
    end_hour_local: str = Field(default="19:00", max_length=10)
    allowed_days_csv: str = Field(default="Mon,Tue,Wed,Thu,Fri,Sat", max_length=100)


class CampaignControlOut(BaseModel):
    ok: bool
    campaign_id: int
    status: str


class LeadCreateIn(BaseModel):
    lead_list_id: int
    homeowner_name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: str
    email: Optional[EmailStr] = None
    property_address: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    lead_source: str = "expired_listing"
    notes: Optional[str] = None
    raw_data_json: Optional[dict[str, Any]] = None


class CallDispositionIn(BaseModel):
    disposition: str = Field(min_length=1, max_length=80)
    status: Optional[str] = Field(default=None, max_length=80)
    notes: Optional[str] = Field(default=None, max_length=5000)


class AppointmentCreateIn(BaseModel):
    lead_id: Optional[int] = None
    campaign_id: Optional[int] = None
    calllog_id: Optional[int] = None
    homeowner_name: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[EmailStr] = None
    property_address: Optional[str] = None
    appointment_time_iso: Optional[str] = None
    timezone: str = "America/New_York"
    notes: Optional[str] = None


class DNCAddIn(BaseModel):
    phone: str
    reason: str = "manual"
    source: str = "workspace"


class WorkspaceSettingsUpdateIn(BaseModel):
    voice_mode: Optional[str] = None
    appointment_mode: Optional[str] = None
    timezone: Optional[str] = None
    twilio_account_sid: Optional[str] = None
    twilio_auth_token: Optional[str] = None
    twilio_from_number: Optional[str] = None
    openai_api_key: Optional[str] = None
    elevenlabs_api_key: Optional[str] = None
    elevenlabs_voice_id: Optional[str] = None
    deepgram_api_key: Optional[str] = None
    calendly_link: Optional[str] = None
    google_calendar_id: Optional[str] = None
    sms_confirmation_enabled: Optional[bool] = None
    email_confirmation_enabled: Optional[bool] = None
    call_window_start: Optional[str] = None
    call_window_end: Optional[str] = None
    call_days_csv: Optional[str] = None
    dnc_enabled: Optional[bool] = None
    recording_enabled: Optional[bool] = None
    strict_pathway_mode: Optional[bool] = None
    preferred_voice_id: Optional[str] = None
    preferred_voice_gender: Optional[str] = None


class DashboardKpiOut(BaseModel):
    total_leads: int
    new_leads: int
    active_campaigns: int
    calls_today: int
    total_booked: int
    total_opt_out: int


class HealthOut(BaseModel):
    ok: bool
    env: str
    ts: int
    database_url: str