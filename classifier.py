from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional

try:
    import openai as _openai_module
    _openai_available = True
except ImportError:
    _openai_available = False

from config import settings

logger = logging.getLogger("a2z.classifier")


FLAG_PATTERNS: dict[str, list[str]] = {
    "opt_out": [
        r"\bdo not call\b",
        r"\bremove me\b",
        r"\btake me off\b",
        r"\bstop calling\b",
        r"\bdon't call\b",
        r"\bnot interested\b",
        r"\bquit calling\b",
    ],
    "wrong_number": [
        r"\bwrong number\b",
        r"\byou have the wrong\b",
        r"\bno (he|she|they) (doesn't|don't) live here\b",
        r"\bnot .* phone\b",
    ],
    "repeat_request": [
        r"\brepeat\b",
        r"\bsay that again\b",
        r"\bcome again\b",
        r"\bwhat was that\b",
    ],
    "confused": [
        r"\bwho is this\b",
        r"\bwhat is this about\b",
        r"\bwhat do you want\b",
        r"\bi don't understand\b",
    ],
    "who_are_you": [
        r"\bwho are you\b",
        r"\bwhat company\b",
        r"\bwhat brokerage\b",
        r"\bwhere are you calling from\b",
    ],
    "user_is_busy": [
        r"\bi'?m busy\b",
        r"\bcall me later\b",
        r"\bcan't talk\b",
        r"\bnot a good time\b",
        r"\bin a meeting\b",
        r"\bdriving\b",
    ],
    "mentions_sold": [
        r"\bsold\b",
        r"\bit sold\b",
        r"\bwe sold it\b",
        r"\balready sold\b",
    ],
    "mentions_already_listed": [
        r"\balready listed\b",
        r"\bwith an agent\b",
        r"\bwe relisted\b",
        r"\bcurrently listed\b",
    ],
    "mentions_available": [
        r"\bstill available\b",
        r"\bstill for sale\b",
        r"\bhasn't sold\b",
        r"\bnot sold yet\b",
    ],
    "user_affirms": [
        r"\byes\b",
        r"\byeah\b",
        r"\byep\b",
        r"\bcorrect\b",
    ],
    "user_denies": [
        r"\bno\b",
        r"\bnope\b",
        r"\bnot really\b",
    ],
    "asks_buyer": [
        r"\bdo you have a buyer\b",
        r"\bbring a buyer\b",
        r"\bare you the buyer\b",
    ],
    "mentions_rates": [
        r"\brates\b",
        r"\binterest rate\b",
        r"\bmortgage\b",
    ],
    "mentions_commission": [
        r"\bcommission\b",
        r"\bfee\b",
        r"\bpercent\b",
    ],
    "wants_list_high": [
        r"\bwant more money\b",
        r"\blist high\b",
        r"\btoo low\b",
    ],
    "wants_think_it_over": [
        r"\bthink about it\b",
        r"\bthink it over\b",
        r"\bneed to think\b",
    ],
    "has_agent_friend": [
        r"\bfriend.*agent\b",
        r"\bcousin.*agent\b",
        r"\bbrother.*agent\b",
    ],
    "time_is_4pm": [
        r"\b4 ?pm\b",
        r"\b4 o'?clock\b",
    ],
    "time_is_6pm": [
        r"\b6 ?pm\b",
        r"\b6 o'?clock\b",
    ],
    "user_requests_other_time": [
        r"\banother time\b",
        r"\bdifferent time\b",
        r"\bneither\b",
        r"\bdoesn't work\b",
    ],
    "email_given": [
        r"\b[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,}\b",
    ],
}


def _contains_pattern(text: str, pattern: str) -> bool:
    return re.search(pattern, text, flags=re.IGNORECASE) is not None


def classify_text(text: str) -> Dict[str, Any]:
    raw = (text or "").strip()
    normalized = raw.lower()

    flags: Dict[str, Any] = {"raw_text": raw}

    for flag_name, patterns in FLAG_PATTERNS.items():
        flags[flag_name] = any(
            _contains_pattern(normalized, pattern) for pattern in patterns
        )

    email_match = re.search(
        r"\b([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[A-Za-z]{2,})\b",
        raw,
        flags=re.IGNORECASE,
    )

    flags["detected_email"] = email_match.group(1) if email_match else None

    phone_digits = re.sub(r"\D", "", raw)

    if len(phone_digits) >= 10:
        flags["has_possible_phone"] = True
        flags["detected_phone"] = phone_digits[-10:]
    else:
        flags["has_possible_phone"] = False
        flags["detected_phone"] = None

    return flags


_CLASSIFY_SYSTEM_PROMPT = """Analyze this homeowner's response in a real estate cold call. Return JSON only:
{
  "opt_out": bool, "wrong_number": bool, "user_affirms": bool, "user_denies": bool,
  "user_is_busy": bool, "confused": bool, "repeat_request": bool, "who_are_you": bool,
  "mentions_sold": bool, "mentions_already_listed": bool, "mentions_available": bool,
  "has_agent_friend": bool, "asks_buyer": bool, "mentions_rates": bool,
  "mentions_commission": bool, "wants_list_high": bool, "wants_think_it_over": bool,
  "time_is_4pm": bool, "time_is_6pm": bool, "user_requests_other_time": bool,
  "user_refuses_email": bool,
  "emotional_state": "neutral|defensive|curious|warm|frustrated|rushed|confused",
  "response_length": "very_short|short|medium|long",
  "contains_question": bool, "hesitation_detected": bool
}"""


