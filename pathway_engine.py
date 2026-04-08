from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


JsonDict = Dict[str, Any]


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