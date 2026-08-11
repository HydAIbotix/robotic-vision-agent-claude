"""DOM coordinate correction — the id↔testid/aria token fallback for bare-symbol controls.

Regression for 2026-07-30: in the tight arm-reachable single-column layout, the "+"/"−" quantity
steppers' vision coordinates were never DOM-corrected (their textContent "+"/"−" is too short to
text-match), so a too-low stepper estimate fell inside the Add-to-Cart button below it — the
exploration walkthrough then tapped a DISABLED button and never explored cart/payment/success.
The fallback matches the element's semantic id tokens against each DOM element's testid + aria-label.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import app_explorer.nodes.explore_screen as es


def test_meaningful_tokens_drops_boilerplate():
    assert es._meaningful_tokens("nexora_phone_increase_button") == {"nexora", "phone", "increase"}
    # testid + aria collapse to the same identity tokens (generic words dropped)
    toks = es._meaningful_tokens("quantity-increase-nexora-phone-x2", "Increase Nexora Phone X2 quantity")
    assert {"nexora", "phone", "increase", "x2"} <= toks
    assert "quantity" not in toks and "button" not in toks


def _fake_dom():
    # Ground-truth DOM: the "+"/"−" steppers carry bare-symbol text but rich testid + aria.
    return [
        {"text": "−", "aria": "Decrease Nexora Phone X2 quantity", "cx": 631, "cy": 576,
         "tag": "button", "testid": "quantity-decrease-nexora-phone-x2"},
        {"text": "+", "aria": "Increase Nexora Phone X2 quantity", "cx": 719, "cy": 576,
         "tag": "button", "testid": "quantity-increase-nexora-phone-x2"},
        {"text": "Add to Cart", "aria": "", "cx": 960, "cy": 634,
         "tag": "button", "testid": "add-to-cart-nexora-phone-x2"},
        {"text": "+", "aria": "Increase OrionBook Pro 14 quantity", "cx": 719, "cy": 877,
         "tag": "button", "testid": "quantity-increase-orionbook-pro-14"},
    ]


def test_stepper_snaps_via_token_fallback(monkeypatch):
    monkeypatch.setattr(es.robot, "get_dom_element_centers", _fake_dom, raising=False)
    # Vision estimate for the "+" stepper is 73px too low — lands on the Add-to-Cart box below it.
    elements = [
        {"id": "nexora_phone_increase_button", "type": "stepper", "label": "+", "center": [727, 649]},
        {"id": "nexora_phone_add_to_cart_button", "type": "button", "label": "Add to Cart", "center": [963, 712]},
    ]
    fixed = {e["id"]: e for e in es._dom_correct_elements(elements)}
    inc = fixed["nexora_phone_increase_button"]
    # snapped to the CORRECT nexora "+" (719,576), NOT the orionbook "+" (719,877) or add-to-cart
    assert inc["center"] == [719, 576]
    assert inc.get("testid") == "quantity-increase-nexora-phone-x2"
    # existing text-match path still corrects the add-to-cart button
    assert fixed["nexora_phone_add_to_cart_button"]["center"] == [960, 634]


def test_stepper_plus_name_snaps_via_direction_synonym(monkeypatch):
    """2026-08-11 regression: Claude named the stepper `nexora_quantity_plus` (symbol/direction) while the
    DOM testid is `quantity-increase-nexora-phone-x2` (action). Without plus→increase canonicalisation the
    only shared token is the product name (1 → too weak), so the stepper kept its raw vision coord and the
    tap snapped to Add-to-Cart (qty never incremented → cart/payment flow skipped). The synonym makes it a
    2-token {nexora, increase} strong snap."""
    monkeypatch.setattr(es.robot, "get_dom_element_centers", _fake_dom, raising=False)
    elements = [
        {"id": "nexora_quantity_plus",  "type": "stepper", "label": "+", "center": [718, 544]},
        {"id": "nexora_quantity_minus", "type": "stepper", "label": "−", "center": [629, 544]},
        {"id": "nexora_add_to_cart_button", "type": "button", "label": "Add to Cart", "center": [960, 609]},
    ]
    fixed = {e["id"]: e for e in es._dom_correct_elements(elements)}
    # plus → the nexora INCREASE control (719,576); NOT decrease, NOT orionbook, NOT add-to-cart
    assert fixed["nexora_quantity_plus"]["center"] == [719, 576]
    assert fixed["nexora_quantity_plus"].get("testid") == "quantity-increase-nexora-phone-x2"
    # minus → the nexora DECREASE control (631,576) — direction is preserved, no cross-snap
    assert fixed["nexora_quantity_minus"]["center"] == [631, 576]
    assert fixed["nexora_quantity_minus"].get("testid") == "quantity-decrease-nexora-phone-x2"
    # add-to-cart still text-matches its own button
    assert fixed["nexora_add_to_cart_button"]["center"] == [960, 634]


def test_direction_synonym_does_not_collide_with_add_to_cart(monkeypatch):
    # "add" must NOT be treated as a synonym of "increase" — else Add-to-Cart would share a token with the
    # plus stepper. app_map id `nexora_add_to_cart_button` tokens must not include 'increase'.
    assert "increase" not in es._meaningful_tokens("nexora_add_to_cart_button")
    assert es._meaningful_tokens("nexora_quantity_plus") == {"nexora", "increase"}
    assert es._meaningful_tokens("nexora_quantity_minus") == {"nexora", "decrease"}


def test_token_fallback_requires_two_shared_tokens(monkeypatch):
    # A single shared generic-ish token must NOT trigger a snap (avoids false corrections).
    monkeypatch.setattr(es.robot, "get_dom_element_centers",
                        lambda: [{"text": "x", "aria": "Increase widget", "cx": 500, "cy": 500,
                                  "tag": "button", "testid": "increase-widget"}], raising=False)
    elements = [{"id": "nexora_phone_increase_button", "type": "stepper", "label": "+", "center": [727, 649]}]
    fixed = es._dom_correct_elements(elements)
    # only "increase" overlaps (1 token) → below the ≥2 threshold → coordinate left unchanged
    assert fixed[0]["center"] == [727, 649]
    assert "testid" not in fixed[0]


def _login_dom():
    # signin screen: email + password inputs (testid shares only ONE token with the app_map id),
    # plus a Sign In button. Mirrors the real robotics-kiosk-pos signin screen.
    return [
        {"text": "", "aria": "", "cx": 691, "cy": 360, "tag": "input", "testid": "signin-email"},
        {"text": "", "aria": "", "cx": 691, "cy": 480, "tag": "input", "testid": "signin-password"},
        {"text": "Sign In", "aria": "", "cx": 691, "cy": 560, "tag": "button", "testid": "signin-button"},
    ]


def test_login_input_snaps_on_single_distinctive_token(monkeypatch):
    # 'email_input' vs 'signin-email' share only {email} ('input' is generic) — the vision estimate is
    # ~50px too high; a single UNAMBIGUOUS type-compatible token must snap it to the true DOM centre.
    monkeypatch.setattr(es.robot, "get_dom_element_centers", _login_dom, raising=False)
    elements = [
        {"id": "email_input", "type": "input", "label": "Email", "center": [690, 310]},
        {"id": "password_input", "type": "input", "label": "Password", "center": [690, 430]},
    ]
    fixed = {e["id"]: e for e in es._dom_correct_elements(elements)}
    assert fixed["email_input"]["center"] == [691, 360]
    assert fixed["email_input"].get("testid") == "signin-email"
    assert fixed["password_input"]["center"] == [691, 480]
    assert fixed["password_input"].get("testid") == "signin-password"


def test_single_token_ambiguous_does_not_snap(monkeypatch):
    # Two 'add to cart' buttons share {add}: a single-token match is AMBIGUOUS → must NOT snap
    # (keeps the vision estimate) to avoid tapping the wrong product's button.
    monkeypatch.setattr(es.robot, "get_dom_element_centers", lambda: [
        {"text": "Add", "aria": "", "cx": 300, "cy": 400, "tag": "button", "testid": "add-nexora"},
        {"text": "Add", "aria": "", "cx": 300, "cy": 700, "tag": "button", "testid": "add-orion"},
    ], raising=False)
    elements = [{"id": "add_button", "type": "button", "label": "Add", "center": [305, 402]}]
    fixed = es._dom_correct_elements(elements)
    # NOTE: the text pass matches "Add" first (label==dom text). Assert it does NOT land on the far one.
    assert fixed[0]["center"] in ([305, 402], [300, 400])
    assert fixed[0]["center"] != [300, 700]


def test_single_token_wrong_type_does_not_snap(monkeypatch):
    # A single shared token whose only candidate is a TYPE-MISMATCH must not snap (stepper vs button).
    monkeypatch.setattr(es.robot, "get_dom_element_centers",
                        lambda: [{"text": "x", "aria": "Increase widget", "cx": 500, "cy": 500,
                                  "tag": "button", "testid": "increase-widget"}], raising=False)
    elements = [{"id": "nexora_increase", "type": "stepper", "label": "+", "center": [727, 649]}]
    fixed = es._dom_correct_elements(elements)
    assert fixed[0]["center"] == [727, 649]
    assert "testid" not in fixed[0]


def test_no_dom_access_returns_unchanged(monkeypatch):
    def _raise():
        raise AttributeError("real robot has no DOM")
    monkeypatch.setattr(es.robot, "get_dom_element_centers", _raise, raising=False)
    elements = [{"id": "x_button", "label": "+", "center": [10, 10]}]
    assert es._dom_correct_elements(elements) == elements


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
