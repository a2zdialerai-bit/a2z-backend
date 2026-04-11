from __future__ import annotations

import base64
import hmac
import hashlib
import json
import logging
import os
from typing import Optional

import requests

logger = logging.getLogger("a2z.telnyx")

TELNYX_API_KEY = os.getenv("TELNYX_API_KEY", "")
TELNYX_FROM_NUMBER = os.getenv("TELNYX_FROM_NUMBER", "")
TELNYX_CONNECTION_ID = os.getenv("TELNYX_CONNECTION_ID", "")
TELNYX_WEBHOOK_SECRET = os.getenv("TELNYX_WEBHOOK_SECRET", "")
BASE_URL = os.getenv("BASE_URL", "https://your-domain.com")

TELNYX_BASE = "https://api.telnyx.com/v2"

# Ambient sound during AI processing gaps (subtle office background noise)
# Telnyx Call Control API supports play_audio action on active calls.
AMBIENT_ENABLED = True
AMBIENT_TYPE = "office"
AMBIENT_VOLUME = 0.05
# Publicly accessible low-volume ambient audio URL (subtle office murmur)
AMBIENT_AUDIO_URL = os.getenv(
    "AMBIENT_AUDIO_URL",
    "https://cdn.a2zdialer.com/ambient/office_murmur_low.mp3",
)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {TELNYX_API_KEY}",
        "Content-Type": "application/json",
    }


def _encode_client_state(data: dict) -> str:
    """Encodes client state dict as base64 for Telnyx call metadata."""
    return base64.b64encode(json.dumps(data).encode()).decode()


def decode_client_state(encoded: str) -> dict:
    """Decodes client state from Telnyx webhook."""
    try:
        return json.loads(base64.b64decode(encoded).decode())
    except Exception:
        return {}


