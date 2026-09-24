"""Option C tuning: judge-confidence threshold + the unresponsive-interaction fallback.

Pure-function tests for the two levers behind a wrong-screen verify. NOTE (2026-09-24): the vision JUDGE is
now AUTHORITATIVE and runs FIRST when enabled (it can SEE whether the action took effect — e.g. a cart badge
incremented — so a healthy "added, now click through to the cart" screen bridges instead of failing). The
`_last_interaction_screen` "same-screen = blocked control" heuristic is now the NO-LLM FALLBACK, used only
when the judge is off (air-gapped path); these tests still cover the pure helpers it relies on."""
from test_runner.nodes.run_vision_step import _conf_at_least, _last_interaction_screen


def test_conf_threshold_only_bridges_sufficiently_confident_gap():
    # Default threshold 'high': only a HIGH-confidence gap bridges; medium/low gap fails fast.
    assert _conf_at_least("high", "high") is True
    assert _conf_at_least("medium", "high") is False
    assert _conf_at_least("low", "high") is False
    # Relaxing the threshold to 'medium' lets a medium gap bridge (closer to always-bridge).
    assert _conf_at_least("medium", "medium") is True
    assert _conf_at_least("high", "medium") is True
    assert _conf_at_least("low", "medium") is False
    # Unknown confidence is treated as the lowest → never bridges under a high bar.
    assert _conf_at_least("", "high") is False
    assert _conf_at_least("bogus", "high") is False


def test_unresponsive_interaction_detects_a_control_that_did_not_advance():
    # (No-LLM fallback path.) The plant: tap add-to-cart on 'products', then the cart verify lands back on
    # 'products' → the interaction's screen is reported so the fallback caller (judge OFF) can fail fast.
    # With the judge ON (default), the judge decides instead — a healthy "Cart/Checkout (1)" screen bridges.
    steps = [
        {"step": "tap: Sign In @ (690,636)", "screen_id": "login"},
        {"step": "tap: audiopulse_increment @ (517,739)", "screen_id": "products"},
        {"step": "tap: audiopulse_add_to_cart @ (369,797)", "screen_id": "products"},
    ]
    assert _last_interaction_screen(steps) == "products"      # caller compares == actual_screen ('products') → defect

    # A genuine nav gap: the last interaction advanced OFF its screen (verify lands elsewhere) → the
    # interaction screen ('login') != the actual wrong screen, so the rule does NOT flag it (bridge as before).
    nav_gap = [{"step": "tap: Sign In @ (690,636)", "screen_id": "login"}]
    assert _last_interaction_screen(nav_gap) == "login"        # caller: 'login' != 'products' → not a defect → bridge

    # Conservative: only structured taps/types with a known screen_id count.
    assert _last_interaction_screen([{"step": "verify: cart shown", "actual_screen": "products"}]) == ""
    assert _last_interaction_screen([{"step": "tap: Use Mock Card @ (1,2)"}]) == ""   # vision tap, no screen_id
