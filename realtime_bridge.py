from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from config import settings

logger = logging.getLogger(__name__)


class RealtimeBridge:
    """
    Lightweight bridge scaffold for Twilio Media Streams -> OpenAI Realtime / clone pipeline.

    This file is intentionally structured so your app can run now, while giving you
    a clean place to expand true low-latency streaming later.
    """

    def __init__(
        self,
        workspace_id: Optional[int] = None,
        campaign_id: Optional[int] = None,
        lead_id: Optional[int] = None,
        pathway_id: Optional[int] = None,
        calllog_id: Optional[int] = None,
        voice_mode: str = "realtime",
    ) -> None:
        self.workspace_id = workspace_id
        self.campaign_id = campaign_id
        self.lead_id = lead_id
        self.pathway_id = pathway_id
        self.calllog_id = calllog_id
        self.voice_mode = voice_mode or settings.voice_mode_default

        self.stream_sid: Optional[str] = None
        self.call_sid: Optional[str] = None
        self.started = False
        self.closed = False

        self.audio_chunks_in = 0
        self.audio_chunks_out = 0
        self.last_transcript: str = ""

    async def start(self) -> None:
        self.started = True
        logger.info(
            "RealtimeBridge started | workspace=%s campaign=%s lead=%s pathway=%s calllog=%s mode=%s",
            self.workspace_id,
            self.campaign_id,
            self.lead_id,
            self.pathway_id,
            self.calllog_id,
            self.voice_mode,
        )

    async def close(self) -> None:
        self.closed = True
        logger.info(
            "RealtimeBridge closed | calllog=%s stream_sid=%s call_sid=%s in=%s out=%s",
            self.calllog_id,
            self.stream_sid,
            self.call_sid,
            self.audio_chunks_in,
            self.audio_chunks_out,
        )

    async def handle_twilio_message(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """
        Accepts a parsed Twilio Media Streams websocket message and returns a normalized event.
        """
        event = (message or {}).get("event")

        if event == "start":
            start_data = message.get("start", {}) or {}
            self.stream_sid = start_data.get("streamSid")
            self.call_sid = start_data.get("callSid")
            return {
                "ok": True,
                "type": "start",
                "stream_sid": self.stream_sid,
                "call_sid": self.call_sid,
            }

        if event == "media":
            self.audio_chunks_in += 1
            media = message.get("media", {}) or {}
            payload = media.get("payload")
            return {
                "ok": True,
                "type": "media",
                "payload": payload,
                "sequence_number": media.get("sequenceNumber"),
                "track": media.get("track"),
                "chunk_count_in": self.audio_chunks_in,
            }

        if event == "mark":
            return {
                "ok": True,
                "type": "mark",
                "mark": message.get("mark", {}),
            }

        if event == "stop":
            stop_data = message.get("stop", {}) or {}
            return {
                "ok": True,
                "type": "stop",
                "stream_sid": stop_data.get("streamSid") or self.stream_sid,
                "call_sid": stop_data.get("callSid") or self.call_sid,
            }

        logger.warning("Unknown Twilio stream event: %s", event)
        return {
            "ok": False,
            "type": "unknown",
            "event": event,
        }

    async def push_transcript_text(self, text: str) -> Dict[str, Any]:
        """
        Placeholder hook for STT results from OpenAI Realtime / Deepgram.
        """
        self.last_transcript = (text or "").strip()
        return {
            "ok": True,
            "type": "transcript",
            "text": self.last_transcript,
        }

    async def build_tts_instruction(self, text: str) -> Dict[str, Any]:
        """
        Returns a normalized response object for downstream TTS generation.
        """
        return {
            "ok": True,
            "type": "tts_request",
            "voice_mode": self.voice_mode,
            "voice": settings.openai_realtime_voice if self.voice_mode == "realtime" else settings.elevenlabs_voice_id,
            "text": (text or "").strip(),
        }

    async def handle_barge_in(self) -> Dict[str, Any]:
        """
        Placeholder for interruption handling.
        """
        return {
            "ok": True,
            "type": "barge_in",
            "message": "Caller interrupted assistant audio",
        }


def safe_parse_ws_message(raw: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {"event": "unknown", "raw": parsed}
    except Exception:
        logger.exception("Failed to parse websocket message")
        return {"event": "invalid_json", "raw": raw}