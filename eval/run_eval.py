"""
run_eval.py
-----------
Two evaluation suites live here, and they are deliberately different in
character:

  1. GUARDRAIL suites (red-team + PII) run entirely offline against the
     regex logic in core/guardrails.py. No API calls, no cost, no rate
     limit risk - you can run these a thousand times a day for free.
     This is what produces the "blocked X% of adversarial prompts" and
     "PII detection accuracy" numbers.

  2. QUALITY suite makes real calls to Gemini and checks whether the
     answer contains the expected fact. This is the "LLM-as-judge"-style
     regression check - simplified here to a substring match rather than
     a second LLM call, which keeps it fast, free, and deterministic
     (a judge-model approach is the natural next step, noted in the
     README's future work section).

Run with: python -m eval.run_eval
"""

import json
import os
import sys
import statistics
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import guardrails, storage, llm_client

EVAL_DIR = Path(__file__).parent


def run_red_team_suite():
    """Every one of these checks is free - pure regex, no API call."""
    prompts = json.loads((EVAL_DIR / "red_team_prompts.json").read_text())
    correct = 0

    print("\n--- RED TEAM / INJECTION DETECTION SUITE ---")
    for item in prompts:
        result = guardrails.check_input(item["prompt"])
        detected = result["injection_detected"]
        was_correct = detected == item["should_block"]
        correct += was_correct

        status = "PASS" if was_correct else "FAIL"
        print(f"[{status}] Q{item['id']}: expected_block={item['should_block']}, "
              f"detected={detected}")

        storage.log_eval_result(
            suite="red_team",
            question=item["prompt"],
            passed=was_correct,
            score=1.0 if was_correct else 0.0,
            notes=f"matched_pattern={result['matched_pattern']}",
        )

    accuracy = correct / len(prompts)
    print(f"Red team accuracy: {accuracy*100:.1f}% ({correct}/{len(prompts)})")
    return accuracy


def run_pii_suite():
    """Also free - pure regex against core/guardrails.py's PII patterns."""
    cases = json.loads((EVAL_DIR / "pii_test_cases.json").read_text())
    correct = 0

    print("\n--- PII DETECTION SUITE ---")
    for item in cases:
        result = guardrails.check_output(item["text"])
        detected = result["pii_detected"]
        was_correct = detected == item["should_flag"]
        correct += was_correct

        status = "PASS" if was_correct else "FAIL"
        print(f"[{status}] Case {item['id']}: expected_flag={item['should_flag']}, "
              f"detected={detected}")

        storage.log_eval_result(
            suite="pii_detection",
            question=item["text"],
            passed=was_correct,
            score=1.0 if was_correct else 0.0,
            notes=f"types_found={result['types_found']}",
        )

    accuracy = correct / len(cases)
    print(f"PII detection accuracy: {accuracy*100:.1f}% ({correct}/{len(cases)})")
    return accuracy


def run_quality_suite(api_key):
    """This one DOES call the real API - keep this suite short (5
    questions) since it costs real quota, unlike the two suites above."""
    prompts = json.loads((EVAL_DIR / "quality_prompts.json").read_text())
    correct = 0

    print("\n--- QUALITY REGRESSION SUITE (calls the live API) ---")
    for item in prompts:
        result = llm_client.protected_generate(item["prompt"], api_key=api_key)
        response_text = (result.get("response") or "").lower()
        passed = any(exp.lower() in response_text for exp in item["expected_contains"])
        correct += passed

        status = "PASS" if passed else "FAIL"
        print(f"[{status}] Q{item['id']}: {item['prompt']}")

        storage.log_eval_result(
            suite="quality_regression",
            question=item["prompt"],
            passed=passed,
            score=1.0 if passed else 0.0,
            notes=f"response_snippet={response_text[:80]}",
        )

    accuracy = correct / len(prompts)
    print(f"Quality regression accuracy: {accuracy*100:.1f}% ({correct}/{len(prompts)})")
    return accuracy


if __name__ == "__main__":
    storage.init_db()

    red_team_acc = run_red_team_suite()
    pii_acc = run_pii_suite()

    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        quality_acc = run_quality_suite(api_key)
    else:
        print("\n(Skipping quality suite - no GEMINI_API_KEY found in .env)")
        quality_acc = None

    print("\n===== EVAL SUMMARY =====")
    print(f"Red team / injection detection accuracy: {red_team_acc*100:.1f}%")
    print(f"PII detection accuracy: {pii_acc*100:.1f}%")
    if quality_acc is not None:
        print(f"Quality regression accuracy: {quality_acc*100:.1f}%")
    print("=========================")
