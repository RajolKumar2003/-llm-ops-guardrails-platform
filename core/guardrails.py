"""
guardrails.py
-------------
The safety checks that sit BEFORE and AFTER every LLM call. Everything
here runs as plain pattern matching, not another LLM call - that's a
deliberate choice, not a shortcut. Real guardrail layers use fast, cheap,
deterministic checks first (regex, keyword lists, schema checks) and only
reach for a second LLM call when those aren't enough. It also means this
whole module runs instantly and free, which matters for a public demo
sitting on a free API quota.

Two checks live here:
  1. check_input()  - runs BEFORE the LLM sees the prompt. Looks for
     prompt-injection patterns (someone trying to override the system's
     instructions).
  2. check_output()  - runs AFTER the LLM responds. Looks for PII the
     model may have echoed back (emails, phone numbers, card-like numbers)
     and redacts it before it reaches the user.
"""

import re

# --- Prompt injection detection ---------------------------------------
# This is a pattern list, not a exhaustive solution - a determined
# attacker can phrase around any fixed list. In production you'd combine
# this with a small classifier model. For a demo/portfolio project,
# a curated pattern list is the honest, explainable version of this
# check, and it's exactly the kind of first-line-of-defence a real
# guardrail stack actually uses before anything heavier.
INJECTION_PATTERNS = [
    r"ignore (all )?(the )?(previous|prior|above) instructions",
    r"disregard (all )?(the )?(previous|prior|above) instructions",
    r"forget (all )?(the )?(previous|prior|above) instructions",
    r"you are now (in )?(developer|dan|jailbreak|unrestricted) mode",
    r"reveal (your|the) (system prompt|instructions)",
    r"what (is|are) your (system prompt|instructions)",
    r"repeat (the words|everything) (above|before this)",
    r"act as if you have no (restrictions|rules|guidelines)",
    r"pretend (you have no|there are no) (rules|restrictions|filters)",
    r"bypass (your|all) (safety|content) (filters|guidelines)",
]
_INJECTION_REGEX = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)


def check_input(prompt: str) -> dict:
    """
    Scans a prompt for injection attempts BEFORE it reaches the model.
    Returns whether it was flagged and which pattern matched, so the
    caller can decide whether to block the call entirely or just log it.
    """
    match = _INJECTION_REGEX.search(prompt)
    return {
        "injection_detected": bool(match),
        "matched_pattern": match.group(0) if match else None,
    }


# --- PII detection & redaction -----------------------------------------
# Order matters here: check card-like numbers before phone numbers, since
# a 16-digit card number would otherwise get partially matched by a looser
# phone pattern first.
PII_PATTERNS = {
    "EMAIL": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "CARD_NUMBER": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "PHONE": re.compile(r"\b(\+?\d{1,3}[-.\s]?)?\(?\d{3,5}\)?[-.\s]?\d{3,4}[-.\s]?\d{3,4}\b"),
}


def check_output(text: str) -> dict:
    """
    Scans model OUTPUT for PII patterns and returns a redacted version.
    We redact rather than block here - blocking a whole response over one
    leaked phone number throws away an otherwise-useful answer, whereas
    redaction keeps the response usable while still protecting the data.
    """
    redacted_text = text
    found_types = []

    for label, pattern in PII_PATTERNS.items():
        if pattern.search(redacted_text):
            found_types.append(label)
            redacted_text = pattern.sub(f"[REDACTED_{label}]", redacted_text)

    return {
        "pii_detected": len(found_types) > 0,
        "types_found": found_types,
        "redacted_text": redacted_text,
    }


def validate_output_shape(text: str, min_length: int = 1) -> dict:
    """
    A minimal schema/sanity check on the output itself - not every
    guardrail is about detecting something malicious, some are just
    about catching a broken or empty response before it reaches a user.
    """
    is_valid = bool(text) and len(text.strip()) >= min_length
    return {
        "valid": is_valid,
        "reason": None if is_valid else "Response was empty or too short.",
    }
