"""
llm_client.py
-------------
This is the one place in the whole project that actually talks to Gemini.
Everything else (guardrails, storage, the dashboard) wraps AROUND this
function - that's intentional, since it means swapping the model or the
provider later only touches this one file.

The retry-with-backoff logic here is copied from a lesson learned the
hard way on a previous project: Gemini's free tier allows only a handful
of requests per minute, and a single 429 shouldn't crash the whole app -
it should just wait a bit and try again.

Rough, approximate per-million-token pricing is used only to show a
COST ESTIMATE on the dashboard - it is deliberately not treated as exact
billing, since provider pricing changes. Treat the cost numbers here as
"good enough to compare calls against each other", not as an invoice.
"""

import os
import time
from google import genai
from google.genai import types
from google.genai.errors import ClientError

from core import guardrails, storage

MAX_RETRIES = 5
BASE_DELAY_SECONDS = 15
DEFAULT_MODEL = "gemini-3.5-flash-lite"

# Approximate, for demo cost-tracking only - not live pricing.
PRICE_PER_MILLION_INPUT_TOKENS = 0.10
PRICE_PER_MILLION_OUTPUT_TOKENS = 0.40


def _get_client(api_key: str) -> genai.Client:
    """
    A fresh client per call rather than a cached global one - this project
    supports a "bring your own key" mode (see app.py), so the key in use
    can change from one call to the next depending on who's using the demo.
    """
    return genai.Client(api_key=api_key)


def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    input_cost = (prompt_tokens / 1_000_000) * PRICE_PER_MILLION_INPUT_TOKENS
    output_cost = (completion_tokens / 1_000_000) * PRICE_PER_MILLION_OUTPUT_TOKENS
    return round(input_cost + output_cost, 6)


def _call_gemini(prompt: str, api_key: str, model: str = DEFAULT_MODEL):
    """Raw call to Gemini with retry-on-rate-limit. No guardrails here on
    purpose - this function's only job is talking to the model."""
    client = _get_client(api_key)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
            )
            return response
        except ClientError as exc:
            is_rate_limit = "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc)
            if is_rate_limit and attempt < MAX_RETRIES:
                wait_time = BASE_DELAY_SECONDS * attempt
                time.sleep(wait_time)
                continue
            raise


def protected_generate(prompt: str, api_key: str, model: str = DEFAULT_MODEL,
                        used_own_key: bool = False) -> dict:
    """
    The main entry point everything else in the app should call. Wraps a
    raw Gemini call with the full guardrail pipeline:

      1. Check the INPUT for prompt-injection attempts before it's sent.
         If found, the call is blocked entirely - we never send a known
         injection attempt to the model.
      2. Call the model.
      3. Check the OUTPUT for PII and redact it if found.
      4. Log everything (even blocked calls) to SQLite for the dashboard.

    Returns a dict the UI layer can render directly - it never needs to
    know about SQLite, Gemini's SDK, or the regex patterns underneath.
    """
    start = time.time()

    # --- Step 1: input guardrail (runs before we spend any tokens) ---
    input_check = guardrails.check_input(prompt)
    if input_check["injection_detected"]:
        storage.log_call(
            prompt=prompt, response=None, model=model, latency_s=0,
            prompt_tokens=0, completion_tokens=0, total_tokens=0,
            estimated_cost_usd=0, injection_detected=True, blocked=True,
            block_reason=f"Matched injection pattern: {input_check['matched_pattern']}",
            used_own_key=used_own_key,
        )
        return {
            "blocked": True,
            "response": None,
            "block_reason": "This prompt was blocked by the input guardrail "
                             "(looked like a prompt-injection attempt).",
            "injection_detected": True,
            "pii_detected": False,
            "latency_s": round(time.time() - start, 3),
        }

    # --- Step 2: the actual model call ---
    response = _call_gemini(prompt, api_key=api_key, model=model)
    latency = time.time() - start

    usage = response.usage_metadata
    prompt_tokens = usage.prompt_token_count if usage else 0
    completion_tokens = usage.candidates_token_count if usage else 0
    total_tokens = prompt_tokens + completion_tokens
    cost = _estimate_cost(prompt_tokens, completion_tokens)

    # --- Step 3: output guardrail ---
    output_check = guardrails.check_output(response.text or "")
    shape_check = guardrails.validate_output_shape(response.text or "")

    # --- Step 4: log the call regardless of outcome ---
    storage.log_call(
        prompt=prompt,
        response=output_check["redacted_text"],
        model=model,
        latency_s=round(latency, 3),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=cost,
        injection_detected=False,
        pii_detected=output_check["pii_detected"],
        blocked=False,
        used_own_key=used_own_key,
    )

    return {
        "blocked": False,
        "response": output_check["redacted_text"],
        "block_reason": None,
        "injection_detected": False,
        "pii_detected": output_check["pii_detected"],
        "pii_types_found": output_check["types_found"],
        "output_valid": shape_check["valid"],
        "latency_s": round(latency, 3),
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "estimated_cost_usd": cost,
        "model": model,
    }
