from __future__ import annotations

import re
from typing import Any, Dict


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