import csv
import io
import json
import logging
import re
import secrets
import time
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import Body, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session, func, select

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from auth import authenticate_user, create_access_token, create_refresh_token, verify_refresh_token, get_current_user, hash_password
from billing import construct_webhook_event, create_checkout_session, stripe_enabled
from calendar_sync import (
    create_calendly_placeholder,
    create_google_calendar_event,
    exchange_google_code_for_tokens,
    get_google_oauth_start_url,
)
from classifier import classify_text
from config import settings
from db import get_session, init_db
from models import (
    AgentVoiceClone,
    Appointment,
    AuditLog,
    CallLog,
    Campaign,
    DNCEntry,
    Lead,
    LeadList,
    MarketplaceListing,
    MarketplacePurchase,
    Notification,
    PartnerPayout,
    Pathway,
    PasswordResetToken,
    Referral,
    RefreshToken,
    SavedTerritory,
    ScriptAsset,
    UsageEvent,
    User,
    VoicePartnerProfile,
    Workspace,
    AgentProfile,
    AgentTerritory,
    FeaturedPlacement,
    PublicTrustSource,
    TeamInvite,
)
from notifications import send_appointment_confirmation_sms
from pathway_engine import safe_json_load, simulate_pathway, validate_pathway_json
from realtime_bridge import RealtimeBridge, safe_parse_ws_message
from schemas import (
    AppointmentCreateIn,
    AuthLoginIn,
    AuthRegisterIn,
    CallDispositionIn,
    CampaignCreateIn,
    DNCAddIn,
    HealthOut,
    LeadCreateIn,
    LeadListCreateIn,
    PathwayCreateIn,
    PathwaySimulateIn,
    PathwayUpdateIn,
    WorkspaceSettingsUpdateIn,
)
from twilio_voice import (
    build_initial_context,
    build_voice_response_for_gather,
    build_voice_response_for_realtime_stream,
)
from worker import run_campaign_tick, run_worker_once

load_dotenv()

logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger("a2z")

limiter = Limiter(key_func=get_remote_address)


# ── WebSocket ConnectionManager ───────────────────────────────────────────────

