from __future__ import annotations

import asyncio
import json
import logging
import os
import time as _time
from typing import Any, Dict, List, Optional

VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "cartesia")

try:
    import httpx as _httpx
    _httpx_available = True
except ImportError:
    _httpx_available = False

from config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default response delay after VAD end-of-speech before processing (ms)
RESPONSE_DELAY_MS = 350

# ---------------------------------------------------------------------------
# Agent persona prompt template
# ---------------------------------------------------------------------------

AGENT_PERSONA_PROMPT = """You are {agent_name}, a local real estate agent calling homeowners about their expired listing. You work for {brokerage_name}.

YOUR PERSONALITY:
- Warm, genuine, conversational — never salesy or scripted
- Confident but not pushy — you believe in what you're offering but you respect their decision
- You sound like a real local agent calling a neighbor, not a call center robot
- Short sentences mixed with longer ones — natural rhythm
- You react genuinely to what they say
- If they are skeptical, you get quieter, slower, and more empathetic
- If they are warm and excited, you match their energy
- If they are busy or frustrated, you acknowledge it immediately

STRICT RULES — NEVER SAY:
- Never say "Certainly", "Absolutely", "Of course I can help you with that"
- Never say "Great question", "I understand your concern", "I'd be happy to assist"
- Never say "As an AI" or anything that sounds like a call center script

SPEECH PATTERNS:
- Use contractions always: I'm, you're, it's, I'll, didn't, wouldn't
- Use natural filler transitions: Yeah, So, Look, Here's the thing, I get it
- Vary sentence length — sometimes very short. Then longer when explaining.

EMOTIONAL CALIBRATION:
- Defensive homeowner → slower, gentler, more empathetic
- Curious homeowner → slightly more energized
- Rushed homeowner → shorter responses, get to the point
- Warm homeowner → match warmth, be slightly more casual
- Frustrated homeowner → validate first before anything else

YOUR ROLE: Follow the conversation pathway provided. Deliver pathway prompts naturally."""


def build_agent_persona(agent_name: str = "Alex", brokerage_name: str = "your brokerage") -> str:
    """Return the agent persona prompt with name/brokerage filled in."""
    return AGENT_PERSONA_PROMPT.format(
        agent_name=agent_name or "Alex",
        brokerage_name=brokerage_name or "your brokerage",
    )


# ---------------------------------------------------------------------------
# Silence thresholds (seconds)
# ---------------------------------------------------------------------------

