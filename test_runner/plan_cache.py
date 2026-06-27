"""
plan_cache — load and save structured test execution plans.

Cache key = short hash of (test_id + steps_raw + app_map version).
A plan is invalid (triggers Tier-2 re-plan) when:
  - the file does not exist (first run)
  - the test case description has changed (new steps_raw)
  - the app_map was re-generated (new explored_at / different screen set)
  - any element_id referenced in the plan no longer exists in the current app_map

Plans are stored in  <project_root>/test_plans/<test_id>_<hash>.json
"""
import hashlib
import json
from pathlib import Path
from typing import Optional

_CACHE_DIR = Path(__file__).parent.parent / "test_plans"


def _key(test_id: str, steps_raw: str, app_map_version: str) -> str:
    raw = f"{test_id}|{steps_raw.strip()}|{app_map_version}"
    return hashlib.md5(raw.encode()).hexdigest()[:10]


def _cache_path(test_id: str, cache_key: str) -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR / f"{test_id}_{cache_key}.json"


def load(test_id: str, steps_raw: str, app_map_version: str) -> Optional[dict]:
    """Return a cached plan dict, or None if the cache is empty / stale."""
    key  = _key(test_id, steps_raw, app_map_version)
    path = _cache_path(test_id, key)
    if not path.exists():
        return None
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
        # Sanity: the file must carry the same key we computed
        if plan.get("cache_key") != key:
            return None
        return plan
    except Exception:
        return None


def save(plan: dict, test_id: str, steps_raw: str, app_map_version: str) -> None:
    """Persist a plan with its cache key embedded for future validation."""
    key  = _key(test_id, steps_raw, app_map_version)
    plan = {**plan, "cache_key": key}
    _cache_path(test_id, key).write_text(
        json.dumps(plan, indent=2), encoding="utf-8"
    )


def is_valid(plan: dict, app_map: Optional[dict]) -> bool:
    """Return True if every element referenced in the plan still exists in the app_map.

    Called after a cache hit to confirm the plan is still executable against the
    current app_map.  If the UI changed and an element was removed or renamed,
    this returns False and Tier-2 re-planning is triggered.
    """
    if not plan or not app_map:
        return bool(plan)   # no app_map means we can't validate — trust the plan

    screens = app_map.get("screens") or {}
    for step in plan.get("steps") or []:
        if step.get("action") != "tap":
            continue
        sid = step.get("screen_id", "")
        eid = step.get("element_id", "")
        if not sid or not eid:
            continue
        screen = screens.get(sid)
        if screen is None:
            return False   # screen was removed
        elements = screen.get("elements") or []
        if not any(e.get("id") == eid for e in elements):
            return False   # element was removed or renamed
    return True


def invalidate_all_for(test_id: str) -> int:
    """Delete all cached plans for a given test_id. Returns count of deleted files."""
    deleted = 0
    for p in _CACHE_DIR.glob(f"{test_id}_*.json"):
        p.unlink(missing_ok=True)
        deleted += 1
    return deleted
