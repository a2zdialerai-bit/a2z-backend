from __future__ import annotations

import csv
import io
import json
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlmodel import Session, func, select

from auth import authenticate_user, create_access_token, get_current_user, hash_password
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
    Appointment,
    AuditLog,
    CallLog,
    Campaign,
    DNCEntry,
    Lead,
    LeadList,
    Pathway,
    UsageEvent,
    User,
    Workspace,
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


app = FastAPI(title="A2Z Dialer API", version="1.0.0")


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


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def create_default_pathway_json() -> dict:
    return {
        "start_node": "step1_intro",
        "nodes": {
            "step1_intro": {
                "type": "say",
                "prompt": "Hi, this is {{caller_name}} with {{brokerage_name}} calling about {{property_address}}.",
                "fallback_next": "step2_listen",
            },
            "step2_listen": {
                "type": "listen",
                "prompt": "I just wanted to ask, is the home still available?",
                "extract": {
                    "listing_status": True,
                },
                "routes": [
                    {"when": "mentions_sold == true", "next": "step_sold"},
                    {"when": "mentions_already_listed == true", "next": "step_listed"},
                    {"when": "mentions_available == true", "next": "step_available"},
                    {"when": "opt_out == true", "next": "step_opt_out"},
                ],
                "fallback_next": "step_followup",
            },
            "step_followup": {
                "type": "say",
                "prompt": "Got it. I was reaching out because we work with owners whose listings came off the market and wanted to see if you were still open to selling.",
                "fallback_next": "step_callback_offer",
            },
            "step_available": {
                "type": "say",
                "prompt": "Understood. If you're still open to selling, would later today at 4 PM or 6 PM be better for a quick conversation?",
                "fallback_next": "step_callback_offer",
            },
            "step_callback_offer": {
                "type": "listen",
                "prompt": "Which time works better for you?",
                "extract": {
                    "appointment_time": True,
                },
                "routes": [
                    {"when": "time_is_4pm == true", "next": "step_booked"},
                    {"when": "time_is_6pm == true", "next": "step_booked"},
                    {"when": "user_requests_other_time == true", "next": "step_other_time"},
                    {"when": "opt_out == true", "next": "step_opt_out"},
                ],
                "fallback_next": "step_other_time",
            },
            "step_other_time": {
                "type": "say",
                "prompt": "No problem. What time tends to work better for you?",
                "fallback_next": "step_booked",
            },
            "step_booked": {
                "type": "end",
                "prompt": "Perfect, I’ll make a note of that and send a confirmation. Thank you.",
            },
            "step_sold": {
                "type": "end",
                "prompt": "Thanks for letting me know. I appreciate your time.",
            },
            "step_listed": {
                "type": "end",
                "prompt": "Understood, thank you for the update and best of luck with the sale.",
            },
            "step_opt_out": {
                "type": "end",
                "prompt": "Understood. We’ll make sure not to call again. Have a good day.",
            },
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


@app.post("/auth/register")
def register(payload: AuthRegisterIn, session: Session = Depends(get_session)) -> dict:
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
        name="Default Expired Listing Pathway",
        description="Starter deterministic pathway",
        is_active=True,
        version=1,
        json_def=json_dumps(create_default_pathway_json()),
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
def login(payload: AuthLoginIn, session: Session = Depends(get_session)) -> dict:
    user = authenticate_user(session, payload.email, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    user.last_login_at = utcnow()
    touch(user)
    session.add(user)
    session.commit()

    token = create_access_token(user.id)
    return {
        "access_token": token,
        "token_type": "bearer",
    }


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

    for row in reader:
        phone = (row.get("phone") or row.get("Phone") or row.get("mobile") or "").strip()
        if not phone:
            continue

        lead = Lead(
            workspace_id=user.workspace_id,
            lead_list_id=lead_list.id,
            homeowner_name=(row.get("homeowner_name") or row.get("owner") or row.get("name") or "").strip() or None,
            first_name=(row.get("first_name") or "").strip() or None,
            last_name=(row.get("last_name") or "").strip() or None,
            phone=phone,
            email=(row.get("email") or "").strip() or None,
            property_address=(row.get("property_address") or row.get("address") or "").strip() or None,
            city=(row.get("city") or "").strip() or None,
            state=(row.get("state") or "").strip() or None,
            postal_code=(row.get("postal_code") or row.get("zip") or "").strip() or None,
            lead_source=(row.get("lead_source") or row.get("source") or "expired_listing").strip(),
            listing_status=(row.get("listing_status") or "").strip() or None,
            listing_status_raw=(row.get("listing_status_raw") or "").strip() or None,
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
        workspace_id=user.workspace_id,
        user_id=user.id,
        action="leadlist_csv_uploaded",
        entity_type="leadlist",
        entity_id=lead_list.id,
        details={"created": created, "filename": file.filename},
    )
    session.commit()

    return {
        "ok": True,
        "lead_list_id": lead_list.id,
        "created": created,
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
    return [row.model_dump() for row in rows]


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
def create_appointment(
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
    total_leads = session.exec(
        select(func.count()).select_from(Lead).where(Lead.workspace_id == user.workspace_id)
    ).one()
    new_leads = session.exec(
        select(func.count()).select_from(Lead).where(Lead.workspace_id == user.workspace_id, Lead.status == "new")
    ).one()
    active_campaigns = session.exec(
        select(func.count()).select_from(Campaign).where(Campaign.workspace_id == user.workspace_id, Campaign.status == "running")
    ).one()
    calls_today = session.exec(
        select(func.count()).select_from(CallLog).where(CallLog.workspace_id == user.workspace_id)
    ).one()
    total_booked = session.exec(
        select(func.count()).select_from(Appointment).where(Appointment.workspace_id == user.workspace_id)
    ).one()
    total_opt_out = session.exec(
        select(func.count()).select_from(DNCEntry).where(DNCEntry.workspace_id == user.workspace_id)
    ).one()

    return {
        "total_leads": int(total_leads or 0),
        "new_leads": int(new_leads or 0),
        "active_campaigns": int(active_campaigns or 0),
        "calls_today": int(calls_today or 0),
        "total_booked": int(total_booked or 0),
        "total_opt_out": int(total_opt_out or 0),
    }


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
    return workspace.model_dump()


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


@app.post("/billing/webhook")
async def billing_webhook(request: Request) -> dict:
    if not stripe_enabled():
        raise HTTPException(status_code=400, detail="Stripe is not enabled")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    event = construct_webhook_event(payload, sig_header)
    return {"ok": True, "type": event["type"]}


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

    bridge = RealtimeBridge(
        workspace_id=workspace_id,
        campaign_id=campaign_id,
        lead_id=lead_id,
        pathway_id=pathway_id,
        calllog_id=calllog_id,
        voice_mode=settings.voice_mode_default,
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
async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled server error")
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "error": "Internal server error",
            "detail": str(exc),
        },
    )