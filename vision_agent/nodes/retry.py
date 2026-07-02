"""
handle_retry node — increments the retry counter, logs recovery context, and
CLEARS the current plan so the agent re-analyzes and RE-PLANS from the current
screen instead of blindly re-executing the same failed step.

Why re-plan rather than re-try the identical step:
  A step fails validation when the action didn't achieve its expected outcome —
  e.g. tapping "Proceed to Checkout" on an EMPTY cart leaves you on the products
  page.  Re-tapping the same button will fail again forever (the old behaviour
  tapped it up to max_retries times).  By clearing planned_steps, the next
  plan_steps call re-reads the screen and lets Claude reason about WHY the step
  failed and what precondition is missing (add an item first, fill a field, etc.).

  If the screen genuinely hasn't changed (a transient timing failure), the
  re-plan naturally produces the same first step and simply retries it — so this
  is a strict improvement over the previous fixed-plan retry.
"""
from vision_agent.state import VisionAgentState


def handle_retry(state: VisionAgentState) -> dict:
    retry_count = (state.get("retry_count") or 0) + 1
    last = (state.get("step_results") or [{}])[-1]
    steps = state.get("planned_steps") or []
    idx   = state.get("current_step_idx", 0)
    step  = steps[idx] if idx < len(steps) else "(step unavailable)"

    print(f"\n  [RETRY {retry_count}] '{step}'  — re-planning from current screen")
    if last.get("error"):
        print(f"    Reason: {last['error']}")
    if last.get("observation"):
        print(f"    Last observation: {last['observation']}")

    # Clear the plan → plan_steps will regenerate from the freshly-analyzed screen.
    return {"retry_count": retry_count, "planned_steps": [], "current_step_idx": 0}