def classify(text: str, use_ai: bool = True) -> Dict[str, Any]:
    """Classify homeowner reply using GPT-4o-mini for richer signals.

    Falls back to regex-based classify_text if AI is unavailable or fails.
    Always returns all original fields plus emotional_state, response_length,
    contains_question, hesitation_detected.
    """
    # Always compute regex-based baseline (used as fallback and for email/phone extraction)
    base = classify_text(text)

    if not use_ai or not _openai_available or not settings.openai_api_key:
        # Add new fields with sensible defaults
        base.setdefault("emotional_state", "neutral")
        base.setdefault("response_length", "short")
        base.setdefault("contains_question", "?" in (text or ""))
        base.setdefault("hesitation_detected", False)
        return base

    try:
        client = _openai_module.OpenAI(api_key=settings.openai_api_key)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
                {"role": "user", "content": (text or "").strip()},
            ],
            temperature=0.0,
            max_tokens=400,
        )
        raw_json = response.choices[0].message.content or "{}"
        ai_flags: dict = json.loads(raw_json)

        # Merge: AI result takes priority for boolean flags, keep base for phone/email
        merged: Dict[str, Any] = dict(base)
        for key, val in ai_flags.items():
            merged[key] = val

        # Ensure phone/email fields from regex are preserved
        merged["raw_text"] = base.get("raw_text", "")
        merged["detected_email"] = base.get("detected_email")
        merged["has_possible_phone"] = base.get("has_possible_phone", False)
        merged["detected_phone"] = base.get("detected_phone")

        return merged

    except Exception:
        logger.exception("classify() AI call failed, falling back to regex")
        base.setdefault("emotional_state", "neutral")
        base.setdefault("response_length", "short")
        base.setdefault("contains_question", "?" in (text or ""))
        base.setdefault("hesitation_detected", False)
        return base


# ---------------------------------------------------------------------------
# classify_full — lightweight intent + emotion dict for the realtime bridge
# ---------------------------------------------------------------------------

_INTENT_PATTERNS: dict[str, list[str]] = {
    "opt_out": [
        r"take me off", r"remove me", r"do not call",
        r"don.t call", r"stop calling", r"never call",
        r"unsubscribe", r"get off my", r"stop contact",
    ],
    "interested": [
        r"tell me more", r"how much", r"when can you",
        r"thinking about", r"considering", r"maybe",
        r"possibly", r"open to", r"could be",
    ],
    "has_agent": [
        r"already have.*agent", r"working with.*agent",
        r"listed.*already", r"have.*realtor",
        r"my agent", r"my realtor",
    ],
    "is_ai_question": [
        r"are you (a robot|an ai|real|a person|human)",
        r"is this (ai|automated|a bot)",
        r"am i talking to", r"real person",
    ],
    "busy": [
        r"bad time", r"not a good time", r"busy right now",
        r"call.*back", r"call.*later", r"in a meeting",
        r"at work", r"driving",
    ],
    "not_interested": [
        r"not interested", r"no thank you", r"no thanks",
        r"don.t want", r"stop",
    ],
    "wrong_number": [
        r"wrong number", r"not.*right person",
        r"no one here by that name", r"moved",
    ],
    "already_sold": [
        r"already sold", r"sold.*already", r"we sold",
    ],
}

_EMOTION_PATTERNS: dict[str, list[str]] = {
    "frustrated": [
        r"stop calling", r"how many times",
        r"already told", r"leave.*alone",
        r"annoying", r"ridiculous",
    ],
    "interested": [
        r"tell me more", r"really", r"interesting",
        r"yeah.*open", r"actually.*thinking",
        r"good timing",
    ],
    "skeptical": [
        r"sure.*right", r"yeah.*right", r"doubt.*that",
        r"how do i know", r"prove it",
    ],
    "confused": [
        r"what do you mean", r"i don.t understand",
        r"who.*again", r"wait.*what", r"huh",
    ],
    "hostile": [
        r"go to hell", r"screw you", r"i.ll sue",
        r"report you", r"get a lawyer",
    ],
    "warm": [
        r"oh nice", r"that.s great", r"sounds good",
        r"i appreciate", r"that.s helpful",
    ],
}


def _classify_intent(text: str) -> str:
    lower = text.lower().strip()
    for intent, patterns in _INTENT_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                return intent
    return "unknown"


def _classify_emotion(text: str) -> str:
    lower = text.lower().strip()
    for emotion, patterns in _EMOTION_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, lower):
                return emotion
    return "neutral"


def classify_full(transcript: str) -> dict:
    """Lightweight synchronous classifier for the realtime bridge.

    Returns intent, emotion, and a handful of boolean signals without
    making any external API calls — safe to call in a hot audio path.
    """
    return {
        "intent": _classify_intent(transcript),
        "emotion": _classify_emotion(transcript),
        "transcript": transcript,
        "word_count": len(transcript.split()),
        "is_question": transcript.strip().endswith("?"),
        "mentions_price": bool(re.search(
            r"price|value|worth|how much", transcript.lower())),
        "mentions_agent": bool(re.search(
            r"agent|realtor|broker", transcript.lower())),
        "wants_callback": bool(re.search(
            r"call.*back|later|tomorrow|busy", transcript.lower())),
    }