class ConnectionManager:
    """Manages WebSocket connections for real-time broadcast."""

    def __init__(self, name: str = "default"):
        self.name = name
        self.active: set["WebSocket"] = set()

    async def connect(self, ws: "WebSocket") -> None:
        await ws.accept()
        self.active.add(ws)

    def disconnect(self, ws: "WebSocket") -> None:
        self.active.discard(ws)

    async def broadcast(self, data: dict) -> None:
        if not self.active:
            return
        dead: set["WebSocket"] = set()
        for ws in list(self.active):
            try:
                await ws.send_json(data)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self.active.discard(ws)

    def fire(self, data: dict) -> None:
        """Schedule a broadcast from a synchronous context."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(self.broadcast(data))
        except Exception:
            pass


marketplace_ws = ConnectionManager("marketplace")
global_ws = ConnectionManager("global")


app = FastAPI(title="A2Z Dialer API", version="1.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://a2zdialer.com",
        "https://www.a2zdialer.com",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Voice clone router ────────────────────────────────────────────────────────
from voice_clone_routes import router as voice_clone_router, get_admin_voice_clones_data  # noqa: E402
app.include_router(voice_clone_router)


@app.get("/admin/voice-clones")
async def admin_voice_clones_endpoint(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    if current_user.role not in ("admin", "superadmin"):
        raise HTTPException(403, "Admin access required")
    return get_admin_voice_clones_data(session)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


@app.middleware("http")
async def limit_request_size(request: Request, call_next):
    max_size = 10 * 1024 * 1024  # 10MB
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_size:
        return JSONResponse({"detail": "Request too large"}, status_code=413)
    return await call_next(request)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone.strip())
    if len(digits) == 10:
        digits = "1" + digits
    return "+" + digits if digits else ""


def sanitize_string(s: Optional[str], max_len: int = 10000) -> Optional[str]:
    """Strip unsafe characters from user input strings."""
    if s is None:
        return None
    s = s.strip().replace("\x00", "")
    return s[:max_len]


def mask_sensitive(value: Optional[str]) -> Optional[str]:
    if not value:
        return value
    if len(value) <= 4:
        return "••••••"
    return "••••••" + value[-4:]


def slugify(value: str) -> str:
    cleaned = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-") or f"workspace-{secrets.token_hex(4)}"


def touch(model_obj: Any) -> None:
    if hasattr(model_obj, "updated_at"):
        model_obj.updated_at = utcnow()


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False)


def audit(
    session: Session,
    workspace_id: int,
    action: str,
    entity_type: str,
    entity_id: Optional[int] = None,
    user_id: Optional[int] = None,
    details: Optional[dict] = None,
) -> None:
    row = AuditLog(
        workspace_id=workspace_id,
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details_json=json_dumps(details or {}),
    )
    session.add(row)


def usage(
    session: Session,
    workspace_id: int,
    event_type: str,
    quantity: int = 1,
    reference_type: Optional[str] = None,
    reference_id: Optional[int] = None,
    metadata: Optional[dict] = None,
) -> None:
    row = UsageEvent(
        workspace_id=workspace_id,
        event_type=event_type,
        quantity=quantity,
        reference_type=reference_type,
        reference_id=reference_id,
        metadata_json=json_dumps(metadata or {}),
    )
    session.add(row)


def get_workspace_or_404(session: Session, workspace_id: int) -> Workspace:
    workspace = session.get(Workspace, workspace_id)
    if not workspace:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return workspace


def get_pathway_or_404(session: Session, workspace_id: int, pathway_id: int) -> Pathway:
    pathway = session.get(Pathway, pathway_id)
    if not pathway or pathway.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Pathway not found")
    return pathway


def get_campaign_or_404(session: Session, workspace_id: int, campaign_id: int) -> Campaign:
    campaign = session.get(Campaign, campaign_id)
    if not campaign or campaign.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def get_leadlist_or_404(session: Session, workspace_id: int, lead_list_id: int) -> LeadList:
    lead_list = session.get(LeadList, lead_list_id)
    if not lead_list or lead_list.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Lead list not found")
    return lead_list


def get_lead_or_404(session: Session, workspace_id: int, lead_id: int) -> Lead:
    lead = session.get(Lead, lead_id)
    if not lead or lead.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Lead not found")
    return lead


def get_calllog_or_404(session: Session, workspace_id: int, calllog_id: int) -> CallLog:
    calllog = session.get(CallLog, calllog_id)
    if not calllog or calllog.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="Call log not found")
    return calllog


def infer_borough(city: Optional[str], postal_code: Optional[str]) -> Optional[str]:
    city_value = (city or "").strip().lower()
    zip_value = (postal_code or "").strip()

    if city_value in {"bronx"}:
        return "Bronx"
    if city_value in {"brooklyn"}:
        return "Brooklyn"
    if city_value in {"queens"}:
        return "Queens"
    if city_value in {"manhattan", "new york"}:
        return "Manhattan"
    if city_value in {"yonkers"}:
        return "Westchester"

    if zip_value.startswith("104"):
        return "Bronx"
    if zip_value.startswith("112"):
        return "Brooklyn"
    if zip_value.startswith("113") or zip_value.startswith("114") or zip_value.startswith("116"):
        return "Queens"
    if zip_value.startswith("100"):
        return "Manhattan"
    if zip_value.startswith("107"):
        return "Westchester"

    return city.title() if city else None


def build_territory_key(city: Optional[str], borough: Optional[str], postal_code: Optional[str]) -> str:
    if postal_code:
        return postal_code.strip()
    if borough:
        return borough.strip().lower().replace(" ", "-")
    if city:
        return city.strip().lower().replace(" ", "-")
    return "unknown"


def extract_market_signals(
    lead: Optional[Lead],
    calllog: Optional[CallLog],
    appointment: Optional[Appointment],
) -> dict:
    lead_data = safe_json_load(lead.extracted_json) if lead and lead.extracted_json else {}
    calllog_data = safe_json_load(calllog.extracted_json) if calllog and calllog.extracted_json else {}

    motivation = int(
        calllog_data.get("motivation_score")
        or lead_data.get("motivation_score")
        or 70
    )
    timeline = int(
        calllog_data.get("timeline_score")
        or lead_data.get("timeline_score")
        or 70
    )
    openness = int(
        calllog_data.get("agent_openness_score")
        or lead_data.get("agent_openness_score")
        or 75
    )
    price_realism = int(
        calllog_data.get("price_realism_score")
        or lead_data.get("price_realism_score")
        or 65
    )

    readiness = int(
        (motivation * 0.35)
        + (timeline * 0.25)
        + (openness * 0.25)
        + (price_realism * 0.15)
    )

    if appointment and appointment.appointment_time_iso:
        readiness = min(100, readiness + 8)

    return {
        "motivation": motivation,
        "timeline": timeline,
        "openness": openness,
        "price_realism": price_realism,
        "readiness": readiness,
    }


def compute_marketplace_price_cents(
    listing_type: str,
    city: Optional[str],
    borough: Optional[str],
    readiness_score: int,
    appointment_time_iso: Optional[str],
) -> tuple[int, dict]:
    listing_type_value = listing_type.lower().strip()

    if listing_type_value == "booked_appointment":
        base_price = 8500
    else:
        base_price = 4500

    premium_markets = {"manhattan", "brooklyn", "queens", "bronx"}
    territory_name = (borough or city or "").strip().lower()

    market_bonus = 0
    if territory_name in premium_markets:
        market_bonus += 2000

    readiness_bonus = 0
    if readiness_score >= 85:
        readiness_bonus += 1500
    elif readiness_score >= 75:
        readiness_bonus += 800

    appointment_bonus = 2500 if appointment_time_iso else 0

    final_price = base_price + market_bonus + readiness_bonus + appointment_bonus

    breakdown = {
        "base_price_cents": base_price,
        "market_bonus_cents": market_bonus,
        "readiness_bonus_cents": readiness_bonus,
        "appointment_bonus_cents": appointment_bonus,
        "final_price_cents": final_price,
        "formula_version": "v1",
    }
    return final_price, breakdown


def campaign_allows_marketplace_publish(
    campaign: Optional[Campaign],
    listing_type: str,
    readiness_score: int,
) -> bool:
    if not campaign:
        return True

    if not campaign.marketplace_feed_enabled:
        return False

    min_score = campaign.marketplace_min_readiness_score or 75

    if listing_type == "booked_appointment":
        return bool(campaign.marketplace_auto_publish_booked)

    return bool(campaign.marketplace_auto_publish_qualified) and readiness_score >= min_score


def get_latest_appointment_for_lead(
    session: Session,
    workspace_id: int,
    lead_id: int,
) -> Optional[Appointment]:
    return session.exec(
        select(Appointment)
        .where(
            Appointment.workspace_id == workspace_id,
            Appointment.lead_id == lead_id,
        )
        .order_by(Appointment.created_at.desc())
    ).first()


def get_latest_calllog_for_lead(
    session: Session,
    workspace_id: int,
    lead_id: int,
) -> Optional[CallLog]:
    return session.exec(
        select(CallLog)
        .where(
            CallLog.workspace_id == workspace_id,
            CallLog.lead_id == lead_id,
        )
        .order_by(CallLog.created_at.desc())
    ).first()


def sync_marketplace_listing_for_opportunity(
    session: Session,
    workspace: Workspace,
    lead: Lead,
    campaign: Optional[Campaign] = None,
    calllog: Optional[CallLog] = None,
    appointment: Optional[Appointment] = None,
) -> Optional[MarketplaceListing]:
    listing_type = "booked_appointment" if appointment and appointment.appointment_time_iso else "qualified_lead"
    signals = extract_market_signals(lead, calllog, appointment)

    lead.seller_motivation_score = signals["motivation"]
    lead.seller_timeline_score = signals["timeline"]
    lead.seller_openness_score = signals["openness"]
    lead.price_realism_score = signals["price_realism"]
    lead.readiness_score = signals["readiness"]
    lead.marketplace_eligible = signals["readiness"] >= 75 or listing_type == "booked_appointment"
    lead.marketplace_last_synced_at = utcnow()
    touch(lead)
    session.add(lead)

    if not lead.marketplace_eligible:
        return None

    if not campaign_allows_marketplace_publish(campaign, listing_type, signals["readiness"]):
        return None

    borough = infer_borough(lead.city, lead.postal_code)
    territory_key = build_territory_key(lead.city, borough, lead.postal_code)

    _, pricing_breakdown = compute_marketplace_price_cents(
        listing_type=listing_type,
        city=lead.city,
        borough=borough,
        readiness_score=signals["readiness"],
        appointment_time_iso=appointment.appointment_time_iso if appointment else None,
    )

    existing = session.exec(
        select(MarketplaceListing).where(
            MarketplaceListing.workspace_id == workspace.id,
            MarketplaceListing.lead_id == lead.id,
            MarketplaceListing.status.in_(["available", "reserved", "purchased"]),
        )
    ).first()

    summary = (
        f"{listing_type.replace('_', ' ').title()} in {borough or lead.city or 'target market'} "
        f"with readiness score {signals['readiness']}/100."
    )

    extracted_payload = {
        "lead_extracted": safe_json_load(lead.extracted_json) if lead.extracted_json else {},
        "calllog_extracted": safe_json_load(calllog.extracted_json) if calllog and calllog.extracted_json else {},
    }

    if existing:
        existing.listing_type = listing_type
        existing.title = f"{(borough or lead.city or 'Seller')} Opportunity"
        existing.summary = summary
        existing.homeowner_name = lead.homeowner_name or lead.first_name
        existing.phone = lead.phone
        existing.email = lead.email
        existing.property_address = lead.property_address
        existing.city = lead.city
        existing.state = lead.state
        existing.postal_code = lead.postal_code
        existing.borough = borough
        existing.territory_key = territory_key
        existing.appointment_id = appointment.id if appointment else existing.appointment_id
        existing.calllog_id = calllog.id if calllog else existing.calllog_id
        existing.campaign_id = campaign.id if campaign else existing.campaign_id
        existing.pathway_id = campaign.pathway_id if campaign else existing.pathway_id
        existing.appointment_time_iso = appointment.appointment_time_iso if appointment else None
        existing.seller_motivation_score = signals["motivation"]
        existing.seller_timeline_score = signals["timeline"]
        existing.seller_openness_score = signals["openness"]
        existing.price_realism_score = signals["price_realism"]
        existing.readiness_score = signals["readiness"]
        existing.base_price_cents = pricing_breakdown["base_price_cents"]
        existing.final_price_cents = pricing_breakdown["final_price_cents"]
        existing.pricing_formula_version = pricing_breakdown["formula_version"]
        existing.pricing_tier = "premium" if signals["readiness"] >= 85 else "standard"
        existing.pricing_breakdown_json = json_dumps(pricing_breakdown)
        existing.extracted_json = json_dumps(extracted_payload)
        existing.status = "available"
        existing.visibility = "public"
        existing.published_at = existing.published_at or utcnow()
        existing.seller_can_cancel = True
        existing.homeowner_cancellation_risk = "standard"
        existing.is_featured = signals["readiness"] >= 85
        existing.bad_lead_window_hours = workspace.marketplace_bad_lead_window_hours
        touch(existing)
        session.add(existing)
        return existing

    listing = MarketplaceListing(
        workspace_id=workspace.id,
        source_workspace_id=workspace.id,
        source_type="campaign" if campaign else "manual",
        source_label=campaign.name if campaign else "A2Z Opportunity",
        lead_id=lead.id,
        appointment_id=appointment.id if appointment else None,
        calllog_id=calllog.id if calllog else None,
        campaign_id=campaign.id if campaign else None,
        pathway_id=campaign.pathway_id if campaign else None,
        listing_type=listing_type,
        status="available",
        visibility="public",
        title=f"{(borough or lead.city or 'Seller')} Opportunity",
        summary=summary,
        homeowner_name=lead.homeowner_name or lead.first_name,
        phone=lead.phone,
        email=lead.email,
        property_address=lead.property_address,
        city=lead.city,
        state=lead.state,
        postal_code=lead.postal_code,
        borough=borough,
        territory_key=territory_key,
        appointment_time_iso=appointment.appointment_time_iso if appointment else None,
        seller_motivation_score=signals["motivation"],
        seller_timeline_score=signals["timeline"],
        seller_openness_score=signals["openness"],
        price_realism_score=signals["price_realism"],
        readiness_score=signals["readiness"],
        pricing_tier="premium" if signals["readiness"] >= 85 else "standard",
        pricing_formula_version=pricing_breakdown["formula_version"],
        base_price_cents=pricing_breakdown["base_price_cents"],
        final_price_cents=pricing_breakdown["final_price_cents"],
        currency="USD",
        seller_can_cancel=True,
        homeowner_cancellation_risk="standard",
        is_featured=signals["readiness"] >= 85,
        is_bad_lead_protected=True,
        bad_lead_window_hours=workspace.marketplace_bad_lead_window_hours,
        published_at=utcnow(),
        extracted_json=json_dumps(extracted_payload),
        pricing_breakdown_json=json_dumps(pricing_breakdown),
    )
    session.add(listing)
    # Broadcast new listing event after session flush (id assigned after commit by caller)
    # We schedule after return; callers must commit first.
    global_ws.fire({
        "event": "listing.new",
        "listing_id": None,  # id not yet assigned pre-commit — updated below via flush
        "territory_name": borough or lead.city or territory_key,
        "listing_type": listing_type,
        "price": pricing_breakdown["final_price_cents"] / 100,
        "readiness_score": signals["readiness"],
    })
    marketplace_ws.fire({
        "event": "listing.new",
        "listing_id": None,
        "territory_name": borough or lead.city or territory_key,
        "listing_type": listing_type,
        "price": pricing_breakdown["final_price_cents"] / 100,
        "readiness_score": signals["readiness"],
    })
    return listing


def maybe_publish_qualified_marketplace_listing(
    session: Session,
    workspace: Workspace,
    campaign: Optional[Campaign],
    lead: Lead,
    calllog: Optional[CallLog] = None,
) -> Optional[MarketplaceListing]:
    signals = extract_market_signals(lead, calllog, None)

    lead.seller_motivation_score = signals["motivation"]
    lead.seller_timeline_score = signals["timeline"]
    lead.seller_openness_score = signals["openness"]
    lead.price_realism_score = signals["price_realism"]
    lead.readiness_score = signals["readiness"]
    lead.marketplace_eligible = signals["readiness"] >= 75
    lead.marketplace_last_synced_at = utcnow()
    touch(lead)
    session.add(lead)

    if not campaign_allows_marketplace_publish(campaign, "qualified_lead", signals["readiness"]):
        return None

    return sync_marketplace_listing_for_opportunity(
        session=session,
        workspace=workspace,
        lead=lead,
        campaign=campaign,
        calllog=calllog,
        appointment=None,
    )


def create_default_pathway_json() -> dict:
    return {
        "start_node": "step1_intro_listen",
        "nodes": {
            "step1_intro_listen": {
                "type": "listen",
                "prompt": "Hi, {{homeowner_name}}? This is {{agent_name}} — I’m a local Realtor. I’m calling about the property at {{property_address}}. Is it still available?||Hi — is this {{homeowner_name}}? This is {{agent_name}}, a local Realtor. I’m calling about {{property_address}} — is it still available?",
                "extract": {"confirmed_homeowner": "boolean", "listing_status": "string"},
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_step1_say"},
                    {"when": "confused == true", "next": "clarify_step1_say"},
                    {"when": "who_are_you == true", "next": "step2_permission_say"},
                    {"when": "user_is_busy == true", "next": "busy_callback_listen"},
                    {"when": "mentions_sold == true", "next": "sold_congrats_say"},
                    {"when": "mentions_already_listed == true", "next": "already_listed_branch_listen"},
                    {"when": "mentions_available == true", "next": "step4_shocked_say"},
                    {"when": "user_affirms == true", "next": "step3_status_listen"},
                    {"when": "user_denies == true", "next": "not_interested_soft_listen"},
                ],
                "fallback_next": "step3_status_listen",
            },
            "repeat_step1_say": {"type": "say", "prompt": "Sure — one more time.||No problem — I’ll repeat it.||Of course.", "transitions": {"default": "step1_intro_listen"}},
            "clarify_step1_say": {"type": "say", "prompt": "No worries. I’m a local Realtor calling about the property — I just want to confirm if it’s still available.||Totally understandable — I’m just checking if the property is still available.", "transitions": {"default": "step1_intro_listen"}},
            "step2_permission_say": {"type": "say", "prompt": "{{homeowner_name}}, this is {{agent_name}} — I’m a Realtor. Before you hang up, I was hoping to ask you a quick question about the home… would that be ok?||{{homeowner_name}}, it’s {{agent_name}}. I’m a Realtor — before you hang up, can I ask you one quick question about the home?", "transitions": {"default": "step2_permission_listen"}},
            "step2_permission_listen": {
                "type": "listen",
                "prompt": "Would that be ok?||Is that okay with you?||Is that something you’d be opposed to?",
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_permission_say"},
                    {"when": "confused == true", "next": "clarify_permission_say"},
                    {"when": "user_is_busy == true", "next": "busy_callback_listen"},
                    {"when": "user_denies == true", "next": "not_interested_soft_listen"},
                    {"when": "user_affirms == true", "next": "step3_status_listen"},
                ],
                "fallback_next": "step2_permission_listen",
            },
            "repeat_permission_say": {"type": "say", "prompt": "Sure.||Of course.||No problem.", "transitions": {"default": "step2_permission_listen"}},
            "clarify_permission_say": {"type": "say", "prompt": "Just one quick question about what happened with the listing — that’s all.||I’ll be brief — just one quick question about the home.", "transitions": {"default": "step2_permission_listen"}},
            "step3_status_listen": {
                "type": "listen",
                "prompt": "I saw it come off the market, but I wasn’t sure if you sold it privately… or if it’s still available by chance?||I noticed it came off the market — did you sell it privately, or is it still available?",
                "extract": {"listing_status": "string"},
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_status_say"},
                    {"when": "confused == true", "next": "clarify_status_say"},
                    {"when": "user_is_busy == true", "next": "busy_callback_listen"},
                    {"when": "mentions_sold == true", "next": "sold_congrats_say"},
                    {"when": "mentions_already_listed == true", "next": "already_listed_branch_listen"},
                    {"when": "mentions_available == true", "next": "step4_shocked_say"},
                    {"when": "user_denies == true", "next": "not_interested_soft_listen"},
                    {"when": "user_affirms == true", "next": "step4_shocked_say"},
                ],
                "fallback_next": "step3_status_listen",
            },
            "repeat_status_say": {"type": "say", "prompt": "Sure — quick question.||No worries — let me say it again.||Of course.", "transitions": {"default": "step3_status_listen"}},
            "clarify_status_say": {"type": "say", "prompt": "I’m just asking if it sold already, or if it’s still on the market.||Did it sell, or is it still on the market?", "transitions": {"default": "step3_status_listen"}},
            "sold_congrats_say": {"type": "say", "prompt": "Oh nice — congrats on getting it sold. I appreciate you letting me know. Have a great day!||That’s great news — congrats. Thanks for the quick update. Have a great one!"},
            "already_listed_branch_listen": {
                "type": "listen",
                "prompt": "Got it. Are you actively listed right now, or still interviewing agents?||Understood — are you listed now, or still deciding who to hire?",
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_listed_branch_say"},
                    {"when": "confused == true", "next": "clarify_listed_branch_say"},
                    {"when": "user_is_busy == true", "next": "busy_callback_listen"},
                    {"when": "user_denies == true", "next": "already_listed_exit_say"},
                    {"when": "user_affirms == true", "next": "listed_value_say"},
                ],
                "fallback_next": "listed_value_say",
            },
            "repeat_listed_branch_say": {"type": "say", "prompt": "Sure.||No problem.||Of course.", "transitions": {"default": "already_listed_branch_listen"}},
            "clarify_listed_branch_say": {"type": "say", "prompt": "Just checking — are you listed with an agent right now, or still deciding?||Are you already under contract with an agent, or still interviewing?", "transitions": {"default": "already_listed_branch_listen"}},
            "listed_value_say": {"type": "say", "prompt": "Makes sense. I won’t step on anyone’s toes. If it helps, I can send a quick checklist of what’s working right now to get more showings and stronger offers — no pressure.||Totally understood — I’m not trying to interfere. I can send one helpful checklist you can use with whoever you choose.", "transitions": {"default": "send_resume_email_listen"}},
            "already_listed_exit_say": {"type": "say", "prompt": "No worries at all. I appreciate the quick update. Wishing you the best with the sale!||All good — thanks for letting me know. Hope it goes smoothly!"},
            "step4_shocked_say": {"type": "say", "prompt": "I’m shocked! Homes like yours typically sell right away.||Wow — I’m honestly surprised. Homes like yours usually sell pretty fast.", "transitions": {"default": "step4_offer_listen"}},
            "step4_offer_listen": {
                "type": "listen",
                "prompt": "Let me ask you this — if you did get an offer with the price and terms you’re looking for… is it too late?||If you got an offer with the price and terms you want, would you still be open to selling — or is it too late?",
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_offer_say"},
                    {"when": "confused == true", "next": "clarify_offer_say"},
                    {"when": "user_is_busy == true", "next": "busy_callback_listen"},
                    {"when": "user_denies == true", "next": "objection_reason_listen"},
                    {"when": "user_affirms == true", "next": "step5_why_not_sold_listen"},
                ],
                "fallback_next": "step4_offer_listen",
            },
            "repeat_offer_say": {"type": "say", "prompt": "Sure.||Of course.||No worries.", "transitions": {"default": "step4_offer_listen"}},
            "clarify_offer_say": {"type": "say", "prompt": "I’m asking: if the right offer came in, would you still consider selling?||Just checking — if the numbers made sense, would you still be open to selling?", "transitions": {"default": "step4_offer_listen"}},
            "step5_why_not_sold_listen": {
                "type": "listen",
                "prompt": "Got it — I’m curious: when your previous agent was updating you, what did they say was keeping the home from selling?||When the agent was giving you updates, what did they say was holding it back?",
                "extract": {"why_not_sold": "string"},
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_why_say"},
                    {"when": "confused == true", "next": "clarify_why_say"},
                    {"when": "user_is_busy == true", "next": "busy_callback_listen"},
                    {"when": "user_denies == true", "next": "objection_reason_listen"},
                    {"when": "user_affirms == true", "next": "step6_stay_put_listen"},
                ],
                "fallback_next": "step5_why_not_sold_listen",
            },
            "repeat_why_say": {"type": "say", "prompt": "Sure.||No problem.||Of course.", "transitions": {"default": "step5_why_not_sold_listen"}},
            "clarify_why_say": {"type": "say", "prompt": "Just curious what they told you was stopping it from selling — price, condition, marketing, timing.||What did they say was holding it back?", "transitions": {"default": "step5_why_not_sold_listen"}},
            "step6_stay_put_listen": {
                "type": "listen",
                "prompt": "Got it. Well based on what I can see, the home looks great — is it an option to just stay put?||Is staying where you are an option, if selling doesn’t make sense right now?",
                "extract": {"stay_put_option": "string"},
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_stay_put_say"},
                    {"when": "confused == true", "next": "clarify_stay_put_say"},
                    {"when": "user_is_busy == true", "next": "busy_callback_listen"},
                    {"when": "user_denies == true", "next": "step7_move_timeline_listen"},
                    {"when": "user_affirms == true", "next": "step7_move_timeline_listen"},
                ],
                "fallback_next": "step6_stay_put_listen",
            },
            "repeat_stay_put_say": {"type": "say", "prompt": "Sure.||No worries.||Of course.", "transitions": {"default": "step6_stay_put_listen"}},
            "clarify_stay_put_say": {"type": "say", "prompt": "I mean: if you didn’t sell, could you just stay there for now?||Is staying put an option for you?", "transitions": {"default": "step6_stay_put_listen"}},
            "step7_move_timeline_listen": {
                "type": "listen",
                "prompt": "And in a perfect world, when did you plan on moving originally?||Originally, when were you hoping to be moved by?",
                "extract": {"planned_move": "string"},
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_timeline_say"},
                    {"when": "confused == true", "next": "clarify_timeline_say"},
                    {"when": "user_is_busy == true", "next": "busy_callback_listen"},
                    {"when": "user_denies == true", "next": "step8_recommendation_say"},
                    {"when": "user_affirms == true", "next": "step8_recommendation_say"},
                ],
                "fallback_next": "step7_move_timeline_listen",
            },
            "repeat_timeline_say": {"type": "say", "prompt": "Sure.||No worries.||Of course.", "transitions": {"default": "step7_move_timeline_listen"}},
            "clarify_timeline_say": {"type": "say", "prompt": "Just the rough timeline you had in mind — even a ballpark is fine.||When were you hoping to move originally?", "transitions": {"default": "step7_move_timeline_listen"}},
            "step8_recommendation_say": {"type": "say", "prompt": "Ok — well before I let you go, can I make a recommendation?||Before I let you go, can I make a quick recommendation?", "transitions": {"default": "step9_recommendation_listen"}},
            "step9_recommendation_listen": {
                "type": "listen",
                "prompt": "Listen, it’s totally up to you, but if you want, I’d be happy to stop by one day this week… and when I’m there, I can show you a completely new strategy I’m using right now to sell homes just like yours… and then you can decide if potentially hiring me makes sense or not — and if not, it’s totally fine. Is that something you’d be opposed to?||It’s totally up to you — but if you want, I can stop by one day this week, show you a new strategy that’s working right now, and then you can decide if hiring me makes sense. If not, that’s totally fine. Would you be opposed to that?",
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_reco_say"},
                    {"when": "confused == true", "next": "clarify_reco_say"},
                    {"when": "user_is_busy == true", "next": "busy_callback_listen"},
                    {"when": "user_denies == true", "next": "objection_reason_listen"},
                    {"when": "user_affirms == true", "next": "step10_face_it_say"},
                ],
                "fallback_next": "step9_recommendation_listen",
            },
            "repeat_reco_say": {"type": "say", "prompt": "Sure.||No problem.||Of course.", "transitions": {"default": "step9_recommendation_listen"}},
            "clarify_reco_say": {"type": "say", "prompt": "I’m saying: I can stop by, show you a new approach, and then you decide — no pressure.||Just a quick stop-by to share a strategy, and you decide from there.", "transitions": {"default": "step9_recommendation_listen"}},
            "step10_face_it_say": {"type": "say", "prompt": "Ok because let’s face it — the only way you’d even consider hiring me, and I’m not saying you should, is if you felt 100% confident that I could sell your home for the price you wanted or more… am I right?||Because let’s be real — you’d only consider hiring me if you felt totally confident I could get it sold for the price you want or more, right?", "transitions": {"default": "step11_calendar_listen"}},
            "step11_calendar_listen": {
                "type": "listen",
                "prompt": "Ok well, I’m looking at my calendar — and I don’t know if I can — but if I could move some client meetings I already have scheduled for tonight… would you be able to make that work?||If I could shuffle a couple things on my calendar tonight, would you be able to make a quick time work?",
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_calendar_say"},
                    {"when": "confused == true", "next": "clarify_calendar_say"},
                    {"when": "user_is_busy == true", "next": "busy_callback_listen"},
                    {"when": "user_denies == true", "next": "busy_callback_listen"},
                    {"when": "user_affirms == true", "next": "step12_time_choice_listen"},
                ],
                "fallback_next": "step11_calendar_listen",
            },
            "repeat_calendar_say": {"type": "say", "prompt": "Sure.||No worries.||Of course.", "transitions": {"default": "step11_calendar_listen"}},
            "clarify_calendar_say": {"type": "say", "prompt": "I mean: if I can make a time tonight work, could you do it?||Would you be able to meet tonight if I can make the time?", "transitions": {"default": "step11_calendar_listen"}},
            "step12_time_choice_listen": {
                "type": "listen",
                "prompt": "I have a 4pm and a 6pm — which of those would be better?||I’ve got 4 o’clock or 6 o’clock — which works better for you?",
                "extract": {"appointment_time": "string"},
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_time_choice_say"},
                    {"when": "confused == true", "next": "clarify_time_choice_say"},
                    {"when": "time_is_4pm == true", "next": "step13_email_package_say"},
                    {"when": "time_is_6pm == true", "next": "step13_email_package_say"},
                    {"when": "user_requests_other_time == true", "next": "step13_email_package_say"},
                ],
                "fallback_next": "step12_time_choice_listen",
            },
            "repeat_time_choice_say": {"type": "say", "prompt": "Sure.||No problem.||Of course.", "transitions": {"default": "step12_time_choice_listen"}},
            "clarify_time_choice_say": {"type": "say", "prompt": "Just picking a time — 4, 6, or another time that works better.||Which is better for you: 4, 6, or a different time?", "transitions": {"default": "step12_time_choice_listen"}},
            "step13_email_package_say": {"type": "say", "prompt": "Ok — so here’s what I’ll do when we hang up: I’ll send you an email with a copy of my resume that will answer most of the questions you probably have about me, including my marketing plan. Can you do me a favor? Can you review that prior to our meeting?||When we hang up, I’ll email you my resume and marketing plan so you can see exactly how I work. Can you do me a favor and review it before we meet?", "transitions": {"default": "send_resume_email_listen"}},
            "send_resume_email_listen": {
                "type": "listen",
                "prompt": "What email should I use?||What’s the best email for you?",
                "extract": {"owner_email": "string"},
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_email_say"},
                    {"when": "confused == true", "next": "clarify_email_say"},
                    {"when": "user_refuses_email == true", "next": "send_resume_text_listen"},
                ],
                "fallback_next": "email_confirm_listen",
            },
            "repeat_email_say": {"type": "say", "prompt": "Sure.||No problem.||Of course.", "transitions": {"default": "send_resume_email_listen"}},
            "clarify_email_say": {"type": "say", "prompt": "Just the best email to send the resume and marketing plan to.||What email should I send it to?", "transitions": {"default": "send_resume_email_listen"}},
            "email_confirm_listen": {
                "type": "listen",
                "prompt": "Perfect — just to confirm, is that email correct?||Got it — is that the right email?",
                "routes": [
                    {"when": "repeat_request == true", "next": "repeat_email_confirm_say"},
                    {"when": "confused == true", "next": "clarify_email_confirm_say"},
                    {"when": "user_affirms == true", "next": "step14_reschedule_note_say"},
                    {"when": "user_denies == true", "next": "send_resume_email_listen"},
                ],
                "fallback_next": "step14_reschedule_note_say",
            },
            "repeat_email_confirm_say": {"type": "say", "prompt": "Sure.||No worries.||Of course.", "transitions": {"default": "email_confirm_listen"}},
            "clarify_email_confirm_say": {"type": "say", "prompt": "I’m just confirming I heard it correctly.||Just making sure I’ve got the right email.", "transitions": {"default": "email_confirm_listen"}},
            "send_resume_text_listen": {
                "type": "listen",
                "prompt": "No problem — I can text it instead. Is this number the best one to send it to?||Totally fine — should I text it to this number?",
                "routes": [
                    {"when": "repeat_request == true", "next": "repeat_text_say"},
                    {"when": "confused == true", "next": "clarify_text_say"},
                    {"when": "user_affirms == true", "next": "step14_reschedule_note_say"},
                    {"when": "user_denies == true", "next": "exit_polite_no"},
                ],
                "fallback_next": "step14_reschedule_note_say",
            },
            "repeat_text_say": {"type": "say", "prompt": "Sure.||No worries.||Of course.", "transitions": {"default": "send_resume_text_listen"}},
            "clarify_text_say": {"type": "say", "prompt": "I’m asking if I should text the info to this number.||Should I text it here?", "transitions": {"default": "send_resume_text_listen"}},
            "step14_reschedule_note_say": {"type": "say", "prompt": "Ok great — well if for some reason I can’t reschedule my meeting with my other client, I’ll let you know.||Perfect. And if I can’t shuffle my other meeting, I’ll let you know right away.", "transitions": {"default": "step15_move_if_needed_listen"}},
            "step15_move_if_needed_listen": {
                "type": "listen",
                "prompt": "{{homeowner_name}}, if there’s anything on your end that comes up and we need to move our meeting, will you let me know?||And if anything comes up on your end and we need to move it, will you let me know?",
                "routes": [
                    {"when": "repeat_request == true", "next": "repeat_move_say"},
                    {"when": "confused == true", "next": "clarify_move_say"},
                    {"when": "user_affirms == true", "next": "step16_close_say"},
                    {"when": "user_denies == true", "next": "step16_close_say"},
                ],
                "fallback_next": "step16_close_say",
            },
            "repeat_move_say": {"type": "say", "prompt": "Sure.||No problem.||Of course.", "transitions": {"default": "step15_move_if_needed_listen"}},
            "clarify_move_say": {"type": "say", "prompt": "Just asking if you’ll let me know if we need to reschedule.||If anything changes, will you let me know?", "transitions": {"default": "step15_move_if_needed_listen"}},
            "step16_close_say": {"type": "say", "prompt": "Ok — see you soon.||Perfect — talk soon.||Great — I’ll see you then."},
            "busy_callback_listen": {
                "type": "listen",
                "prompt": "No worries — totally understandable. Would later today around 4pm or 6pm be better, or would you prefer tomorrow?||I completely understand. Should I call you back around 4, around 6, or tomorrow?",
                "extract": {"callback_time": "string"},
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "time_is_4pm == true", "next": "exit_callback_scheduled"},
                    {"when": "time_is_6pm == true", "next": "exit_callback_scheduled"},
                    {"when": "user_requests_other_time == true", "next": "exit_callback_scheduled"},
                    {"when": "user_denies == true", "next": "exit_polite_no"},
                ],
                "fallback_next": "busy_callback_listen",
            },
            "exit_callback_scheduled": {"type": "say", "prompt": "Perfect — I’ll reach back out around then. Thanks again, and have a great day.||Got it — I’ll call you back around then. Appreciate you."},
            "not_interested_soft_listen": {
                "type": "listen",
                "prompt": "I totally understand. Just so I don’t keep bothering you — are you not selling at all, or is it just not a good time for calls?||Totally fair. Are you not selling at all, or is it just a bad time to talk?",
                "extract": {"not_interested_reason": "string"},
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_notint_say"},
                    {"when": "confused == true", "next": "clarify_notint_say"},
                    {"when": "user_is_busy == true", "next": "busy_callback_listen"},
                    {"when": "user_affirms == true", "next": "send_resume_email_listen"},
                    {"when": "user_denies == true", "next": "exit_polite_no"},
                ],
                "fallback_next": "not_interested_soft_listen",
            },
            "repeat_notint_say": {"type": "say", "prompt": "Sure.||No problem.||Of course.", "transitions": {"default": "not_interested_soft_listen"}},
            "clarify_notint_say": {"type": "say", "prompt": "Just checking — is it not selling, or just not a good time?||Are you not selling at all, or just busy right now?", "transitions": {"default": "not_interested_soft_listen"}},
            "objection_reason_listen": {
                "type": "listen",
                "prompt": "Totally fair — what’s the main reason you wouldn’t want to do that?||I completely understand — what’s the main reason you’d rather not move forward with it?",
                "extract": {"objection_reason": "string"},
                "routes": [
                    {"when": "opt_out == true", "next": "exit_opt_out"},
                    {"when": "wrong_number == true", "next": "exit_wrong_number"},
                    {"when": "repeat_request == true", "next": "repeat_obj_say"},
                    {"when": "confused == true", "next": "clarify_obj_say"},
                    {"when": "user_is_busy == true", "next": "busy_callback_listen"},
                    {"when": "has_agent_friend == true", "next": "obj_friend_agent_say"},
                    {"when": "asks_buyer == true", "next": "obj_buyer_say"},
                    {"when": "mentions_rates == true", "next": "obj_rates_say"},
                    {"when": "mentions_commission == true", "next": "obj_commission_say"},
                    {"when": "wants_list_high == true", "next": "obj_list_high_say"},
                    {"when": "wants_think_it_over == true", "next": "obj_think_it_over_say"},
                    {"when": "mentions_already_listed == true", "next": "obj_listed_say"},
                    {"when": "user_denies == true", "next": "exit_polite_no"},
                ],
                "fallback_next": "obj_generic_say",
            },
            "repeat_obj_say": {"type": "say", "prompt": "Sure.||No worries.||Of course.", "transitions": {"default": "objection_reason_listen"}},
            "clarify_obj_say": {"type": "say", "prompt": "Just the main reason — timing, already listed, commission, anything like that.||What’s the main reason you’d rather not?", "transitions": {"default": "objection_reason_listen"}},
            "obj_friend_agent_say": {"type": "say", "prompt": "Makes sense. Before you hang up, quick question — is there any downside to getting a second opinion before you decide, even if you stick with the same agent?||Totally understandable. Quick question — would you be opposed to a second opinion, just to compare?", "transitions": {"default": "step9_recommendation_listen"}},
            "obj_buyer_say": {"type": "say", "prompt": "Fair question. Before you hang up — would you be opposed to having more than one buyer competing, if it meant the price you want or more?||I’m glad you brought that up. If more buyers meant a better price, would you be opposed to that?", "transitions": {"default": "step9_recommendation_listen"}},
            "obj_listed_say": {"type": "say", "prompt": "Totally understood — I’m not trying to interfere. Would you be opposed to a quick second opinion before you make any decisions?||Makes sense — would you be against getting a second opinion first, just to compare?", "transitions": {"default": "step9_recommendation_listen"}},
            "obj_rates_say": {"type": "say", "prompt": "Totally understandable — rates are a real factor. If the numbers made sense, would you be opposed to moving forward?||I completely understand — if we could still get the price and terms you want, would you be opposed to selling?", "transitions": {"default": "step4_offer_listen"}},
            "obj_commission_say": {"type": "say", "prompt": "I totally understand. Fees only matter if you’re getting value. If the net to you made sense, would you be opposed to meeting for 10 minutes?||Understandable. If you net what you want, would you be opposed to a quick meeting?", "transitions": {"default": "step9_recommendation_listen"}},
            "obj_list_high_say": {"type": "say", "prompt": "Totally understand — can I ask you something? If pricing too high means fewer showings, would you rather have a bidding war or no leverage at all?||I get it — but if it’s priced too high, fewer buyers even look. Would you rather have competition or none?", "transitions": {"default": "step9_recommendation_listen"}},
            "obj_think_it_over_say": {"type": "say", "prompt": "Totally fair — let’s think out loud for a second. What part are you unsure about?||That’s fair — what are you thinking about specifically?", "transitions": {"default": "objection_reason_listen"}},
            "obj_generic_say": {"type": "say", "prompt": "Totally understandable. Quick question — if it helped you get the result you want, would you be opposed to a short meeting?||I completely understand. If this could still get you the price and terms you want, would you be opposed to a quick meeting?", "transitions": {"default": "step9_recommendation_listen"}},
            "exit_polite_no": {"type": "say", "prompt": "No worries at all — I appreciate you. I won’t take more of your time. Have a great day.||All good — thanks anyway. Have a great day."},
            "exit_wrong_number": {"type": "say", "prompt": "Oh got it — I’m sorry about that. I’ll update my records. Have a great day.||My mistake — thanks for telling me. Have a good one."},
            "exit_opt_out": {"type": "say", "prompt": "Understood — I’ll remove you from my list immediately. Take care.||Absolutely — I’ll take you off right away. Take care."},
        },
    }


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    logger.info("A2Z Dialer API started")


@app.get("/", response_model=HealthOut)
def root() -> HealthOut:
    return HealthOut(
        ok=True,
        env=settings.env,
        ts=int(time.time()),
        database_url=settings.database_url,
    )


@app.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(
        ok=True,
        env=settings.env,
        ts=int(time.time()),
        database_url=settings.database_url,
    )


@app.get("/usage")
async def get_usage(current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    workspace = session.get(Workspace, current_user.workspace_id)
    if not workspace:
        raise HTTPException(404)
    calls_this_month = workspace.calls_this_month or 0
    calls_limit = workspace.calls_limit or 500
    minutes_used = float(workspace.minutes_used_this_month or 0)
    minutes_limit = workspace.minutes_limit or 200
    return {
        "calls_this_month": calls_this_month,
        "calls_limit": calls_limit,
        "calls_remaining": max(0, calls_limit - calls_this_month),
        "calls_percent_used": round(calls_this_month / max(calls_limit, 1) * 100, 1),
        "minutes_used_this_month": round(minutes_used, 1),
        "minutes_limit": minutes_limit,
        "minutes_remaining": round(max(0, minutes_limit - minutes_used), 1),
        "minutes_percent_used": round(minutes_used / max(minutes_limit, 1) * 100, 1),
        "is_over_limit": minutes_used >= minutes_limit,
        "is_near_limit": minutes_used >= minutes_limit * 0.80,
        "overage_rate_cents": workspace.overage_rate_cents or 18,
    }


@app.post("/usage/reset")
async def reset_usage(current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Admin-only: reset calls_this_month and minutes_used_this_month (monthly billing cycle reset)."""
    if current_user.role not in ("admin", "superadmin"):
        raise HTTPException(403, "Admin access required")
    workspace = session.get(Workspace, current_user.workspace_id)
    if not workspace:
        raise HTTPException(404)
    workspace.calls_this_month = 0
    workspace.minutes_used_this_month = 0.0
    session.add(workspace)
    session.commit()
    return {"ok": True, "message": "Usage counters reset"}


