"""
Voice Clone Routes — standalone APIRouter for /voice-clone endpoints.
Mounted into main.py via app.include_router(voice_clone_router).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlmodel import Session, select

from auth import get_current_user
from db import get_session
from models import AgentVoiceClone, Campaign, Notification, User, Workspace

logger = logging.getLogger("a2z")

router = APIRouter(prefix="/voice-clone", tags=["voice-clone"])

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_CLONE_URL = "https://api.elevenlabs.io/v1/voices/add"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"

VOICE_PROVIDER = os.getenv("VOICE_PROVIDER", "cartesia")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_active_clone(workspace_id: int, session: Session) -> AgentVoiceClone | None:
    """Return the clone flagged is_active=True, or fall back to the first non-deleted one."""
    active = session.exec(
        select(AgentVoiceClone).where(
            AgentVoiceClone.workspace_id == workspace_id,
            AgentVoiceClone.is_active == True,  # noqa: E712
            AgentVoiceClone.status != "deleted",
        )
    ).first()
    if active:
        return active
    # Legacy fallback — first non-deleted
    return session.exec(
        select(AgentVoiceClone).where(
            AgentVoiceClone.workspace_id == workspace_id,
            AgentVoiceClone.status != "deleted",
        )
    ).first()


def _get_all_clones(workspace_id: int, session: Session) -> list[AgentVoiceClone]:
    """Return all non-deleted clones for this workspace."""
    return list(
        session.exec(
            select(AgentVoiceClone).where(
                AgentVoiceClone.workspace_id == workspace_id,
                AgentVoiceClone.status != "deleted",
            )
        ).all()
    )


def _clone_to_dict(c: AgentVoiceClone) -> dict:
    return {
        "id": c.id,
        "display_name": c.display_name,
        "status": c.status,
        "is_active": c.is_active,
        "elevenlabs_voice_id": c.elevenlabs_voice_id,
        "sample_count": c.sample_count,
        "quality_score": c.quality_score,
        "rejection_reason": c.rejection_reason,
        "created_at": str(c.created_at),
    }


async def _call_elevenlabs_clone(
    files: list[tuple[str, bytes, str]],
    voice_name: str,
    workspace_id: int,
) -> dict:
    """POST to ElevenLabs /v1/voices/add and return response JSON."""
    file_list = [("files", (fname, data, mime)) for fname, data, mime in files]
    form_data = {
        "name": voice_name,
        "description": "Agent voice clone for A2Z Dialer",
        "labels": json.dumps({"type": "agent_clone", "workspace_id": str(workspace_id)}),
    }
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            ELEVENLABS_CLONE_URL,
            headers={"xi-api-key": ELEVENLABS_API_KEY},
            data=form_data,
            files=file_list,
        )
    resp.raise_for_status()
    return resp.json()


async def _finish_clone(
    clone: AgentVoiceClone,
    prepared: list[tuple[str, bytes, str]],
    workspace: Workspace,
    current_user: User,
    session: Session,
) -> None:
    """Clone voice via Cartesia (primary) or ElevenLabs (fallback), update status, notify."""
    try:
        if VOICE_PROVIDER == "cartesia":
            from cartesia_tts import clone_voice as cartesia_clone_voice  # type: ignore
            audio_files = [data for _fname, data, _mime in prepared]
            result = await cartesia_clone_voice(
                name=clone.display_name,
                audio_files=audio_files,
            )
            if result.get("ok"):
                clone.elevenlabs_voice_id = result.get("voice_id", "")  # reuse field for Cartesia voice ID
                clone.status = "active"
                clone.is_active = True
            else:
                clone.status = "failed"
                clone.rejection_reason = result.get("error", "Cartesia clone failed")[:500]
                clone.is_active = False
                clone.updated_at = _utcnow()
                session.add(clone)
                session.commit()
                raise Exception(clone.rejection_reason)
        elif ELEVENLABS_API_KEY:
            result = await _call_elevenlabs_clone(
                prepared,
                clone.display_name,
                current_user.workspace_id,
            )
            clone.elevenlabs_voice_id = result.get("voice_id", "")
            clone.status = "active"
            clone.is_active = True
        else:
            # Dev mode — no real API key
            clone.elevenlabs_voice_id = "dev_placeholder"
            clone.status = "active"
            clone.is_active = True

        clone.updated_at = _utcnow()
        session.add(clone)

        # Auto-assign clone to all campaigns that don't have one yet
        unassigned_campaigns = session.exec(
            select(Campaign).where(
                Campaign.workspace_id == current_user.workspace_id,
                Campaign.agent_voice_clone_id == None,  # noqa: E711
            )
        ).all()
        campaigns_updated = 0
        for camp in unassigned_campaigns:
            camp.agent_voice_clone_id = clone.id
            camp.voice_type = "clone"
            session.add(camp)
            campaigns_updated += 1

        # In-app notification
        session.add(Notification(
            user_id=current_user.id,
            workspace_id=current_user.workspace_id,
            type="voice_clone_ready",
            title="Your voice clone is ready",
            body=(
                f'"{clone.display_name}" is active and assigned to '
                f"{campaigns_updated} campaign{'s' if campaigns_updated != 1 else ''}."
                if campaigns_updated > 0
                else f'"{clone.display_name}" is ready to use in campaigns.'
            ),
            link="/app/settings/voice-clone",
        ))
        session.commit()

        try:
            from email_service import send_voice_clone_activated  # type: ignore
            send_voice_clone_activated(
                current_user.email,
                current_user.full_name or "Agent",
                workspace.name,
                clone.display_name,
                campaigns_updated,
            )
        except Exception:
            pass

    except HTTPException:
        raise
    except Exception as exc:
        clone.status = "failed"
        clone.rejection_reason = str(exc)[:500]
        clone.is_active = False
        clone.updated_at = _utcnow()
        session.add(clone)
        session.commit()
        logger.error(f"Voice clone failed workspace={current_user.workspace_id}: {exc}")
        raise HTTPException(500, f"Voice cloning failed: {exc}")


# ── GET /voice-clone ──────────────────────────────────────────────────────────

@router.get("")
async def get_voice_clone(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Return the workspace's current active voice clone or null."""
    clone = _get_active_clone(current_user.workspace_id, session)
    return {"clone": _clone_to_dict(clone) if clone else None}


