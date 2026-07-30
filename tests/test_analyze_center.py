"""Malformed vision `center` must not crash exploration (2026-07-30).

Claude's vision occasionally returns a `center` with the wrong arity (a 4-value bbox, a 3-value
point). `_norm_to_px` preserves the length, so `cx, cy = el["center"]` in _log_screen then raised
`ValueError: too many values to unpack` and took the whole explorer down (exit code 1). _center2
coerces every center to exactly [x, y] at the source, and _log_screen is defensive too.
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from vision_agent.nodes.analyze import _center2, _log_screen


def test_center2_coercions():
    assert _center2([0.5, 0.6]) == [0.5, 0.6]          # already 2 → unchanged
    assert _center2([0.2, 0.2, 0.6, 0.8]) == [0.4, 0.5]  # 4-value bbox → midpoint
    assert _center2([0.5, 0.6, 0.7]) == [0.5, 0.6]     # 3-value → first two
    assert _center2([0.9]) == [0.5, 0.5]               # too few → frame centre
    assert _center2([]) == [0.5, 0.5]
    assert _center2("nope") == [0.5, 0.5]              # non-numeric → frame centre
    assert _center2(None) == [0.5, 0.5]


def test_log_screen_survives_malformed_center(capsys):
    # A 4-value and a 3-value center must NOT raise (was ValueError → explorer exit 1).
    screen = {
        "screen_id": "products", "description": "d",
        "elements": [
            {"id": "ok", "type": "button", "center": [100, 200], "confidence": 0.9},
            {"id": "bbox_center", "type": "button", "center": [10, 20, 110, 220], "confidence": 0.8},
            {"id": "three", "type": "stepper", "center": [5, 6, 7], "confidence": 0.7},
            {"id": "missing", "type": "button", "confidence": 0.6},
        ],
    }
    _log_screen(screen)   # must not raise
    out = capsys.readouterr().out
    assert "bbox_center" in out and "three" in out and "missing" in out


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
