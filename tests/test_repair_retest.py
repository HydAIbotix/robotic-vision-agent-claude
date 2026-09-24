"""Auto-Repair fix-verification retest gating (fix → retest → PR).

These lock the no-regression contract of the 2026-09-24 retest feature WITHOUT needing a DB or a live
run: the retest is only attempted when the fixer produced a green build AND a prepared (not-yet-opened)
PR, and the internal RunRequest flag that suppresses recursion exists and defaults off.
"""
from api import main
from api.main import RunRequest
from vision_agent.config import settings


def test_retest_before_pr_defaults_on():
    # The user-requested behaviour (verify the fix before raising the PR) is the default.
    assert settings.repair_retest_before_pr is True


def test_retest_run_request_flag_default_off():
    # A normal run never carries the internal retest flag → auto-repair/defect fire as before.
    assert RunRequest().is_repair_retest is False
    assert RunRequest(is_repair_retest=True).is_repair_retest is True


def test_retest_is_noop_without_green_build(monkeypatch):
    # If the fixer did not produce a green build, no verification run is minted (guard returns first,
    # before any DB / broadcast). We trip a sentinel on _next_run_number to prove it is never reached.
    reached = {"minted": False}
    monkeypatch.setattr(main, "_next_run_number", lambda: reached.__setitem__("minted", True) or 1)

    main._retest_and_maybe_open_pr(
        "repair-x", "run-1", "TC-RPS-001",
        {"stages": {"build": {"ok": False}, "pr": {"prepared": True}}}, None, "K-01",
    )
    assert reached["minted"] is False


def test_retest_is_noop_without_prepared_pr(monkeypatch):
    reached = {"minted": False}
    monkeypatch.setattr(main, "_next_run_number", lambda: reached.__setitem__("minted", True) or 1)

    main._retest_and_maybe_open_pr(
        "repair-x", "run-1", "TC-RPS-001",
        {"stages": {"build": {"ok": True}, "pr": {"prepared": False}}}, None, "K-01",
    )
    assert reached["minted"] is False


def test_retest_is_noop_without_test_id(monkeypatch):
    reached = {"minted": False}
    monkeypatch.setattr(main, "_next_run_number", lambda: reached.__setitem__("minted", True) or 1)

    main._retest_and_maybe_open_pr(
        "repair-x", "run-1", "",
        {"stages": {"build": {"ok": True}, "pr": {"prepared": True}}}, None, "K-01",
    )
    assert reached["minted"] is False


# ── App rebuild helper ─────────────────────────────────────────────────────────

def test_rebuild_app_noop_when_no_command(monkeypatch):
    # With no configured command (the local dev-server posture) nothing runs; treated as OK so the
    # retest proceeds against the live dev server. rebuild_app() falls back to settings.repair_rebuild_cmd,
    # so empty that out to exercise the no-op path.
    monkeypatch.setattr(settings, "repair_rebuild_cmd", "")
    from repair_agent.repair_failed_test import rebuild_app
    out = rebuild_app()
    assert out["ran"] is False and out["ok"] is True


def test_rebuild_cmd_defaults_to_vm_compose_and_restore_on():
    # Default targets the GCP VM (POS is a built nginx image) so the rebuild happens with no env set;
    # restore-on returns the app to baseline after the retest. Local dev sets REPAIR_REBUILD_CMD="".
    assert settings.repair_rebuild_cmd == "docker compose up -d --build pos"
    assert settings.repair_rebuild_restore is True


def test_wait_for_app_ready_trivial():
    # No URL / no timeout → ready immediately (never blocks the retest when there's nothing to wait for).
    assert main._wait_for_app_ready("", 0) is True
    assert main._wait_for_app_ready("http://x", 0) is True


# ── Per-failure batch loop (one repair + one PR per failed test) ────────────────

def _stub_batch(monkeypatch):
    """Stub _run_one_repair + git helpers so the batch loop is exercised without a real repair/git."""
    import repair_agent.repair_failed_test as rf
    monkeypatch.setattr(rf, "current_branch", lambda: "baseline")
    checkouts = []
    monkeypatch.setattr(rf, "checkout_branch", lambda b: checkouts.append(b) or {"ok": True})
    return checkouts


def test_batch_runs_one_repair_per_failure(monkeypatch):
    calls = []

    def fake_one(run_id, kiosk_id, tr, cred, tenant, batch_index=0, batch_total=1):
        calls.append((tr["test_id"], batch_index, batch_total))
        return {}

    monkeypatch.setattr(main, "_run_one_repair", fake_one)
    checkouts = _stub_batch(monkeypatch)

    failed = [{"test_id": "T1"}, {"test_id": "T2"}, {"test_id": "T3"}]
    main._run_auto_repair("run-x", "K-01", failed, credentials=None, tenant_id="")

    assert [c[0] for c in calls] == ["T1", "T2", "T3"]      # one repair per failure, in order
    assert [c[1] for c in calls] == [0, 1, 2]               # correct batch_index
    assert all(c[2] == 3 for c in calls)                    # correct batch_total
    assert checkouts == ["baseline", "baseline"]            # re-based to baseline before repairs 2 & 3


def test_batch_stops_on_cancel(monkeypatch):
    calls = []

    def fake_one(run_id, kiosk_id, tr, cred, tenant, batch_index=0, batch_total=1):
        calls.append(tr["test_id"])
        return {"cancelled": True} if tr["test_id"] == "T2" else {}

    monkeypatch.setattr(main, "_run_one_repair", fake_one)
    _stub_batch(monkeypatch)

    failed = [{"test_id": "T1"}, {"test_id": "T2"}, {"test_id": "T3"}]
    main._run_auto_repair("run-x", "K-01", failed, credentials=None, tenant_id="")

    assert calls == ["T1", "T2"]   # T2 cancelled → T3 never attempted
