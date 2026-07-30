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


def test_no_dom_access_returns_unchanged(monkeypatch):
    def _raise():
        raise AttributeError("real robot has no DOM")
    monkeypatch.setattr(es.robot, "get_dom_element_centers", _raise, raising=False)
    elements = [{"id": "x_button", "label": "+", "center": [10, 10]}]
    assert es._dom_correct_elements(elements) == elements


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
