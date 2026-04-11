from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from twilio.rest import Client as TwilioClient
from twilio.twiml.voice_response import Connect, Gather, VoiceResponse

from config import settings
from models import Campaign, Lead, Pathway, Workspace
from pathway_engine import render_prompt, safe_json_load

logger = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def get_workspace_twilio_client(workspace: Workspace) -> Optional[TwilioClient]:
    sid = workspace.twilio_account_sid or settings.twilio_account_sid
    token = workspace.twilio_auth_token or settings.twilio_auth_token
    if not sid or not token:
        return None
    return TwilioClient(sid, token)


def resolve_caller_identity(workspace: Workspace, campaign: Campaign) -> Dict[str, str]:
    caller_name = (
        campaign.caller_name
        or workspace.default_agent_name
        or workspace.brand_name
        or workspace.name
        or "our team"
    )
    caller_title = (
        campaign.caller_title
        or workspace.default_caller_title
        or "representative"
    )
    brokerage_name = (
        campaign.brokerage_name
        or workspace.default_brokerage_name
        or workspace.brand_name
        or workspace.name
        or "our office"
    )

    return {
        "caller_name": caller_name,
        "caller_title": caller_title,
        "brokerage_name": brokerage_name,
    }


def build_initial_context(
    workspace: Workspace,
    lead: Lead,
    campaign: Campaign,
    pathway: Pathway,
) -> Dict[str, Any]:
    identity = resolve_caller_identity(workspace, campaign)
    homeowner_first = lead.first_name or (lead.homeowner_name or "").split()[0] if (lead.homeowner_name or lead.first_name) else "there"
    homeowner_name = lead.homeowner_name or (
        f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "there"
    )
    return {
        "lead_id": lead.id,
        "campaign_id": campaign.id,
        "pathway_id": pathway.id,
        "workspace_name": workspace.name,
        "brand_name": workspace.brand_name or workspace.name,
        "caller_name": identity["caller_name"],
        "caller_title": identity["caller_title"],
        "brokerage_name": identity["brokerage_name"],
        # agent_name alias used in pathway {{agent_name}} templates
        "agent_name": identity["caller_name"],
        "agent_title": identity["caller_title"],
        "agent_brokerage": identity["brokerage_name"],
        "homeowner_name": homeowner_name,
        "homeowner_first_name": homeowner_first,
        "homeowner_last_name": lead.last_name or "",
        "first_name": lead.first_name or "",
        "last_name": lead.last_name or "",
        "phone": lead.phone,
        "email": lead.email or "",
        "property_address": lead.property_address or "your property",
        "city": lead.city or "",
        "state": lead.state or "",
        "postal_code": lead.postal_code or "",
        "listing_status": lead.listing_status or "",
        "lead_source": lead.lead_source or "",
        "days_expired": str(getattr(lead, "days_expired", "") or ""),
        "list_price": str(getattr(lead, "last_list_price", "") or ""),
    }


def build_immediate_greeting(
    workspace: Workspace,
    pathway: Pathway,
    lead: Lead,
    campaign: Campaign,
) -> str:
    pathway_obj = safe_json_load(pathway.json_def)
    start_node = pathway_obj.get("start_node")
    context = build_initial_context(workspace, lead, campaign, pathway)

    if not isinstance(start_node, str) or not start_node:
        return (
            f"Hi, this is {context['caller_name']} with "
            f"{context['brokerage_name']} calling about your property."
        )

    node = pathway_obj.get("nodes", {}).get(start_node, {})
    prompt = str(node.get("prompt") or "").strip()

    if not prompt:
        return (
            f"Hi, this is {context['caller_name']} with "
            f"{context['brokerage_name']} calling about your property."
        )

    return render_prompt(prompt, context)


def build_voice_response_for_gather(
    workspace: Workspace,
    calllog_id: int,
    pathway: Pathway,
    lead: Lead,
    campaign: Campaign,
) -> str:
    greeting = build_immediate_greeting(workspace, pathway, lead, campaign)
    vr = VoiceResponse()

    gather = Gather(
        input="speech",
        action=f"/twilio/speech?calllog_id={calllog_id}",
        method="POST",
        timeout=settings.twilio_gather_timeout,
        speech_timeout=settings.twilio_speech_timeout,
        speech_model=settings.twilio_speech_model,
        enhanced=settings.twilio_enhanced_speech,
        barge_in=settings.twilio_gather_barge_in,
    )
    gather.say(
        greeting,
        voice=settings.twilio_tts_voice,
        language=settings.twilio_tts_language,
    )
    vr.append(gather)

    vr.redirect(f"/twilio/repair?calllog_id={calllog_id}", method="POST")
    return str(vr)


def build_voice_response_for_realtime_stream(
    workspace: Workspace,
    calllog_id: int,
    pathway: Pathway,
    lead: Lead,
    campaign: Campaign,
) -> str:
    greeting = build_immediate_greeting(workspace, pathway, lead, campaign)
    vr = VoiceResponse()

    vr.say(
        greeting,
        voice=settings.twilio_tts_voice,
        language=settings.twilio_tts_language,
    )

    stream_params = {
        "calllog_id": calllog_id,
        "campaign_id": campaign.id,
        "lead_id": lead.id,
        "pathway_id": pathway.id,
        "workspace_id": workspace.id,
    }
    stream_base = settings.base_url.replace("http://", "ws://").replace("https://", "wss://")
    stream_url = f"{stream_base}/twilio/stream?{urlencode(stream_params)}"

    connect = Connect()
    connect.stream(url=stream_url)
    vr.append(connect)

    return str(vr)


def place_outbound_call(
    workspace: Workspace,
    lead: Lead,
    campaign: Campaign,
    pathway: Pathway,
    calllog_id: int,
) -> Dict[str, Any]:
    client = get_workspace_twilio_client(workspace)
    if not client:
        return {"ok": False, "error": "Twilio client not configured"}

    from_number = workspace.twilio_from_number or settings.twilio_from_number
    if not from_number:
        return {"ok": False, "error": "Twilio from number not configured"}

    use_realtime = (campaign.voice_mode or workspace.voice_mode or settings.voice_mode_default) == "realtime"
    answer_url = f"{settings.base_url}/twilio/voice?calllog_id={calllog_id}&mode={'realtime' if use_realtime else 'gather'}"
    status_callback = f"{settings.base_url}/twilio/status?calllog_id={calllog_id}"

    try:
        call = client.calls.create(
            to=lead.phone,
            from_=from_number,
            url=answer_url,
            method="POST",
            status_callback=status_callback,
            status_callback_method="POST",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )
        return {
            "ok": True,
            "call_sid": call.sid,
            "status": getattr(call, "status", "queued"),
        }
    except Exception as exc:
        logger.exception("Failed to place outbound call")
        return {"ok": False, "error": str(exc)}