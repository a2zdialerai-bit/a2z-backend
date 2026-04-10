from __future__ import annotations

import secrets
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
    stripe_customer_id: Optional[str] = Field(default=None, max_length=255)
    subscription_plan: str = Field(default="starter", max_length=50)
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

    marketplace_enabled: bool = Field(default=True)
    marketplace_default_visibility: str = Field(default="private", max_length=50)
    marketplace_buyer_fee_pct: float = Field(default=0.0)
    marketplace_partner_payout_pct: float = Field(default=0.0)
    marketplace_bad_lead_window_hours: int = Field(default=24)

    calls_this_month: int = Field(default=0)
    calls_limit: int = Field(default=500)
    minutes_used_this_month: float = Field(default=0.0)
    minutes_limit: int = Field(default=200)
    overage_rate_cents: int = Field(default=18)

    preferred_voice_id: Optional[str] = Field(default=None, max_length=255)
    preferred_voice_gender: Optional[str] = Field(default=None, max_length=50)
    is_admin_workspace: bool = Field(default=False)


class User(Timestamped, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(index=True, foreign_key="workspaces.id")

    email: str = Field(index=True, unique=True, max_length=255)
    full_name: str = Field(default="", max_length=255)
    password_hash: str = Field(max_length=255)

    role: str = Field(default="owner", max_length=50)
    is_admin: bool = Field(default=False)
    is_active: bool = Field(default=True)
    is_verified: bool = Field(default=False)
    last_login_at: Optional[datetime] = Field(default=None)
    failed_login_attempts: int = Field(default=0)
    locked_until: Optional[datetime] = Field(default=None)


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

    seller_motivation_score: Optional[int] = Field(default=None)
    seller_timeline_score: Optional[int] = Field(default=None)
    seller_openness_score: Optional[int] = Field(default=None)
    price_realism_score: Optional[int] = Field(default=None)
    readiness_score: Optional[int] = Field(default=None, index=True)

    marketplace_eligible: bool = Field(default=False)
    marketplace_last_synced_at: Optional[datetime] = Field(default=None)


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

    marketplace_publishable: bool = Field(default=False)
    royalty_enabled: bool = Field(default=False)
    royalty_mode: str = Field(default="license", max_length=50)


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

    marketplace_feed_enabled: bool = Field(default=False)
    marketplace_min_readiness_score: int = Field(default=75)
    marketplace_auto_publish_booked: bool = Field(default=True)
    marketplace_auto_publish_qualified: bool = Field(default=False)
    partner_revenue_share_pct: float = Field(default=0.0)

    # Voice type for AI calls
    # "platform" = default platform AI voice
    # "partner"  = voice partner from marketplace
    # "clone"    = agent's own cloned voice
    voice_type: Optional[str] = Field(default="platform", max_length=50)
    agent_voice_clone_id: Optional[int] = Field(default=None, foreign_key="agentvoiceclone.id")

    pause_reason: Optional[str] = Field(default=None, max_length=100)

    send_profile_sms_after_call: bool = Field(default=True)
    profile_sms_delay_minutes: int = Field(default=10)

    is_admin_campaign: bool = Field(default=False)
    marketplace_listings_count: int = Field(default=0)
    marketplace_revenue_cents: int = Field(default=0)


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

    marketplace_candidate: bool = Field(default=False)
    marketplace_synced_at: Optional[datetime] = Field(default=None)


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

    seller_can_cancel: bool = Field(default=True)
    canceled_by_homeowner: bool = Field(default=False, index=True)
    canceled_at: Optional[datetime] = Field(default=None)
    cancellation_reason: Optional[str] = Field(default=None, max_length=500)

    marketplace_eligible: bool = Field(default=False)
    marketplace_last_synced_at: Optional[datetime] = Field(default=None)


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


# -----------------------------
# Marketplace economy
# -----------------------------

class MarketplaceListing(Timestamped, table=True):
    __tablename__ = "marketplace_listings"

    id: Optional[int] = Field(default=None, primary_key=True)

    workspace_id: int = Field(index=True, foreign_key="workspaces.id")
    source_workspace_id: Optional[int] = Field(default=None, index=True, foreign_key="workspaces.id")

    source_type: str = Field(default="campaign", index=True, max_length=80)
    source_label: Optional[str] = Field(default=None, max_length=255)

    lead_id: Optional[int] = Field(default=None, index=True, foreign_key="leads.id")
    appointment_id: Optional[int] = Field(default=None, index=True, foreign_key="appointments.id")
    calllog_id: Optional[int] = Field(default=None, index=True, foreign_key="calllogs.id")
    campaign_id: Optional[int] = Field(default=None, index=True, foreign_key="campaigns.id")
    pathway_id: Optional[int] = Field(default=None, index=True, foreign_key="pathways.id")

    listing_type: str = Field(default="qualified_lead", index=True, max_length=80)
    status: str = Field(default="available", index=True, max_length=80)
    visibility: str = Field(default="public", index=True, max_length=50)

    title: str = Field(default="Seller Opportunity", max_length=255)
    summary: Optional[str] = Field(default=None, max_length=5000)
    notes: Optional[str] = Field(default=None, max_length=5000)

    homeowner_name: Optional[str] = Field(default=None, max_length=255)
    phone: Optional[str] = Field(default=None, max_length=40)
    email: Optional[str] = Field(default=None, max_length=255)

    property_address: Optional[str] = Field(default=None, max_length=500)
    city: Optional[str] = Field(default=None, max_length=120)
    state: Optional[str] = Field(default=None, max_length=50)
    postal_code: Optional[str] = Field(default=None, index=True, max_length=20)
    borough: Optional[str] = Field(default=None, index=True, max_length=120)
    territory_key: Optional[str] = Field(default=None, index=True, max_length=120)

    latitude: Optional[float] = Field(default=None)
    longitude: Optional[float] = Field(default=None)

    appointment_time_iso: Optional[str] = Field(default=None, max_length=100)

    seller_motivation_score: Optional[int] = Field(default=None)
    seller_timeline_score: Optional[int] = Field(default=None)
    seller_openness_score: Optional[int] = Field(default=None)
    price_realism_score: Optional[int] = Field(default=None)
    readiness_score: int = Field(default=0, index=True)

    pricing_tier: str = Field(default="standard", max_length=80)
    pricing_formula_version: str = Field(default="v1", max_length=50)
    base_price_cents: int = Field(default=0)
    final_price_cents: int = Field(default=0, index=True)
    currency: str = Field(default="USD", max_length=10)

    seller_can_cancel: bool = Field(default=True)
    homeowner_cancellation_risk: str = Field(default="standard", max_length=50)
    cancellation_disclosure_shown: bool = Field(default=False)

    is_featured: bool = Field(default=False)
    is_bad_lead_protected: bool = Field(default=True)
    bad_lead_window_hours: int = Field(default=24)

    published_at: Optional[datetime] = Field(default=None, index=True)
    reserved_at: Optional[datetime] = Field(default=None)
    purchased_at: Optional[datetime] = Field(default=None)
    expires_at: Optional[datetime] = Field(default=None)

    buyer_user_id: Optional[int] = Field(default=None, index=True, foreign_key="users.id")
    buyer_workspace_id: Optional[int] = Field(default=None, index=True, foreign_key="workspaces.id")

    raw_source_json: Optional[str] = Field(default=None)
    extracted_json: Optional[str] = Field(default=None)
    pricing_breakdown_json: Optional[str] = Field(default=None)


class MarketplacePurchase(Timestamped, table=True):
    __tablename__ = "marketplace_purchases"

    id: Optional[int] = Field(default=None, primary_key=True)

    listing_id: int = Field(index=True, foreign_key="marketplace_listings.id")
    buyer_user_id: int = Field(index=True, foreign_key="users.id")
    buyer_workspace_id: int = Field(index=True, foreign_key="workspaces.id")

    seller_workspace_id: Optional[int] = Field(default=None, index=True, foreign_key="workspaces.id")
    source_workspace_id: Optional[int] = Field(default=None, index=True, foreign_key="workspaces.id")

    status: str = Field(default="purchased", index=True, max_length=80)
    payment_status: str = Field(default="pending", index=True, max_length=80)

    price_paid_cents: int = Field(default=0)
    buyer_fee_cents: int = Field(default=0)
    platform_fee_cents: int = Field(default=0)
    partner_payout_cents: int = Field(default=0)
    script_royalty_cents: int = Field(default=0)
    voice_royalty_cents: int = Field(default=0)
    currency: str = Field(default="USD", max_length=10)

    agreement_accepted: bool = Field(default=False)
    agreement_version: str = Field(default="marketplace-v1", max_length=80)
    cancellation_disclosure_accepted: bool = Field(default=False)
    no_refund_disclosure_accepted: bool = Field(default=False)

    is_refundable: bool = Field(default=False)
    refund_status: str = Field(default="not_requested", index=True, max_length=80)
    refund_reason: Optional[str] = Field(default=None, max_length=500)
    refunded_amount_cents: int = Field(default=0)

    duplicate_claimed: bool = Field(default=False)
    invalid_claimed: bool = Field(default=False)
    materially_defective_claimed: bool = Field(default=False)

    homeowner_canceled_after_purchase: bool = Field(default=False)
    homeowner_canceled_at: Optional[datetime] = Field(default=None)

    stripe_checkout_session_id: Optional[str] = Field(default=None, index=True, max_length=255)
    stripe_payment_intent_id: Optional[str] = Field(default=None, index=True, max_length=255)

    purchase_notes: Optional[str] = Field(default=None, max_length=5000)
    internal_review_json: Optional[str] = Field(default=None)


class SavedTerritory(Timestamped, table=True):
    __tablename__ = "saved_territories"

    id: Optional[int] = Field(default=None, primary_key=True)

    workspace_id: int = Field(index=True, foreign_key="workspaces.id")
    user_id: Optional[int] = Field(default=None, index=True, foreign_key="users.id")

    name: str = Field(max_length=255)
    territory_key: str = Field(index=True, max_length=120)

    city: Optional[str] = Field(default=None, max_length=120)
    state: Optional[str] = Field(default=None, max_length=50)
    borough: Optional[str] = Field(default=None, max_length=120)
    postal_code: Optional[str] = Field(default=None, max_length=20)

    min_readiness_score: int = Field(default=0)
    listing_type_filter: Optional[str] = Field(default=None, max_length=80)
    max_price_cents: Optional[int] = Field(default=None)
    notify_on_new_inventory: bool = Field(default=True)

    filters_json: Optional[str] = Field(default=None)


class PartnerPayout(Timestamped, table=True):
    __tablename__ = "partner_payouts"

    id: Optional[int] = Field(default=None, primary_key=True)

    workspace_id: int = Field(index=True, foreign_key="workspaces.id")
    purchase_id: Optional[int] = Field(default=None, index=True, foreign_key="marketplace_purchases.id")
    listing_id: Optional[int] = Field(default=None, index=True, foreign_key="marketplace_listings.id")

    payout_type: str = Field(default="partner_inventory", index=True, max_length=80)
    recipient_workspace_id: Optional[int] = Field(default=None, index=True, foreign_key="workspaces.id")
    recipient_user_id: Optional[int] = Field(default=None, index=True, foreign_key="users.id")

    gross_amount_cents: int = Field(default=0)
    net_amount_cents: int = Field(default=0)
    platform_fee_cents: int = Field(default=0)
    currency: str = Field(default="USD", max_length=10)

    status: str = Field(default="pending", index=True, max_length=80)
    payout_provider: Optional[str] = Field(default=None, max_length=80)
    payout_reference: Optional[str] = Field(default=None, index=True, max_length=255)
    payout_notes: Optional[str] = Field(default=None, max_length=2000)


class ScriptAsset(Timestamped, table=True):
    __tablename__ = "script_assets"

    id: Optional[int] = Field(default=None, primary_key=True)

    workspace_id: int = Field(index=True, foreign_key="workspaces.id")
    creator_user_id: Optional[int] = Field(default=None, index=True, foreign_key="users.id")
    pathway_id: Optional[int] = Field(default=None, index=True, foreign_key="pathways.id")

    name: str = Field(max_length=255)
    slug: str = Field(index=True, unique=True, max_length=255)
    description: Optional[str] = Field(default=None, max_length=4000)

    category: str = Field(default="expired_listing", max_length=100)
    status: str = Field(default="draft", index=True, max_length=80)
    listing_price_cents: int = Field(default=0)
    subscription_price_cents: int = Field(default=0)
    royalty_rate_pct: float = Field(default=0.0)

    is_marketplace_visible: bool = Field(default=False)
    is_featured: bool = Field(default=False)

    validation_score: Optional[int] = Field(default=None)
    compatibility_score: Optional[int] = Field(default=None)
    booking_readiness_score: Optional[int] = Field(default=None)

    sample_script_text: Optional[str] = Field(default=None)
    tags_csv: Optional[str] = Field(default=None, max_length=1000)
    metadata_json: Optional[str] = Field(default=None)


class VoicePartnerProfile(Timestamped, table=True):
    __tablename__ = "voice_partner_profiles"

    id: Optional[int] = Field(default=None, primary_key=True)

    workspace_id: int = Field(index=True, foreign_key="workspaces.id")
    user_id: Optional[int] = Field(default=None, index=True, foreign_key="users.id")

    display_name: str = Field(max_length=255)
    status: str = Field(default="pending_review", index=True, max_length=80)

    approved_script_asset_id: Optional[int] = Field(default=None, index=True, foreign_key="script_assets.id")
    approved_pathway_id: Optional[int] = Field(default=None, index=True, foreign_key="pathways.id")

    voice_provider: str = Field(default="elevenlabs", max_length=80)
    voice_external_id: Optional[str] = Field(default=None, index=True, max_length=255)

    royalty_model: str = Field(default="per_booked_appointment", max_length=80)
    royalty_rate_pct: float = Field(default=0.0)
    royalty_flat_cents: int = Field(default=0)

    contract_accepted: bool = Field(default=False)
    contract_version: Optional[str] = Field(default=None, max_length=80)
    can_a2z_run_campaigns: bool = Field(default=False)
    can_publish_in_marketplace: bool = Field(default=False)

    performance_notes: Optional[str] = Field(default=None, max_length=4000)
    metadata_json: Optional[str] = Field(default=None)

    # -----------------------------
# Top Agent Network
# -----------------------------

class AgentProfile(Timestamped, table=True):
    __tablename__ = "agent_profiles"

    id: Optional[int] = Field(default=None, primary_key=True)

    workspace_id: int = Field(index=True, foreign_key="workspaces.id")
    user_id: Optional[int] = Field(default=None, index=True, foreign_key="users.id")

    # Public identity
    full_name: str = Field(default="", max_length=255)
    brokerage: Optional[str] = Field(default=None, max_length=255)
    headline: Optional[str] = Field(default=None, max_length=255)
    bio: Optional[str] = Field(default=None, max_length=5000)
    photo_url: Optional[str] = Field(default=None, max_length=500)

    # Specialization
    specialty: Optional[str] = Field(default=None, max_length=255)
    languages: Optional[str] = Field(default=None, max_length=255)
    years_experience: Optional[int] = Field(default=None)

    # Contact
    phone: Optional[str] = Field(default=None, max_length=40)
    email: Optional[str] = Field(default=None, max_length=255)
    website: Optional[str] = Field(default=None, max_length=500)

    # Primary territory
    primary_territory_key: Optional[str] = Field(default=None, index=True, max_length=120)
    primary_territory_name: Optional[str] = Field(default=None, max_length=255)

    # Visibility & ranking
    is_public: bool = Field(default=True)
    placement_tier: str = Field(default="standard", max_length=50)  # standard, premium, elite
    ranking_score: int = Field(default=0, index=True)
    profile_completeness: int = Field(default=0)

    # Verification
    is_verified: bool = Field(default=False)
    verified_at: Optional[datetime] = Field(default=None)

    # Featured placement
    is_featured: bool = Field(default=False, index=True)
    featured_until: Optional[datetime] = Field(default=None)

    metadata_json: Optional[str] = Field(default=None)


class AgentTerritory(Timestamped, table=True):
    __tablename__ = "agent_territories"

    id: Optional[int] = Field(default=None, primary_key=True)

    workspace_id: int = Field(index=True, foreign_key="workspaces.id")
    user_id: Optional[int] = Field(default=None, index=True, foreign_key="users.id")
    agent_profile_id: Optional[int] = Field(default=None, index=True, foreign_key="agent_profiles.id")

    # Territory identity — supports city, zip, county, borough, state
    territory_key: str = Field(index=True, max_length=120)   # e.g. "city:bronx-ny", "zip:10451", "county:westchester-ny"
    territory_name: str = Field(max_length=255)               # e.g. "Bronx, NY"
    territory_type: str = Field(default="city", max_length=50) # city, zip, county, borough, state

    city: Optional[str] = Field(default=None, max_length=120)
    state: Optional[str] = Field(default=None, max_length=50)
    county: Optional[str] = Field(default=None, max_length=120)
    borough: Optional[str] = Field(default=None, max_length=120)
    postal_code: Optional[str] = Field(default=None, max_length=20)
    country: str = Field(default="US", max_length=10)

    # Status
    is_primary: bool = Field(default=False)
    is_active: bool = Field(default=True, index=True)

    # Placement & ranking within this territory
    placement_tier: str = Field(default="standard", max_length=50)  # standard, boosted, featured
    ranking_score: int = Field(default=0, index=True)
    territory_rank: Optional[int] = Field(default=None)

    # Placement active window
    placement_active_until: Optional[datetime] = Field(default=None)


class FeaturedPlacement(Timestamped, table=True):
    __tablename__ = "featured_placements"

    id: Optional[int] = Field(default=None, primary_key=True)

    workspace_id: int = Field(index=True, foreign_key="workspaces.id")
    user_id: Optional[int] = Field(default=None, index=True, foreign_key="users.id")
    agent_profile_id: Optional[int] = Field(default=None, index=True, foreign_key="agent_profiles.id")
    agent_territory_id: Optional[int] = Field(default=None, index=True, foreign_key="agent_territories.id")

    territory_key: str = Field(index=True, max_length=120)
    territory_name: str = Field(max_length=255)

    # Package
    placement_type: str = Field(default="standard", max_length=50)  # standard, boosted, featured, elite
    package_name: Optional[str] = Field(default=None, max_length=255)

    # Pricing
    amount_cents: int = Field(default=0)
    currency: str = Field(default="USD", max_length=10)
    billing_period: str = Field(default="monthly", max_length=50)

    # Status
    status: str = Field(default="active", index=True, max_length=50)  # active, expired, cancelled, pending

    # Duration
    starts_at: Optional[datetime] = Field(default=None)
    ends_at: Optional[datetime] = Field(default=None, index=True)

    # Payment
    stripe_subscription_id: Optional[str] = Field(default=None, max_length=255)
    stripe_payment_intent_id: Optional[str] = Field(default=None, max_length=255)

    notes: Optional[str] = Field(default=None, max_length=2000)


class PublicTrustSource(Timestamped, table=True):
    __tablename__ = "public_trust_sources"

    id: Optional[int] = Field(default=None, primary_key=True)

    workspace_id: int = Field(index=True, foreign_key="workspaces.id")
    user_id: Optional[int] = Field(default=None, index=True, foreign_key="users.id")
    agent_profile_id: Optional[int] = Field(default=None, index=True, foreign_key="agent_profiles.id")

    # Source type: google, zillow, brokerage, realtor, public_listings
    source_type: str = Field(index=True, max_length=80)
    source_label: str = Field(default="", max_length=120)  # e.g. "Google Business"
    source_url: Optional[str] = Field(default=None, max_length=1000)

    # Verification
    is_verified: bool = Field(default=False)
    verified_at: Optional[datetime] = Field(default=None)

    # Scraped / ingested data (future)
    review_count: Optional[int] = Field(default=None)
    average_rating: Optional[float] = Field(default=None)
    raw_json: Optional[str] = Field(default=None)
    last_synced_at: Optional[datetime] = Field(default=None)


# -----------------------------
# Auth: Password Reset
# -----------------------------

class PasswordResetToken(SQLModel, table=True):
    __tablename__ = "passwordresettoken"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id")
    token: str = Field(index=True, unique=True)
    expires_at: datetime = Field()
    used: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# -----------------------------
# Notifications
# -----------------------------

class Notification(SQLModel, table=True):
    __tablename__ = "notification"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    type: str = Field(default="info")
    title: str
    body: str
    link: Optional[str] = Field(default=None)
    read: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# -----------------------------
# Auth: Refresh Tokens
# -----------------------------

class RefreshToken(SQLModel, table=True):
    __tablename__ = "refreshtoken"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    token: str = Field(index=True, unique=True)
    expires_at: datetime
    revoked: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)


# -----------------------------
# Agent Voice Clone
# -----------------------------

class AgentVoiceClone(SQLModel, table=True):
    __tablename__ = "agentvoiceclone"

    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)

    elevenlabs_voice_id: Optional[str] = Field(default=None, max_length=255)
    display_name: str = Field(default="My Voice", max_length=255)

    # pending → processing → active → failed → rejected → deleted
    status: str = Field(default="pending", max_length=50, index=True)

    sample_count: int = Field(default=0)
    quality_score: Optional[float] = Field(default=None)
    rejection_reason: Optional[str] = Field(default=None, max_length=1000)

    is_active: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)

    # Sharing & royalties
    is_shared: bool = Field(default=False)
    display_name_public: Optional[str] = Field(default=None, max_length=255)
    royalty_rate_cents_per_min: int = Field(default=1)
    total_minutes_used: int = Field(default=0)
    total_royalties_earned_cents: int = Field(default=0)


# -----------------------------
# Team / Multi-User
# -----------------------------

class TeamInvite(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    workspace_id: int = Field(foreign_key="workspaces.id")
    invited_email: str
    role: str = Field(default="member")  # "admin" | "member"
    token: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    accepted: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: Optional[datetime] = Field(default=None)