def place_outbound_call(
    to_number: str,
    calllog_id: int,
    campaign_id: Optional[int] = None,
    lead_id: Optional[int] = None,
    pathway_id: Optional[int] = None,
    workspace_id: Optional[int] = None,
    from_number: Optional[str] = None,
) -> dict:
    """Place an outbound call via Telnyx.

    Returns {"ok": True, "call_control_id": str, "status": "initiating"} or
            {"ok": False, "error": str}
    """
    if not TELNYX_API_KEY:
        return {"ok": False, "error": "TELNYX_API_KEY not configured"}
    if not TELNYX_CONNECTION_ID:
        return {"ok": False, "error": "TELNYX_CONNECTION_ID not configured"}

    from_num = from_number or TELNYX_FROM_NUMBER
    if not from_num:
        return {"ok": False, "error": "No from number configured"}

    base_host = BASE_URL.replace("https://", "").replace("http://", "")
    webhook_url = f"{BASE_URL}/telnyx/events"
    stream_url = (
        f"wss://{base_host}/ws/call/{calllog_id}"
        f"?calllog_id={calllog_id}"
        f"&campaign_id={campaign_id or ''}"
        f"&lead_id={lead_id or ''}"
        f"&pathway_id={pathway_id or ''}"
        f"&workspace_id={workspace_id or ''}"
        f"&provider=telnyx"
    )

    payload: dict = {
        "connection_id": TELNYX_CONNECTION_ID,
        "to": to_number,
        "from": from_num,
        "from_display_name": "Local Agent",
        "webhook_url": webhook_url,
        "webhook_url_method": "POST",
        "stream_url": stream_url,
        "stream_track": "both_tracks",
        "stream_bidirectional_mode": "rtp",
        "media_encoding": "PCMU",
        "sample_rate": 8000,
        "answering_machine_detection": "premium",
        "answering_machine_detection_config": {
            "after_greeting_silence_millis": 800,
            "between_words_silence_millis": 50,
            "greeting_duration_millis": 3500,
            "initial_silence_millis": 3000,
            "maximum_number_of_words": 5,
            "silence_threshold": 512,
            "total_analysis_time_millis": 5000,
        },
        "client_state": _encode_client_state({
            "calllog_id": calllog_id,
            "campaign_id": campaign_id,
            "lead_id": lead_id,
            "workspace_id": workspace_id,
        }),
        "timeout_secs": 30,
        "record": os.getenv("ENABLE_CALL_RECORDING", "false").lower() == "true",
    }

    try:
        resp = requests.post(
            f"{TELNYX_BASE}/calls",
            headers=_headers(),
            json=payload,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            data = resp.json().get("data", {})
            logger.info(
                f"Telnyx call placed: calllog={calllog_id} "
                f"call_control_id={data.get('call_control_id')}"
            )
            return {
                "ok": True,
                "call_control_id": data.get("call_control_id"),
                "call_leg_id": data.get("call_leg_id"),
                "call_session_id": data.get("call_session_id"),
                "status": "initiating",
            }
        error = resp.text
        logger.error(f"Telnyx call failed: {resp.status_code} {error[:300]}")
        return {"ok": False, "error": f"Telnyx error {resp.status_code}: {error[:300]}"}
    except Exception as e:
        logger.error(f"Telnyx call exception: {e}")
        return {"ok": False, "error": str(e)}


# Keep original name as alias for backward compatibility
place_telnyx_call = place_outbound_call


def hangup_call(call_control_id: str) -> dict:
    """Hangs up an active Telnyx call."""
    if not TELNYX_API_KEY:
        return {"ok": False, "error": "TELNYX_API_KEY not configured"}
    try:
        resp = requests.post(
            f"{TELNYX_BASE}/calls/{call_control_id}/actions/hangup",
            headers=_headers(),
            json={},
            timeout=10,
        )
        return {"ok": resp.status_code in (200, 201)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Alias for backward compatibility
hangup_telnyx_call = hangup_call


def reject_call(call_control_id: str) -> dict:
    """Rejects an incoming call."""
    try:
        resp = requests.post(
            f"{TELNYX_BASE}/calls/{call_control_id}/actions/reject",
            headers=_headers(),
            json={"cause": "USER_BUSY"},
            timeout=10,
        )
        return {"ok": resp.status_code in (200, 201)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def transfer_call(call_control_id: str, to_number: str) -> dict:
    """Transfers a call to another number."""
    try:
        resp = requests.post(
            f"{TELNYX_BASE}/calls/{call_control_id}/actions/transfer",
            headers=_headers(),
            json={"to": to_number},
            timeout=10,
        )
        return {"ok": resp.status_code in (200, 201)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def start_recording(call_control_id: str) -> dict:
    """Starts call recording."""
    try:
        resp = requests.post(
            f"{TELNYX_BASE}/calls/{call_control_id}/actions/record_start",
            headers=_headers(),
            json={"format": "mp3", "channels": "dual", "play_beep": False},
            timeout=10,
        )
        return {"ok": resp.status_code in (200, 201)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def stop_recording(call_control_id: str) -> dict:
    """Stops call recording."""
    try:
        resp = requests.post(
            f"{TELNYX_BASE}/calls/{call_control_id}/actions/record_stop",
            headers=_headers(),
            json={},
            timeout=10,
        )
        return {"ok": resp.status_code in (200, 201)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_dtmf(call_control_id: str, digits: str) -> dict:
    """Sends DTMF tones on an active call."""
    try:
        resp = requests.post(
            f"{TELNYX_BASE}/calls/{call_control_id}/actions/send_dtmf",
            headers=_headers(),
            json={"digits": digits, "duration_millis": 500},
            timeout=10,
        )
        return {"ok": resp.status_code in (200, 201)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Alias for backward compatibility
send_telnyx_dtmf = send_dtmf


def get_call_recording(call_control_id: str) -> dict:
    """Retrieves recording URL after call ends."""
    try:
        resp = requests.get(
            f"{TELNYX_BASE}/recordings",
            headers=_headers(),
            params={"filter[call_control_id]": call_control_id},
            timeout=10,
        )
        if resp.status_code == 200:
            records = resp.json().get("data", [])
            if records:
                return {
                    "ok": True,
                    "recording_url": records[0].get("download_url"),
                    "duration": records[0].get("duration_secs"),
                }
        return {"ok": False, "error": "No recording found"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def list_available_numbers(
    area_code: Optional[str] = None,
    state: Optional[str] = None,
    country_code: str = "US",
    limit: int = 10,
) -> list:
    """Lists available phone numbers to purchase."""
    params: dict = {
        "filter[country_code]": country_code,
        "filter[number_type]": "local",
        "page[size]": limit,
    }
    if area_code:
        params["filter[national_destination_code]"] = area_code
    if state:
        params["filter[administrative_area]"] = state

    try:
        resp = requests.get(
            f"{TELNYX_BASE}/available_phone_numbers",
            headers=_headers(),
            params=params,
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("data", [])
        return []
    except Exception:
        return []


def purchase_phone_number(phone_number: str) -> dict:
    """Purchases a specific phone number."""
    try:
        resp = requests.post(
            f"{TELNYX_BASE}/number_orders",
            headers=_headers(),
            json={
                "phone_numbers": [{"phone_number": phone_number}],
                "connection_id": TELNYX_CONNECTION_ID,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            data = resp.json().get("data", {})
            return {
                "ok": True,
                "order_id": data.get("id"),
                "phone_number": phone_number,
                "status": data.get("status"),
            }
        return {"ok": False, "error": resp.text[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def play_audio(call_control_id: str, audio_url: str, loop: bool = False) -> dict:
    """Plays an audio file on an active Telnyx call.

    Uses the Telnyx Call Control play_audio action.
    Can be used for ambient sound during AI processing gaps.
    """
    if not TELNYX_API_KEY:
        return {"ok": False, "error": "TELNYX_API_KEY not configured"}
    try:
        payload: dict = {
            "audio_url": audio_url,
            "loop": "infinity" if loop else "0",
            "overlay": True,  # Mix with existing audio (don't replace)
        }
        resp = requests.post(
            f"{TELNYX_BASE}/calls/{call_control_id}/actions/playback_start",
            headers=_headers(),
            json=payload,
            timeout=10,
        )
        return {"ok": resp.status_code in (200, 201)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def stop_audio(call_control_id: str) -> dict:
    """Stops audio playback on an active Telnyx call."""
    if not TELNYX_API_KEY:
        return {"ok": False, "error": "TELNYX_API_KEY not configured"}
    try:
        resp = requests.post(
            f"{TELNYX_BASE}/calls/{call_control_id}/actions/playback_stop",
            headers=_headers(),
            json={},
            timeout=10,
        )
        return {"ok": resp.status_code in (200, 201)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def play_ambient_sound(call_control_id: str) -> dict:
    """Plays subtle ambient office sound during AI processing gaps.

    Only plays if AMBIENT_ENABLED is True. Uses overlay mode so it mixes
    with any existing call audio without replacing it.
    """
    if not AMBIENT_ENABLED:
        return {"ok": False, "error": "Ambient sound disabled"}
    if not AMBIENT_AUDIO_URL or "cdn.a2zdialer.com" in AMBIENT_AUDIO_URL:
        # URL not configured — skip silently to avoid 404 errors
        return {"ok": False, "error": "Ambient audio URL not configured"}
    return play_audio(call_control_id, AMBIENT_AUDIO_URL, loop=False)


async def handle_voicemail(
    call_control_id: str,
    lead_phone: str,
    agent_name: str,
    brokerage_name: str,
    property_address: Optional[str],
    callback_number: str,
    calllog_id: Optional[int] = None,
) -> dict:
    """Leave a personalized voicemail via TTS + Telnyx play_audio.

    Flow:
    1. Build personalized voicemail text
    2. Generate TTS audio via Cartesia
    3. Upload/serve the audio (or use a pre-generated URL)
    4. Play audio on the call via Telnyx play_audio
    5. Hang up after playing
    6. Log disposition as "voicemail"
    """
    import asyncio as _asyncio

    addr_phrase = f"about your property at {property_address}" if property_address else "about your home"
    voicemail_text = (
        f"Hi, this is {agent_name} calling from {brokerage_name} {addr_phrase}. "
        f"I'd love to connect with you about the real estate market in your area. "
        f"Please give me a call back at {callback_number}. "
        f"Again, that's {agent_name} from {brokerage_name}. Thanks, talk soon."
    )

    logger.info(
        f"Leaving voicemail on call_control_id={call_control_id} | "
        f"lead_phone={lead_phone} | calllog={calllog_id}"
    )

    # Try to generate TTS via Cartesia and get a playable URL
    # For now, use Telnyx speak action as fallback (no external URL needed)
    try:
        speak_result = _telnyx_speak(call_control_id, voicemail_text)
        if speak_result.get("ok"):
            # Wait for speech to finish (estimate based on text length)
            est_duration = max(len(voicemail_text) / 15, 5)  # ~15 chars/sec
            await _asyncio.sleep(est_duration + 1.0)
    except Exception as exc:
        logger.error(f"Voicemail TTS failed: {exc}")

    # Hang up after voicemail
    hangup_call(call_control_id)

    # Update calllog disposition
    if calllog_id:
        try:
            from db import session_scope
            from models import CallLog
            with session_scope() as session:
                calllog = session.get(CallLog, calllog_id)
                if calllog:
                    calllog.disposition = "voicemail"
                    calllog.status = "completed"
                    session.add(calllog)
                    session.commit()
        except Exception as exc:
            logger.error(f"Failed to update calllog disposition for voicemail: {exc}")

    return {"ok": True, "action": "voicemail_left"}


def _telnyx_speak(call_control_id: str, text: str, voice: str = "Polly.Joanna") -> dict:
    """Use Telnyx speak action for TTS on an active call."""
    try:
        resp = requests.post(
            f"{TELNYX_BASE}/calls/{call_control_id}/actions/speak",
            headers=_headers(),
            json={
                "payload": text,
                "voice": voice,
                "language": "en-US",
            },
            timeout=10,
        )
        return {"ok": resp.status_code in (200, 201)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def handle_voicemail_detection(
    call_control_id: str,
    detection_result: str,
    lead_phone: str,
    agent_name: str = "Alex",
    brokerage_name: str = "your brokerage",
    property_address: Optional[str] = None,
    callback_number: str = "",
    calllog_id: Optional[int] = None,
) -> dict:
    """Triggered when Telnyx sends call.machine.detection.ended with result 'machine'.

    Dispatches voicemail handling for machine-answered calls.
    """
    machine_results = {
        "machine_start", "machine_end_beep",
        "machine_end_silence", "machine_end_other", "fax"
    }

    if detection_result not in machine_results:
        return {"ok": True, "action": "human_answered", "result": detection_result}

    logger.info(
        f"AMD: machine detected (result={detection_result}) on call_control_id={call_control_id}"
    )

    if detection_result in ("machine_end_beep",) and callback_number:
        # Best moment to leave voicemail — after the beep
        return await handle_voicemail(
            call_control_id=call_control_id,
            lead_phone=lead_phone,
            agent_name=agent_name,
            brokerage_name=brokerage_name,
            property_address=property_address,
            callback_number=callback_number,
            calllog_id=calllog_id,
        )
    else:
        # For machine_start, fax, etc. — just hang up
        hangup_call(call_control_id)
        if calllog_id:
            try:
                from db import session_scope
                from models import CallLog
                with session_scope() as session:
                    calllog = session.get(CallLog, calllog_id)
                    if calllog:
                        calllog.disposition = "voicemail"
                        calllog.status = "no_answer"
                        session.add(calllog)
                        session.commit()
            except Exception as exc:
                logger.error(f"Failed to update calllog for voicemail hangup: {exc}")
        return {"ok": True, "action": "voicemail_hangup"}


def get_outbound_caller_id(workspace: Any, lead: Optional[Any] = None) -> str:
    """Determine the outbound caller ID for a call.

    Preference order:
    1. Agent's verified callback number (if set on workspace)
       Note: For Telnyx, the number must be verified/purchased in your account.
       The agent's personal mobile cannot be used unless verified with Telnyx.
    2. Workspace provisioned Telnyx number (twilio_from_number field)
    3. Global TELNYX_FROM_NUMBER env fallback

    IMPORTANT: Telnyx requires that the from_number be either:
    - A number purchased/ported into your Telnyx account, OR
    - A verified outbound caller ID (requires verification call/PIN process)
    Using an unverified number will cause calls to fail with a 403.
    """
    agent_callback = getattr(workspace, 'agent_callback_number', None)
    if agent_callback:
        logger.info(
            "Using agent callback number as caller ID — ensure it is Telnyx-verified"
        )
        return agent_callback
    return getattr(workspace, 'twilio_from_number', None) or TELNYX_FROM_NUMBER


def auto_provision_number(
    workspace_id: int,
    area_code: Optional[str] = None,
    state: Optional[str] = None,
) -> dict:
    """Fully automatic phone number provisioning for a new workspace.

    Finds an available local number and purchases it automatically.
    Called when a workspace subscribes — agent never configures a number manually.
    """
    numbers = list_available_numbers(area_code=area_code, state=state, limit=5)
    if not numbers:
        # Fall back to any US number
        numbers = list_available_numbers(limit=5)
    if not numbers:
        return {"ok": False, "error": "No numbers available"}

    best_number = numbers[0].get("phone_number") or numbers[0].get("id", "")
    if not best_number:
        return {"ok": False, "error": "Could not parse available number"}

    result = purchase_phone_number(best_number)
    if result["ok"]:
        logger.info(f"Auto-provisioned {best_number} for workspace {workspace_id}")
    return result


def verify_webhook_signature(
    payload: bytes,
    signature: str,
    timestamp: str,
) -> bool:
    """Verifies Telnyx webhook HMAC signature for security.

    Returns True if valid or if TELNYX_WEBHOOK_SECRET is not configured.
    """
    if not TELNYX_WEBHOOK_SECRET:
        return True  # Skip verification if secret not set
    try:
        message = timestamp + "|" + payload.decode("utf-8")
        expected = hmac.new(
            TELNYX_WEBHOOK_SECRET.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)
    except Exception:
        return False