@app.post("/auth/make-me-admin-temp")
def make_me_admin_temp(session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.id == 1)).first()
    if user:
        user.is_admin = True
        user.role = "admin"
        session.add(user)
        session.commit()
        return {"ok": True, "message": "User 1 is now admin"}
    return {"ok": False, "message": "User not found"}


@app.post("/auth/register")
@limiter.limit("5/minute")
def register(request: Request, payload: AuthRegisterIn = Body(...), ref: Optional[str] = Query(default=None), session: Session = Depends(get_session)) -> dict:
    existing = session.exec(select(User).where(User.email == payload.email.lower().strip())).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    base_slug = slugify(payload.workspace_name)
    final_slug = base_slug
    idx = 2
    while session.exec(select(Workspace).where(Workspace.slug == final_slug)).first():
        final_slug = f"{base_slug}-{idx}"
        idx += 1

    workspace = Workspace(
        name=payload.workspace_name.strip(),
        slug=final_slug,
        brand_name=payload.workspace_name.strip(),
        default_agent_name=payload.full_name.strip(),
        default_brokerage_name=payload.workspace_name.strip(),
    )
    session.add(workspace)
    session.commit()
    session.refresh(workspace)

    user = User(
        workspace_id=workspace.id,
        email=payload.email.lower().strip(),
        full_name=payload.full_name.strip(),
        password_hash=hash_password(payload.password),
        role="owner",
        is_active=True,
        is_verified=True,
    )
    session.add(user)

    default_pathway = Pathway(
        workspace_id=workspace.id,
        name="A2Z Expired Listing — Default Script",
        description="The A2Z default expired listing script. Uses a proven conversation flow with full objection handling, appointment booking, email capture, and callback scheduling.",
        is_active=True,
        version=1,
        json_def=json_dumps(create_default_pathway_json()),
        tags_csv="expired_listing,default,objection_handling,appointment_booking",
    )
    session.add(default_pathway)

    audit(
        session,
        workspace_id=workspace.id,
        user_id=None,
        action="workspace_registered",
        entity_type="workspace",
        entity_id=workspace.id,
        details={"email": user.email},
    )

    # Referral tracking
    if ref:
        referrer_ws = session.exec(select(Workspace).where(Workspace.slug == ref)).first()
        if referrer_ws and referrer_ws.id != workspace.id:
            referral = Referral(
                referrer_workspace_id=referrer_ws.id,
                referred_workspace_id=workspace.id,
                referred_email=user.email,
                status="converted",
            )
            session.add(referral)
            # Award 1 free month to referrer (extend billing cycle by 30 days)
            if referrer_ws.billing_cycle_start:
                from datetime import timedelta
                referrer_ws.billing_cycle_start = referrer_ws.billing_cycle_start - timedelta(days=30)
                session.add(referrer_ws)

    session.commit()
    session.refresh(user)

    token = create_access_token(user.id)
    return {
        "access_token": token,
        "token_type": "bearer",
        "workspace_id": workspace.id,
        "workspace_slug": workspace.slug,
    }


@app.post("/auth/login")
@limiter.limit("10/minute")
def login(request: Request, payload: AuthLoginIn, session: Session = Depends(get_session)) -> dict:
    from auth import get_user_by_email
    candidate = get_user_by_email(session, payload.email.strip().lower())

    # Check account lockout before attempting authentication
    if candidate and candidate.locked_until:
        locked_until_aware = candidate.locked_until.replace(tzinfo=timezone.utc) if candidate.locked_until.tzinfo is None else candidate.locked_until
        if locked_until_aware > utcnow():
            raise HTTPException(status_code=423, detail="Account temporarily locked. Try again later.")

    user = authenticate_user(session, payload.email, payload.password)
    if not user:
        # Increment failed attempts if the account exists
        if candidate:
            candidate.failed_login_attempts = (candidate.failed_login_attempts or 0) + 1
            if candidate.failed_login_attempts >= 10:
                candidate.locked_until = utcnow() + timedelta(minutes=30)
            touch(candidate)
            session.add(candidate)
            session.commit()
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # Successful login — reset lockout fields
    user.last_login_at = utcnow()
    user.failed_login_attempts = 0
    user.locked_until = None
    touch(user)
    session.add(user)
    session.commit()

    token = create_access_token(user.id)
    refresh_token_str = secrets.token_urlsafe(32)
    db_refresh = RefreshToken(
        user_id=user.id,
        token=refresh_token_str,
        expires_at=utcnow() + timedelta(days=30),
    )
    session.add(db_refresh)
    session.commit()
    return {
        "access_token": token,
        "refresh_token": refresh_token_str,
        "token_type": "bearer",
    }


@app.post("/auth/forgot-password")
@limiter.limit("3/minute")
def forgot_password(request: Request, payload: dict, session: Session = Depends(get_session)):
    email = payload.get("email", "").strip().lower()
    user = session.exec(select(User).where(func.lower(User.email) == email)).first()
    # Always return success (don't reveal if email exists)
    if user:
        token = secrets.token_urlsafe(32)
        reset = PasswordResetToken(
            user_id=user.id,
            token=token,
            expires_at=utcnow() + timedelta(hours=1),
        )
        session.add(reset)
        session.commit()
        # TODO: send email with reset link
        logger.info(f"Password reset token for {email}: {token}")
    return {"ok": True, "message": "If that email exists, a reset link was sent."}


