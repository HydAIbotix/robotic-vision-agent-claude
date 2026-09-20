"""Auto-Repair v2 — failure-anchored retrieval, bug-class routing, and patch verification.

These are the generic pieces that fix the class of failure seen live on TC-RPS-003: retrieval anchored on
the test's TITLE (payment) instead of the FAILURE POINT (add-to-cart quantity 0), so the model was fed the
wrong code. Pure-function tests (no Chroma/Ollama needed)."""
from repair_agent.repair_failed_test import (
    _failure_point_text, _failure_point_query, _bug_class, _round_robin,
    _signal_tokens, _lexical_score, _patch_relevance, _hits_best_relevance, RepairPatch,
)

# The real (abbreviated) TC-RPS-003 failure: a PAYMENT test that actually dies at ADD-TO-CART.
FAILURE = (
    "TC-RPS-003 failed. [P1] Mock card payment with sufficient balance succeeds on RPS. "
    "EXPECTED BEHAVIOUR (design intent): A purchase paid with a card that has enough balance is approved "
    "and the balance is deducted. "
    "Steps attempted (in order): tap: Sign In @ (690,636) ; tap: Add to Cart (AudioPulse Pro Buds) @ (369,815) ; "
    "verify: Cart screen is shown after adding the item [FAILED HERE] "
    "OBSERVED: quantity_required_popup -> product_list -> products. "
    "Failing assertions: Wrong screen: expected cart, got products [dom]; Tapping Add to Cart for Nexora "
    "with quantity 0 triggered a Quantity Required popup instead of adding the product."
)

# A SPEC/value failure (cross-kiosk balance) — must route to the generous 'spec' profile (no regression).
SPEC_FAILURE = (
    "TC-VPS-009 failed. Purchase deducts the shared card balance across kiosks. "
    "EXPECTED BEHAVIOUR (design intent): a PURCHASE transaction is recorded and the balance is deducted. "
    "verify: the balance is reduced [FAILED HERE] "
    "OBSERVED: balance unchanged after purchase. "
    "Failing assertions: expected a PURCHASE transaction recorded; the shared card balance was not deducted."
)


def test_failure_point_text_targets_where_it_broke_not_the_title():
    fp = _failure_point_text(FAILURE).lower()
    # The diagnostic slice: the failed step + observed symptom + assertions.
    assert "cart" in fp and "quantity" in fp and "popup" in fp
    # It must NOT be dominated by the payment design-intent that misled retrieval.
    assert "sufficient balance is approved" not in fp


def test_failure_point_query_is_nonempty_and_stripped():
    q = _failure_point_query(FAILURE)
    assert q and "TC-RPS-003" not in q            # test id stripped
    assert "quantity" in q.lower()


def test_bug_class_routes_interaction_vs_spec():
    # The add-to-cart popup failure is an INTERACTION bug (code, not doc) even though its TITLE is payment.
    assert _bug_class(FAILURE) == "interaction"
    # A balance/transaction failure is SPEC — keeps the generous design-doc profile.
    assert _bug_class(SPEC_FAILURE) == "spec"


def test_verification_signals_come_from_failure_point_not_title():
    # Whole-failure signals leak payment vocab; failure-point signals do not — this is what makes the
    # off-target guard actually fire on the payment patch.
    fp_sig = _signal_tokens(_failure_point_text(FAILURE))
    assert "quantity" in fp_sig and "cart" in fp_sig
    assert "payment" not in fp_sig and "approved" not in fp_sig


def test_off_target_patch_scores_zero_on_target_scores_positive():
    fp_sig = _signal_tokens(_failure_point_text(FAILURE))
    wrong = RepairPatch(
        file_path="App.tsx",
        find="const approved = readerStatus?.state === 'approved' || payment.cardReaderStatus === 'MOCK_APPROVED';",
        replace="... || payment.mockApproved;",
        explanation="Ensure mock approved payments are recognized as approved.",
    )
    right = RepairPatch(
        file_path="App.tsx", find="onAddToCart(product, 0)", replace="onAddToCart(product, quantity)",
        explanation="Add to Cart passed a hardcoded 0 instead of the selected quantity.",
    )
    assert _patch_relevance(wrong, fp_sig) == 0     # off-target → guard fires
    assert _patch_relevance(right, fp_sig) >= 2      # on-target → trusted


def test_best_context_relevance_finds_the_add_to_cart_chunk():
    fp_sig = _signal_tokens(_failure_point_text(FAILURE))
    hits = [
        {"file": "App.tsx", "start_line": 2352, "snippet": "startCardReaderPayment onPaymentReady MOCK_APPROVED"},
        {"file": "App.tsx", "start_line": 1150, "snippet": "ProductCard quantity useState(0) Quantity Required popup onAddToCart(product,0)"},
    ]
    best, where = _hits_best_relevance(hits, fp_sig)
    assert best >= 3 and where == "App.tsx:1150"    # the add-to-cart chunk wins, not the payment chunk


def test_round_robin_interleaves_and_dedups():
    class D:
        def __init__(self, s, l):
            self.metadata = {"source": s, "start_line": l}
            self.page_content = f"{s}:{l}"
    a1, a2 = D("a.ts", 1), D("a.ts", 2)
    b1 = D("b.ts", 1)
    out = _round_robin([[a1, a2], [b1, a1]])          # a1 appears in both lanes
    keys = [(d.metadata["source"], d.metadata["start_line"]) for d in out]
    assert keys == [("a.ts", 1), ("b.ts", 1), ("a.ts", 2)]   # one-per-lane per round, deduped, order kept


def test_lexical_score_word_boundary_for_numbers():
    sig = {"1", "quantity"}
    assert _lexical_score("if (quantity <= 1) show popup", sig) == 2
    assert _lexical_score("const x = 100", {"1"}) == 0        # 1 must not match inside 100
