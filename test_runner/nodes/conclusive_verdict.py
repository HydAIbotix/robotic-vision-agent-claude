"""
conclusive_verdict — post-execution verdict node.

Runs AFTER all test steps complete.  Uses Claude to reason about test
INTENT vs the actual execution path, not just whether each step passed.

Key distinction:
  - Individual step failures may be navigational accidents, not functional bugs.
  - The test intent (primary objective) may have been achieved via an alternative path.
  - Some outcomes are genuinely ambiguous and require a human judgement call.

Return values (added to the last TestResult in state["test_results"]):
    conclusive_verdict: {
        "primary_objective": str,          # what the test was verifying
        "objective_achieved": bool | None, # True/False/None (ambiguous)
        "failure_type": "A"|"B"|"C"|"D"|None,
        "verdict": "PASS" | "FAIL" | "AMBIGUOUS",
        "confidence": float,               # 0.0–1.0
        "reasoning": str,                  # two sentences
        "requires_human_confirmation": bool,
        "human_prompt": str | None,        # question for operator if ambiguous
        "suggested_action": str,           # next step recommendation
    }

Failure types:
  A — Critical: the tested feature does not work
  B — Navigation: couldn't reach the feature due to an earlier step
  C — Validation gap: reached the right state but couldn't confirm it
  D — Environment: external factors (network, device, timing)
"""
import json
from langchain_core.messages import HumanMessage
from vision_agent.llm import get_fast_llm
from test_runner.state import TestRunnerState
from test_runner import broadcaster

CONCLUSIVE_VERDICT_PROMPT = """\
You are determining the final PASS/FAIL verdict for an automated test case.
Your role is to reason about TEST INTENT, not just literal step execution.
A test can have failed steps but still achieve its objective (alternative path).
A test can have passed steps but still fail its objective (wrong screen, wrong data).

═══ TEST INTENT ═══════════════════════════════════════════════════════
Test ID: {test_id}
Summary: {summary}
Expected outcome stated by the test author:
{expected_results}

═══ EXECUTION SUMMARY ════════════════════════════════════════════════
Total steps: {total_steps}
Steps passed: {passed_steps}
Steps failed: {failed_steps_count}
Verification gaps (unknown screens): {gap_steps}

Step-by-step log:
{step_log}

Failed steps detail:
{failed_steps_detail}

═══ REASONING FRAMEWORK ══════════════════════════════════════════════
Step 1 — Identify primary objective:
  What is the ONE thing this test is designed to verify?
  (not a step, but the outcome — e.g. "card payment completes successfully",
  "order appears in history", "error shown on invalid login")

Step 2 — Assess objective achievement:
  Was the primary objective met?
  Consider: alternative paths (different steps, same outcome),
            partial success (some objectives met, others not),
            side effects (something else broke while main flow worked),
            verification gaps (screen not in app_map — could still be correct).

Step 3 — Classify the failure type if objective was NOT met:
  A. Critical failure — the feature being tested does not work
  B. Navigation failure — the test could not reach the feature due to an earlier step
  C. Validation gap — reached the right state but validation logic could not confirm it
  D. Environment issue — external factors (network, device, timing) caused the failure

Step 4 — Human confirmation check:
  Is the verdict genuinely ambiguous? Does it require domain knowledge or
  business judgment that cannot be derived from the execution log alone?
  Verification gaps (type C) are common candidates for human review.

═══ TRUST THE STEP-LEVEL VALIDATION ══════════════════════════════════
Each "verify" step was ALREADY validated by the execution pipeline: it compared the ACTUAL
on-screen value against the expected value (numbers are compared ignoring currency symbols and
thousands separators). So a verify step marked passed (✓) means the expected value WAS present on
screen — you do NOT need to, and CANNOT, re-derive it from the log. Do NOT invent doubt about
arithmetic, totals, or data the app computed and a passed verify step already confirmed.

DEFAULT-TO-PASS RULE: if EVERY step passed (✓) with zero verification gaps and zero failed steps,
the objective was achieved — return verdict "PASS", objective_achieved true,
requires_human_confirmation false, suggested_action "none". Reserve "AMBIGUOUS" strictly for runs
that have genuine verification gaps (?) or failed steps whose effect on the objective is unclear;
never for a fully clean run.

Return ONLY valid JSON — no markdown fences:
{{
  "primary_objective": "<one sentence>",
  "objective_achieved": true | false | null,
  "failure_type": "A" | "B" | "C" | "D" | null,
  "verdict": "PASS" | "FAIL" | "AMBIGUOUS",
  "confidence": 0.0,
  "reasoning": "<two sentences: evidence for the verdict>",
  "requires_human_confirmation": true | false,
  "human_prompt": "<question for human operator, or null>",
  "suggested_action": "<re-explore app | fix test case | file defect | none>"
}}
"""


