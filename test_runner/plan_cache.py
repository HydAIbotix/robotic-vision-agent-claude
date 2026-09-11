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


def _cache_dir() -> Path:
    """Tenant-scoped plan-cache directory (== _CACHE_DIR when single-tenant, so no MVP change)."""
    from ports import paths as tenant_paths
    return tenant_paths.test_plans_dir()

# Bump this whenever the planner model or PLAN_FROM_MAP prompt changes materially.
# It is part of the cache key, so bumping it invalidates every previously-cached plan
# and forces a fresh Tier-2 re-plan.  "v6" retires plans generated before the walkthrough wrote
# back execution-CONFIRMED dependencies (observed=True) — data not reflected in the app_map
# version hash, so a version bump is needed to pick it up.
_PLANNER_VERSION = "v11-agv-crosskiosk-capture"


def _key(test_id: str, steps_raw: str, app_map_version: str, expected_results_raw: str = "") -> str:
    # expected_results_raw is part of the key so editing a test case's expected value (e.g. the
    # final amount to verify) invalidates the cached plan even when the steps text is unchanged.
    raw = f"{_PLANNER_VERSION}|{test_id}|{steps_raw.strip()}|{expected_results_raw.strip()}|{app_map_version}"
    return hashlib.md5(raw.encode()).hexdigest()[:10]


def _cache_path(test_id: str, cache_key: str) -> Path:
    d = _cache_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{test_id}_{cache_key}.json"


def load(test_id: str, steps_raw: str, app_map_version: str, expected_results_raw: str = "") -> Optional[dict]:
    """Return a cached plan dict, or None if the cache is empty / stale."""
    key  = _key(test_id, steps_raw, app_map_version, expected_results_raw)
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


def save(plan: dict, test_id: str, steps_raw: str, app_map_version: str, expected_results_raw: str = "") -> None:
    """Persist a plan with its cache key embedded for future validation."""
    key  = _key(test_id, steps_raw, app_map_version, expected_results_raw)
    plan = {**plan, "cache_key": key}
    _cache_path(test_id, key).write_text(
        json.dumps(plan, indent=2), encoding="utf-8"
    )
    # Mirror the planner's output (cached plan) to the durable object store when enabled.
    try:
        from ports.archive import archive
        archive(str(_cache_path(test_id, key)))
    except Exception:
        pass


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
        if not eid:
            return False   # empty element_id means the plan hallucinated an unexplored screen
        if not sid:
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
