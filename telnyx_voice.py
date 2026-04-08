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