def conclusive_verdict(state: TestRunnerState) -> dict:
    """
    Evaluate the last completed test case and attach a conclusive verdict.

    Only called after run_vision_step / run_backend_step finishes.
    Mutates the last item in state["test_results"] in-place by adding
    a "conclusive_verdict" key.
    """
    results = list(state.get("test_results") or [])
    if not results:
        return {}

    last = results[-1]
    tc = state.get("current_tc") or {}
    run_id = state.get("run_id", "")

    step_results = last.get("step_results") or []
    total_steps = len(step_results)
    passed_steps = sum(1 for s in step_results if s.get("success"))
    failed_steps = [s for s in step_results if not s.get("success")]
    gap_steps = sum(1 for s in step_results if s.get("verification_gap"))

    # Build compact step log for the prompt
    log_lines = []
    for idx, s in enumerate(step_results, 1):
        icon = "✓" if s.get("success") else ("?" if s.get("verification_gap") else "✗")
        obs = s.get("observation") or s.get("note") or ""
        log_lines.append(f"  {icon} Step {idx}: {s.get('step', '')[:80]}  {obs[:60]}")
    step_log = "\n".join(log_lines) or "  (no steps)"

    failed_detail_lines = []
    for s in failed_steps[:5]:
        failed_detail_lines.append(
            f"  • {s.get('step', '')[:80]}\n"
            f"    Observation: {s.get('observation') or s.get('error') or '(none)'}"
        )
    failed_steps_detail = "\n".join(failed_detail_lines) or "  (none)"

    prompt = CONCLUSIVE_VERDICT_PROMPT.format(
        test_id=last.get("test_id", tc.get("test_id", "")),
        summary=last.get("summary", tc.get("summary", "")),
        expected_results=tc.get("expected_results_raw", "(not specified)"),
        total_steps=total_steps,
        passed_steps=passed_steps,
        failed_steps_count=len(failed_steps),
        gap_steps=gap_steps,
        step_log=step_log,
        failed_steps_detail=failed_steps_detail,
    )

    cv: dict = {}
    try:
        llm = get_fast_llm()
        raw = llm.invoke([HumanMessage(content=prompt)]).content.strip()
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()
        cv = json.loads(raw)
    except Exception as e:
        print(f"  [VERDICT] LLM error — using step-based verdict: {e}")
        # Fallback: derive verdict purely from step outcomes
        if last.get("outcome") == "passed" and gap_steps == 0:
            cv = {
                "primary_objective": last.get("summary", ""),
                "objective_achieved": True,
                "failure_type": None,
                "verdict": "PASS",
                "confidence": 1.0,
                "reasoning": "All steps passed.",
                "requires_human_confirmation": False,
                "human_prompt": None,
                "suggested_action": "none",
            }
        elif gap_steps > 0:
            cv = {
                "primary_objective": last.get("summary", ""),
                "objective_achieved": None,
                "failure_type": "C",
                "verdict": "AMBIGUOUS",
                "confidence": 0.4,
                "reasoning": f"{gap_steps} verify step(s) could not be confirmed — screen not in app_map.",
                "requires_human_confirmation": True,
                "human_prompt": "Were the expected screens actually shown during this test?",
                "suggested_action": "re-explore app",
            }
        else:
            cv = {
                "primary_objective": last.get("summary", ""),
                "objective_achieved": False,
                "failure_type": "A",
                "verdict": "FAIL",
                "confidence": 0.9,
                "reasoning": f"{len(failed_steps)} step(s) failed.",
                "requires_human_confirmation": False,
                "human_prompt": None,
                "suggested_action": "file defect",
            }

    # Deterministic guard against FABRICATED ambiguity on a clean run. When every step passed with
    # zero verification gaps and zero failures, the step-level validation pipeline already confirmed
    # each expected value on screen (a passed "verify" step compares on-screen text to the expected
    # value, tolerant of currency/thousands formatting). The verdict LLM must not invent doubt
    # (e.g. "couldn't confirm the arithmetic") and demand human review on such a run — observed live,
    # where a full 33-step PASS with the correct balance still came back AMBIGUOUS. A legitimate
    # step-level human_review (a value that matched only after ignoring number formatting) is
    # preserved and re-surfaced; a fabricated one is dropped.
    step_human_review = any(s.get("human_review") for s in step_results)
    clean_run = (
        total_steps > 0
        and not failed_steps
        and gap_steps == 0
        and last.get("outcome") == "passed"
    )
    if clean_run and cv.get("verdict") != "PASS":
        print(f"  [VERDICT] Clean run (all {total_steps} steps passed, 0 gaps, 0 failures) — "
              f"overriding fabricated {cv.get('verdict')} → PASS")
        cv["verdict"] = "PASS"
        cv["objective_achieved"] = True
        cv["failure_type"] = None
        cv["confidence"] = max(float(cv.get("confidence") or 0.0), 0.9)
        cv["requires_human_confirmation"] = bool(step_human_review)
        cv["human_prompt"] = (
            "A value matched only after ignoring number formatting (e.g. 5000 vs 5,000) — "
            "confirm the formatting difference is acceptable."
            if step_human_review else None
        )
        if cv.get("suggested_action") in (None, "", "fix test case", "re-explore app", "file defect"):
            cv["suggested_action"] = "none"

    # The conclusive verdict is authoritative — reconcile the reported outcome with it in BOTH
    # directions so the suite's pass/fail count can never contradict the verdict.
    final_verdict = cv.get("verdict", "FAIL")
    if final_verdict == "PASS" and last.get("outcome") != "passed":
        print(f"  [VERDICT] Upgrading outcome: step-level FAIL → conclusive PASS (intent achieved)")
        last = {**last, "outcome": "passed"}
    elif final_verdict == "FAIL" and last.get("outcome") != "failed":
        print(f"  [VERDICT] Downgrading outcome: step-level PASS → conclusive FAIL (objective not met)")
        last = {**last, "outcome": "failed"}
    elif final_verdict == "AMBIGUOUS":
        print(f"  [VERDICT] AMBIGUOUS — broadcasting human_review_required")
        if run_id:
            broadcaster.emit(run_id, {
                "event":        "human_review_required",
                "run_id":       run_id,
                "test_id":      last.get("test_id"),
                "verdict":      "AMBIGUOUS",
                "human_prompt": cv.get("human_prompt"),
                "reasoning":    cv.get("reasoning"),
            })

    icon = "PASS" if final_verdict == "PASS" else ("?" if final_verdict == "AMBIGUOUS" else "FAIL")
    print(f"  [VERDICT] {icon}  {cv.get('reasoning', '')}")

    updated_last = {**last, "conclusive_verdict": cv}
    return {"test_results": [*results[:-1], updated_last]}
