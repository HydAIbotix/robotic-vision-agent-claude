from typing_extensions import TypedDict


class RepairAgentState(TypedDict, total=False):
    """Shared state for the Auto-Repair LangGraph (retrieve → diagnose → apply → test → build → pr).

    Nodes are pure `state -> dict` partial updates, exactly like the other four agents. The progress
    callback is NOT stored here — callables stay out of graph state (see repair_agent/broadcaster.py,
    the same pattern as test_runner/broadcaster.py); `repair_id` keys the broadcaster instead.
    """
    # ── inputs ──
    repair_id: str        # keys the progress broadcaster (per-invocation)
    failure: str          # plain-English failed-test / defect description
    test_id: str          # e.g. "TC-RPS-001" (labels the branch/PR)
    apply: bool           # False → dry run (retrieve + diagnose only)
    auto_pr: bool         # push + open the PR after a green build
    branch_suffix: str    # per-run suffix so fix branches never collide

    # ── accumulated results ──
    context: str          # retrieved code context rendered for the LLM prompt
    hits: list            # structured retrieval hits (file/type/lines/snippet)
    patch: dict           # {file_path, find, replace, explanation}
    target: str           # absolute path of the file the patch was applied to
    blocked: str          # repo-state block reason (mid-merge/rebase/conflicts), else ""
    build_ok: bool        # whether `npm run build` passed (gates auto-PR)
    stages: dict          # stage-key → result dict (mirrors the streamed stage updates)
    success: bool         # overall success == build passed
    error: str            # terminal error message, if any
