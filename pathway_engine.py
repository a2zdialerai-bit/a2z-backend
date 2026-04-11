from __future__ import annotations

import json
import logging
import os
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]

logger = logging.getLogger(__name__)


@dataclass
class RouteDecision:
    current_node: str
    next_node: Optional[str]
    fired_route: Optional[dict]
    prompt: str
    node: dict


def safe_json_load(raw: str | dict | list | None) -> Any:
    if raw is None:
        return {}
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return {}
    s = raw.strip()
    if not s:
        return {}
    return json.loads(s)


def validate_pathway_json(obj: Any) -> List[str]:
    errors: List[str] = []

    if not isinstance(obj, dict):
        return ["Pathway JSON must be an object"]

    start_node = obj.get("start_node")
    nodes = obj.get("nodes")

    if not isinstance(start_node, str) or not start_node.strip():
        errors.append("Missing or invalid 'start_node'")

    if not isinstance(nodes, dict) or not nodes:
        errors.append("Missing or invalid 'nodes' map")
        return errors

    if isinstance(start_node, str) and start_node not in nodes:
        errors.append(f"start_node '{start_node}' not found in nodes")

    for node_id, node in nodes.items():
        if not isinstance(node_id, str) or not node_id.strip():
            errors.append("All node ids must be non-empty strings")
            continue

        if not isinstance(node, dict):
            errors.append(f"Node '{node_id}' must be an object")
            continue

        node_type = node.get("type")
        if node_type not in {"say", "listen", "end"}:
            errors.append(f"Node '{node_id}' has invalid type '{node_type}'")

        if "prompt" in node and not isinstance(node.get("prompt"), str):
            errors.append(f"Node '{node_id}' prompt must be a string")

        if "extract" in node and not isinstance(node.get("extract"), dict):
            errors.append(f"Node '{node_id}' extract must be an object")

        routes = node.get("routes")
        if routes is not None:
            if not isinstance(routes, list):
                errors.append(f"Node '{node_id}' routes must be an array")
            else:
                for idx, route in enumerate(routes):
                    if not isinstance(route, dict):
                        errors.append(f"Node '{node_id}' route #{idx + 1} must be an object")
                        continue

                    when = route.get("when")
                    nxt = route.get("next")

                    if not isinstance(when, str) or not when.strip():
                        errors.append(f"Node '{node_id}' route #{idx + 1} missing 'when'")

                    if not isinstance(nxt, str) or not nxt.strip():
                        errors.append(f"Node '{node_id}' route #{idx + 1} missing 'next'")
                    elif nxt not in nodes:
                        errors.append(
                            f"Node '{node_id}' route #{idx + 1} points to unknown node '{nxt}'"
                        )

        transitions = node.get("transitions")
        if transitions is not None:
            if not isinstance(transitions, dict):
                errors.append(f"Node '{node_id}' transitions must be an object")
            else:
                for _, nxt in transitions.items():
                    if not isinstance(nxt, str):
                        errors.append(f"Node '{node_id}' transitions values must be strings")
                    elif nxt not in nodes:
                        errors.append(f"Node '{node_id}' transition points to unknown node '{nxt}'")

        fallback_next = node.get("fallback_next")
        if fallback_next is not None:
            if not isinstance(fallback_next, str):
                errors.append(f"Node '{node_id}' fallback_next must be a string")
            elif fallback_next not in nodes:
                errors.append(
                    f"Node '{node_id}' fallback_next points to unknown node '{fallback_next}'"
                )

    return errors