SILENCE_PROMPT_AFTER_S = 4.0    # "You still there?"
SILENCE_REASK_AFTER_S = 6.0     # re-ask question briefly
SILENCE_HANGUP_AFTER_S = 8.0    # end call gracefully


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
        agent_name: str = "Alex",
        brokerage_name: str = "your brokerage",
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

        # Agent persona
        self.agent_name = agent_name
        self.brokerage_name = brokerage_name

        # Transcript recording
        self.transcript_turns: List[Dict[str, Any]] = []
        self._call_start_ms: float = _time.monotonic() * 1000

        # Silence detection state
        self._last_homeowner_speech_ms: Optional[float] = None
        self._silence_check_task: Optional[asyncio.Task] = None  # type: ignore[type-arg]

        # Barge-in / AI speaking state
        self._ai_speaking: bool = False
        self._ai_cancel_requested: bool = False

        # Current node tracking for transcript
        self.current_node_id: Optional[str] = None
        self.nodes_visited: List[str] = []
        self.extracted_fields: Dict[str, Any] = {}
        self.final_disposition: Optional[str] = None

    def _elapsed_ms(self) -> int:
        return int(_time.monotonic() * 1000 - self._call_start_ms)

    # ------------------------------------------------------------------
    # Session / persona
    # ------------------------------------------------------------------

    def get_session_instructions(self) -> str:
        """Return the base system instructions for the OpenAI Realtime session."""
        return build_agent_persona(self.agent_name, self.brokerage_name)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self.started = True
        self._call_start_ms = _time.monotonic() * 1000
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
        if self._silence_check_task and not self._silence_check_task.done():
            self._silence_check_task.cancel()
        logger.info(
            "RealtimeBridge closed | calllog=%s stream_sid=%s call_sid=%s in=%s out=%s",
            self.calllog_id,
            self.stream_sid,
            self.call_sid,
            self.audio_chunks_in,
            self.audio_chunks_out,
        )

    # ------------------------------------------------------------------
    # Twilio message handling
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # VAD / response delay
    # ------------------------------------------------------------------

    async def on_vad_speech_start(self) -> None:
        """Called when VAD detects the homeowner has started speaking.

        If AI is currently generating/speaking, trigger barge-in cancellation.
        Also reset silence timer.
        """
        self._last_homeowner_speech_ms = self._elapsed_ms()
        # Cancel silence timer
        if self._silence_check_task and not self._silence_check_task.done():
            self._silence_check_task.cancel()
            self._silence_check_task = None

        if self._ai_speaking:
            await self.handle_barge_in()

    async def on_vad_speech_end(self, classifier_result: Optional[Dict[str, Any]] = None) -> None:
        """Called when VAD detects end-of-speech from the homeowner.

        Applies response delay before returning control to the caller:
        - 200 ms if the homeowner asked a question
        - 350 ms (RESPONSE_DELAY_MS) otherwise
        """
        contains_question = bool((classifier_result or {}).get("contains_question", False))
        delay_s = (200 if contains_question else RESPONSE_DELAY_MS) / 1000.0
        await asyncio.sleep(delay_s)

    # ------------------------------------------------------------------
    # Silence detection
    # ------------------------------------------------------------------

    async def _silence_detection_loop(
        self,
        reask_text: Optional[str] = None,
        on_hangup: Optional[Any] = None,
    ) -> None:
        """Background task: monitor silence after AI asks a question.

        - 0-4 s : wait
        - 4-6 s : inject "You still there?"
        - 6-8 s : re-ask question briefly (reask_text if provided)
        - 8+ s  : end call gracefully (calls on_hangup if provided)
        """
        try:
            await asyncio.sleep(SILENCE_PROMPT_AFTER_S)
            logger.info("Silence detected: injecting presence check")
            await self.push_transcript_text("You still there?")

            await asyncio.sleep(SILENCE_REASK_AFTER_S - SILENCE_PROMPT_AFTER_S)
            if reask_text:
                logger.info("Silence detected: re-asking question")
                await self.push_transcript_text(reask_text)

            await asyncio.sleep(SILENCE_HANGUP_AFTER_S - SILENCE_REASK_AFTER_S)
            logger.info("Silence detected: ending call due to no response")
            self.final_disposition = "no_response"
            if callable(on_hangup):
                await on_hangup()

        except asyncio.CancelledError:
            pass  # Homeowner spoke — timer cancelled normally

    def start_silence_timer(
        self,
        reask_text: Optional[str] = None,
        on_hangup: Optional[Any] = None,
    ) -> None:
        """Start the silence detection background task after AI asks a question."""
        if self._silence_check_task and not self._silence_check_task.done():
            self._silence_check_task.cancel()
        self._silence_check_task = asyncio.create_task(
            self._silence_detection_loop(reask_text=reask_text, on_hangup=on_hangup)
        )

    def stop_silence_timer(self) -> None:
        """Cancel the silence detection timer (homeowner spoke)."""
        if self._silence_check_task and not self._silence_check_task.done():
            self._silence_check_task.cancel()
            self._silence_check_task = None

    # ------------------------------------------------------------------
    # Transcript recording
    # ------------------------------------------------------------------

    def record_agent_turn(self, text: str, node_id: Optional[str] = None) -> None:
        """Append an agent utterance to the transcript."""
        turn = {
            "speaker": "agent",
            "text": text,
            "timestamp_ms": self._elapsed_ms(),
            "node": node_id or self.current_node_id,
        }
        self.transcript_turns.append(turn)
        if node_id and node_id not in self.nodes_visited:
            self.nodes_visited.append(node_id)

    def record_homeowner_turn(self, text: str, emotional_state: str = "neutral") -> None:
        """Append a homeowner utterance to the transcript."""
        turn = {
            "speaker": "homeowner",
            "text": text,
            "timestamp_ms": self._elapsed_ms(),
            "emotional_state": emotional_state,
        }
        self.transcript_turns.append(turn)

    def build_transcript_json(self) -> str:
        """Serialize the full call transcript to a JSON string for CallLog storage."""
        return json.dumps({
            "turns": self.transcript_turns,
            "nodes_visited": self.nodes_visited,
            "extracted_fields": self.extracted_fields,
            "final_disposition": self.final_disposition,
        })

    # ------------------------------------------------------------------
    # Original STT/TTS hooks
    # ------------------------------------------------------------------

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
        Supports both OpenAI Realtime and ElevenLabs voice modes.
        """
        return {
            "ok": True,
            "type": "tts_request",
            "voice_mode": self.voice_mode,
            "voice": settings.openai_realtime_voice if self.voice_mode == "realtime" else settings.elevenlabs_voice_id,
            "text": (text or "").strip(),
        }

    # ------------------------------------------------------------------
    # Barge-in handling (enhanced)
    # ------------------------------------------------------------------

    async def handle_barge_in(self) -> Dict[str, Any]:
        """
        Handle caller interruption: signal to cancel AI audio generation and
        stop streaming to Twilio. Sets internal state so the pipeline knows
        to discard in-flight audio.
        """
        self._ai_cancel_requested = True
        self._ai_speaking = False
        logger.info(
            "Barge-in detected | calllog=%s stream_sid=%s — cancelling AI audio",
            self.calllog_id,
            self.stream_sid,
        )
        return {
            "ok": True,
            "type": "barge_in",
            "action": "cancel_ai_audio",
            "message": "Caller interrupted assistant audio",
        }

    def on_ai_speaking_start(self) -> None:
        """Mark that the AI has started generating/streaming audio."""
        self._ai_speaking = True
        self._ai_cancel_requested = False

    def on_ai_speaking_end(self) -> None:
        """Mark that the AI has finished generating/streaming audio."""
        self._ai_speaking = False

    # ------------------------------------------------------------------
    # ElevenLabs streaming TTS
    # ------------------------------------------------------------------

    async def stream_elevenlabs_audio(
        self,
        text: str,
        voice_id: str,
        api_key: str,
        emotional_state: str = "neutral",
    ) -> Optional[bytes]:
        """Stream audio from ElevenLabs TTS and return raw bytes (ulaw 8k).

        Voice settings are adjusted dynamically based on the homeowner's
        emotional state so the AI sounds appropriate in the moment.

        Returns None on failure so the caller can fall back to OpenAI TTS.
        """
        if not _httpx_available:
            logger.warning("httpx not available — cannot stream ElevenLabs audio")
            return None

        # Emotion-aware voice parameters
        # neutral/default: stable, warm, conversational
        stability: float = 0.45
        similarity: float = 0.80
        style: float = 0.25
        speed: float = 1.0

        if emotional_state == "frustrated":
            # Calmer, slower, less expressive — de-escalation mode
            stability = 0.70
            similarity = 0.80
            style = 0.10
            speed = 0.88
        elif emotional_state == "interested":
            # More expressive, slightly faster — match their energy
            stability = 0.40
            similarity = 0.82
            style = 0.32
            speed = 1.05
        elif emotional_state == "hostile":
            # Very flat and calm — never match hostile energy
            stability = 0.80
            similarity = 0.78
            style = 0.05
            speed = 0.85
        elif emotional_state == "warm":
            # Slightly more expressive — match their warmth
            stability = 0.42
            similarity = 0.80
            style = 0.30
            speed = 1.02
        elif emotional_state == "skeptical":
            # Measured, steady, trustworthy
            stability = 0.60
            similarity = 0.80
            style = 0.15
            speed = 0.95
        elif emotional_state == "confused":
            # Slow and clear — easy to follow
            stability = 0.65
            similarity = 0.80
            style = 0.10
            speed = 0.90

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "text": text,
            "model_id": "eleven_turbo_v2",
            "voice_settings": {
                "stability": stability,
                "similarity_boost": similarity,
                "style": style,
                "use_speaker_boost": True,
                "speed": speed,
            },
            "output_format": "ulaw_8000",
        }

        try:
            async with _httpx.AsyncClient(timeout=30.0) as client:
                chunks: list[bytes] = []
                async with client.stream("POST", url, headers=headers, json=payload) as response:
                    if response.status_code != 200:
                        logger.error(
                            "ElevenLabs API error %s for voice %s",
                            response.status_code,
                            voice_id,
                        )
                        return None
                    async for chunk in response.aiter_bytes(chunk_size=4096):
                        if self._ai_cancel_requested:
                            logger.info("ElevenLabs stream cancelled due to barge-in")
                            return None
                        chunks.append(chunk)
                return b"".join(chunks)
        except Exception:
            logger.exception("ElevenLabs streaming failed for voice %s", voice_id)
            return None

    # ------------------------------------------------------------------
    # Provider-aware TTS streaming
    # ------------------------------------------------------------------

    async def stream_tts_audio(
        self,
        text: str,
        voice_id: str,
        api_key: str,
        emotional_state: str = "neutral",
    ) -> Optional[bytes]:
        """Stream TTS audio using the configured VOICE_PROVIDER.

        VOICE_PROVIDER=cartesia  → Cartesia Sonic 3 (primary)
        VOICE_PROVIDER=elevenlabs → ElevenLabs (fallback / legacy)

        Returns raw audio bytes (ulaw 8k) or None on failure.
        """
        if VOICE_PROVIDER == "cartesia":
            try:
                from cartesia_tts import stream_tts  # type: ignore
                chunks: list[bytes] = []
                async for chunk in stream_tts(
                    text=text,
                    voice_id=voice_id if voice_id and voice_id != "dev_placeholder" else None,
                    emotional_state=emotional_state,
                ):
                    if self._ai_cancel_requested:
                        logger.info("Cartesia stream cancelled due to barge-in")
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
            except Exception:
                logger.exception("Cartesia TTS failed, falling back to ElevenLabs")
                return await self.stream_elevenlabs_audio(text, voice_id, api_key, emotional_state)
        else:
            return await self.stream_elevenlabs_audio(text, voice_id, api_key, emotional_state)

    # ------------------------------------------------------------------
    # Voicemail / AMD handling
    # ------------------------------------------------------------------

    async def handle_amd_result(self, answered_by: str) -> Dict[str, Any]:
        """Process Twilio AMD (answering machine detection) result.

        If answered_by is 'machine_start' or 'fax', the call should be
        hung up and the CallLog status set to 'no_answer'.
        """
        is_machine = answered_by in ("machine_start", "fax")
        logger.info(
            "AMD result: answered_by=%s is_machine=%s | calllog=%s",
            answered_by,
            is_machine,
            self.calllog_id,
        )
        if is_machine:
            self.final_disposition = "no_answer"
        return {
            "ok": True,
            "type": "amd_result",
            "answered_by": answered_by,
            "is_machine": is_machine,
            "action": "hangup" if is_machine else "continue",
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
