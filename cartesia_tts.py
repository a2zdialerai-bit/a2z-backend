from __future__ import annotations

import os
from typing import AsyncGenerator, Optional

import aiohttp

CARTESIA_API_KEY = os.getenv("CARTESIA_API_KEY", "")
CARTESIA_MODEL_ID = os.getenv("CARTESIA_MODEL_ID", "sonic-3")
CARTESIA_VOICE_ID_DEFAULT = os.getenv(
    "CARTESIA_VOICE_ID_DEFAULT",
    "f786b574-daa5-4673-aa0c-cbe3e8534c02",
)

CARTESIA_STREAM_URL = "https://api.cartesia.ai/tts/bytes"

# Emotion → Cartesia speed/emotion settings
EMOTION_SETTINGS: dict[str, dict] = {
    "neutral":    {"speed": "normal", "emotion": []},
    "warm":       {"speed": "normal", "emotion": ["positivity:high"]},
    "frustrated": {"speed": "slow",   "emotion": ["sadness:low"]},
    "interested": {"speed": "normal", "emotion": ["positivity:high", "curiosity:medium"]},
    "skeptical":  {"speed": "normal", "emotion": []},
    "hostile":    {"speed": "slow",   "emotion": []},
    "confused":   {"speed": "slow",   "emotion": []},
}


def inject_emotion_tags(text: str, emotional_state: str) -> str:
    """Inject Cartesia SSML-style tags for natural delivery."""
    text = text.replace("So...", "<break time='300ms'/> So...")
    text = text.replace("Yeah...", "Yeah <break time='200ms'/>")
    text = text.replace("Hmm,", "Hmm, <break time='150ms'/>")
    text = text.replace("I mean,", "I mean, <break time='100ms'/>")
    text = text.replace("Ha —", "[laughter] —")
    text = text.replace("Haha", "[laughter]")
    return text


async def stream_tts(
    text: str,
    voice_id: Optional[str] = None,
    emotional_state: str = "neutral",
    sample_rate: int = 8000,
) -> AsyncGenerator[bytes, None]:
    """Stream TTS audio from Cartesia Sonic 3.

    Yields PCM mulaw chunks at 8000 Hz for Telnyx/Twilio compatibility.
    """
    if not CARTESIA_API_KEY:
        raise ValueError("CARTESIA_API_KEY not set")

    vid = voice_id or CARTESIA_VOICE_ID_DEFAULT
    settings = EMOTION_SETTINGS.get(emotional_state, EMOTION_SETTINGS["neutral"])
    processed_text = inject_emotion_tags(text, emotional_state)

    headers = {
        "X-API-Key": CARTESIA_API_KEY,
        "Cartesia-Version": "2024-06-10",
        "Content-Type": "application/json",
    }

    payload = {
        "model_id": CARTESIA_MODEL_ID,
        "transcript": processed_text,
        "voice": {
            "mode": "id",
            "id": vid,
        },
        "output_format": {
            "container": "raw",
            "encoding": "pcm_mulaw",
            "sample_rate": sample_rate,
        },
        "language": "en",
        "__experimental_controls": {
            "speed": settings["speed"],
            "emotion": settings["emotion"],
        },
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            CARTESIA_STREAM_URL,
            headers=headers,
            json=payload,
        ) as resp:
            if resp.status == 200:
                async for chunk in resp.content.iter_chunked(2048):
                    if chunk:
                        yield chunk
            else:
                error_text = await resp.text()
                raise Exception(f"Cartesia TTS error {resp.status}: {error_text}")


async def clone_voice(
    name: str,
    audio_files: list[bytes],
    description: str = "A2Z Dialer voice clone",
) -> dict:
    """Create a voice clone via Cartesia API.

    audio_files: list of raw bytes objects (WAV/MP3/etc.)
    Returns: {"ok": True, "voice_id": str, "name": str} or {"ok": False, "error": str}
    """
    if not CARTESIA_API_KEY:
        return {"ok": False, "error": "CARTESIA_API_KEY not set"}

    headers = {
        "X-API-Key": CARTESIA_API_KEY,
        "Cartesia-Version": "2024-06-10",
    }

    form_data = aiohttp.FormData()
    form_data.add_field("name", name)
    form_data.add_field("description", description)
    form_data.add_field("language", "en")
    form_data.add_field("mode", "stability")

    for i, audio_bytes in enumerate(audio_files):
        form_data.add_field(
            "clip",
            audio_bytes,
            filename=f"sample_{i}.wav",
            content_type="audio/wav",
        )

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://api.cartesia.ai/voices/clone",
            headers=headers,
            data=form_data,
        ) as resp:
            if resp.status in (200, 201):
                data = await resp.json()
                return {
                    "ok": True,
                    "voice_id": data.get("id"),
                    "name": data.get("name"),
                }
            else:
                error = await resp.text()
                return {"ok": False, "error": error}


async def delete_voice(voice_id: str) -> dict:
    """Delete a cloned voice from Cartesia."""
    if not CARTESIA_API_KEY:
        return {"ok": False, "error": "CARTESIA_API_KEY not set"}

    headers = {
        "X-API-Key": CARTESIA_API_KEY,
        "Cartesia-Version": "2024-06-10",
    }
    async with aiohttp.ClientSession() as session:
        async with session.delete(
            f"https://api.cartesia.ai/voices/{voice_id}",
            headers=headers,
        ) as resp:
            return {"ok": resp.status in (200, 204)}


async def generate_test_sample(
    voice_id: str,
    agent_name: str = "your agent",
    workspace_name: str = "your brokerage",
) -> bytes:
    """Generate a short test audio preview for a voice clone."""
    text = (
        f"Hey, this is {agent_name} calling from {workspace_name}. "
        "Quick question about your property — do you have like sixty seconds?"
    )
    chunks: list[bytes] = []
    async for chunk in stream_tts(text, voice_id=voice_id, emotional_state="warm"):
        chunks.append(chunk)
    return b"".join(chunks)