def render_prompt(template: str, context: Optional[dict] = None) -> str:
    if not template:
        return ""

    context = context or {}
    variants = [x.strip() for x in template.split("||") if x.strip()]
    chosen = random.choice(variants) if variants else template

    def repl(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        value = context.get(key)
        return "" if value is None else str(value)

    return re.sub(r"{{\s*([^}]+)\s*}}", repl, chosen).strip()


def evaluate_when_expression(when: str, flags: dict, user_reply: str = "") -> bool:
    expr = (when or "").strip()
    if not expr:
        return False

    if expr.startswith("contains:"):
        needle = expr.split(":", 1)[1].strip().lower()
        return bool(needle and needle in (user_reply or "").lower())

    m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*==\s*(true|false)$", expr, flags=re.IGNORECASE)
    if m:
        key = m.group(1)
        want = m.group(2).lower() == "true"
        return bool(flags.get(key)) is want

    m = re.match(r"^([a-zA-Z_][a-zA-Z0-9_]*)\s*!=\s*(true|false)$", expr, flags=re.IGNORECASE)
    if m:
        key = m.group(1)
        want = m.group(2).lower() == "true"
        return bool(flags.get(key)) is not want

    return False


def get_node(pathway_obj: dict, node_id: str) -> dict:
    nodes = pathway_obj.get("nodes", {}) if isinstance(pathway_obj, dict) else {}
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        raise KeyError(f"Node not found: {node_id}")
    return node


def resolve_next_node(pathway_obj: dict, current_node: str, flags: dict, user_reply: str = "") -> RouteDecision:
    node = get_node(pathway_obj, current_node)
    prompt = render_prompt(node.get("prompt", ""), flags)

    fired_route = None
    next_node = None

    for route in node.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        when = str(route.get("when") or "").strip()
        nxt = route.get("next")

        if not isinstance(nxt, str) or not nxt:
            continue

        if evaluate_when_expression(when, flags, user_reply=user_reply):
            fired_route = route
            next_node = nxt
            break

    if not next_node:
        transitions = node.get("transitions") or {}
        if isinstance(transitions, dict) and isinstance(transitions.get("default"), str):
            next_node = transitions["default"]

    if not next_node and isinstance(node.get("fallback_next"), str):
        next_node = node["fallback_next"]

    return RouteDecision(
        current_node=current_node,
        next_node=next_node,
        fired_route=fired_route,
        prompt=prompt,
        node=node,
    )


def extract_fields_from_text(node: dict, reply: str, flags: dict) -> dict:
    result: dict[str, Any] = {}
    extract = node.get("extract") or {}

    if not isinstance(extract, dict):
        return result

    reply_clean = (reply or "").strip()

    for field_name in extract.keys():
        if field_name == "listing_status":
            if flags.get("mentions_sold"):
                result[field_name] = "sold"
            elif flags.get("mentions_already_listed"):
                result[field_name] = "already_listed"
            elif flags.get("mentions_available"):
                result[field_name] = "available"
            else:
                result[field_name] = reply_clean
        elif field_name == "appointment_time":
            result[field_name] = reply_clean
        elif field_name == "email":
            result[field_name] = flags.get("detected_email") or reply_clean
        else:
            result[field_name] = reply_clean

    return result


# ---------------------------------------------------------------------------
# Back-channel acknowledgment system
# ---------------------------------------------------------------------------

BACKCHANNELS: list[str] = ["Mm-hmm.", "Right.", "Yeah.", "Got it.", "Okay.", "Sure."]


def get_backchannel_phrase() -> str:
    """Return a random back-channel acknowledgment phrase."""
    return random.choice(BACKCHANNELS)


def should_send_backchannel(node: dict, speaking_duration_ms: int) -> bool:
    """Return True if a back-channel filler should be injected before the AI responds.

    Conditions:
    - Node type is "listen"
    - Node does not have enable_backchannels: False
    - Homeowner has been speaking for > 3 seconds
    - Not an exit/opt-out/end node
    """
    if not isinstance(node, dict):
        return False
    if node.get("type") != "listen":
        return False
    if node.get("enable_backchannels") is False:
        return False
    if speaking_duration_ms <= 3000:
        return False
    node_id: str = str(node.get("id", "") or "")
    if any(kw in node_id for kw in ("exit", "opt_out", "end")):
        return False
    return True


# ---------------------------------------------------------------------------
# Pacing modes
# ---------------------------------------------------------------------------

PACING_MODES: dict[str, float] = {
    "fast": 1.05,
    "normal": 1.00,
    "slow": 0.92,
    "gentle": 0.88,
}

_KEY_QUESTION_WORDS = ("appointment", "offer", "recommend", "hiring", "meeting")


def get_pacing_mode(node: dict, emotional_state: str = "neutral") -> str:
    """Return a pacing mode string based on node context and homeowner emotional state."""
    if not isinstance(node, dict):
        return "normal"

    if emotional_state in ("frustrated", "defensive"):
        return "gentle"

    prompt_text = str(node.get("prompt") or "").lower()

    if node.get("type") == "listen" and any(w in prompt_text for w in _KEY_QUESTION_WORDS):
        return "slow"

    if node.get("type") == "say" and len(prompt_text) < 30:
        return "fast"

    return "normal"


# ---------------------------------------------------------------------------
# GPT bridge fallback (used only when pathway has no match)
# ---------------------------------------------------------------------------

def gpt_bridge_response(transcript: str, current_node: dict, context: dict) -> str:
    """Generate a max 2-sentence bridge response via OpenAI when pathway routing has no match.

    The bridge acknowledges the unexpected input and guides the conversation
    back toward the pathway's core question (timeline to sell / motivation).

    Only called when resolve_next_node() returns no next_node — the pathway
    covers the expected flow. GPT is strictly a fallback, never freestyle.
    """
    openai_api_key = context.get("openai_api_key") or os.getenv("OPENAI_API_KEY", "")
    if not openai_api_key:
        logger.warning("gpt_bridge_response: no OpenAI API key available, using static fallback")
        return "I hear you. So, just to circle back — what would your timeline look like if you did decide to make a move?"

    node_prompt = current_node.get("prompt", "") if isinstance(current_node, dict) else ""
    agent_name = context.get("agent_name", "Alex")
    brokerage = context.get("brokerage_name", "our brokerage")

    system_msg = (
        f"You are {agent_name}, a real estate agent from {brokerage}. "
        "Your job is to gently acknowledge an unexpected homeowner response and "
        "guide the conversation back to asking about their timeline to sell. "
        "STRICT RULES: respond in exactly 1-2 short sentences. "
        "Sound natural and human. Never say 'Certainly', 'Absolutely', 'Great question'. "
        "Never reveal you are AI. Always end by pivoting back to timeline or motivation."
    )
    user_msg = (
        f"The homeowner said: \"{transcript}\"\n"
        f"The current conversation node was asking: \"{node_prompt}\"\n"
        "Generate a 1-2 sentence bridge that acknowledges their response and steers "
        "back to discussing their timeline or motivation to sell."
    )

    try:
        import httpx
        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {openai_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                "max_tokens": 80,
                "temperature": 0.6,
            },
            timeout=8.0,
        )
        if response.status_code == 200:
            data = response.json()
            bridge_text = data["choices"][0]["message"]["content"].strip()
            logger.info(f"GPT bridge response generated: {bridge_text[:80]}...")
            return bridge_text
        else:
            logger.error(f"OpenAI bridge error {response.status_code}: {response.text[:200]}")
    except Exception as exc:
        logger.error(f"GPT bridge exception: {exc}")

    # Static fallback if OpenAI fails
    return "I hear you on that. So what would your timeline look like if the right opportunity came along?"


