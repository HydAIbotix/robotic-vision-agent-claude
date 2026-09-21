"""Auto-Repair v2 — failure-anchored retrieval, bug-class routing, and patch verification.

These are the generic pieces that fix the class of failure seen live on TC-RPS-003: retrieval anchored on
the test's TITLE (payment) instead of the FAILURE POINT (add-to-cart quantity 0), so the model was fed the
wrong code. Pure-function tests (no Chroma/Ollama needed)."""
from repair_agent.repair_failed_test import (
    _failure_point_text, _failure_point_query, _bug_class, _round_robin,
    _signal_tokens, _lexical_score, _patch_relevance, _hits_best_relevance, RepairPatch,
    _subtokens, _rca_should_stop, _resilient_replace,
)
from vision_agent.config import settings

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


# ── generic lexical fix: sub-token matching (no substring false positives) ───────────────────────

def test_subtokens_splits_camel_snake_and_digits():
    assert _subtokens("onAddToCart") == {"on", "add", "to", "cart"}
    assert _subtokens("quantity_required_popup") == {"quantity", "required", "popup"}
    assert _subtokens("MOCK_APPROVED") == {"mock", "approved"}
    assert _subtokens("quantity <= 1") == {"quantity", "1"}


def test_lexical_score_no_substring_false_positive_generic():
    # The live TC-RPS-003 defeat: 'action' (a failure signal from 'checkout action') scored the OFF-TARGET
    # balance patch as relevant because it is a SUBSTRING of 'transaction'. Sub-token matching kills that
    # generically, while STILL matching a compound identifier — no per-test special-casing.
    sig = {"action", "cart", "quantity", "popup"}
    assert _lexical_score("recordCardTransaction balanceAfter issuedSmartCard", sig) == 0   # 'action' ⊄ 'transaction'
    assert _lexical_score("onAddToCart(product, quantity)", sig) == 2                       # cart + quantity
    assert _lexical_score("show the Quantity Required popup", sig) == 2                     # quantity + popup


def test_off_target_balance_patch_now_scores_zero_on_the_real_failure():
    # End-to-end on the real failure point: the payment/balance patch the model actually produced must now
    # score 0 (it did NOT before the fix — 'action' ⊂ 'transaction' gave it 1, defeating the guard).
    fp_sig = _signal_tokens(_failure_point_text(FAILURE))
    balance_patch = RepairPatch(
        file_path="App.tsx",
        find="if (balanceAfter !== undefined && !issuedSmartCard) {",
        replace="if (balanceAfter !== undefined && issuedSmartCard) {",
        explanation="The condition incorrectly checks for the absence of an issued smart card; deduct the balance and log the transaction.",
    )
    assert _patch_relevance(balance_patch, fp_sig) == 0


# ── RCA gate: conservative, only stops on a high-confidence spec/test verdict ─────────────────────

def test_rca_gate_stops_only_on_high_confidence_spec_or_test():
    assert _rca_should_stop("spec_bug", "high") is True
    assert _rca_should_stop("test_invalid", "high") is True
    # A code bug NEVER stops (this is what protects the demo bugs + the Claude result from regression).
    assert _rca_should_stop("code_bug", "high") is False
    # Low/medium confidence never stops — when unsure, proceed to the fixer.
    assert _rca_should_stop("spec_bug", "medium") is False
    assert _rca_should_stop("test_invalid", "low") is False


def test_rca_gate_disabled_never_stops():
    prev = settings.repair_rca_gate
    settings.repair_rca_gate = False
    try:
        assert _rca_should_stop("spec_bug", "high") is False
    finally:
        settings.repair_rca_gate = prev


# ── Apply resilience: RAG chunk dropped a leading `const `/indent (find not byte-exact on disk) ───

def test_resilient_replace_applies_when_index_dropped_const_prefix():
    # The live TC-RPS-003 apply failure: retrieval + diagnose were CORRECT, but the Tree-sitter chunk
    # stored `addToCart = (…) => {` while the file has `const addToCart = (…) => {`, so the model's
    # byte-exact find was not on disk. The tolerant apply must preserve `const` and change only `<=`→`<`.
    on_disk = ("function App() {\n"
               "  const addToCart = (product: Product, quantity: number) => {\n"
               "    if (quantity <= 1) {\n"
               "      return;\n"
               "    }\n"
               "  };\n}\n")
    find = "  addToCart = (product: Product, quantity: number) => {\n    if (quantity <= 1) {"
    replace = "  addToCart = (product: Product, quantity: number) => {\n    if (quantity < 1) {"
    out = _resilient_replace(on_disk, find, replace)
    assert out is not None
    assert "const addToCart" in out                 # the dropped keyword is preserved
    assert "if (quantity < 1) {" in out and "quantity <= 1" not in out   # only the fragment moved
    assert out.count("return;") == 1                # nothing else touched


def test_resilient_replace_is_safe_on_mismatch_and_ambiguity():
    on_disk = "  const a = 1;\n  const b = 2;\n  const a = 1;\n"   # `const a = 1;` appears twice
    # unequal find/replace line counts → refuse
    assert _resilient_replace(on_disk, "a\nb", "c") is None
    # find not present at all → refuse (no false latch)
    assert _resilient_replace(on_disk, "  const zzz = 9;", "  const zzz = 8;") is None
    # ambiguous (matches two lines) → refuse rather than edit the wrong one
    assert _resilient_replace(on_disk, "a = 1;", "a = 2;") is None


def test_bug_class_ignores_appended_root_cause_guidance_boilerplate():
    # The live misroute: `_failure_text_for` appends a "Fix the ROOT CAUSE… persisted or shared across
    # screens/kiosks (a balance, a transaction)…" guidance tail. That boilerplate — not the real symptom —
    # pushed _bug_class to 'spec'. The failure POINT must exclude it so an add-to-cart interaction bug
    # routes 'interaction'. Generic: the boilerplate is dropped for EVERY test.
    failure_with_boilerplate = (
        "TC-RPS-003 failed. [P1] Mock card payment with sufficient balance succeeds on RPS. "
        "Steps attempted (in order): tap: Sign In ; tap: Add to Cart ; "
        "verify: Cart screen is shown after adding the item [FAILED HERE] "
        "OBSERVED: Wrong screen: expected cart, got products [dom] "
        "Failing assertions: Wrong screen: expected cart, got products [dom] "
        "Fix the ROOT CAUSE of why the expected behaviour did not happen. If the expected outcome is a "
        "value that should have been persisted or shared across screens/kiosks (a balance, a transaction), "
        "correct the code that PRODUCES or PERSISTS that state, NOT code that merely displays or labels it."
    )
    fp = _failure_point_text(failure_with_boilerplate).lower()
    assert "balance" not in fp and "transaction" not in fp and "persisted" not in fp
    assert "cart" in fp and "products" in fp
    assert _bug_class(failure_with_boilerplate) == "interaction"
    # A genuine spec failure whose OBSERVED/assertions (not the boilerplate) carry the spec vocab still routes spec.
    assert _bug_class(SPEC_FAILURE) == "spec"