# ── GET /voice-clone/library ──────────────────────────────────────────────────

@router.get("/library")
async def get_voice_clone_library(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Return ALL non-deleted voice clones for this workspace, newest first."""
    clones = _get_all_clones(current_user.workspace_id, session)
    clones_sorted = sorted(clones, key=lambda c: c.created_at, reverse=True)
    return {"clones": [_clone_to_dict(c) for c in clones_sorted]}


# ── GET /voice-clone/status ───────────────────────────────────────────────────

@router.get("/status")
async def get_voice_clone_status(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Poll for processing status."""
    clone = _get_active_clone(current_user.workspace_id, session)
    if not clone:
        return {"status": "none", "is_active": False, "quality_score": None, "elevenlabs_voice_id": None}
    return {
        "status": clone.status,
        "is_active": clone.is_active,
        "quality_score": clone.quality_score,
        "elevenlabs_voice_id": clone.elevenlabs_voice_id,
    }


# ── POST /voice-clone/upload ──────────────────────────────────────────────────

ACCEPTED_AUDIO = {
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav",
    "audio/m4a", "audio/x-m4a", "audio/webm", "video/webm", "audio/ogg",
}
MAX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


@router.post("/upload")
async def upload_voice_clone(
    files: list[UploadFile] = File(...),
    display_name: str = Form(default="My Voice"),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Accept up to 25 audio files and create a named voice clone via ElevenLabs.

    display_name lets the agent name the voice anything (e.g. 'Jordan Voice').
    The new clone becomes the active voice for campaigns.
    """
    if not files:
        raise HTTPException(400, "At least 1 audio file is required")
    if len(files) > 25:
        raise HTTPException(400, "Maximum 25 files allowed per ElevenLabs spec")

    workspace = session.get(Workspace, current_user.workspace_id)
    if not workspace:
        raise HTTPException(404, "Workspace not found")

    prepared: list[tuple[str, bytes, str]] = []
    for f in files:
        raw = await f.read()
        if len(raw) > MAX_FILE_BYTES:
            raise HTTPException(400, f"'{f.filename}' exceeds the 10 MB per-file limit")
        mime = f.content_type or "audio/webm"
        if mime not in ACCEPTED_AUDIO and not mime.startswith("audio/"):
            raise HTTPException(400, f"Unsupported audio type: {mime}")
        prepared.append((f.filename or "sample.webm", raw, mime))

    # Deactivate current active clone (keep it in library)
    for existing in _get_all_clones(current_user.workspace_id, session):
        if existing.is_active:
            existing.is_active = False
            session.add(existing)
    session.commit()

    clone = AgentVoiceClone(
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        display_name=display_name.strip() or "My Voice",
        status="processing",
        sample_count=len(prepared),
    )
    session.add(clone)
    session.commit()
    session.refresh(clone)

    await _finish_clone(clone, prepared, workspace, current_user, session)
    return {"ok": True, "voice_clone_id": clone.id, "status": clone.status}


# ── POST /voice-clone/upload-url ──────────────────────────────────────────────

@router.post("/upload-url")
async def upload_voice_clone_from_url(
    url: str = Form(...),
    display_name: str = Form(default="My Voice"),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Download audio from a URL (Google Drive, Dropbox, etc.) and clone it server-side."""
    workspace = session.get(Workspace, current_user.workspace_id)
    if not workspace:
        raise HTTPException(404, "Workspace not found")

    try:
        async with httpx.AsyncClient(
            timeout=60.0,
            follow_redirects=True,
            headers={"User-Agent": "A2ZDialer/1.0"},
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            raw = resp.content
    except httpx.HTTPStatusError as e:
        raise HTTPException(400, f"Could not download file: HTTP {e.response.status_code}")
    except Exception as e:
        raise HTTPException(400, f"Could not download file from URL: {e}")

    if len(raw) == 0:
        raise HTTPException(400, "Downloaded file is empty")
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(400, "Downloaded file exceeds the 10 MB limit")

    content_type = resp.headers.get("content-type", "audio/mpeg").split(";")[0].strip()
    if content_type not in ACCEPTED_AUDIO and not content_type.startswith("audio/"):
        content_type = "audio/mpeg"

    fname = url.rstrip("/").split("/")[-1].split("?")[0] or "audio.mp3"

    for existing in _get_all_clones(current_user.workspace_id, session):
        if existing.is_active:
            existing.is_active = False
            session.add(existing)
    session.commit()

    clone = AgentVoiceClone(
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        display_name=display_name.strip() or "My Voice",
        status="processing",
        sample_count=1,
    )
    session.add(clone)
    session.commit()
    session.refresh(clone)

    await _finish_clone(clone, [(fname, raw, content_type)], workspace, current_user, session)
    return {"ok": True, "voice_clone_id": clone.id, "status": clone.status}


# ── POST /voice-clone/record ──────────────────────────────────────────────────

@router.post("/record")
async def record_voice_clone(
    file: UploadFile = File(...),
    display_name: str = Form(default="My Voice"),
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Accept a single recorded audio blob (webm/wav from browser MediaRecorder)."""
    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(400, "No audio data received — check microphone permissions")
    if len(raw) > MAX_FILE_BYTES:
        raise HTTPException(400, "Recording exceeds the 10 MB limit")

    workspace = session.get(Workspace, current_user.workspace_id)
    if not workspace:
        raise HTTPException(404, "Workspace not found")

    for existing in _get_all_clones(current_user.workspace_id, session):
        if existing.is_active:
            existing.is_active = False
            session.add(existing)
    session.commit()

    clone = AgentVoiceClone(
        workspace_id=current_user.workspace_id,
        user_id=current_user.id,
        display_name=display_name.strip() or "My Voice",
        status="processing",
        sample_count=1,
    )
    session.add(clone)
    session.commit()
    session.refresh(clone)

    mime = file.content_type or "audio/webm"
    await _finish_clone(clone, [("recording.webm", raw, mime)], workspace, current_user, session)
    return {"ok": True, "voice_clone_id": clone.id, "status": clone.status}


# ── POST /voice-clone/{clone_id}/set-active ───────────────────────────────────

@router.post("/{clone_id}/set-active")
async def set_active_voice_clone(
    clone_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Set a specific clone as the active voice. Deactivates all others."""
    target = session.exec(
        select(AgentVoiceClone).where(
            AgentVoiceClone.id == clone_id,
            AgentVoiceClone.workspace_id == current_user.workspace_id,
            AgentVoiceClone.status != "deleted",
        )
    ).first()
    if not target:
        raise HTTPException(404, "Voice clone not found")
    if target.status != "active":
        raise HTTPException(400, f"Cannot activate a clone with status '{target.status}'")

    for c in _get_all_clones(current_user.workspace_id, session):
        if c.id != clone_id and c.is_active:
            c.is_active = False
            session.add(c)

    target.is_active = True
    session.add(target)
    session.commit()
    return {"ok": True, "active_clone_id": clone_id}


# ── PUT /voice-clone/test ─────────────────────────────────────────────────────

@router.put("/test")
async def test_voice_clone(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Generate a short test audio sample using the active cloned voice."""
    clone = session.exec(
        select(AgentVoiceClone).where(
            AgentVoiceClone.workspace_id == current_user.workspace_id,
            AgentVoiceClone.is_active == True,  # noqa: E712
        )
    ).first()
    if not clone or not clone.elevenlabs_voice_id:
        raise HTTPException(404, "No active voice clone found")
    if clone.elevenlabs_voice_id == "dev_placeholder":
        raise HTTPException(400, "Configure a voice provider API key to enable playback")

    workspace = session.get(Workspace, current_user.workspace_id)
    agent_name = current_user.full_name or "your agent"
    ws_name = workspace.name if workspace else "A2Z Dialer"

    if VOICE_PROVIDER == "cartesia":
        from cartesia_tts import generate_test_sample  # type: ignore
        audio = await generate_test_sample(
            voice_id=clone.elevenlabs_voice_id,
            agent_name=agent_name,
            workspace_name=ws_name,
        )
        return Response(content=audio, media_type="audio/wav")

    # ElevenLabs fallback
    if not ELEVENLABS_API_KEY:
        raise HTTPException(400, "ELEVENLABS_API_KEY is not configured")
    test_text = (
        f"Hi, this is {agent_name} from {ws_name}. "
        "I'm reaching out about your property. Is now a good time to chat?"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{ELEVENLABS_TTS_URL}/{clone.elevenlabs_voice_id}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": test_text,
                "model_id": "eleven_turbo_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
        )
    resp.raise_for_status()
    return Response(content=resp.content, media_type="audio/mpeg")


# ── PUT /voice-clone/{clone_id}/test ─────────────────────────────────────────

@router.put("/{clone_id}/test")
async def test_specific_voice_clone(
    clone_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> Response:
    """Generate a test audio sample for a specific clone by ID."""
    clone = session.exec(
        select(AgentVoiceClone).where(
            AgentVoiceClone.id == clone_id,
            AgentVoiceClone.workspace_id == current_user.workspace_id,
            AgentVoiceClone.status == "active",
        )
    ).first()
    if not clone or not clone.elevenlabs_voice_id:
        raise HTTPException(404, "Voice clone not found or not active")
    if clone.elevenlabs_voice_id == "dev_placeholder":
        raise HTTPException(400, "Configure a voice provider API key to enable playback")

    workspace = session.get(Workspace, current_user.workspace_id)
    agent_name = current_user.full_name or "your agent"
    ws_name = workspace.name if workspace else "A2Z Dialer"

    if VOICE_PROVIDER == "cartesia":
        from cartesia_tts import generate_test_sample  # type: ignore
        audio = await generate_test_sample(
            voice_id=clone.elevenlabs_voice_id,
            agent_name=agent_name,
            workspace_name=ws_name,
        )
        return Response(content=audio, media_type="audio/wav")

    # ElevenLabs fallback
    if not ELEVENLABS_API_KEY:
        raise HTTPException(400, "ELEVENLABS_API_KEY is not configured")
    test_text = (
        f"Hey, this is {agent_name} with {ws_name}. "
        "I'm reaching out about your property — do you have sixty seconds?"
    )
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{ELEVENLABS_TTS_URL}/{clone.elevenlabs_voice_id}",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": test_text,
                "model_id": "eleven_turbo_v2",
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
        )
    resp.raise_for_status()
    return Response(content=resp.content, media_type="audio/mpeg")


# ── DELETE /voice-clone/{clone_id} ────────────────────────────────────────────

@router.delete("/{clone_id}")
async def delete_voice_clone_by_id(
    clone_id: int,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Delete a specific voice clone by ID. If it was active, promote another."""
    clone = session.exec(
        select(AgentVoiceClone).where(
            AgentVoiceClone.id == clone_id,
            AgentVoiceClone.workspace_id == current_user.workspace_id,
        )
    ).first()
    if not clone:
        raise HTTPException(404, "Voice clone not found")

    was_active = clone.is_active

    if clone.elevenlabs_voice_id and clone.elevenlabs_voice_id != "dev_placeholder":
        try:
            if VOICE_PROVIDER == "cartesia":
                from cartesia_tts import delete_voice as cartesia_delete_voice  # type: ignore
                import asyncio as _asyncio
                _asyncio.ensure_future(cartesia_delete_voice(clone.elevenlabs_voice_id))
            elif ELEVENLABS_API_KEY:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    await client.delete(
                        f"https://api.elevenlabs.io/v1/voices/{clone.elevenlabs_voice_id}",
                        headers={"xi-api-key": ELEVENLABS_API_KEY},
                    )
        except Exception as e:
            logger.warning(f"Could not delete voice {clone.elevenlabs_voice_id}: {e}")

    for c in session.exec(
        select(Campaign).where(Campaign.agent_voice_clone_id == clone_id)
    ).all():
        c.voice_type = "platform"
        c.agent_voice_clone_id = None
        session.add(c)

    clone.status = "deleted"
    clone.is_active = False
    clone.updated_at = _utcnow()
    session.add(clone)

    if was_active:
        next_clone = session.exec(
            select(AgentVoiceClone).where(
                AgentVoiceClone.workspace_id == current_user.workspace_id,
                AgentVoiceClone.status == "active",
                AgentVoiceClone.id != clone_id,
            )
        ).first()
        if next_clone:
            next_clone.is_active = True
            session.add(next_clone)

    session.commit()
    return {"ok": True}


# ── DELETE /voice-clone (legacy — deletes active clone) ───────────────────────

@router.delete("")
async def delete_voice_clone(
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict:
    """Delete the currently active voice clone (legacy endpoint)."""
    clone = _get_active_clone(current_user.workspace_id, session)
    if not clone:
        raise HTTPException(404, "No voice clone found")

    if clone.elevenlabs_voice_id and clone.elevenlabs_voice_id != "dev_placeholder":
        try:
            if VOICE_PROVIDER == "cartesia":
                from cartesia_tts import delete_voice as cartesia_delete_voice  # type: ignore
                import asyncio as _asyncio
                _asyncio.ensure_future(cartesia_delete_voice(clone.elevenlabs_voice_id))
            elif ELEVENLABS_API_KEY:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    await client.delete(
                        f"https://api.elevenlabs.io/v1/voices/{clone.elevenlabs_voice_id}",
                        headers={"xi-api-key": ELEVENLABS_API_KEY},
                    )
        except Exception as e:
            logger.warning(f"Could not delete voice {clone.elevenlabs_voice_id}: {e}")

    for c in session.exec(
        select(Campaign).where(Campaign.agent_voice_clone_id == clone.id)
    ).all():
        c.voice_type = "platform"
        c.agent_voice_clone_id = None
        session.add(c)

    clone.status = "deleted"
    clone.is_active = False
    clone.updated_at = _utcnow()
    session.add(clone)
    session.commit()
    return {"ok": True}


# ── Admin helper ──────────────────────────────────────────────────────────────

def get_admin_voice_clones_data(session: Session) -> dict:
    """Return aggregate voice-clone metrics for the admin dashboard."""
    all_clones = session.exec(select(AgentVoiceClone)).all()
    active = [c for c in all_clones if c.status == "active"]
    failed = [c for c in all_clones if c.status == "failed"]
    recent = sorted(active, key=lambda c: c.created_at, reverse=True)[:5]
    return {
        "total_active": len(active),
        "total_failed": len(failed),
        "total_all": len(all_clones),
        "recent_activations": [
            {"id": c.id, "workspace_id": c.workspace_id, "display_name": c.display_name, "created_at": str(c.created_at)}
            for c in recent
        ],
        "failed_clones": [
            {"id": c.id, "workspace_id": c.workspace_id, "rejection_reason": c.rejection_reason, "created_at": str(c.created_at)}
            for c in failed
        ],
    }