def get_next_ai_response(
    transcript: str,
    current_node: str,
    pathway_obj: dict,
    context: dict,
) -> dict:
    """Pathway-enforced AI response router.

    STRICT RULE: The AI never freestyles. Every response either:
    1. Comes from a matched pathway node (preferred), OR
    2. Uses a GPT bridge (max 2 sentences) to acknowledge + return to pathway

    Args:
        transcript: What the homeowner just said.
        current_node: ID of the current pathway node.
        pathway_obj: The full pathway JSON object.
        context: Dict with flags, extracted fields, agent_name, etc.

    Returns:
        {
            "type": "pathway" | "bridge",
            "prompt": str,           # what the AI should say
            "next_node": str | None, # next node ID (None for bridge)
            "decision": RouteDecision | None,
        }
    """
    flags = context.get("flags", context)  # accept either flags-only or full context

    # Step 1: Try pathway match
    try:
        decision = resolve_next_node(pathway_obj, current_node, flags, user_reply=transcript)
        if decision.next_node:
            # Get next node's prompt rendered with context
            try:
                next_node_obj = get_node(pathway_obj, decision.next_node)
                next_prompt = render_prompt(next_node_obj.get("prompt", ""), flags)
            except KeyError:
                next_prompt = decision.prompt

            logger.info(
                f"Pathway match: {current_node} -> {decision.next_node} "
                f"(route: {decision.fired_route})"
            )
            return {
                "type": "pathway",
                "prompt": next_prompt,
                "next_node": decision.next_node,
                "decision": decision,
            }
    except Exception as exc:
        logger.error(f"Pathway resolve error at node {current_node}: {exc}")

    # Step 2: GPT bridge (only if truly no pathway match)
    logger.info(f"No pathway match at node {current_node} — using GPT bridge")
    current_node_obj = {}
    try:
        current_node_obj = get_node(pathway_obj, current_node)
    except KeyError:
        pass

    bridge_prompt = gpt_bridge_response(transcript, current_node_obj, context)
    return {
        "type": "bridge",
        "prompt": bridge_prompt,
        "next_node": None,
        "decision": None,
    }


# ---------------------------------------------------------------------------
# Original simulate_pathway (kept intact)
# ---------------------------------------------------------------------------

def simulate_pathway(pathway_obj: dict, current_node: Optional[str], user_reply: str, flags: dict) -> dict:
    start_node = pathway_obj.get("start_node")
    cur = current_node or start_node

    if not isinstance(cur, str) or not cur:
        raise ValueError("Pathway start_node missing")

    decision = resolve_next_node(pathway_obj, cur, flags, user_reply=user_reply)
    extracted = extract_fields_from_text(decision.node, user_reply, flags)

    next_prompt = ""
    if decision.next_node:
        next_node = get_node(pathway_obj, decision.next_node)
        next_prompt = render_prompt(next_node.get("prompt", ""), extracted | flags)

    return {
        "current_node": decision.current_node,
        "current_prompt": decision.prompt,
        "fired_route": decision.fired_route,
        "next_node": decision.next_node,
        "next_prompt": next_prompt,
        "extracted": extracted,
        "flags": flags,
    }