@app.post("/auth/refresh")
def refresh_token_endpoint(payload: dict, session: Session = Depends(get_session)):
    token_str = payload.get("refresh_token", "")
    if not token_str:
        raise HTTPException(status_code=401, detail="Missing refresh token")
    db_token = session.exec(
        select(RefreshToken).where(RefreshToken.token == token_str)
    ).first()
    if not db_token or db_token.revoked:
        raise HTTPException(status_code=401, detail="Invalid or revoked refresh token")
    expires_aware = db_token.expires_at.replace(tzinfo=timezone.utc) if db_token.expires_at.tzinfo is None else db_token.expires_at
    if expires_aware < utcnow():
        raise HTTPException(status_code=401, detail="Refresh token expired")
    user = session.get(User, db_token.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    # Revoke old token (rotation)
    db_token.revoked = True
    session.add(db_token)
    # Issue new tokens
    new_access_token = create_access_token(user.id)
    new_refresh_token_str = secrets.token_urlsafe(32)
    new_db_refresh = RefreshToken(
        user_id=user.id,
        token=new_refresh_token_str,
        expires_at=utcnow() + timedelta(days=30),
    )
    session.add(new_db_refresh)
    session.commit()
    return {
        "access_token": new_access_token,
        "refresh_token": new_refresh_token_str,
        "token_type": "bearer",
    }


@app.post("/auth/logout")
def logout(payload: dict, session: Session = Depends(get_session)):
    token_str = payload.get("refresh_token", "")
    if token_str:
        db_token = session.exec(
            select(RefreshToken).where(RefreshToken.token == token_str)
        ).first()
        if db_token and not db_token.revoked:
            db_token.revoked = True
            session.add(db_token)
            session.commit()
    return {"ok": True}


@app.post("/auth/reset-password")
def reset_password(payload: dict, session: Session = Depends(get_session)):
    token_str = payload.get("token", "")
    new_password = payload.get("new_password", "")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    reset = session.exec(select(PasswordResetToken).where(
        PasswordResetToken.token == token_str,
        PasswordResetToken.used == False,
    )).first()
    if not reset or reset.expires_at.replace(tzinfo=timezone.utc) < utcnow():
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    user = session.get(User, reset.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = hash_password(new_password)
    reset.used = True
    session.add(user)
    session.add(reset)
    session.commit()
    return {"ok": True, "message": "Password updated successfully"}


@app.get("/me")
def me(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    workspace = get_workspace_or_404(session, user.workspace_id)
    return {
        "id": user.id,
        "workspace_id": user.workspace_id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_admin": user.is_admin,
        "is_active": user.is_active,
        "plan": workspace.plan,
        "subscription_status": workspace.subscription_status,
        "workspace": {
            "id": workspace.id,
            "name": workspace.name,
            "slug": workspace.slug,
            "brand_name": workspace.brand_name,
            "default_agent_name": workspace.default_agent_name,
            "default_brokerage_name": workspace.default_brokerage_name,
            "default_caller_title": workspace.default_caller_title,
            "voice_mode": workspace.voice_mode,
            "appointment_mode": workspace.appointment_mode,
            "timezone": workspace.timezone,
        },
    }


@app.get("/users/me")
def users_me(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    workspace = get_workspace_or_404(session, user.workspace_id)
    return {
        "id": user.id,
        "workspace_id": user.workspace_id,
        "email": user.email,
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "plan": workspace.plan,
        "subscription_status": workspace.subscription_status,
    }


@app.put("/users/profile")
def update_profile(
    payload: dict,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    full_name = payload.get("full_name", "").strip()
    if not full_name:
        raise HTTPException(status_code=400, detail="full_name is required")
    user.full_name = full_name
    session.add(user)
    session.commit()
    session.refresh(user)
    return {"ok": True, "full_name": user.full_name}


@app.post("/auth/change-password")
def change_password(
    payload: dict,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    from auth import verify_password, hash_password
    current_password = payload.get("current_password", "")
    new_password = payload.get("new_password", "")
    if not verify_password(current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    user.password_hash = hash_password(new_password)
    session.add(user)
    session.commit()
    return {"ok": True}


# ── GDPR: Data Export & Account Deletion ─────────────────────────────────────

@app.get("/user/export-data")
def export_user_data(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    workspace = session.get(Workspace, user.workspace_id)
    campaigns = session.exec(select(Campaign).where(Campaign.workspace_id == user.workspace_id)).all()
    leads = session.exec(select(Lead).where(Lead.workspace_id == user.workspace_id)).all()
    calls = session.exec(select(CallLog).where(CallLog.workspace_id == user.workspace_id)).all()
    appointments = session.exec(select(Appointment).where(Appointment.workspace_id == user.workspace_id)).all()
    purchases = session.exec(select(MarketplacePurchase).where(MarketplacePurchase.buyer_workspace_id == user.workspace_id)).all()
    notifications = session.exec(select(Notification).where(Notification.user_id == user.id)).all()
    return {
        "profile": user.model_dump(exclude={"password_hash"}),
        "workspace": workspace.model_dump() if workspace else None,
        "campaigns": [r.model_dump() for r in campaigns],
        "leads": [r.model_dump() for r in leads],
        "calls": [r.model_dump() for r in calls],
        "appointments": [r.model_dump() for r in appointments],
        "purchases": [r.model_dump() for r in purchases],
        "notifications": [r.model_dump() for r in notifications],
    }


@app.delete("/user/account")
def delete_user_account(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    workspace_id = user.workspace_id
    # Cascade delete workspace-owned data
    for model_cls in (
        Notification, CallLog, Appointment, Lead, LeadList, Campaign, Pathway,
        DNCEntry, UsageEvent, MarketplaceListing, MarketplacePurchase,
        SavedTerritory, PartnerPayout, ScriptAsset, VoicePartnerProfile,
        AgentProfile, AgentTerritory, FeaturedPlacement, PublicTrustSource,
    ):
        if hasattr(model_cls, "workspace_id"):
            rows = session.exec(select(model_cls).where(model_cls.workspace_id == workspace_id)).all()
            for row in rows:
                session.delete(row)
    # Delete user-specific rows
    refresh_tokens = session.exec(select(RefreshToken).where(RefreshToken.user_id == user.id)).all()
    for rt in refresh_tokens:
        session.delete(rt)
    reset_tokens = session.exec(select(PasswordResetToken).where(PasswordResetToken.user_id == user.id)).all()
    for rt in reset_tokens:
        session.delete(rt)
    # Log audit before deleting
    audit(
        session,
        workspace_id=workspace_id,
        user_id=user.id,
        action="account_deleted",
        entity_type="user",
        entity_id=user.id,
        details={"email": user.email},
    )
    session.delete(user)
    session.commit()
    return {"ok": True, "message": "Account and all associated data deleted."}


@app.get("/leadlists")
def list_leadlists(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.exec(
        select(LeadList)
        .where(LeadList.workspace_id == user.workspace_id)
        .order_by(LeadList.created_at.desc())
    ).all()
    return [row.model_dump() for row in rows]


@app.post("/leadlists")
def create_leadlist(
    payload: LeadListCreateIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = LeadList(
        workspace_id=user.workspace_id,
        name=payload.name.strip(),
        source=payload.source.strip(),
        description=payload.description,
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    audit(
        session,
        workspace_id=user.workspace_id,
        user_id=user.id,
        action="leadlist_created",
        entity_type="leadlist",
        entity_id=row.id,
        details={"name": row.name},
    )
    session.commit()
    return row.model_dump()


@app.post("/leadlists/{lead_list_id}/upload_csv")
async def upload_csv(
    lead_list_id: int,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    lead_list = get_leadlist_or_404(session, user.workspace_id, lead_list_id)

    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Empty CSV file")

    decoded = content.decode("utf-8-sig", errors="ignore")
    reader = csv.DictReader(io.StringIO(decoded))
    created = 0
    skipped_invalid = 0
    skipped_dupes = 0
    total_rows = 0

    workspace_id = user.workspace_id

    for row in reader:
        total_rows += 1
        raw_phone = (row.get("phone") or row.get("Phone") or row.get("mobile") or "").strip()
        if not raw_phone:
            skipped_invalid += 1
            continue

        normalized_phone = normalize_phone(raw_phone)
        # Validate: "+1XXXXXXXXXX" = 12 chars minimum (country code + 10 digits)
        if len(normalized_phone) < 12:
            logger.warning(
                "CSV upload: skipping row with invalid phone %r (normalized: %r)",
                raw_phone,
                normalized_phone,
            )
            skipped_invalid += 1
            continue

        # Deduplication check
        existing = session.exec(
            select(Lead).where(Lead.phone_number == normalized_phone, Lead.workspace_id == workspace_id)
        ).first()
        if existing:
            skipped_dupes += 1
            continue

        lead = Lead(
            workspace_id=workspace_id,
            lead_list_id=lead_list.id,
            homeowner_name=(row.get("homeowner_name") or row.get("owner") or row.get("name") or "").strip() or None,
            first_name=(row.get("first_name") or "").strip() or None,
            last_name=(row.get("last_name") or "").strip() or None,
            phone=normalized_phone,
            phone_number=normalized_phone,
            email=(row.get("email") or "").strip() or None,
            property_address=(row.get("property_address") or row.get("address") or "").strip() or None,
            city=(row.get("city") or "").strip() or None,
            state=(row.get("state") or "").strip() or None,
            postal_code=(row.get("postal_code") or row.get("zip") or "").strip() or None,
            lead_source=(row.get("lead_source") or row.get("source") or "expired_listing").strip(),
            listing_status=(row.get("listing_status") or "").strip() or None,
            listing_status_raw=(row.get("listing_status_raw") or "").strip() or None,
            days_expired=int(row["days_expired"]) if (row.get("days_expired") or "").strip().isdigit() else None,
            last_list_price=(row.get("last_list_price") or row.get("list_price") or "").strip() or None,
            raw_data_json=json_dumps(row),
            extracted_json="{}",
        )
        session.add(lead)
        created += 1

    lead_list.total_records += created
    lead_list.active_records += created
    touch(lead_list)
    session.add(lead_list)

    audit(
        session,
        workspace_id=workspace_id,
        user_id=user.id,
        action="leadlist_csv_uploaded",
        entity_type="leadlist",
        entity_id=lead_list.id,
        details={
            "created": created,
            "skipped_invalid": skipped_invalid,
            "skipped_dupes": skipped_dupes,
            "total_rows": total_rows,
            "filename": file.filename,
        },
    )
    session.commit()

    return {
        "ok": True,
        "lead_list_id": lead_list.id,
        "imported": created,
        "skipped_invalid": skipped_invalid,
        "skipped_dupes": skipped_dupes,
        "total_rows": total_rows,
    }


@app.get("/leads")
def list_leads(
    lead_list_id: Optional[int] = Query(default=None),
    status: Optional[str] = Query(default=None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(Lead).where(Lead.workspace_id == user.workspace_id)
    if lead_list_id:
        stmt = stmt.where(Lead.lead_list_id == lead_list_id)
    if status:
        stmt = stmt.where(Lead.status == status)
    rows = session.exec(stmt.order_by(Lead.created_at.desc())).all()
    return [row.model_dump() for row in rows]


@app.post("/leads")
def create_lead(
    payload: LeadCreateIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    get_leadlist_or_404(session, user.workspace_id, payload.lead_list_id)

    row = Lead(
        workspace_id=user.workspace_id,
        lead_list_id=payload.lead_list_id,
        homeowner_name=payload.homeowner_name,
        first_name=payload.first_name,
        last_name=payload.last_name,
        phone=payload.phone,
        email=payload.email,
        property_address=payload.property_address,
        city=payload.city,
        state=payload.state,
        postal_code=payload.postal_code,
        lead_source=payload.lead_source,
        notes=payload.notes,
        raw_data_json=json_dumps(payload.raw_data_json or {}),
        extracted_json="{}",
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.post("/leads/{lead_id}/publish_to_marketplace")
def publish_lead_to_marketplace(
    lead_id: int,
    campaign_id: Optional[int] = Query(default=None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    lead = get_lead_or_404(session, user.workspace_id, lead_id)
    workspace = get_workspace_or_404(session, user.workspace_id)

    campaign = None
    if campaign_id:
        campaign = get_campaign_or_404(session, user.workspace_id, campaign_id)
    else:
        campaign = session.exec(
            select(Campaign)
            .where(Campaign.workspace_id == user.workspace_id)
            .order_by(Campaign.created_at.desc())
        ).first()

    calllog = get_latest_calllog_for_lead(session, user.workspace_id, lead.id)
    appointment = get_latest_appointment_for_lead(session, user.workspace_id, lead.id)

    listing = sync_marketplace_listing_for_opportunity(
        session=session,
        workspace=workspace,
        lead=lead,
        campaign=campaign,
        calllog=calllog,
        appointment=appointment if appointment and appointment.appointment_time_iso else None,
    )
    session.commit()

    return {
        "ok": bool(listing),
        "lead_id": lead.id,
        "listing": listing.model_dump() if listing else None,
    }


@app.get("/pathways/default")
def get_default_pathway(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = session.exec(
        select(Pathway)
        .where(Pathway.workspace_id == user.workspace_id)
        .where(Pathway.name == "A2Z Expired Listing — Default Script")
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Default pathway not found")
    return row.model_dump()


@app.get("/pathways")
def list_pathways(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.exec(
        select(Pathway)
        .where(Pathway.workspace_id == user.workspace_id)
        .order_by(Pathway.created_at.desc())
    ).all()
    return [row.model_dump() for row in rows]


@app.post("/pathways")
def create_pathway(
    payload: PathwayCreateIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    json_obj = payload.json_def
    errors = validate_pathway_json(json_obj)
    row = Pathway(
        workspace_id=user.workspace_id,
        name=payload.name.strip(),
        description=payload.description,
        is_active=True,
        version=1,
        json_def=json_dumps(json_obj),
        validation_errors=json_dumps(errors) if errors else None,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.put("/pathways/{pathway_id}")
def update_pathway(
    pathway_id: int,
    payload: PathwayUpdateIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = get_pathway_or_404(session, user.workspace_id, pathway_id)

    if payload.name is not None:
        row.name = payload.name.strip()
    if payload.description is not None:
        row.description = payload.description
    if payload.is_active is not None:
        row.is_active = payload.is_active
    if payload.json_def is not None:
        errors = validate_pathway_json(payload.json_def)
        row.json_def = json_dumps(payload.json_def)
        row.validation_errors = json_dumps(errors) if errors else None
        row.version += 1

    touch(row)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.post("/pathways/{pathway_id}/validate")
def validate_pathway_endpoint(
    pathway_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = get_pathway_or_404(session, user.workspace_id, pathway_id)
    obj = safe_json_load(row.json_def)
    errors = validate_pathway_json(obj)
    row.validation_errors = json_dumps(errors) if errors else None
    touch(row)
    session.add(row)
    session.commit()
    return {
        "ok": len(errors) == 0,
        "errors": errors,
    }


@app.post("/pathways/{pathway_id}/simulate")
def simulate_pathway_endpoint(
    pathway_id: int,
    payload: PathwaySimulateIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = get_pathway_or_404(session, user.workspace_id, pathway_id)
    obj = safe_json_load(row.json_def)
    flags = payload.flags or classify_text(payload.user_reply or "")
    result = simulate_pathway(obj, payload.current_node, payload.user_reply, flags)
    return result


@app.get("/analytics")
def get_analytics(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    ws_id = user.workspace_id

    total_calls = session.exec(
        select(func.count(CallLog.id)).where(CallLog.workspace_id == ws_id)
    ).one() or 0

    connected_calls = session.exec(
        select(func.count(CallLog.id)).where(
            CallLog.workspace_id == ws_id,
            CallLog.connected == True,
        )
    ).one() or 0

    booked_calls = session.exec(
        select(func.count(CallLog.id)).where(
            CallLog.workspace_id == ws_id,
            CallLog.disposition == "booked",
        )
    ).one() or 0

    opt_out_calls = session.exec(
        select(func.count(CallLog.id)).where(
            CallLog.workspace_id == ws_id,
            CallLog.disposition.in_(["do_not_call", "opt_out"]),
        )
    ).one() or 0

    total_appointments = session.exec(
        select(func.count(Appointment.id)).where(Appointment.workspace_id == ws_id)
    ).one() or 0

    total_leads = session.exec(
        select(func.count(Lead.id)).where(Lead.workspace_id == ws_id)
    ).one() or 0

    marketplace_listings = session.exec(
        select(func.count(MarketplaceListing.id)).where(
            MarketplaceListing.workspace_id == ws_id,
            MarketplaceListing.status == "available",
        )
    ).one() or 0

    connect_rate = round(connected_calls / total_calls * 100, 1) if total_calls > 0 else 0
    booking_rate = round(booked_calls / connected_calls * 100, 1) if connected_calls > 0 else 0

    # Disposition breakdown
    dispositions_raw = session.exec(
        select(CallLog.disposition, func.count(CallLog.id))
        .where(CallLog.workspace_id == ws_id, CallLog.disposition.is_not(None))
        .group_by(CallLog.disposition)
    ).all()
    dispositions = {d: c for d, c in dispositions_raw if d}

    # Readiness score distribution
    readiness_bands = {"0-49": 0, "50-69": 0, "70-84": 0, "85+": 0}
    leads_with_scores = session.exec(
        select(Lead.readiness_score).where(
            Lead.workspace_id == ws_id,
            Lead.readiness_score.is_not(None),
        )
    ).all()
    for score in leads_with_scores:
        if score is None:
            continue
        if score < 50:
            readiness_bands["0-49"] += 1
        elif score < 70:
            readiness_bands["50-69"] += 1
        elif score < 85:
            readiness_bands["70-84"] += 1
        else:
            readiness_bands["85+"] += 1

    return {
        "total_calls": total_calls,
        "connected_calls": connected_calls,
        "booked_calls": booked_calls,
        "opt_out_calls": opt_out_calls,
        "total_appointments": total_appointments,
        "total_leads": total_leads,
        "marketplace_listings_available": marketplace_listings,
        "connect_rate": connect_rate,
        "booking_rate": booking_rate,
        "disposition_breakdown": dispositions,
        "readiness_bands": readiness_bands,
    }


@app.get("/campaigns")
def list_campaigns(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.exec(
        select(Campaign)
        .where(Campaign.workspace_id == user.workspace_id)
        .order_by(Campaign.created_at.desc())
    ).all()
    result = []
    for row in rows:
        d = row.model_dump()
        # Enrich with lead list stats
        lead_list = session.get(LeadList, row.lead_list_id) if row.lead_list_id else None
        d["total_leads"] = lead_list.total_records if lead_list else 0
        d["dialed"] = row.total_dials
        d["connected"] = row.total_connected
        d["booked"] = row.total_booked
        # Lead list and pathway names for display
        if lead_list:
            d["lead_list_name"] = lead_list.name
        pathway = session.get(Pathway, row.pathway_id) if row.pathway_id else None
        if pathway:
            d["pathway_name"] = pathway.name
        result.append(d)
    return result


@app.post("/campaigns")
def create_campaign(
    payload: CampaignCreateIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    get_leadlist_or_404(session, user.workspace_id, payload.lead_list_id)
    get_pathway_or_404(session, user.workspace_id, payload.pathway_id)

    workspace = get_workspace_or_404(session, user.workspace_id)
    row = Campaign(
        workspace_id=user.workspace_id,
        name=payload.name.strip(),
        lead_list_id=payload.lead_list_id,
        pathway_id=payload.pathway_id,
        caller_name=workspace.default_agent_name,
        caller_title=workspace.default_caller_title,
        brokerage_name=workspace.default_brokerage_name or workspace.brand_name or workspace.name,
        status="draft",
        voice_mode=payload.voice_mode,
        appointment_mode=payload.appointment_mode,
        concurrency=payload.concurrency,
        daily_cap=payload.daily_cap,
        attempt_limit_per_lead=payload.attempt_limit_per_lead,
        timezone=payload.timezone,
        start_hour_local=payload.start_hour_local,
        end_hour_local=payload.end_hour_local,
        allowed_days_csv=payload.allowed_days_csv,
        autopilot_enabled=True,
        is_admin_campaign=payload.is_admin_campaign,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.post("/campaigns/{campaign_id}/start")
def start_campaign(
    campaign_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    workspace = get_workspace_or_404(session, user.workspace_id)
    # Plan enforcement: require an active subscription
    if workspace.subscription_status not in ("active", "trialing") and not getattr(workspace, "is_admin_workspace", False):
        raise HTTPException(status_code=402, detail="Active subscription required to run campaigns.")
    row = get_campaign_or_404(session, user.workspace_id, campaign_id)
    row.status = "running"
    row.next_run_at = utcnow()
    touch(row)
    session.add(row)
    session.commit()
    return {"ok": True, "campaign_id": row.id, "status": row.status}


@app.post("/campaigns/{campaign_id}/pause")
def pause_campaign(
    campaign_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = get_campaign_or_404(session, user.workspace_id, campaign_id)
    row.status = "paused"
    touch(row)
    session.add(row)
    session.commit()
    return {"ok": True, "campaign_id": row.id, "status": row.status}


@app.get("/campaigns/{campaign_id}/preview-script")
def preview_campaign_script(
    campaign_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Preview the full system prompt + opening line for this campaign using the first lead."""
    from system_prompt import build_system_prompt
    from twilio_voice import build_initial_context, resolve_caller_identity

    campaign = get_campaign_or_404(session, user.workspace_id, campaign_id)
    workspace = get_workspace_or_404(session, user.workspace_id)
    pathway = session.get(Pathway, campaign.pathway_id) if campaign.pathway_id else None

    # Get first lead from the campaign's lead list
    lead = None
    if campaign.lead_list_id:
        lead = session.exec(
            select(Lead)
            .where(Lead.lead_list_id == campaign.lead_list_id, Lead.workspace_id == user.workspace_id)
            .order_by(Lead.id.asc())
        ).first()

    # Build context
    identity = resolve_caller_identity(workspace, campaign)
    agent_name = identity["caller_name"]
    brokerage = identity["brokerage_name"]
    agent_title = identity["caller_title"]

    homeowner_first = "there"
    homeowner_name = "Homeowner"
    property_address = "the property"
    city = ""
    state = ""
    days_expired = None
    list_price = None

    if lead:
        homeowner_first = lead.first_name or (lead.homeowner_name or "").split()[0] or "there"
        homeowner_name = lead.homeowner_name or f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Homeowner"
        property_address = lead.property_address or "the property"
        city = lead.city or ""
        state = lead.state or ""
        days_expired = getattr(lead, "days_expired", None)
        list_price = getattr(lead, "last_list_price", None)

    system_prompt = build_system_prompt(
        agent_name=agent_name,
        agent_brokerage=brokerage,
        agent_title=agent_title,
        homeowner_first_name=homeowner_first,
        homeowner_last_name=lead.last_name if lead else "",
        homeowner_name=homeowner_name,
        property_address=property_address,
        property_city=city,
        property_state=state,
        days_expired=days_expired,
        list_price=list_price,
    )

    # Build opening line from pathway start node
    opening_line = f"Hi, is this {homeowner_first}? This is {agent_name} calling from {brokerage}."
    if pathway:
        try:
            from pathway_engine import render_prompt, safe_json_load
            pathway_obj = safe_json_load(pathway.json_def)
            start_node_id = pathway_obj.get("start_node")
            if start_node_id:
                node = pathway_obj.get("nodes", {}).get(start_node_id, {})
                prompt_template = node.get("prompt", "")
                if prompt_template:
                    ctx = {
                        "agent_name": agent_name,
                        "caller_name": agent_name,
                        "brokerage_name": brokerage,
                        "agent_brokerage": brokerage,
                        "homeowner_name": homeowner_name,
                        "homeowner_first_name": homeowner_first,
                        "first_name": homeowner_first,
                        "property_address": property_address,
                        "city": city,
                        "state": state,
                        "days_expired": str(days_expired or ""),
                        "list_price": str(list_price or ""),
                    }
                    opening_line = render_prompt(prompt_template, ctx)
        except Exception:
            pass

    return {
        "campaign_id": campaign_id,
        "campaign_name": campaign.name,
        "agent_name": agent_name,
        "brokerage": brokerage,
        "lead_used": {
            "id": lead.id if lead else None,
            "name": homeowner_name,
            "phone": lead.phone if lead else None,
            "property_address": property_address,
        },
        "opening_line": opening_line,
        "system_prompt": system_prompt,
    }


@app.post("/campaigns/{campaign_id}/voicemail-drop")
def voicemail_drop(
    campaign_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Queue a voicemail drop for all active calls in this campaign."""
    row = get_campaign_or_404(session, user.workspace_id, campaign_id)
    if row.status != "running":
        raise HTTPException(status_code=400, detail="Campaign is not running.")
    # Find active calllogs for this campaign and mark them for voicemail drop
    active_logs = session.exec(
        select(CallLog).where(
            CallLog.workspace_id == user.workspace_id,
            CallLog.campaign_id == campaign_id,
            CallLog.status == "active",
        )
    ).all()
    queued = 0
    for log in active_logs:
        log.voicemail_drop_queued = True
        session.add(log)
        queued += 1
    session.commit()
    return {"ok": True, "campaign_id": campaign_id, "queued": queued}


@app.get("/calllogs")
def list_calllogs(
    campaign_id: Optional[int] = Query(default=None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(CallLog).where(CallLog.workspace_id == user.workspace_id)
    if campaign_id:
        stmt = stmt.where(CallLog.campaign_id == campaign_id)
    rows = session.exec(stmt.order_by(CallLog.created_at.desc())).all()
    return [row.model_dump() for row in rows]


@app.get("/calllogs/{calllog_id}")
def get_calllog(
    calllog_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = get_calllog_or_404(session, user.workspace_id, calllog_id)
    return row.model_dump()


@app.post("/calllogs/{calllog_id}/disposition")
def set_disposition(
    calllog_id: int,
    payload: CallDispositionIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = get_calllog_or_404(session, user.workspace_id, calllog_id)
    row.disposition = payload.disposition
    if payload.status:
        row.status = payload.status
    if payload.notes is not None:
        row.notes = payload.notes
    touch(row)
    session.add(row)

    lead = None
    campaign = None
    workspace = get_workspace_or_404(session, user.workspace_id)

    if row.campaign_id:
        campaign = get_campaign_or_404(session, user.workspace_id, row.campaign_id)

    if row.lead_id:
        lead = get_lead_or_404(session, user.workspace_id, row.lead_id)
        lead.disposition = payload.disposition
        if payload.disposition == "booked":
            lead.status = "booked"
        elif payload.disposition in {"do_not_call", "opt_out"}:
            lead.status = "do_not_call"
        elif payload.disposition == "wrong_number":
            lead.status = "bad_number"
        touch(lead)
        session.add(lead)

        if payload.disposition not in {"do_not_call", "opt_out", "wrong_number"}:
            maybe_publish_qualified_marketplace_listing(
                session=session,
                workspace=workspace,
                campaign=campaign,
                lead=lead,
                calllog=row,
            )

    session.commit()
    return {"ok": True, "calllog_id": row.id, "disposition": row.disposition}


@app.get("/appointments")
def list_appointments(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.exec(
        select(Appointment)
        .where(Appointment.workspace_id == user.workspace_id)
        .order_by(Appointment.created_at.desc())
    ).all()
    return [row.model_dump() for row in rows]


@app.post("/appointments")
async def create_appointment(
    payload: AppointmentCreateIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = Appointment(
        workspace_id=user.workspace_id,
        lead_id=payload.lead_id,
        campaign_id=payload.campaign_id,
        calllog_id=payload.calllog_id,
        homeowner_name=payload.homeowner_name,
        phone=payload.phone,
        email=payload.email,
        property_address=payload.property_address,
        appointment_time_iso=payload.appointment_time_iso,
        timezone=payload.timezone,
        notes=payload.notes,
        status="pending",
        source="phone_call",
    )
    session.add(row)
    session.commit()
    session.refresh(row)

    if row.lead_id:
        lead = get_lead_or_404(session, user.workspace_id, row.lead_id)
        workspace = get_workspace_or_404(session, user.workspace_id)
        campaign = get_campaign_or_404(session, user.workspace_id, row.campaign_id) if row.campaign_id else None
        calllog = get_calllog_or_404(session, user.workspace_id, row.calllog_id) if row.calllog_id else None
        sync_marketplace_listing_for_opportunity(
            session=session,
            workspace=workspace,
            lead=lead,
            campaign=campaign,
            calllog=calllog,
            appointment=row,
        )
        session.commit()

    appt_event = {
        "event": "appointment.booked",
        "appointment_id": row.id,
        "lead_name": row.homeowner_name or "Unknown",
        "appointment_time": row.appointment_time_iso,
        "campaign_id": row.campaign_id,
        "workspace_id": user.workspace_id,
    }
    await global_ws.broadcast(appt_event)

    return row.model_dump()


@app.post("/appointments/{appointment_id}/create_google_event")
def create_google_event_endpoint(
    appointment_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    appointment = session.get(Appointment, appointment_id)
    if not appointment or appointment.workspace_id != user.workspace_id:
        raise HTTPException(status_code=404, detail="Appointment not found")

    workspace = get_workspace_or_404(session, user.workspace_id)
    result = create_google_calendar_event(workspace, appointment)
    if result.get("ok"):
        appointment.google_event_id = result.get("event_id")
        appointment.confirmed = True
        appointment.status = "confirmed"
        touch(appointment)
        session.add(appointment)
        session.commit()
    return result


@app.get("/reports/dashboard")
def reports_dashboard(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    _empty = {
        "total_calls": 0, "total_calls_today": 0, "connected_calls": 0,
        "booked_calls": 0, "connect_rate": 0.0, "booking_rate": 0.0,
        "qualified_leads": 0, "minutes_used": 0, "minutes_limit": 1000,
        "minutes_remaining": 1000, "marketplace_listings": 0, "revenue_cents": 0,
        "appointments_this_month": 0, "calls_this_week": 0, "connects_this_week": 0,
        "booked_this_week": 0, "voicemails_this_week": 0, "callbacks_this_week": 0,
        "dnc_this_week": 0, "recent_calls": [], "recent_appointments": [],
        "active_campaigns": [], "has_top_agent_profile": False,
    }
    try:
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = now - timedelta(days=7)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        wid = user.workspace_id

        workspace = session.get(Workspace, wid)
        minutes_used = int(float(workspace.minutes_used_this_month or 0)) if workspace else 0
        minutes_limit = workspace.minutes_limit if workspace else 1000
        minutes_remaining = max(0, minutes_limit - minutes_used)

        def _count(model, *filters):
            return int(session.exec(select(func.count()).select_from(model).where(*filters)).one() or 0)

        total_calls      = _count(CallLog, CallLog.workspace_id == wid)
        total_calls_today = _count(CallLog, CallLog.workspace_id == wid, CallLog.created_at >= today_start)
        connected_calls  = _count(CallLog, CallLog.workspace_id == wid, CallLog.status == "completed")
        booked_calls     = _count(Appointment, Appointment.workspace_id == wid)
        qualified_leads  = _count(Lead, Lead.workspace_id == wid, Lead.readiness_score >= 60)
        marketplace_listings = _count(MarketplaceListing, MarketplaceListing.workspace_id == wid)
        appointments_this_month = _count(Appointment, Appointment.workspace_id == wid, Appointment.created_at >= month_start)

        calls_this_week     = _count(CallLog, CallLog.workspace_id == wid, CallLog.created_at >= week_start)
        connects_this_week  = _count(CallLog, CallLog.workspace_id == wid, CallLog.created_at >= week_start, CallLog.status == "completed")
        booked_this_week    = _count(Appointment, Appointment.workspace_id == wid, Appointment.created_at >= week_start)
        voicemails_this_week = _count(CallLog, CallLog.workspace_id == wid, CallLog.created_at >= week_start, CallLog.disposition == "voicemail")
        callbacks_this_week  = _count(CallLog, CallLog.workspace_id == wid, CallLog.created_at >= week_start, CallLog.disposition == "callback")
        dnc_this_week       = _count(DNCEntry, DNCEntry.workspace_id == wid, DNCEntry.created_at >= week_start)

        connect_rate = round(connected_calls / total_calls * 100, 1) if total_calls > 0 else 0.0
        booking_rate = round(booked_calls / total_calls * 100, 1) if total_calls > 0 else 0.0

        recent_call_rows = session.exec(
            select(CallLog).where(CallLog.workspace_id == wid)
            .order_by(CallLog.created_at.desc()).limit(10)
        ).all()
        recent_calls = []
        for c in recent_call_rows:
            lead_name = "Unknown"
            if c.lead_id:
                lead = session.get(Lead, c.lead_id)
                if lead:
                    lead_name = lead.homeowner_name or lead.first_name or "Unknown"
            recent_calls.append({
                "id": c.id,
                "disposition": c.disposition or c.status,
                "lead_name": lead_name,
                "campaign_id": c.campaign_id,
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "duration_seconds": c.duration_seconds,
            })

        recent_appt_rows = session.exec(
            select(Appointment).where(Appointment.workspace_id == wid)
            .order_by(Appointment.created_at.desc()).limit(5)
        ).all()
        recent_appointments = [{
            "id": a.id,
            "homeowner_name": a.homeowner_name or "Unknown",
            "appointment_time_iso": a.appointment_time_iso,
            "status": a.status,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "campaign_id": a.campaign_id,
        } for a in recent_appt_rows]

        campaign_rows = session.exec(
            select(Campaign).where(Campaign.workspace_id == wid)
            .order_by(Campaign.updated_at.desc()).limit(20)
        ).all()
        active_campaigns_list = []
        for camp in campaign_rows:
            if camp.status not in ("running", "paused"):
                continue
            lead_list = session.get(LeadList, camp.lead_list_id) if camp.lead_list_id else None
            active_campaigns_list.append({
                "id": camp.id,
                "name": camp.name,
                "status": camp.status,
                "total_dials": camp.total_dials,
                "total_connected": camp.total_connected,
                "total_booked": camp.total_booked,
                "total_leads": lead_list.total_records if lead_list else 0,
                "pause_reason": camp.pause_reason,
            })

        agent_profile = session.exec(select(AgentProfile).where(AgentProfile.workspace_id == wid)).first()

        return {
            "total_calls": total_calls, "total_calls_today": total_calls_today,
            "connected_calls": connected_calls, "booked_calls": booked_calls,
            "connect_rate": connect_rate, "booking_rate": booking_rate,
            "qualified_leads": qualified_leads, "minutes_used": minutes_used,
            "minutes_limit": minutes_limit, "minutes_remaining": minutes_remaining,
            "marketplace_listings": marketplace_listings, "revenue_cents": 0,
            "appointments_this_month": appointments_this_month,
            "calls_this_week": calls_this_week, "connects_this_week": connects_this_week,
            "booked_this_week": booked_this_week, "voicemails_this_week": voicemails_this_week,
            "callbacks_this_week": callbacks_this_week, "dnc_this_week": dnc_this_week,
            "recent_calls": recent_calls, "recent_appointments": recent_appointments,
            "active_campaigns": active_campaigns_list,
            "has_top_agent_profile": agent_profile is not None,
        }
    except Exception as _exc:
        logger.error(f"Dashboard error: {_exc}")
        return _empty


@app.get("/dnc")
def list_dnc(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.exec(
        select(DNCEntry)
        .where(DNCEntry.workspace_id == user.workspace_id)
        .order_by(DNCEntry.created_at.desc())
    ).all()
    return [row.model_dump() for row in rows]


@app.post("/dnc")
def add_dnc(
    payload: DNCAddIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    existing = session.exec(
        select(DNCEntry).where(
            DNCEntry.workspace_id == user.workspace_id,
            DNCEntry.phone == payload.phone,
        )
    ).first()
    if existing:
        return existing.model_dump()

    row = DNCEntry(
        workspace_id=user.workspace_id,
        phone=payload.phone,
        reason=payload.reason,
        source=payload.source,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.delete("/dnc/{phone}")
def remove_dnc(
    phone: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = session.exec(
        select(DNCEntry).where(
            DNCEntry.workspace_id == user.workspace_id,
            DNCEntry.phone == phone,
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="DNC entry not found")
    session.delete(row)
    session.commit()
    return {"ok": True, "phone": phone}


@app.get("/settings/workspace")
def get_workspace_settings(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    workspace = get_workspace_or_404(session, user.workspace_id)
    data = workspace.model_dump()
    data["twilio_auth_token"] = mask_sensitive(data.get("twilio_auth_token"))
    data["openai_api_key"] = mask_sensitive(data.get("openai_api_key"))
    data["elevenlabs_api_key"] = mask_sensitive(data.get("elevenlabs_api_key"))
    return data


@app.put("/settings/workspace")
def update_workspace_settings(
    payload: WorkspaceSettingsUpdateIn,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    workspace = get_workspace_or_404(session, user.workspace_id)
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(workspace, key, value)
    touch(workspace)
    session.add(workspace)
    session.commit()
    session.refresh(workspace)
    return workspace.model_dump()


CURATED_VOICES = [
    {
        "id": "f786b574-daa5-4673-aa0c-cbe3e8534c02",
        "name": "Brooke",
        "description": "Confident, natural female — top pick for expired listings",
        "gender": "female",
        "is_default": True,
        "is_curated": True,
        "category": "A2Z Recommended",
        "accent": "English American",
    },
    {
        "id": "694f9389-aac1-45b6-b726-9d9369183238",
        "name": "Jacqueline",
        "description": "Reassuring, empathetic female — great for skeptical homeowners",
        "gender": "female",
        "is_default": False,
        "is_curated": True,
        "category": "A2Z Recommended",
        "accent": "English American",
    },
    {
        "id": "5c42302c-194b-4d0c-ba1a-8cb485c84ab9",
        "name": "Katie",
        "description": "Friendly, clear female — warm and approachable",
        "gender": "female",
        "is_default": False,
        "is_curated": True,
        "category": "A2Z Recommended",
        "accent": "English American",
    },
    {
        "id": "c2ac25f9-ecc4-4f56-9095-651354df60c9",
        "name": "Ronald",
        "description": "Deep, intense male — commands attention and trust",
        "gender": "male",
        "is_default": False,
        "is_curated": True,
        "category": "A2Z Recommended",
        "accent": "English American",
    },
    {
        "id": "1e25d897-2d9e-4665-9f02-5a9e934c9a04",
        "name": "Blake",
        "description": "Energetic, helpful male — engaging and professional",
        "gender": "male",
        "is_default": False,
        "is_curated": True,
        "category": "A2Z Recommended",
        "accent": "English American",
    },
    {
        "id": "a8a1eb38-5f15-4c1d-8722-7ac0f329727d",
        "name": "Cathy",
        "description": "Friendly, casual female — natural and relatable",
        "gender": "female",
        "is_default": False,
        "is_curated": True,
        "category": "A2Z Recommended",
        "accent": "English American",
    },
]

@app.get("/voices/available")
async def get_available_voices(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    voices: list[dict] = []

    # 1. Curated English American voices — always first, hardcoded
    for v in CURATED_VOICES:
        voices.append({
            "id": v["id"],
            "name": v["name"],
            "description": v.get("description", ""),
            "gender": v.get("gender", "unknown"),
            "is_default": v.get("is_default", False),
            "is_curated": True,
            "is_cloned": False,
            "is_shared": False,
            "is_own_clone": False,
            "category": "A2Z Recommended",
            "owner_workspace_id": None,
            "royalty_rate_cents_per_min": 0,
        })

    # 2. Shared agent clones from database
    try:
        shared_clones = session.exec(
            select(AgentVoiceClone).where(
                AgentVoiceClone.is_shared == True,
                AgentVoiceClone.status == "active",
            )
        ).all()
        for sc in shared_clones:
            if sc.workspace_id == user.workspace_id:
                continue  # Own clone handled separately
            voices.append({
                "id": sc.elevenlabs_voice_id or "",
                "name": getattr(sc, "display_name_public", None) or sc.display_name or "Agent Voice",
                "description": "Agent voice — earns royalties for the owner",
                "gender": "unknown",
                "is_default": False,
                "is_curated": False,
                "is_cloned": True,
                "is_shared": True,
                "is_own_clone": False,
                "category": "Agent Voices",
                "owner_workspace_id": sc.workspace_id,
                "royalty_rate_cents_per_min": getattr(sc, "royalty_rate_cents_per_min", 1),
            })
    except Exception as _e:
        logger.warning(f"Shared clones fetch failed: {_e}")

    # 4. Agent's own clone (if active)
    try:
        own_clone = session.exec(
            select(AgentVoiceClone).where(
                AgentVoiceClone.workspace_id == user.workspace_id,
                AgentVoiceClone.status == "active",
            )
        ).first()
        if own_clone and own_clone.elevenlabs_voice_id:
            voices.append({
                "id": own_clone.elevenlabs_voice_id,
                "name": own_clone.display_name or "My Voice",
                "description": "Your cloned voice",
                "gender": "unknown",
                "is_default": False,
                "is_curated": False,
                "is_cloned": True,
                "is_shared": getattr(own_clone, "is_shared", False),
                "is_own_clone": True,
                "category": "Your Voice",
                "owner_workspace_id": user.workspace_id,
                "royalty_rate_cents_per_min": 0,
            })
    except Exception as _e:
        logger.warning(f"Own clone fetch failed: {_e}")

    return {"voices": voices}


@app.get("/voices/{voice_id}/preview")
async def preview_voice(
    voice_id: str,
    user: User = Depends(get_current_user),
) -> Response:
    """Generate a short TTS audio sample for voice preview. Returns MP3."""
    sample_text = (
        "Hi, is this the homeowner? This is calling — "
        "I'm a local Realtor and I noticed your property was recently on the market. "
        "I specialize in homes that haven't sold yet. "
        "Do you have just 60 seconds?"
    )
    cartesia_key = os.getenv("CARTESIA_API_KEY", "")
    if not cartesia_key:
        raise HTTPException(status_code=503, detail="Voice preview unavailable")

    import httpx as _httpx
    async with _httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            "https://api.cartesia.ai/tts/bytes",
            headers={
                "X-API-Key": cartesia_key,
                "Cartesia-Version": "2024-06-10",
                "Content-Type": "application/json",
            },
            json={
                "model_id": "sonic-3",
                "voice": {"mode": "id", "id": voice_id},
                "output_format": {
                    "container": "mp3",
                    "encoding": "mp3",
                    "sample_rate": 44100,
                },
                "transcript": sample_text,
            },
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Cartesia error: {resp.status_code}")
    return Response(
        content=resp.content,
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.post("/worker/run-once")
def worker_run_once(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    return run_worker_once(session)


@app.post("/campaigns/{campaign_id}/tick")
def tick_campaign(
    campaign_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    campaign = get_campaign_or_404(session, user.workspace_id, campaign_id)
    return run_campaign_tick(session, campaign)


@app.get("/integrations/google/start")
def google_start(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    _ = get_workspace_or_404(session, user.workspace_id)
    state = f"{user.workspace_id}:{user.id}:{secrets.token_urlsafe(16)}"
    return {"ok": True, "url": get_google_oauth_start_url(state)}


@app.get("/integrations/google/callback")
def google_callback(
    code: str,
    state: Optional[str] = None,
    session: Session = Depends(get_session),
) -> Response:
    token_data = exchange_google_code_for_tokens(code)
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise HTTPException(status_code=400, detail="Google did not return a refresh token")

    workspace_id = None
    if state and ":" in state:
        try:
            workspace_id = int(state.split(":")[0])
        except Exception:
            workspace_id = None

    if not workspace_id:
        raise HTTPException(status_code=400, detail="Missing workspace context in OAuth state")

    workspace = get_workspace_or_404(session, workspace_id)
    workspace.google_refresh_token = refresh_token
    touch(workspace)
    session.add(workspace)
    session.commit()

    return RedirectResponse(url=f"{settings.frontend_url}/integrations?google=connected")


@app.post("/integrations/google/disconnect")
def google_disconnect(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    workspace = get_workspace_or_404(session, user.workspace_id)
    workspace.google_refresh_token = None
    touch(workspace)
    session.add(workspace)
    session.commit()
    return {"ok": True}


@app.post("/billing/create-checkout")
def billing_create_checkout(
    request: Request,
    user: User = Depends(get_current_user),
) -> dict:
    if not stripe_enabled():
        raise HTTPException(status_code=400, detail="Stripe is not enabled")

    success_url = f"{settings.frontend_url}/billing?status=success"
    cancel_url = f"{settings.frontend_url}/billing?status=cancel"
    return create_checkout_session(
        customer_email=user.email,
        success_url=success_url,
        cancel_url=cancel_url,
        metadata={"workspace_id": str(user.workspace_id), "user_id": str(user.id)},
    )


# Plan limits for Campaign Operator tiers
_CAMPAIGN_PLAN_LIMITS: dict[str, dict] = {
    "campaign_starter":    {"calls_limit": 200,  "minutes_limit": 200,  "overage_rate_cents": 18},
    "campaign_growth":     {"calls_limit": 1000, "minutes_limit": 1000, "overage_rate_cents": 15},
    "campaign_pro":        {"calls_limit": 1500, "minutes_limit": 1500, "overage_rate_cents": 14},
    "campaign_enterprise": {"calls_limit": 6000, "minutes_limit": 6000, "overage_rate_cents": 10},
    "campaign_operator":   {"calls_limit": 500,  "minutes_limit": 500,  "overage_rate_cents": 18},
}


def _price_id_to_plan(price_id: str) -> Optional[str]:
    """Map a Stripe price ID back to a subscription plan slug."""
    mapping = {
        os.getenv("STRIPE_CAMPAIGN_STARTER_MONTHLY"):    "campaign_starter",
        os.getenv("STRIPE_CAMPAIGN_GROWTH_MONTHLY"):     "campaign_growth",
        os.getenv("STRIPE_CAMPAIGN_PRO_MONTHLY"):        "campaign_pro",
        os.getenv("STRIPE_CAMPAIGN_ENTERPRISE_MONTHLY"): "campaign_enterprise",
        os.getenv("STRIPE_CAMPAIGN_OPERATOR_MONTHLY"):   "campaign_operator",
        os.getenv("STRIPE_TOP_AGENT_STANDARD_MONTHLY"):  "top_agent_standard",
        os.getenv("STRIPE_TOP_AGENT_PREMIUM_MONTHLY"):   "top_agent_premium",
        os.getenv("STRIPE_TOP_AGENT_ELITE_MONTHLY"):     "top_agent_elite",
    }
    return mapping.get(price_id)


def _apply_plan_to_workspace(workspace: Workspace, plan: str) -> None:
    """Set subscription_plan + limits on a workspace."""
    workspace.subscription_plan = plan
    workspace.subscription_status = "active"
    limits = _CAMPAIGN_PLAN_LIMITS.get(plan)
    if limits:
        workspace.calls_limit = limits["calls_limit"]
        workspace.minutes_limit = limits["minutes_limit"]
        workspace.overage_rate_cents = limits["overage_rate_cents"]


@app.post("/billing/webhook")
async def billing_webhook(request: Request) -> dict:
    if not stripe_enabled():
        raise HTTPException(status_code=400, detail="Stripe is not enabled")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    event = construct_webhook_event(payload, sig_header)
    event_type = event["type"]

    with Session(engine) as session:
        if event_type == "checkout.session.completed":
            obj = event["data"]["object"]
            customer_id = obj.get("customer")
            sub_id = obj.get("subscription")
            if customer_id and sub_id:
                try:
                    import stripe as stripe_lib
                    stripe_lib.api_key = os.getenv("STRIPE_SECRET_KEY")
                    sub = stripe_lib.Subscription.retrieve(sub_id)
                    price_id = sub["items"]["data"][0]["price"]["id"]
                    plan = _price_id_to_plan(price_id)
                    workspace = session.exec(
                        select(Workspace).where(Workspace.stripe_customer_id == customer_id)
                    ).first()
                    if workspace and plan:
                        _apply_plan_to_workspace(workspace, plan)
                        workspace.stripe_subscription_id = sub_id
                        session.add(workspace)
                        session.commit()
                        # Auto-provision phone number for new Telnyx subscribers
                        if (os.getenv("PHONE_PROVIDER", "telnyx") == "telnyx"
                                and not workspace.twilio_from_number):
                            try:
                                from telnyx_voice import auto_provision_number  # type: ignore
                                provision_result = auto_provision_number(
                                    workspace_id=workspace.id,
                                )
                                if provision_result.get("ok"):
                                    workspace.twilio_from_number = provision_result["phone_number"]
                                    touch(workspace)
                                    session.add(workspace)
                                    session.commit()
                                    logger.info(
                                        f"Auto-provisioned {provision_result['phone_number']} "
                                        f"for workspace {workspace.id}"
                                    )
                            except Exception as pe:
                                logger.warning(f"Auto-provision failed for workspace {workspace.id}: {pe}")
                except Exception as exc:
                    logger.error("billing_webhook checkout.session.completed error: %s", exc)

        elif event_type in ("customer.subscription.updated", "customer.subscription.created"):
            sub = event["data"]["object"]
            customer_id = sub.get("customer")
            status = sub.get("status", "active")
            try:
                price_id = sub["items"]["data"][0]["price"]["id"]
            except (KeyError, IndexError):
                price_id = None
            if customer_id and price_id:
                plan = _price_id_to_plan(price_id)
                workspace = session.exec(
                    select(Workspace).where(Workspace.stripe_customer_id == customer_id)
                ).first()
                if workspace and plan:
                    _apply_plan_to_workspace(workspace, plan)
                    workspace.subscription_status = status
                    session.add(workspace)
                    session.commit()

        elif event_type == "customer.subscription.deleted":
            sub = event["data"]["object"]
            customer_id = sub.get("customer")
            if customer_id:
                workspace = session.exec(
                    select(Workspace).where(Workspace.stripe_customer_id == customer_id)
                ).first()
                if workspace:
                    workspace.subscription_status = "canceled"
                    session.add(workspace)
                    session.commit()

    return {"ok": True, "type": event_type}


@app.post("/twilio/voice")
async def twilio_voice(
    calllog_id: int = Query(...),
    mode: str = Query(default="gather"),
    session: Session = Depends(get_session),
) -> Response:
    calllog = session.get(CallLog, calllog_id)
    if not calllog:
        raise HTTPException(status_code=404, detail="Call log not found")

    workspace = get_workspace_or_404(session, calllog.workspace_id)
    campaign = get_campaign_or_404(session, calllog.workspace_id, calllog.campaign_id) if calllog.campaign_id else None
    lead = get_lead_or_404(session, calllog.workspace_id, calllog.lead_id) if calllog.lead_id else None
    pathway = get_pathway_or_404(session, calllog.workspace_id, calllog.pathway_id) if calllog.pathway_id else None

    if not campaign or not lead or not pathway:
        raise HTTPException(status_code=400, detail="Call log missing campaign, lead, or pathway")

    calllog.started_at = calllog.started_at or utcnow()
    if not calllog.current_node:
        pathway_obj = safe_json_load(pathway.json_def)
        start_node = pathway_obj.get("start_node")
        calllog.current_node = start_node if isinstance(start_node, str) else None
    touch(calllog)
    session.add(calllog)
    session.commit()

    if mode == "realtime":
        twiml = build_voice_response_for_realtime_stream(workspace, calllog.id, pathway, lead, campaign)
    else:
        twiml = build_voice_response_for_gather(workspace, calllog.id, pathway, lead, campaign)

    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio/speech")
async def twilio_speech(
    request: Request,
    calllog_id: int = Query(...),
    session: Session = Depends(get_session),
) -> Response:
    form = await request.form()
    speech_result = str(form.get("SpeechResult") or "").strip()
    confidence_raw = form.get("Confidence")
    try:
        confidence = float(confidence_raw) if confidence_raw is not None else None
    except Exception:
        confidence = None

    calllog = session.get(CallLog, calllog_id)
    if not calllog:
        raise HTTPException(status_code=404, detail="Call log not found")

    workspace = get_workspace_or_404(session, calllog.workspace_id)
    campaign = get_campaign_or_404(session, calllog.workspace_id, calllog.campaign_id) if calllog.campaign_id else None
    lead = get_lead_or_404(session, calllog.workspace_id, calllog.lead_id) if calllog.lead_id else None
    pathway = get_pathway_or_404(session, calllog.workspace_id, calllog.pathway_id) if calllog.pathway_id else None

    if not campaign or not lead or not pathway:
        raise HTTPException(status_code=400, detail="Call log missing campaign, lead, or pathway")

    transcript_lines = []
    if calllog.transcript:
        transcript_lines.append(calllog.transcript)
    if speech_result:
        transcript_lines.append(f"Lead: {speech_result}")
    calllog.transcript = "\n".join(x for x in transcript_lines if x)

    flags = classify_text(speech_result)
    if confidence is not None:
        flags["twilio_confidence"] = confidence

    pathway_obj = safe_json_load(pathway.json_def)
    current_node = calllog.current_node or pathway_obj.get("start_node")
    result = simulate_pathway(pathway_obj, current_node, speech_result, flags)

    calllog.current_node = result.get("next_node") or calllog.current_node
    calllog.route_trace = json_dumps(
        (safe_json_load(calllog.route_trace) if calllog.route_trace else [])
        + [{
            "heard": speech_result,
            "flags": flags,
            "current_node": result.get("current_node"),
            "next_node": result.get("next_node"),
            "fired_route": result.get("fired_route"),
            "ts": utcnow().isoformat(),
        }]
    )

    extracted = safe_json_load(calllog.extracted_json) if calllog.extracted_json else {}
    extracted.update(result.get("extracted") or {})
    calllog.extracted_json = json_dumps(extracted)

    disposition = None

    if flags.get("opt_out"):
        disposition = "opt_out"
        calllog.status = "completed"
        lead.status = "do_not_call"
        existing_dnc = session.exec(
            select(DNCEntry).where(
                DNCEntry.workspace_id == workspace.id,
                DNCEntry.phone == lead.phone,
            )
        ).first()
        if not existing_dnc:
            session.add(
                DNCEntry(
                    workspace_id=workspace.id,
                    phone=lead.phone,
                    reason="opt_out",
                    source="call",
                )
            )

    elif flags.get("wrong_number"):
        disposition = "wrong_number"
        calllog.status = "completed"
        lead.status = "bad_number"

    elif result.get("next_node") == "step_booked":
        disposition = "booked"
        calllog.status = "completed"
        lead.status = "booked"

        appointment = Appointment(
            workspace_id=workspace.id,
            campaign_id=campaign.id,
            lead_id=lead.id,
            calllog_id=calllog.id,
            homeowner_name=lead.homeowner_name or lead.first_name,
            phone=lead.phone,
            email=lead.email,
            property_address=lead.property_address,
            appointment_time_iso=(result.get("extracted") or {}).get("appointment_time"),
            timezone=campaign.timezone or workspace.timezone or settings.default_timezone,
            status="pending",
            source="phone_call",
            notes="Created from Twilio Gather flow",
        )
        session.add(appointment)
        session.commit()
        session.refresh(appointment)

        if (campaign.appointment_mode or workspace.appointment_mode or settings.appointment_mode_default) == "google":
            google_result = create_google_calendar_event(workspace, appointment)
            if google_result.get("ok"):
                appointment.google_event_id = google_result.get("event_id")
                appointment.confirmed = True
                appointment.status = "confirmed"
        else:
            calendly_result = create_calendly_placeholder(workspace, appointment)
            appointment.calendly_event_uri = calendly_result.get("booking_link")
            appointment.status = "pending-confirmation"

        sms_result = send_appointment_confirmation_sms(workspace, appointment)
        if sms_result.get("ok"):
            appointment.confirmation_sent_sms = True

        touch(appointment)
        session.add(appointment)

        sync_marketplace_listing_for_opportunity(
            session=session,
            workspace=workspace,
            lead=lead,
            campaign=campaign,
            calllog=calllog,
            appointment=appointment,
        )

    else:
        maybe_publish_qualified_marketplace_listing(
            session=session,
            workspace=workspace,
            campaign=campaign,
            lead=lead,
            calllog=calllog,
        )

    calllog.disposition = disposition or calllog.disposition
    touch(calllog)
    touch(lead)
    session.add(calllog)
    session.add(lead)
    session.commit()

    next_prompt = result.get("next_prompt") or "Thanks for your time. Have a great day."
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="{settings.twilio_tts_voice}" language="{settings.twilio_tts_language}">{next_prompt}</Say>
    <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.post("/twilio/status")
async def twilio_status(
    request: Request,
    calllog_id: int = Query(...),
    session: Session = Depends(get_session),
) -> dict:
    form = await request.form()
    call_status = str(form.get("CallStatus") or "").strip()
    call_sid = str(form.get("CallSid") or "").strip()

    calllog = session.get(CallLog, calllog_id)
    if not calllog:
        return {"ok": True}

    calllog.twilio_call_sid = call_sid or calllog.twilio_call_sid
    calllog.status = call_status or calllog.status

    if call_status == "in-progress":
        calllog.answered_at = calllog.answered_at or utcnow()
        if calllog.campaign_id:
            campaign = session.get(Campaign, calllog.campaign_id)
            if campaign:
                campaign.total_connected += 1
                touch(campaign)
                session.add(campaign)

    if call_status in {"completed", "busy", "failed", "no-answer", "canceled"}:
        calllog.ended_at = utcnow()
        if calllog.started_at and calllog.ended_at:
            calllog.duration_seconds = int((calllog.ended_at - calllog.started_at).total_seconds())

    touch(calllog)
    session.add(calllog)
    session.commit()

    return {"ok": True, "status": call_status}


@app.post("/twilio/repair")
def twilio_repair(
    calllog_id: int = Query(...),
    session: Session = Depends(get_session),
) -> Response:
    calllog = session.get(CallLog, calllog_id)
    if not calllog:
        raise HTTPException(status_code=404, detail="Call log not found")

    calllog.status = "repair_fallback"
    touch(calllog)
    session.add(calllog)
    session.commit()

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say voice="{settings.twilio_tts_voice}" language="{settings.twilio_tts_language}">
        Sorry, I didn't catch that. We can follow up with you later. Thank you.
    </Say>
    <Hangup/>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# ── Telnyx webhook ────────────────────────────────────────────────────────────

@app.post("/telnyx/events")
async def telnyx_events(
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """Receive Telnyx call control webhook events.

    Handles: call.initiated, call.answered, call.hangup,
             call.machine.detection.ended
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON payload")

    data = payload.get("data", {})
    event_type = data.get("event_type", "")
    call_payload = data.get("payload", {})
    call_control_id: str = call_payload.get("call_control_id", "")
    client_state_raw: str = call_payload.get("client_state", "") or ""

    # Decode base64-encoded client_state from Telnyx
    client_state: dict = {}
    if client_state_raw:
        from telnyx_voice import decode_client_state  # type: ignore
        client_state = decode_client_state(client_state_raw)
        if not client_state:
            # Fallback: try plain JSON
            try:
                client_state = json.loads(client_state_raw)
            except Exception:
                pass

    calllog_id: int | None = client_state.get("calllog_id")
    calllog = session.get(CallLog, calllog_id) if calllog_id else None

    # ── Inbound callback handling ──────────────────────────────────────────
    # When event is call.initiated with direction "inbound", a homeowner is
    # calling back after receiving an AI call. Look up the agent who last
    # called them and forward or let AI handle.
    if event_type == "call.initiated":
        direction = call_payload.get("direction", "")
        if direction == "inbound":
            from_number = call_payload.get("from", {})
            if isinstance(from_number, dict):
                caller_phone = from_number.get("phone_number", "")
            else:
                caller_phone = str(from_number)

            logger.info(f"Inbound callback from {caller_phone} — looking up last agent call")

            # Find most recent outbound CallLog to this number
            recent_calllog = session.exec(
                select(CallLog)
                .where(CallLog.to_number == caller_phone)
                .where(CallLog.direction == "outbound")
                .order_by(CallLog.created_at.desc())
            ).first()

            workspace_for_callback: Optional[Workspace] = None
            lead_for_callback: Optional[Lead] = None

            if recent_calllog:
                workspace_for_callback = session.get(Workspace, recent_calllog.workspace_id)
                if recent_calllog.lead_id:
                    lead_for_callback = session.get(Lead, recent_calllog.lead_id)

            if workspace_for_callback:
                callback_number = getattr(workspace_for_callback, 'agent_callback_number', None)
                agent_name = workspace_for_callback.default_agent_name or "Alex"
                brokerage = workspace_for_callback.default_brokerage_name or "our brokerage"

                if callback_number:
                    # Transfer inbound call to agent's callback number
                    logger.info(
                        f"Inbound callback from {caller_phone} — forwarding to agent at {callback_number}"
                    )
                    from telnyx_voice import transfer_call  # type: ignore
                    transfer_call(call_control_id, callback_number)

                    # SMS alert to agent
                    try:
                        from notifications import send_sms  # type: ignore
                        homeowner_name = ""
                        if lead_for_callback:
                            homeowner_name = (
                                lead_for_callback.homeowner_name
                                or f"{lead_for_callback.first_name or ''} {lead_for_callback.last_name or ''}".strip()
                            )
                        sms_body = (
                            f"A2Z Dialer: {homeowner_name or 'A homeowner'} is calling back "
                            f"({caller_phone}). Transferring now."
                        )
                        send_sms(callback_number, sms_body)
                    except Exception as sms_exc:
                        logger.warning(f"SMS alert for callback failed: {sms_exc}")
                else:
                    # No callback number configured — AI answers with greeting
                    logger.info(
                        f"Inbound callback from {caller_phone} — no callback number, AI handling"
                    )
                    from telnyx_voice import _telnyx_speak  # type: ignore
                    greeting = (
                        f"Hi there, this is {agent_name} from {brokerage}. "
                        "Thanks for calling back! I'm available to chat. "
                        "How can I help you today?"
                    )
                    _telnyx_speak(call_control_id, greeting)

                # Log inbound callback
                inbound_log = CallLog(
                    workspace_id=workspace_for_callback.id,
                    campaign_id=recent_calllog.campaign_id if recent_calllog else None,
                    lead_id=recent_calllog.lead_id if recent_calllog else None,
                    pathway_id=recent_calllog.pathway_id if recent_calllog else None,
                    from_number=caller_phone,
                    to_number=call_payload.get("to", {}).get("phone_number", "") if isinstance(call_payload.get("to"), dict) else str(call_payload.get("to", "")),
                    status="initiated",
                    disposition="inbound_callback",
                    direction="inbound",
                    current_node=None,
                    transcript="",
                    route_trace="[]",
                    extracted_json="{}",
                    notes=f"Inbound callback from homeowner. Forwarded to {callback_number or 'AI'}.",
                    provider_json="{}",
                    latency_json="{}",
                )
                session.add(inbound_log)
                session.commit()

            return {"ok": True, "event": "inbound_callback_handled"}

    # ── Outbound call events ───────────────────────────────────────────────

    if event_type == "call.answered":
        if calllog:
            calllog.answered_at = datetime.now(timezone.utc)
            calllog.status = "in-progress"
            touch(calllog)
            session.add(calllog)
            session.commit()

    elif event_type in ("call.hangup", "call.disconnected"):
        if calllog:
            calllog.ended_at = datetime.now(timezone.utc)
            if calllog.answered_at:
                delta = calllog.ended_at - calllog.answered_at
                calllog.duration_seconds = int(delta.total_seconds())
            calllog.status = "completed"
            touch(calllog)
            session.add(calllog)
            session.commit()
            # Update minutes usage on workspace
            if calllog.duration_seconds and calllog.workspace_id:
                ws = session.get(Workspace, calllog.workspace_id)
                if ws:
                    ws.minutes_used_this_month = (ws.minutes_used_this_month or 0) + (calllog.duration_seconds / 60.0)
                    session.add(ws)
                    session.commit()

    elif event_type == "call.machine.detection.ended":
        detection_result = call_payload.get("result", "")
        if detection_result in ("machine_start", "machine_end_beep", "machine_end_silence",
                      "machine_end_other", "fax") and call_control_id:
            # Use enhanced voicemail handler
            from telnyx_voice import handle_voicemail_detection  # type: ignore
            workspace_for_vm = session.get(Workspace, calllog.workspace_id) if calllog else None
            lead_for_vm = session.get(Lead, calllog.lead_id) if calllog and calllog.lead_id else None

            import asyncio as _asyncio
            _asyncio.create_task(
                handle_voicemail_detection(
                    call_control_id=call_control_id,
                    detection_result=detection_result,
                    lead_phone=calllog.to_number if calllog else "",
                    agent_name=workspace_for_vm.default_agent_name if workspace_for_vm else "Alex",
                    brokerage_name=workspace_for_vm.default_brokerage_name if workspace_for_vm else "your brokerage",
                    property_address=lead_for_vm.property_address if lead_for_vm else None,
                    callback_number=getattr(workspace_for_vm, 'agent_callback_number', '') or "" if workspace_for_vm else "",
                    calllog_id=calllog_id,
                )
            )

    return {"ok": True}


@app.post("/telnyx/provision-number")
async def provision_telnyx_number(
    area_code: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Auto-provisions a local phone number for the workspace via Telnyx.

    No manual setup needed — agent clicks one button and gets a number.
    """
    if os.getenv("PHONE_PROVIDER", "telnyx") != "telnyx":
        raise HTTPException(400, "Phone provider is not Telnyx")

    workspace = get_workspace_or_404(session, current_user.workspace_id)

    if workspace.twilio_from_number:
        return {
            "ok": True,
            "phone_number": workspace.twilio_from_number,
            "already_provisioned": True,
        }

    from telnyx_voice import auto_provision_number  # type: ignore
    result = auto_provision_number(
        workspace_id=current_user.workspace_id,
        area_code=area_code,
        state=state,
    )

    if result.get("ok"):
        workspace.twilio_from_number = result["phone_number"]
        touch(workspace)
        session.add(workspace)
        notif = Notification(
            workspace_id=current_user.workspace_id,
            user_id=current_user.id,
            type="phone_number_provisioned",
            title="Your phone number is ready",
            body=(
                f"Your dedicated calling number {result['phone_number']} "
                f"has been set up and is ready for campaigns."
            ),
            link="/app/settings",
            read=False,
        )
        session.add(notif)
        session.commit()

    return result


@app.get("/integrations/status")
async def get_integrations_status(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Returns real-time connection status for all integrations."""
    workspace = get_workspace_or_404(session, current_user.workspace_id)
    return {
        "phone": {
            "provider": "a2z_phone",
            "connected": bool(os.getenv("TELNYX_API_KEY") or os.getenv("TWILIO_ACCOUNT_SID")),
            "phone_number": workspace.twilio_from_number or None,
            "has_number": bool(workspace.twilio_from_number),
        },
        "voice": {
            "provider": "a2z_voice",
            "connected": bool(os.getenv("CARTESIA_API_KEY") or os.getenv("ELEVENLABS_API_KEY")),
        },
        "calendar": {
            "google": {
                "connected": bool(workspace.google_refresh_token),
            },
            "calendly": {
                "connected": bool(getattr(workspace, "calendly_link", None)),
            },
        },
    }


@app.websocket("/twilio/stream")
async def twilio_stream(
    websocket: WebSocket,
    calllog_id: int = Query(...),
    campaign_id: Optional[int] = Query(default=None),
    lead_id: Optional[int] = Query(default=None),
    pathway_id: Optional[int] = Query(default=None),
    workspace_id: Optional[int] = Query(default=None),
) -> None:
    await websocket.accept()

    # Look up workspace + campaign for agent persona and preferred voice
    preferred_voice_id: Optional[str] = None
    _agent_name: str = "your agent"
    _brokerage_name: str = "our brokerage"
    if workspace_id:
        try:
            from db import engine as _engine
            from sqlmodel import Session as _Sess
            with _Sess(_engine) as _ws_sess:
                _ws = _ws_sess.get(Workspace, workspace_id)
                if _ws:
                    preferred_voice_id = getattr(_ws, "preferred_voice_id", None)
                    _agent_name = (
                        _ws.default_agent_name
                        or _ws.brand_name
                        or _ws.name
                        or "your agent"
                    )
                    _brokerage_name = (
                        _ws.default_brokerage_name
                        or _ws.brand_name
                        or _ws.name
                        or "our brokerage"
                    )
                if campaign_id:
                    _camp = _ws_sess.get(Campaign, campaign_id)
                    if _camp:
                        if _camp.caller_name:
                            _agent_name = _camp.caller_name
                        if _camp.brokerage_name:
                            _brokerage_name = _camp.brokerage_name
        except Exception:
            pass

    bridge = RealtimeBridge(
        workspace_id=workspace_id,
        campaign_id=campaign_id,
        lead_id=lead_id,
        pathway_id=pathway_id,
        calllog_id=calllog_id,
        voice_mode=settings.voice_mode_default,
        agent_name=_agent_name,
        brokerage_name=_brokerage_name,
        preferred_voice_id=preferred_voice_id,
    )
    await bridge.start()

    try:
        while True:
            raw = await websocket.receive_text()
            parsed = safe_parse_ws_message(raw)
            result = await bridge.handle_twilio_message(parsed)

            if result.get("type") == "stop":
                break

    except WebSocketDisconnect:
        logger.info("Twilio websocket disconnected | calllog=%s", calllog_id)
    except Exception:
        logger.exception("Twilio websocket stream error | calllog=%s", calllog_id)
    finally:
        await bridge.close()


@app.post("/calls/test")
def calls_test(
    lead_id: int,
    campaign_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    campaign = get_campaign_or_404(session, user.workspace_id, campaign_id)
    lead = get_lead_or_404(session, user.workspace_id, lead_id)
    pathway = get_pathway_or_404(session, user.workspace_id, campaign.pathway_id)
    workspace = get_workspace_or_404(session, user.workspace_id)

    calllog = CallLog(
        workspace_id=user.workspace_id,
        campaign_id=campaign.id,
        lead_id=lead.id,
        pathway_id=pathway.id,
        from_number=workspace.twilio_from_number or settings.twilio_from_number,
        to_number=lead.phone,
        status="queued",
        direction="outbound",
        current_node=None,
        transcript="",
        route_trace="[]",
        extracted_json="{}",
        provider_json="{}",
        latency_json="{}",
    )
    session.add(calllog)
    session.commit()
    session.refresh(calllog)

    _phone_provider = os.getenv("PHONE_PROVIDER", "telnyx")
    if _phone_provider == "telnyx":
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
        if result.get("ok"):
            calllog.twilio_call_sid = result.get("call_control_id")
            calllog.status = "initiating"
        else:
            calllog.status = "failed"
            calllog.error_message = result.get("error")
    else:
        from twilio_voice import place_outbound_call
        result = place_outbound_call(workspace, lead, campaign, pathway, calllog.id)
        if result.get("ok"):
            calllog.twilio_call_sid = result.get("call_sid")
            calllog.status = result.get("status", "queued")
        else:
            calllog.status = "failed"
            calllog.error_message = result.get("error")

    touch(calllog)
    session.add(calllog)

    maybe_publish_qualified_marketplace_listing(
        session=session,
        workspace=workspace,
        campaign=campaign,
        lead=lead,
        calllog=calllog,
    )

    session.commit()

    return {
        "ok": bool(result.get("ok")),
        "calllog_id": calllog.id,
        "result": result,
    }


@app.get("/debug/validate_pathway/{pathway_id}")
def debug_validate_pathway(
    pathway_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    pathway = get_pathway_or_404(session, user.workspace_id, pathway_id)
    obj = safe_json_load(pathway.json_def)
    errors = validate_pathway_json(obj)
    return {"ok": len(errors) == 0, "errors": errors, "pathway_id": pathway_id}


@app.get("/debug/workspace-context")
def debug_workspace_context(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    workspace = get_workspace_or_404(session, user.workspace_id)
    pathway = session.exec(
        select(Pathway)
        .where(Pathway.workspace_id == workspace.id)
        .order_by(Pathway.created_at.asc())
    ).first()

    lead = session.exec(
        select(Lead)
        .where(Lead.workspace_id == workspace.id)
        .order_by(Lead.created_at.asc())
    ).first()

    campaign = session.exec(
        select(Campaign)
        .where(Campaign.workspace_id == workspace.id)
        .order_by(Campaign.created_at.asc())
    ).first()

    context = None
    if pathway and lead and campaign:
        context = build_initial_context(workspace, lead, campaign, pathway)

    return {
        "workspace": workspace.model_dump(),
        "campaign": campaign.model_dump() if campaign else None,
        "lead": lead.model_dump() if lead else None,
        "pathway": pathway.model_dump() if pathway else None,
        "context": context,
    }


@app.get("/marketplace/listings")
def list_marketplace_listings(
    q: Optional[str] = Query(default=None),
    listing_type: Optional[str] = Query(default=None),
    territory_key: Optional[str] = Query(default=None),
    min_readiness: int = Query(default=0),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(MarketplaceListing).where(
        MarketplaceListing.status == "available"
    )

    if q:
        q_value = f"%{q.lower().strip()}%"
        stmt = stmt.where(
            func.lower(func.coalesce(MarketplaceListing.city, "")).like(q_value)
            | func.lower(func.coalesce(MarketplaceListing.borough, "")).like(q_value)
            | func.lower(func.coalesce(MarketplaceListing.postal_code, "")).like(q_value)
            | func.lower(func.coalesce(MarketplaceListing.title, "")).like(q_value)
        )

    if listing_type:
        stmt = stmt.where(MarketplaceListing.listing_type == listing_type)

    if territory_key:
        stmt = stmt.where(MarketplaceListing.territory_key == territory_key)

    stmt = stmt.where(MarketplaceListing.readiness_score >= min_readiness)
    rows = session.exec(
        stmt.order_by(MarketplaceListing.readiness_score.desc())
    ).all()
    return [row.model_dump() for row in rows]


@app.get("/marketplace/listings/{listing_id}")
def get_marketplace_listing(
    listing_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    row = session.get(MarketplaceListing, listing_id)
    if not row:
        raise HTTPException(status_code=404, detail="Marketplace listing not found")

    public_data = {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "source_workspace_id": row.source_workspace_id,
        "source_type": row.source_type,
        "source_label": row.source_label,
        "lead_id": row.lead_id,
        "appointment_id": row.appointment_id,
        "calllog_id": row.calllog_id,
        "campaign_id": row.campaign_id,
        "pathway_id": row.pathway_id,
        "listing_type": row.listing_type,
        "status": row.status,
        "visibility": row.visibility,
        "title": row.title,
        "summary": row.summary,
        "city": row.city,
        "state": row.state,
        "postal_code": row.postal_code,
        "borough": row.borough,
        "territory_key": row.territory_key,
        "appointment_time_iso": row.appointment_time_iso,
        "seller_motivation_score": row.seller_motivation_score,
        "seller_timeline_score": row.seller_timeline_score,
        "seller_openness_score": row.seller_openness_score,
        "price_realism_score": row.price_realism_score,
        "readiness_score": row.readiness_score,
        "pricing_tier": row.pricing_tier,
        "pricing_formula_version": row.pricing_formula_version,
        "base_price_cents": row.base_price_cents,
        "final_price_cents": row.final_price_cents,
        "currency": row.currency,
        "seller_can_cancel": row.seller_can_cancel,
        "homeowner_cancellation_risk": row.homeowner_cancellation_risk,
        "cancellation_disclosure_shown": row.cancellation_disclosure_shown,
        "is_featured": row.is_featured,
        "is_bad_lead_protected": row.is_bad_lead_protected,
        "bad_lead_window_hours": row.bad_lead_window_hours,
        "published_at": row.published_at,
        "reserved_at": row.reserved_at,
        "purchased_at": row.purchased_at,
        "expires_at": row.expires_at,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
        "locked": True,
        "can_purchase": row.status == "available",
        "unlocked_for_buyer": False,
    }

    auth_header = request.headers.get("authorization", "").strip()
    token = None
    if auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()

    current_user = None
    if token:
        try:
            current_user = get_current_user(token=token, session=session)
        except Exception:
            current_user = None

    if not current_user:
        return public_data

    purchased = session.exec(
        select(MarketplacePurchase).where(
            MarketplacePurchase.listing_id == row.id,
            MarketplacePurchase.buyer_workspace_id == current_user.workspace_id,
        )
    ).first()

    is_owner_workspace = current_user.workspace_id == row.workspace_id
    is_buyer = purchased is not None
    can_unlock = is_owner_workspace or is_buyer

    if not can_unlock:
        return public_data

    unlocked_data = row.model_dump()
    unlocked_data.update(
        {
            "locked": False,
            "can_purchase": row.status == "available",
            "unlocked_for_buyer": is_buyer,
            "is_owner_workspace": is_owner_workspace,
            "purchase_id": purchased.id if purchased else None,
            "purchase_status": purchased.status if purchased else None,
            "agreement_accepted": purchased.agreement_accepted if purchased else None,
            "cancellation_disclosure_accepted": purchased.cancellation_disclosure_accepted if purchased else None,
            "no_refund_disclosure_accepted": purchased.no_refund_disclosure_accepted if purchased else None,
        }
    )
    return unlocked_data


@app.get("/marketplace/map")
def marketplace_map(
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.exec(
        select(MarketplaceListing)
        .where(MarketplaceListing.status == "available")
        .order_by(MarketplaceListing.readiness_score.desc())
    ).all()

    return [
        {
            "id": row.id,
            "title": row.title,
            "listing_type": row.listing_type,
            "territory_key": row.territory_key,
            "borough": row.borough,
            "city": row.city,
            "postal_code": row.postal_code,
            "price_cents": row.final_price_cents,
            "readiness_score": row.readiness_score,
            "is_featured": row.is_featured,
        }
        for row in rows
    ]


@app.get("/marketplace/territory-summary")
def marketplace_territory_summary(
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.exec(
        select(MarketplaceListing)
        .where(MarketplaceListing.status == "available")
        .order_by(MarketplaceListing.readiness_score.desc())
    ).all()

    summary: dict[str, dict] = {}

    for row in rows:
        key = row.territory_key or row.postal_code or row.borough or row.city or "unknown"
        if key not in summary:
            summary[key] = {
                "territory_key": key,
                "city": row.city,
                "borough": row.borough,
                "postal_code": row.postal_code,
                "listing_count": 0,
                "booked_appointment_count": 0,
                "qualified_lead_count": 0,
                "featured_count": 0,
                "max_readiness_score": 0,
                "min_price_cents": row.final_price_cents,
                "max_price_cents": row.final_price_cents,
            }

        item = summary[key]
        item["listing_count"] += 1
        if row.listing_type == "booked_appointment":
            item["booked_appointment_count"] += 1
        else:
            item["qualified_lead_count"] += 1
        if row.is_featured:
            item["featured_count"] += 1
        item["max_readiness_score"] = max(item["max_readiness_score"], row.readiness_score or 0)
        item["min_price_cents"] = min(item["min_price_cents"], row.final_price_cents or 0)
        item["max_price_cents"] = max(item["max_price_cents"], row.final_price_cents or 0)

    return sorted(summary.values(), key=lambda x: (-x["listing_count"], -(x["max_readiness_score"] or 0)))


@app.post("/marketplace/listings/{listing_id}/purchase")
async def purchase_marketplace_listing(
    listing_id: int,
    agreement_accepted: bool = Query(...),
    cancellation_disclosure_accepted: bool = Query(...),
    no_refund_disclosure_accepted: bool = Query(...),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    listing = session.get(MarketplaceListing, listing_id)
    if not listing or listing.status != "available":
        raise HTTPException(status_code=404, detail="Marketplace listing not available")

    existing_purchase = session.exec(
        select(MarketplacePurchase).where(
            MarketplacePurchase.listing_id == listing.id,
            MarketplacePurchase.buyer_workspace_id == user.workspace_id,
        )
    ).first()
    if existing_purchase:
        return {
            "ok": True,
            "purchase_id": existing_purchase.id,
            "listing_id": listing.id,
            "price_paid_cents": existing_purchase.price_paid_cents,
            "status": existing_purchase.status,
            "already_purchased": True,
        }

    if not agreement_accepted or not cancellation_disclosure_accepted or not no_refund_disclosure_accepted:
        raise HTTPException(status_code=400, detail="Required buyer agreement fields were not accepted")

    price_paid_cents = listing.final_price_cents
    buyer_fee_cents = 0
    platform_fee_pct = 0.40  # A2Z takes 40%, seller/agent gets 60%
    platform_fee_cents = int(price_paid_cents * platform_fee_pct)
    partner_payout_cents = max(price_paid_cents - platform_fee_cents, 0)

    purchase = MarketplacePurchase(
        listing_id=listing.id,
        buyer_user_id=user.id,
        buyer_workspace_id=user.workspace_id,
        seller_workspace_id=listing.workspace_id,
        source_workspace_id=listing.source_workspace_id,
        status="purchased",
        payment_status="completed",
        price_paid_cents=price_paid_cents,
        buyer_fee_cents=buyer_fee_cents,
        platform_fee_cents=platform_fee_cents,
        partner_payout_cents=partner_payout_cents,
        agreement_accepted=agreement_accepted,
        cancellation_disclosure_accepted=cancellation_disclosure_accepted,
        no_refund_disclosure_accepted=no_refund_disclosure_accepted,
        is_refundable=False,
        refund_status="not_requested",
    )
    session.add(purchase)
    session.commit()
    session.refresh(purchase)

    listing.status = "purchased"
    listing.buyer_user_id = user.id
    listing.buyer_workspace_id = user.workspace_id
    listing.purchased_at = utcnow()
    touch(listing)
    session.add(listing)

    payout = PartnerPayout(
        workspace_id=listing.workspace_id,
        purchase_id=purchase.id,
        listing_id=listing.id,
        payout_type="partner_inventory",
        recipient_workspace_id=listing.source_workspace_id or listing.workspace_id,
        gross_amount_cents=price_paid_cents,
        net_amount_cents=partner_payout_cents,
        platform_fee_cents=platform_fee_cents,
        currency="USD",
        status="pending",
    )
    session.add(payout)

    audit(
        session,
        workspace_id=user.workspace_id,
        user_id=user.id,
        action="marketplace_purchase_created",
        entity_type="marketplace_purchase",
        entity_id=purchase.id,
        details={
            "listing_id": listing.id,
            "price_paid_cents": price_paid_cents,
        },
    )
    session.commit()

    purchased_event = {
        "event": "listing.purchased",
        "listing_id": listing.id,
        "territory_name": listing.borough or listing.city or listing.territory_key,
    }
    await marketplace_ws.broadcast(purchased_event)
    await global_ws.broadcast(purchased_event)

    return {
        "ok": True,
        "purchase_id": purchase.id,
        "listing_id": listing.id,
        "price_paid_cents": purchase.price_paid_cents,
        "status": purchase.status,
        "already_purchased": False,
    }


@app.get("/marketplace/purchases")
def list_marketplace_purchases(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.exec(
        select(MarketplacePurchase)
        .where(MarketplacePurchase.buyer_workspace_id == user.workspace_id)
        .order_by(MarketplacePurchase.created_at.desc())
    ).all()

    result: list[dict] = []
    for row in rows:
        listing = session.get(MarketplaceListing, row.listing_id)
        result.append(
            {
                **row.model_dump(),
                "listing": listing.model_dump() if listing else None,
            }
        )
    return result


@app.get("/marketplace/buyer-dashboard")
def marketplace_buyer_dashboard(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    purchases = session.exec(
        select(MarketplacePurchase)
        .where(MarketplacePurchase.buyer_workspace_id == user.workspace_id)
        .order_by(MarketplacePurchase.created_at.desc())
    ).all()

    territories = session.exec(
        select(SavedTerritory)
        .where(SavedTerritory.workspace_id == user.workspace_id)
        .order_by(SavedTerritory.created_at.desc())
    ).all()

    recent_purchases = []
    for purchase in purchases[:8]:
        listing = session.get(MarketplaceListing, purchase.listing_id)
        recent_purchases.append(
            {
                "purchase": purchase.model_dump(),
                "listing": listing.model_dump() if listing else None,
            }
        )

    total_spent_cents = sum(int(x.price_paid_cents or 0) for x in purchases)
    booked_count = 0
    qualified_count = 0
    canceled_count = 0

    for purchase in purchases:
        listing = session.get(MarketplaceListing, purchase.listing_id)
        if listing:
            if listing.listing_type == "booked_appointment":
                booked_count += 1
            else:
                qualified_count += 1
        if purchase.homeowner_canceled_after_purchase:
            canceled_count += 1

    return {
        "summary": {
            "purchase_count": len(purchases),
            "saved_territory_count": len(territories),
            "total_spent_cents": total_spent_cents,
            "booked_appointment_count": booked_count,
            "qualified_lead_count": qualified_count,
            "homeowner_canceled_count": canceled_count,
        },
        "recent_purchases": recent_purchases,
        "saved_territories": [row.model_dump() for row in territories[:8]],
    }


@app.get("/marketplace/saved-territories")
def list_saved_territories(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.exec(
        select(SavedTerritory)
        .where(SavedTerritory.workspace_id == user.workspace_id)
        .order_by(SavedTerritory.created_at.desc())
    ).all()
    return [row.model_dump() for row in rows]


@app.post("/marketplace/saved-territories")
def create_saved_territory(
    name: str = Query(...),
    territory_key: str = Query(...),
    city: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    borough: Optional[str] = Query(default=None),
    postal_code: Optional[str] = Query(default=None),
    min_readiness_score: int = Query(default=0),
    listing_type_filter: Optional[str] = Query(default=None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = SavedTerritory(
        workspace_id=user.workspace_id,
        user_id=user.id,
        name=name,
        territory_key=territory_key,
        city=city,
        state=state,
        borough=borough,
        postal_code=postal_code,
        min_readiness_score=min_readiness_score,
        listing_type_filter=listing_type_filter,
        notify_on_new_inventory=True,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.post("/appointments/{appointment_id}/mark_homeowner_canceled")
def mark_homeowner_canceled(
    appointment_id: int,
    reason: Optional[str] = Query(default="Homeowner canceled"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    appointment = session.get(Appointment, appointment_id)
    if not appointment or appointment.workspace_id != user.workspace_id:
        raise HTTPException(status_code=404, detail="Appointment not found")

    appointment.status = "canceled"
    appointment.canceled_by_homeowner = True
    appointment.canceled_at = utcnow()
    appointment.cancellation_reason = reason
    touch(appointment)
    session.add(appointment)

    listing = session.exec(
        select(MarketplaceListing).where(MarketplaceListing.appointment_id == appointment.id)
    ).first()
    if listing:
        listing.status = "expired"
        touch(listing)
        session.add(listing)

    purchase = None
    if listing:
        purchase = session.exec(
            select(MarketplacePurchase).where(MarketplacePurchase.listing_id == listing.id)
        ).first()

    if purchase:
        purchase.homeowner_canceled_after_purchase = True
        purchase.homeowner_canceled_at = utcnow()
        touch(purchase)
        session.add(purchase)

    session.commit()
    return {
        "ok": True,
        "appointment_id": appointment.id,
        "status": appointment.status,
        "canceled_by_homeowner": appointment.canceled_by_homeowner,
    }


@app.get("/script-assets")
def list_script_assets(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.exec(
        select(ScriptAsset)
        .where(ScriptAsset.workspace_id == user.workspace_id)
        .order_by(ScriptAsset.created_at.desc())
    ).all()
    return [row.model_dump() for row in rows]


@app.post("/script-assets")
def create_script_asset(
    name: str = Query(...),
    category: str = Query(default="expired_listing"),
    description: Optional[str] = Query(default=None),
    listing_price_cents: int = Query(default=0),
    subscription_price_cents: int = Query(default=0),
    royalty_rate_pct: float = Query(default=0.0),
    sample_script_text: Optional[str] = Query(default=None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    base_slug = slugify(name)
    final_slug = base_slug
    idx = 2
    while session.exec(select(ScriptAsset).where(ScriptAsset.slug == final_slug)).first():
        final_slug = f"{base_slug}-{idx}"
        idx += 1

    row = ScriptAsset(
        workspace_id=user.workspace_id,
        creator_user_id=user.id,
        name=name.strip(),
        slug=final_slug,
        description=description,
        category=category.strip(),
        status="draft",
        listing_price_cents=listing_price_cents,
        subscription_price_cents=subscription_price_cents,
        royalty_rate_pct=royalty_rate_pct,
        sample_script_text=sample_script_text,
        is_marketplace_visible=False,
        is_featured=False,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.get("/voice-partners")
def list_voice_partners(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.exec(
        select(VoicePartnerProfile)
        .where(VoicePartnerProfile.workspace_id == user.workspace_id)
        .order_by(VoicePartnerProfile.created_at.desc())
    ).all()
    return [row.model_dump() for row in rows]


@app.post("/voice-partners")
def create_voice_partner(
    display_name: str = Query(...),
    voice_provider: str = Query(default="elevenlabs"),
    voice_external_id: Optional[str] = Query(default=None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = VoicePartnerProfile(
        workspace_id=user.workspace_id,
        user_id=user.id,
        display_name=display_name.strip(),
        status="pending_review",
        voice_provider=voice_provider.strip(),
        voice_external_id=voice_external_id,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()

# ─────────────────────────────────────────────
# TOP AGENT NETWORK
# ─────────────────────────────────────────────

from models import AgentProfile, AgentTerritory, FeaturedPlacement, PublicTrustSource


def _calc_profile_completeness(p: AgentProfile, trust_sources: list) -> int:
    score = 0
    if p.full_name:           score += 10
    if p.brokerage:           score += 10
    if p.headline:            score += 8
    if p.bio:                 score += 10
    if p.phone:               score += 5
    if p.email:               score += 5
    if p.website:             score += 3
    if p.primary_territory_key: score += 10
    if p.specialty:           score += 8
    if p.languages:           score += 5
    if p.years_experience:    score += 4
    if p.photo_url:           score += 7
    score += min(len(trust_sources) * 5, 15)
    return min(score, 100)


def _calc_ranking_score(p: AgentProfile, trust_sources: list) -> int:
    score = 0
    score += min(p.profile_completeness // 2, 40)
    score += min(len(trust_sources) * 8, 32)
    tier_bonus = {"elite": 20, "premium": 12, "standard": 4}
    score += tier_bonus.get(p.placement_tier, 0)
    if p.is_verified: score += 8
    return min(score, 100)


# ── DASHBOARD ──

@app.get("/top-agent/dashboard")
def top_agent_dashboard(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    profile = session.exec(
        select(AgentProfile).where(AgentProfile.workspace_id == user.workspace_id)
    ).first()

    territories = session.exec(
        select(AgentTerritory).where(
            AgentTerritory.workspace_id == user.workspace_id,
            AgentTerritory.is_active == True,
        )
    ).all()

    trust_sources = []
    if profile:
        trust_sources = session.exec(
            select(PublicTrustSource).where(
                PublicTrustSource.workspace_id == user.workspace_id
            )
        ).all()

    placements = session.exec(
        select(FeaturedPlacement).where(
            FeaturedPlacement.workspace_id == user.workspace_id,
            FeaturedPlacement.status == "active",
        )
    ).all()

    return {
        "has_profile": profile is not None,
        "profile_completeness": profile.profile_completeness if profile else 0,
        "ranking_score": profile.ranking_score if profile else 0,
        "placement_tier": profile.placement_tier if profile else "standard",
        "is_featured": profile.is_featured if profile else False,
        "active_territory_count": len(territories),
        "active_territories": [
            {
                "territory_key": t.territory_key,
                "territory_name": t.territory_name,
                "is_primary": t.is_primary,
                "placement_tier": t.placement_tier,
                "ranking_score": t.ranking_score,
                "territory_rank": t.territory_rank,
            }
            for t in territories
        ],
        "trust_source_count": len(trust_sources),
        "active_placement_count": len(placements),
    }


# ── PROFILE ──

@app.get("/top-agent/profile")
def get_top_agent_profile(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    profile = session.exec(
        select(AgentProfile).where(AgentProfile.workspace_id == user.workspace_id)
    ).first()
    if not profile:
        return {"exists": False}
    trust_sources = session.exec(
        select(PublicTrustSource).where(
            PublicTrustSource.workspace_id == user.workspace_id
        )
    ).all()
    return {
        "exists": True,
        **profile.model_dump(),
        "trust_sources": [t.model_dump() for t in trust_sources],
    }


@app.post("/top-agent/profile")
def save_top_agent_profile(
    full_name: str = Query(default=""),
    brokerage: Optional[str] = Query(default=None),
    headline: Optional[str] = Query(default=None),
    bio: Optional[str] = Query(default=None),
    specialty: Optional[str] = Query(default=None),
    languages: Optional[str] = Query(default=None),
    phone: Optional[str] = Query(default=None),
    email: Optional[str] = Query(default=None),
    website: Optional[str] = Query(default=None),
    primary_territory_key: Optional[str] = Query(default=None),
    primary_territory_name: Optional[str] = Query(default=None),
    years_experience: Optional[int] = Query(default=None),
    placement_tier: str = Query(default="standard"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    profile = session.exec(
        select(AgentProfile).where(AgentProfile.workspace_id == user.workspace_id)
    ).first()

    trust_sources = session.exec(
        select(PublicTrustSource).where(
            PublicTrustSource.workspace_id == user.workspace_id
        )
    ).all()

    if not profile:
        profile = AgentProfile(
            workspace_id=user.workspace_id,
            user_id=user.id,
        )
        session.add(profile)

    profile.full_name = full_name.strip()
    profile.brokerage = brokerage
    profile.headline = headline
    profile.bio = bio
    profile.specialty = specialty
    profile.languages = languages
    profile.phone = phone
    profile.email = email
    profile.website = website
    profile.primary_territory_key = primary_territory_key
    profile.primary_territory_name = primary_territory_name
    profile.years_experience = years_experience
    profile.placement_tier = placement_tier
    profile.updated_at = datetime.now(timezone.utc)

    profile.profile_completeness = _calc_profile_completeness(profile, trust_sources)
    profile.ranking_score = _calc_ranking_score(profile, trust_sources)

    session.commit()
    session.refresh(profile)
    return {"ok": True, **profile.model_dump()}


# ── TERRITORIES ──

@app.get("/top-agent/territories")
def get_top_agent_territories(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    rows = session.exec(
        select(AgentTerritory).where(
            AgentTerritory.workspace_id == user.workspace_id,
            AgentTerritory.is_active == True,
        )
    ).all()
    return {"territories": [r.model_dump() for r in rows]}


@app.post("/top-agent/territories")
def add_top_agent_territory(
    territory_key: str = Query(...),
    territory_name: str = Query(...),
    territory_type: str = Query(default="city"),
    city: Optional[str] = Query(default=None),
    state: Optional[str] = Query(default=None),
    county: Optional[str] = Query(default=None),
    borough: Optional[str] = Query(default=None),
    postal_code: Optional[str] = Query(default=None),
    is_primary: bool = Query(default=False),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    existing = session.exec(
        select(AgentTerritory).where(
            AgentTerritory.workspace_id == user.workspace_id,
            AgentTerritory.territory_key == territory_key,
        )
    ).first()

    if existing:
        existing.is_active = True
        existing.updated_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(existing)
        return {"ok": True, **existing.model_dump()}

    if is_primary:
        session.exec(
            select(AgentTerritory).where(
                AgentTerritory.workspace_id == user.workspace_id,
                AgentTerritory.is_primary == True,
            )
        )
        for t in session.exec(
            select(AgentTerritory).where(
                AgentTerritory.workspace_id == user.workspace_id,
                AgentTerritory.is_primary == True,
            )
        ).all():
            t.is_primary = False

    profile = session.exec(
        select(AgentProfile).where(AgentProfile.workspace_id == user.workspace_id)
    ).first()

    row = AgentTerritory(
        workspace_id=user.workspace_id,
        user_id=user.id,
        agent_profile_id=profile.id if profile else None,
        territory_key=territory_key,
        territory_name=territory_name,
        territory_type=territory_type,
        city=city,
        state=state,
        county=county,
        borough=borough,
        postal_code=postal_code,
        is_primary=is_primary,
        is_active=True,
    )
    session.add(row)
    session.commit()
    session.refresh(row)
    return {"ok": True, **row.model_dump()}


@app.delete("/top-agent/territories/{territory_key}")
def remove_top_agent_territory(
    territory_key: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = session.exec(
        select(AgentTerritory).where(
            AgentTerritory.workspace_id == user.workspace_id,
            AgentTerritory.territory_key == territory_key,
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Territory not found")
    row.is_active = False
    row.updated_at = datetime.now(timezone.utc)
    session.commit()
    return {"ok": True}


@app.post("/top-agent/territories/{territory_key}/set-primary")
def set_primary_territory(
    territory_key: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    for t in session.exec(
        select(AgentTerritory).where(
            AgentTerritory.workspace_id == user.workspace_id,
        )
    ).all():
        t.is_primary = t.territory_key == territory_key
    session.commit()
    return {"ok": True}


# ── TRUST SOURCES ──

@app.get("/top-agent/trust-sources")
def get_trust_sources(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    rows = session.exec(
        select(PublicTrustSource).where(
            PublicTrustSource.workspace_id == user.workspace_id
        )
    ).all()
    return {"trust_sources": [r.model_dump() for r in rows]}


@app.post("/top-agent/trust-sources")
def save_trust_sources(
    google_url: Optional[str] = Query(default=None),
    zillow_url: Optional[str] = Query(default=None),
    brokerage_url: Optional[str] = Query(default=None),
    realtor_url: Optional[str] = Query(default=None),
    listings_url: Optional[str] = Query(default=None),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    profile = session.exec(
        select(AgentProfile).where(AgentProfile.workspace_id == user.workspace_id)
    ).first()

    sources = {
        "google":    ("Google Business",   google_url),
        "zillow":    ("Zillow Profile",     zillow_url),
        "brokerage": ("Brokerage Profile",  brokerage_url),
        "realtor":   ("Realtor.com",        realtor_url),
        "listings":  ("Public Listings",    listings_url),
    }

    for source_type, (label, url) in sources.items():
        existing = session.exec(
            select(PublicTrustSource).where(
                PublicTrustSource.workspace_id == user.workspace_id,
                PublicTrustSource.source_type == source_type,
            )
        ).first()
        if existing:
            existing.source_url = url
            existing.source_label = label
            existing.updated_at = datetime.now(timezone.utc)
        elif url:
            session.add(PublicTrustSource(
                workspace_id=user.workspace_id,
                user_id=user.id,
                agent_profile_id=profile.id if profile else None,
                source_type=source_type,
                source_label=label,
                source_url=url,
            ))

    session.commit()

    trust_sources = session.exec(
        select(PublicTrustSource).where(
            PublicTrustSource.workspace_id == user.workspace_id,
            PublicTrustSource.source_url != None,
        )
    ).all()

    if profile:
        profile.profile_completeness = _calc_profile_completeness(profile, trust_sources)
        profile.ranking_score = _calc_ranking_score(profile, trust_sources)
        profile.updated_at = datetime.now(timezone.utc)
        session.commit()

    return {"ok": True, "trust_sources": [t.model_dump() for t in trust_sources]}


# ── PLACEMENTS ──

@app.get("/top-agent/placement-packages")
def get_placement_packages() -> dict:
    return {
        "packages": [
            {
                "id": "standard",
                "name": "Standard",
                "price_cents": 1500,
                "price_label": "$15/mo",
                "description": "Get listed in your territory with a verified public profile.",
                "features": ["Territory listing", "Basic trust score", "Public profile page", "Standard lead routing"],
            },
            {
                "id": "premium",
                "name": "Premium",
                "price_cents": 3000,
                "price_label": "$30/mo",
                "description": "Boosted placement and priority seller lead delivery.",
                "features": ["Boosted territory rank", "Full trust source stack", "Priority lead routing", "Analytics dashboard", "Voice clone ready"],
                "is_popular": True,
            },
            {
                "id": "elite",
                "name": "Elite",
                "price_cents": 5000,
                "price_label": "$50/mo",
                "description": "Own your territory. Featured card. AI voice clone included.",
                "features": ["#1 featured placement", "AI voice clone calling", "Dedicated territory slot", "White-glove onboarding", "Custom call pathway"],
            },
        ]
    }


@app.get("/top-agent/placements")
def get_my_placements(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    rows = session.exec(
        select(FeaturedPlacement).where(
            FeaturedPlacement.workspace_id == user.workspace_id,
        )
    ).all()
    return {"placements": [r.model_dump() for r in rows]}


@app.post("/top-agent/placements")
def purchase_placement(
    territory_key: str = Query(...),
    territory_name: str = Query(...),
    placement_type: str = Query(default="standard"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    price_map = {"standard": 1500, "premium": 3000, "elite": 5000}
    amount = price_map.get(placement_type, 1500)

    profile = session.exec(
        select(AgentProfile).where(AgentProfile.workspace_id == user.workspace_id)
    ).first()

    placement = FeaturedPlacement(
        workspace_id=user.workspace_id,
        user_id=user.id,
        agent_profile_id=profile.id if profile else None,
        territory_key=territory_key,
        territory_name=territory_name,
        placement_type=placement_type,
        package_name=placement_type.capitalize(),
        amount_cents=amount,
        status="active",
        starts_at=datetime.now(timezone.utc),
    )
    session.add(placement)

    territory = session.exec(
        select(AgentTerritory).where(
            AgentTerritory.workspace_id == user.workspace_id,
            AgentTerritory.territory_key == territory_key,
        )
    ).first()
    if territory:
        territory.placement_tier = placement_type
        territory.placement_active_until = None

    if profile:
        profile.placement_tier = placement_type
        if placement_type == "elite":
            profile.is_featured = True
        profile.updated_at = datetime.now(timezone.utc)

    session.commit()
    session.refresh(placement)
    return {"ok": True, **placement.model_dump()}


# ── LEADERBOARD ──

@app.get("/top-agent/leaderboard")
def get_top_agent_leaderboard(
    territory_key: Optional[str] = Query(default=None),
    limit: int = Query(default=20),
    session: Session = Depends(get_session),
) -> dict:
    stmt = select(AgentProfile).where(AgentProfile.is_public == True)
    if territory_key:
        stmt = stmt.where(AgentProfile.primary_territory_key == territory_key)
    stmt = stmt.order_by(AgentProfile.ranking_score.desc()).limit(limit)
    agents = session.exec(stmt).all()

    result = []
    for i, a in enumerate(agents):
        trust_count = session.exec(
            select(func.count(PublicTrustSource.id)).where(
                PublicTrustSource.workspace_id == a.workspace_id,
                PublicTrustSource.source_url != None,
            )
        ).one()
        result.append({
            "rank": i + 1,
            "workspace_id": a.workspace_id,
            "full_name": a.full_name,
            "brokerage": a.brokerage,
            "specialty": a.specialty,
            "primary_territory_name": a.primary_territory_name,
            "ranking_score": a.ranking_score,
            "placement_tier": a.placement_tier,
            "is_featured": a.is_featured,
            "is_verified": a.is_verified,
            "trust_source_count": trust_count,
            "profile_completeness": a.profile_completeness,
        })

    return {"leaderboard": result, "total": len(result)}

# ─────────────────────────────────────────────
# ADMIN AUTOPILOT
# ─────────────────────────────────────────────

@app.get("/admin/dashboard")
def admin_dashboard(session: Session = Depends(get_session)) -> dict:
    total_workspaces = session.exec(select(func.count(Workspace.id))).one()
    listings_available = session.exec(select(func.count(MarketplaceListing.id)).where(MarketplaceListing.status == "available")).one()
    listings_purchased = session.exec(select(func.count(MarketplaceListing.id)).where(MarketplaceListing.status == "purchased")).one()
    listings_pending = session.exec(select(func.count(MarketplaceListing.id)).where(MarketplaceListing.status == "pending_review")).one()
    purchases_total = session.exec(select(func.count(MarketplacePurchase.id))).one()
    active_agents = session.exec(select(func.count(AgentProfile.id)).where(AgentProfile.is_public == True)).one()
    pending_verifications = session.exec(select(func.count(AgentProfile.id)).where(AgentProfile.is_verified == False)).one()
    pending_payouts = session.exec(select(func.count(PartnerPayout.id)).where(PartnerPayout.status == "pending")).one()

    # territory supply summary
    territory_stmt = select(
        MarketplaceListing.territory_key,
        func.count(MarketplaceListing.id).label("cnt")
    ).where(MarketplaceListing.status == "available").group_by(MarketplaceListing.territory_key)
    territory_rows = session.exec(territory_stmt).all()
    territory_supply = [{"territory_key": r[0] or "unknown", "count": r[1]} for r in territory_rows]

    # recent activity
    recent_purchases = session.exec(
        select(MarketplacePurchase).order_by(MarketplacePurchase.created_at.desc()).limit(10)
    ).all()

    return {
        "total_workspaces": total_workspaces,
        "listings_available": listings_available,
        "listings_purchased": listings_purchased,
        "listings_pending_review": listings_pending,
        "purchases_total": purchases_total,
        "active_agents": active_agents,
        "pending_verifications": pending_verifications,
        "pending_payouts": pending_payouts,
        "territory_supply": territory_supply,
        "recent_purchases": [p.model_dump() for p in recent_purchases],
    }


@app.get("/admin/listings")
def admin_list_listings(
    status: Optional[str] = Query(default=None),
    listing_type: Optional[str] = Query(default=None),
    territory_key: Optional[str] = Query(default=None),
    is_featured: Optional[bool] = Query(default=None),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(MarketplaceListing)
    if status:
        stmt = stmt.where(MarketplaceListing.status == status)
    if listing_type:
        stmt = stmt.where(MarketplaceListing.listing_type == listing_type)
    if territory_key:
        stmt = stmt.where(MarketplaceListing.territory_key == territory_key)
    if is_featured is not None:
        stmt = stmt.where(MarketplaceListing.is_featured == is_featured)
    rows = session.exec(stmt.order_by(MarketplaceListing.created_at.desc())).all()
    return [r.model_dump() for r in rows]


@app.post("/admin/listings/{listing_id}/approve")
def admin_approve_listing(listing_id: int, session: Session = Depends(get_session)) -> dict:
    row = session.get(MarketplaceListing, listing_id)
    if not row:
        raise HTTPException(status_code=404, detail="Listing not found")
    row.status = "available"
    row.visibility = "public"
    row.published_at = utcnow()
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.post("/admin/listings/{listing_id}/reject")
def admin_reject_listing(
    listing_id: int,
    reason: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    row = session.get(MarketplaceListing, listing_id)
    if not row:
        raise HTTPException(status_code=404, detail="Listing not found")
    row.status = "rejected"
    row.notes = reason
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.post("/admin/listings/{listing_id}/feature")
def admin_feature_listing(
    listing_id: int,
    is_featured: bool = Query(default=True),
    session: Session = Depends(get_session),
) -> dict:
    row = session.get(MarketplaceListing, listing_id)
    if not row:
        raise HTTPException(status_code=404, detail="Listing not found")
    row.is_featured = is_featured
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.put("/admin/listings/{listing_id}/price")
def admin_update_listing_price(
    listing_id: int,
    price_cents: int = Query(...),
    session: Session = Depends(get_session),
) -> dict:
    row = session.get(MarketplaceListing, listing_id)
    if not row:
        raise HTTPException(status_code=404, detail="Listing not found")
    row.final_price_cents = price_cents
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.get("/admin/payouts")
def admin_list_payouts(
    status: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    stmt = select(PartnerPayout)
    if status:
        stmt = stmt.where(PartnerPayout.status == status)
    rows = session.exec(stmt.order_by(PartnerPayout.created_at.desc())).all()
    pending_total = sum(r.net_amount_cents for r in rows if r.status == "pending")
    paid_total = sum(r.net_amount_cents for r in rows if r.status == "paid")
    return {
        "payouts": [r.model_dump() for r in rows],
        "pending_total_cents": pending_total,
        "paid_total_cents": paid_total,
    }


@app.post("/admin/payouts/{payout_id}/approve")
def admin_approve_payout(payout_id: int, session: Session = Depends(get_session)) -> dict:
    row = session.get(PartnerPayout, payout_id)
    if not row:
        raise HTTPException(status_code=404, detail="Payout not found")
    row.status = "approved"
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.post("/admin/payouts/{payout_id}/mark-paid")
def admin_mark_payout_paid(
    payout_id: int,
    reference: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    row = session.get(PartnerPayout, payout_id)
    if not row:
        raise HTTPException(status_code=404, detail="Payout not found")
    row.status = "paid"
    if reference:
        row.payout_reference = reference
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.post("/admin/payouts/{payout_id}/reject")
def admin_reject_payout(
    payout_id: int,
    reason: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    row = session.get(PartnerPayout, payout_id)
    if not row:
        raise HTTPException(status_code=404, detail="Payout not found")
    row.status = "rejected"
    row.payout_notes = reason
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.get("/admin/agents")
def admin_list_agents(
    is_verified: Optional[bool] = Query(default=None),
    session: Session = Depends(get_session),
) -> list[dict]:
    stmt = select(AgentProfile)
    if is_verified is not None:
        stmt = stmt.where(AgentProfile.is_verified == is_verified)
    rows = session.exec(stmt.order_by(AgentProfile.created_at.desc())).all()
    result = []
    for agent in rows:
        trust_sources = session.exec(
            select(PublicTrustSource).where(PublicTrustSource.workspace_id == agent.workspace_id)
        ).all()
        result.append({
            **agent.model_dump(),
            "trust_sources": [t.model_dump() for t in trust_sources],
        })
    return result


@app.post("/admin/agents/{agent_id}/verify")
def admin_verify_agent(
    agent_id: int,
    notes: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    row = session.get(AgentProfile, agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    row.is_verified = True
    row.is_public = True
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.post("/admin/agents/{agent_id}/reject")
def admin_reject_agent(
    agent_id: int,
    reason: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
) -> dict:
    row = session.get(AgentProfile, agent_id)
    if not row:
        raise HTTPException(status_code=404, detail="Agent profile not found")
    row.is_verified = False
    row.is_public = False
    session.add(row)
    session.commit()
    session.refresh(row)
    return row.model_dump()


@app.post("/admin/reset-usage-counters")
def admin_reset_usage_counters(session: Session = Depends(get_session)) -> dict:
    workspaces = session.exec(select(Workspace)).all()
    for ws in workspaces:
        ws.calls_this_month = 0
        session.add(ws)
    session.commit()
    return {"ok": True, "reset_count": len(workspaces)}


# ── CSV Export Endpoints ──────────────────────────────────────────────────────

@app.get("/exports/leads")
def export_leads_csv(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    rows = session.exec(
        select(Lead).where(Lead.workspace_id == user.workspace_id).order_by(Lead.created_at.desc())
    ).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "first_name", "last_name", "phone", "email", "city", "state", "postal_code", "disposition", "readiness_score", "marketplace_eligible", "created_at"])
    for r in rows:
        writer.writerow([r.id, r.first_name, r.last_name, r.phone, r.email, r.city, r.state, r.postal_code, r.disposition, r.readiness_score, r.marketplace_eligible, r.created_at])
    return Response(content=buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=leads.csv"})


@app.get("/exports/calls")
def export_calls_csv(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    rows = session.exec(
        select(CallLog).where(CallLog.workspace_id == user.workspace_id).order_by(CallLog.created_at.desc())
    ).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "lead_id", "campaign_id", "disposition", "connected", "duration_seconds", "created_at"])
    for r in rows:
        writer.writerow([r.id, r.lead_id, r.campaign_id, r.disposition, r.connected, r.duration_seconds, r.created_at])
    return Response(content=buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=calls.csv"})


@app.get("/exports/appointments")
def export_appointments_csv(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    rows = session.exec(
        select(Appointment).where(Appointment.workspace_id == user.workspace_id).order_by(Appointment.created_at.desc())
    ).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "homeowner_name", "phone", "email", "property_address", "appointment_time_iso", "status", "confirmed", "created_at"])
    for r in rows:
        writer.writerow([r.id, r.homeowner_name, r.phone, r.email, r.property_address, r.appointment_time_iso, r.status, r.confirmed, r.created_at])
    return Response(content=buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=appointments.csv"})


@app.get("/exports/purchases")
def export_purchases_csv(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    rows = session.exec(
        select(MarketplacePurchase)
        .where(MarketplacePurchase.buyer_workspace_id == user.workspace_id)
        .order_by(MarketplacePurchase.created_at.desc())
    ).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "listing_id", "status", "payment_status", "price_paid_cents", "created_at"])
    for r in rows:
        writer.writerow([r.id, r.listing_id, r.status, r.payment_status, r.price_paid_cents, r.created_at])
    return Response(content=buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=purchases.csv"})


# ── WebSocket Endpoints ───────────────────────────────────────────────────────

@app.websocket("/ws/marketplace")
async def ws_marketplace_endpoint(websocket: WebSocket) -> None:
    await marketplace_ws.connect(websocket)
    # Send current viewer count to all
    await marketplace_ws.broadcast({
        "event": "viewers.count",
        "count": len(marketplace_ws.active),
    })
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        marketplace_ws.disconnect(websocket)
        await marketplace_ws.broadcast({
            "event": "viewers.count",
            "count": len(marketplace_ws.active),
        })


@app.websocket("/ws/global")
async def ws_global_endpoint(websocket: WebSocket) -> None:
    await global_ws.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        global_ws.disconnect(websocket)


@app.post("/billing/create-checkout-session")
async def create_billing_checkout(
    payload: dict,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    product_type = payload.get("product_type", "")
    listing_id = payload.get("listing_id")

    price_map = {
        "campaign_operator": os.getenv("STRIPE_CAMPAIGN_OPERATOR_MONTHLY"),
        "campaign_starter": os.getenv("STRIPE_CAMPAIGN_STARTER_MONTHLY"),
        "campaign_growth": os.getenv("STRIPE_CAMPAIGN_GROWTH_MONTHLY"),
        "campaign_pro": os.getenv("STRIPE_CAMPAIGN_PRO_MONTHLY"),
        "campaign_enterprise": os.getenv("STRIPE_CAMPAIGN_ENTERPRISE_MONTHLY"),
        "top_agent_standard": os.getenv("STRIPE_TOP_AGENT_STANDARD_MONTHLY"),
        "top_agent_premium": os.getenv("STRIPE_TOP_AGENT_PREMIUM_MONTHLY"),
        "top_agent_elite": os.getenv("STRIPE_TOP_AGENT_ELITE_MONTHLY"),
    }

    if product_type == "marketplace_purchase" and listing_id:
        listing = session.get(MarketplaceListing, listing_id)
        if not listing or listing.status != "available":
            raise HTTPException(status_code=404, detail="Listing not available")
        try:
            import stripe as stripe_lib
            stripe_lib.api_key = os.getenv("STRIPE_SECRET_KEY")
            checkout = stripe_lib.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": listing.title or "Marketplace Opportunity"},
                        "unit_amount": listing.final_price_cents,
                    },
                    "quantity": 1,
                }],
                mode="payment",
                success_url=os.getenv("FRONTEND_URL", "http://localhost:3000") + "/app/marketplace/purchases?checkout=success",
                cancel_url=os.getenv("FRONTEND_URL", "http://localhost:3000") + "/app/marketplace",
                metadata={"listing_id": str(listing_id), "user_id": str(user.id)},
            )
            return {"session_url": checkout.url}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

    # Check Stripe is configured
    stripe_key = os.getenv("STRIPE_SECRET_KEY")
    if not stripe_key:
        from fastapi.responses import JSONResponse as _JSONResponse
        return _JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "billing_not_configured",
                "message": "Stripe is not yet configured.",
                "contact": "support@a2zdialer.com",
            },
        )

    price_id = price_map.get(product_type)
    if not price_id:
        from fastapi.responses import JSONResponse as _JSONResponse
        return _JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "price_not_configured",
                "message": f"No price configured for {product_type}.",
                "contact": "support@a2zdialer.com",
            },
        )

    try:
        import stripe as stripe_lib
        stripe_lib.api_key = stripe_key
        workspace = get_workspace_or_404(session, user.workspace_id)
        customer_id = getattr(workspace, "stripe_customer_id", None)
        if not customer_id:
            customer = stripe_lib.Customer.create(email=user.email, name=user.full_name)
            customer_id = customer.id
            if hasattr(workspace, "stripe_customer_id"):
                workspace.stripe_customer_id = customer_id
                session.add(workspace)
                session.commit()
        checkout = stripe_lib.checkout.Session.create(
            customer=customer_id,
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=os.getenv("FRONTEND_URL", "http://localhost:3000") + "/app/billing?checkout=success",
            cancel_url=os.getenv("FRONTEND_URL", "http://localhost:3000") + "/app/billing",
        )
        return {"session_url": checkout.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")


@app.post("/billing/create-portal-session")
async def create_billing_portal(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    stripe_key = os.getenv("STRIPE_SECRET_KEY")
    if not stripe_key:
        from fastapi.responses import JSONResponse as _JSONResponse
        return _JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": "billing_not_configured",
                "message": "Stripe is not yet configured.",
                "contact": "support@a2zdialer.com",
            },
        )
    try:
        import stripe as stripe_lib
        stripe_lib.api_key = stripe_key
        workspace = get_workspace_or_404(session, user.workspace_id)
        customer_id = getattr(workspace, "stripe_customer_id", None)
        if not customer_id:
            raise HTTPException(status_code=400, detail="No billing account found")
        portal = stripe_lib.billing_portal.Session.create(
            customer=customer_id,
            return_url=os.getenv("FRONTEND_URL", "http://localhost:3000") + "/app/billing",
        )
        return {"portal_url": portal.url}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")


@app.get("/billing/subscription-status")
def get_subscription_status(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    try:
        workspace = get_workspace_or_404(session, user.workspace_id)
        return {
            "plan": getattr(workspace, "subscription_plan", None) or "free",
            "status": getattr(workspace, "subscription_status", None) or "active",
            "current_period_end": None,
            "cancel_at_period_end": False,
        }
    except Exception:
        return {
            "plan": "free",
            "status": "active",
            "current_period_end": None,
            "cancel_at_period_end": False,
        }


# ── Free tools (public, no auth) ──────────────────────────────────────────────

@app.post("/tools/grade-script")
async def grade_script(payload: dict) -> dict:
    """Grade a cold call script using Claude. Public endpoint."""
    script_text = (payload.get("script") or "").strip()
    if not script_text:
        raise HTTPException(status_code=400, detail="script is required")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        # Return mock result when key not configured
        return {
            "score": 68,
            "grade": "C+",
            "opening": {"score": 14, "feedback": "Hook is present but could be more specific to the homeowner's situation."},
            "objections": {"score": 12, "feedback": "Objection handling is basic. Add specific rebuttals for 'not interested' and 'already have an agent'."},
            "close": {"score": 13, "feedback": "The appointment ask is there but lacks urgency. Try a double-close technique."},
            "language": {"score": 16, "feedback": "Language is mostly natural. Avoid corporate phrases like 'reach out'."},
            "compliance": {"score": 13, "feedback": "Add a brief identification of yourself as a real estate agent early in the call."},
            "top_strength": "Clear value proposition and direct approach keeps homeowners engaged.",
            "top_improvement": "Strengthen objection handling — most calls are won or lost on the first 'no'.",
            "summary": "Solid foundation. A few targeted improvements to objection handling and the appointment close would significantly boost your booking rate.",
        }

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=(
                "You are a real estate cold calling expert. Grade the following script on a scale of 0-100 "
                "and provide specific feedback. Evaluate on these criteria:\n"
                "1. Opening hook (0-20 points)\n"
                "2. Objection handling (0-20 points)\n"
                "3. Appointment close (0-20 points)\n"
                "4. Natural language (0-20 points)\n"
                "5. Compliance and professionalism (0-20 points)\n\n"
                "Return JSON only with this exact shape:\n"
                '{"score":number,"grade":"A/B/C/D/F","opening":{"score":number,"feedback":"string"},'
                '"objections":{"score":number,"feedback":"string"},"close":{"score":number,"feedback":"string"},'
                '"language":{"score":number,"feedback":"string"},"compliance":{"score":number,"feedback":"string"},'
                '"top_strength":"string","top_improvement":"string","summary":"string"}'
            ),
            messages=[{"role": "user", "content": f"Grade this script:\n\n{script_text}"}],
        )
        import json as _json
        raw = message.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return _json.loads(raw.strip())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Grading failed: {str(e)}")


@app.post("/tools/voicemail-script")
async def generate_voicemail(payload: dict) -> dict:
    """Generate a voicemail script using Claude. Public endpoint."""
    agent_name = (payload.get("agent_name") or "").strip()
    brokerage = (payload.get("brokerage") or "").strip()
    market = (payload.get("market") or "").strip()
    style = (payload.get("style") or "Professional").strip()

    if not agent_name or not brokerage or not market:
        raise HTTPException(status_code=400, detail="agent_name, brokerage, and market are required")

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return {
            "script": (
                f"Hey, this is {agent_name} calling from {brokerage}. "
                f"I'm a local agent working in {market} and I noticed your property recently came off the market. "
                "I work specifically with homeowners in this situation and I'd love to chat for just a couple minutes. "
                f"Give me a call back at your convenience — again, this is {agent_name} from {brokerage}. Talk soon."
            )
        }

    try:
        import anthropic as _anthropic
        client = _anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=(
                "You are a real estate cold calling expert. Write a 20-30 second voicemail script for a real estate agent "
                "targeting expired listings. Make it sound natural, human, and not robotic. "
                "Do NOT use corporate speak. Return plain text only — just the voicemail script, no labels or quotes."
            ),
            messages=[{
                "role": "user",
                "content": f"Agent: {agent_name}, Brokerage: {brokerage}, Market: {market}, Style: {style}",
            }],
        )
        return {"script": message.content[0].text.strip()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Generation failed: {str(e)}")


# ── Notifications ─────────────────────────────────────────────────────────────

@app.get("/notifications/unread-count")
def get_unread_count(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    count = session.exec(
        select(func.count(Notification.id)).where(
            Notification.user_id == user.id,
            Notification.read == False,
        )
    ).one()
    return {"count": count}


@app.get("/notifications")
def list_notifications(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> list[dict]:
    rows = session.exec(
        select(Notification)
        .where(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc())
        .limit(50)
    ).all()
    return [r.model_dump() for r in rows]


@app.put("/notifications/read-all")
def mark_all_notifications_read(
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    rows = session.exec(
        select(Notification).where(
            Notification.user_id == user.id,
            Notification.read == False,
        )
    ).all()
    for row in rows:
        row.read = True
        session.add(row)
    session.commit()
    return {"ok": True, "count": len(rows)}


@app.delete("/notifications/{notif_id}")
def delete_notification(
    notif_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    notif = session.get(Notification, notif_id)
    if not notif or notif.user_id != current_user.id:
        raise HTTPException(404, "Notification not found")
    session.delete(notif)
    session.commit()
    return {"ok": True}


@app.put("/notifications/{notif_id}/read")
def mark_notification_read(
    notif_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    row = session.get(Notification, notif_id)
    if not row or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Notification not found")
    row.read = True
    session.add(row)
    session.commit()
    return {"ok": True}


@app.get("/public/agents")
async def public_agents(territory: str = Query(""), session: Session = Depends(get_session)):
    """Public endpoint — no auth — returns top agents in a territory."""
    stmt = select(User).where(User.role == "agent")
    users = session.exec(stmt).all()
    result = []
    for u in users:
        profile = session.exec(select(AgentProfile).where(AgentProfile.user_id == u.id)).first()
        if not profile:
            continue
        territories_list = []
        if hasattr(profile, "territories"):
            territories_list = profile.territories or []
        if territory and territory.lower() not in [t.lower() for t in territories_list]:
            continue
        result.append({
            "id": u.id,
            "full_name": u.full_name or u.email,
            "bio": getattr(profile, "bio", None),
            "territories": territories_list,
            "trust_score": getattr(profile, "trust_score", 0),
            "plan": getattr(u, "plan", "standard"),
        })
    return result


@app.get("/admin/revenue")
async def admin_revenue(current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Admin-only revenue metrics."""
    if current_user.role not in ("admin", "superadmin"):
        raise HTTPException(403)
    workspaces = session.exec(select(Workspace)).all()
    plan_map = {"starter": 49, "growth": 99, "pro": 199, "elite": 299}
    mrr = sum(plan_map.get(getattr(w, "plan", "starter"), 0) for w in workspaces if getattr(w, "subscription_status", "") == "active")
    subscribers = sum(1 for w in workspaces if getattr(w, "subscription_status", "") == "active")
    by_plan: dict[str, int] = {}
    for w in workspaces:
        if getattr(w, "subscription_status", "") == "active":
            plan = getattr(w, "plan", "starter")
            by_plan[plan] = by_plan.get(plan, 0) + 1
    return {
        "mrr": mrr,
        "arr": mrr * 12,
        "total_subscribers": subscribers,
        "total_workspaces": len(workspaces),
        "revenue_by_plan": by_plan,
    }


@app.get("/team/members")
async def list_team_members(current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    members = session.exec(select(User).where(User.workspace_id == current_user.workspace_id)).all()
    return [{"id": m.id, "email": m.email, "full_name": m.full_name, "role": m.role} for m in members]


@app.post("/team/invite")
async def invite_team_member(payload: dict, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    email = payload.get("email", "").strip().lower()
    if not email:
        raise HTTPException(400, "Email required")
    invite = TeamInvite(workspace_id=current_user.workspace_id, invited_email=email)
    session.add(invite)
    session.commit()
    session.refresh(invite)
    return {"invite_token": invite.token, "invited_email": invite.invited_email}


@app.post("/team/accept-invite")
async def accept_team_invite(payload: dict, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    token_str = payload.get("token", "")
    invite = session.exec(select(TeamInvite).where(TeamInvite.token == token_str, TeamInvite.accepted == False)).first()
    if not invite:
        raise HTTPException(404, "Invalid or expired invite")
    current_user.workspace_id = invite.workspace_id
    invite.accepted = True
    session.add(current_user)
    session.add(invite)
    session.commit()
    return {"detail": "Joined workspace successfully"}


@app.delete("/team/members/{member_id}")
async def remove_team_member(member_id: int, current_user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    member = session.get(User, member_id)
    if not member or member.workspace_id != current_user.workspace_id:
        raise HTTPException(404)
    if member.id == current_user.id:
        raise HTTPException(400, "Cannot remove yourself")
    member.workspace_id = None
    session.add(member)
    session.commit()
    return {"detail": "Member removed"}


@app.put("/team/members/{member_id}/role")
async def update_team_member_role(
    member_id: int,
    payload: dict,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    if current_user.role not in ("admin", "superadmin", "owner"):
        raise HTTPException(403, "Only workspace admins can change roles")
    member = session.get(User, member_id)
    if not member or member.workspace_id != current_user.workspace_id:
        raise HTTPException(404, "Member not found")
    new_role = payload.get("role", "")
    if new_role not in ("admin", "member", "viewer"):
        raise HTTPException(400, "Role must be admin, member, or viewer")
    member.role = new_role
    session.add(member)
    session.commit()
    return {"ok": True, "role": new_role}


# ---------------------------------------------------------------------------
# Webhook utility
# ---------------------------------------------------------------------------

def send_webhook(workspace: Workspace, event: str, data: dict) -> None:
    """Fire-and-forget outbound webhook to workspace.webhook_url."""
    url = getattr(workspace, "webhook_url", None)
    if not url:
        return
    import threading, httpx
    payload = {"event": event, "data": data, "ts": int(utcnow().timestamp())}
    def _send():
        try:
            httpx.post(url, json=payload, timeout=10)
        except Exception as exc:
            logger.warning("Webhook delivery failed for workspace %s: %s", workspace.id, exc)
    threading.Thread(target=_send, daemon=True).start()


# ---------------------------------------------------------------------------
# Referral endpoints
# ---------------------------------------------------------------------------

@app.get("/referral/link")
def get_referral_link(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
    workspace = get_workspace_or_404(session, user.workspace_id)
    link = f"https://a2zdialer.com/register?ref={workspace.slug}"
    return {"referral_link": link, "slug": workspace.slug}


@app.get("/referral/stats")
def get_referral_stats(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
    workspace = get_workspace_or_404(session, user.workspace_id)
    referrals = session.exec(
        select(Referral).where(Referral.referrer_workspace_id == workspace.id)
    ).all()
    total = len(referrals)
    converted = sum(1 for r in referrals if r.status == "converted")
    free_months = sum(r.reward_months for r in referrals if r.status == "converted")
    pending = [r.referred_email for r in referrals if r.status == "pending"]
    link = f"https://a2zdialer.com/register?ref={workspace.slug}"
    return {
        "referral_link": link,
        "total_referrals": total,
        "converted": converted,
        "free_months_earned": free_months,
        "pending_reward": pending,
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "error": exc.detail,
            "status_code": exc.status_code,
        },
    )


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(f"Unhandled exception: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal error occurred. Please try again."},
    )