"""
Auto-Repair agent — the self-healing arm of defect intelligence.

Given a failed-test message (or plain-English defect), it:
  1. RETRIEVE — pull the offending code from the Chroma RAG index (Chroma + HuggingFace,
     built by parse_code_and_store.py — left exactly as the POC wrote it).
  2. DIAGNOSE — ask Claude (Opus 4.8, via vision_agent.llm) for ONE minimal find/replace patch.
  3. APPLY    — apply the patch to the live kiosk app (single-occurrence, in-codebase guard).
  4. TEST     — npm run lint (the app's static "unit" gate).
  5. BUILD    — npm run build (tsc type-check + vite build) — the real validation.
  6. PR PREP  — create a local branch + commit and produce the diff. Pushing the branch and
     opening the PR is a SEPARATE, gated step (open_pull_request) — never automatic.

Only steps 2 (diagnose) and the repair reasoning use Claude. Retrieval stays Chroma/HuggingFace.

Progress is streamed through a callback so the API/UI can render each stage live.
"""
from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from langchain_core.messages import HumanMessage

from vision_agent.config import settings
from vision_agent.llm import get_llm, get_local_llm, invoke_json
from repair_agent.canceller import RepairCancelled
from repair_agent.parse_code_and_store import CODEBASE_DIR, PERSIST_DIR, SKIP_DIRS, search


ProgressCb = Callable[[dict], None]


@dataclass
class RepairPatch:
    file_path: str
    find: str
    replace: str
    explanation: str
    source: str = "claude"   # which provider produced it: claude | local | demo-fallback
    model: str = ""          # the concrete model name (for UI/telemetry)


_DIAGNOSE_PROMPT = """You are a senior software repair agent fixing a FAILED automated test for a
React/TypeScript kiosk app. Use ONLY the retrieved code context to locate the real bug.

Return a SINGLE JSON object (no markdown, no commentary) with exactly these keys:
{{
  "file_path": "path to the file to edit (absolute, or repo-relative as shown in the context)",
  "find": "exact text currently in that file — copy it verbatim, must be unique in the file",
  "replace": "the corrected replacement text",
  "explanation": "one sentence describing the fix"
}}

Rules:
- Make the SMALLEST possible fix — change ONLY the buggy sub-expression, ideally a single token.
- PRESERVE every other condition, guard, and clause on the line. Do NOT drop, merge, simplify, or
  reorder any logic that is not itself the bug (e.g. keep unrelated `||`/`&&` guards intact).
- Keep "find" and "replace" nearly identical except for the exact buggy fragment.
- "find" must appear EXACTLY ONCE in the target file so the replacement is unambiguous.
- Do not reformat unrelated code. Do not invent files or symbols not in the context.
- Fix the ROOT CAUSE, not the symptom. If the failure is a wrong/missing VALUE, LABEL or on-screen
  TEXT, do NOT make it pass by hardcoding or relabeling a string to match the expected text. Find and
  fix the code that PRODUCES or PERSISTS that value/state — a wrong guard/condition that skips a write,
  a wrong endpoint, a dropped update — even when it lives in a different function or file than where the
  text is displayed. Relabeling the displayed text (e.g. changing a transaction "type" from one label
  to another) is almost never the correct fix.

FAILED TEST / DEFECT:
{failure}

RETRIEVED CODE CONTEXT:
{context}
"""


def _npm_cmd() -> str:
    return "npm.cmd" if platform.system() == "Windows" else "npm"


def _local_bin(cwd: Path, name: str) -> Optional[Path]:
    """Path to a node_modules/.bin executable, if installed."""
    exe = name + (".cmd" if platform.system() == "Windows" else "")
    p = cwd / "node_modules" / ".bin" / exe
    return p if p.exists() else None


def _unit_test(cwd: Path) -> dict:
    """The 'unit test' gate. Prefer a real `npm test`; otherwise run a TypeScript type-check
    (the meaningful static test when the app ships no unit-test runner). ESLint is intentionally
    NOT used here — this repo's eslint config is broken (thousands of pre-existing parser errors)
    and unrelated to any repair, so it would only add noise."""
    try:
        scripts = json.loads((cwd / "package.json").read_text(encoding="utf-8")).get("scripts", {})
    except Exception:
        scripts = {}
    if "test" in scripts:
        return _run([_npm_cmd(), "test", "--silent"], cwd)
    tsc = _local_bin(cwd, "tsc")
    if tsc:
        return _run([str(tsc), "-b"], cwd)
    return {"ok": True, "code": 0, "output": "no unit-test runner found — skipped", "cmd": "(skip)"}


def _run(cmd: list[str], cwd: Path, timeout: int = 600) -> dict:
    """Run a shell command, capturing combined output. Never raises — returns a result dict."""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return {"ok": proc.returncode == 0, "code": proc.returncode, "output": out.strip(), "cmd": " ".join(cmd)}
    except FileNotFoundError as e:
        return {"ok": False, "code": -1, "output": f"command not found: {e}", "cmd": " ".join(cmd)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "output": f"timed out after {timeout}s", "cmd": " ".join(cmd)}
    except Exception as e:  # pragma: no cover
        return {"ok": False, "code": -1, "output": str(e), "cmd": " ".join(cmd)}


# ── 1. RETRIEVE ────────────────────────────────────────────────────────────────

def _retrieval_query(failure: str) -> str:
    """Strip test-harness jargon (test-id, "failed", "step", …) from the failure before searching.

    Those words match the test WORKBOOK far more strongly than the source code, dragging the real
    code chunk out of the top results (measured: the buggy chunk went from rank ~11 → rank 0 once
    removed). The full failure text still goes to the LLM prompt for context; only the vector query
    is cleaned."""
    q = re.sub(r"\bTC-[A-Za-z0-9]+-\d+\b", " ", failure)   # strip TC-RPS-001 AND TC-E2E-001 style ids
    q = re.sub(r"\b(failed|failure|test case|test|step|steps|expected|observed|actual)\b", " ", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip(" .:-")
    return q or failure


def _action_query(failure: str) -> str:
    """A second, ACTION-focused retrieval query built from what the test was DOING (its executed
    steps) rather than what it was supposed to achieve (the design intent).

    The full failure text is dominated by the design intent + the failed-assertion SYMPTOM, whose
    vocabulary points at the outcome/navigation (e.g. a login failure reads as "did not reach the
    products screen") — semantically far from the code that is actually broken (the credential
    check). The executed steps ("Enter tester email", "Tap Sign In to submit credentials") carry the
    ACTION vocabulary that anchors retrieval to the code path under test. Running this as a SEPARATE
    query (interleaved with the intent query in retrieve_context) means neither vocabulary can bury
    the other — the design-intent lane still surfaces persistence/spec code (needed for the
    cross-kiosk value bugs), while this lane surfaces the code for the action that failed. Falls back
    to the full jargon-stripped query when the failure carries no recognisable steps line."""
    summary = failure.split("EXPECTED BEHAVIOUR")[0]
    m = re.search(r"Steps attempted \(in order\):(.*?)(?: OBSERVED:| Fix the ROOT CAUSE|$)", failure, re.S)
    steps = m.group(1) if m else ""
    fa = re.search(r"Failing assertions:(.*?)(?: Fix the ROOT CAUSE|$)", failure, re.S)
    parts = summary + " " + steps + " " + (fa.group(1) if fa else "")
    aq = _retrieval_query(parts)
    return aq if (steps.strip() and aq.strip()) else _retrieval_query(failure)


# File-neighborhood expansion tuning: when a SMALL support module is implicated, show Claude the whole
# module. Large files (App.tsx) are skipped so context stays focused.
_SMALL_FILE_MAX_CHUNKS = 25   # a file with ≤ this many indexed chunks is a "small module" → expand fully
_EXPAND_FILES          = 2    # expand at most this many implicated small modules
_MAX_CONTEXT_BLOCKS    = 16   # hard cap on chunks handed to the LLM
_DESIGN_DOCS           = 5    # design-doc chunks to ALWAYS include (the spec that reveals the root cause).
                              # The doc is small (~12 focused section chunks); a symptom-side failure query
                              # can rank the CAUSE-side section (e.g. "Payment and Card Reader Design") ~#4,
                              # so pull a few to reliably include it without bloating context.


def retrieve_context(failure: str, top_k: int = 8) -> tuple[str, list[dict]]:
    """Semantic search over the Chroma RAG index. Returns (prompt_text, structured_hits).

    top_k is the number of CODE chunks pulled (was 6). A real buggy chunk can sit at rank ~7–8 when
    the failure query is dominated by symptom/navigation vocabulary (e.g. a login failure reads as
    "wrong screen: products vs login"); a slightly wider code window keeps that chunk in the set
    without crowding out the design-doc / general context (final cap `_MAX_CONTEXT_BLOCKS`)."""
    if not PERSIST_DIR.exists():
        raise RuntimeError(
            f"RAG index not found at {PERSIST_DIR}. Build it first: "
            f"python -m repair_agent.parse_code_and_store"
        )

    # Bias toward SOURCE CODE: a failure message like "TC-RPS-001 login rejected" matches the test
    # workbook / lockfiles more strongly than the code that needs fixing, so a plain search returns
    # no code and the LLM has nothing to patch. Pull code chunks explicitly, then top up with a few
    # general hits (design doc / test cases) for product context. Code always comes first.
    rq = _retrieval_query(failure)
    code_where = {"type": {"$in": ["code_block", "code_file"]}}
    # TWO-LANE code retrieval, interleaved. Lane 1 (intent/symptom) = the full failure — surfaces the
    # code that PRODUCES the wrong outcome (a cross-kiosk persistence guard, a value path). Lane 2
    # (action) = the executed steps — surfaces the code for the ACTION that failed (a credential
    # check, an add-to-cart). A single blended query lets whichever vocabulary is heavier bury the
    # other (observed: the design-intent "products screen" navigation words pushed the login
    # credential code out of the retrieved set → "insufficient context"). Interleaving guarantees
    # both lanes are represented. When there is no distinct steps line the action query collapses to
    # the intent query, so this degrades cleanly to the previous single-lane behaviour.
    rq_action = _action_query(failure)
    intent_code = search(rq, k=top_k, where=code_where)
    if rq_action == rq:
        code_docs = intent_code
    else:
        action_code = search(rq_action, k=top_k, where=code_where)
        code_docs, _cseen = [], set()
        for a, b in zip(intent_code, action_code):
            for d in (a, b):
                ck = (d.metadata.get("source"), d.metadata.get("start_line"), d.page_content[:40])
                if ck not in _cseen:
                    _cseen.add(ck)
                    code_docs.append(d)
        # append any tail (unequal lengths) preserving order, still deduped
        for d in list(intent_code) + list(action_code):
            ck = (d.metadata.get("source"), d.metadata.get("start_line"), d.page_content[:40])
            if ck not in _cseen:
                _cseen.add(ck)
                code_docs.append(d)
        # Keep only the interleaved top-`top_k` so the two lanes don't crowd the DESIGN/general docs
        # out of the final `_MAX_CONTEXT_BLOCKS` budget (both lanes' best hits are up front).
        code_docs = code_docs[:top_k]
    # DESIGN INTENT. The design doc states the SPEC ("if the purchase succeeds … a PURCHASE transaction
    # is recorded"), which is what tells the LLM the ROOT CAUSE — e.g. that a guard skipping the record
    # for issued cards is the bug, not a label to relabel. It IS indexed but ranks below code in a plain
    # search, so a code-first retrieval never surfaced it and the LLM had to guess. Pull it EXPLICITLY by
    # type and include it up-front so the spec always reaches Claude alongside the offending code.
    design_docs = search(rq, k=_DESIGN_DOCS, where={"type": "design_document"})
    general_docs = search(rq, k=2)

    # FILE-NEIGHBORHOOD EXPANSION. A symptom-level failure query ("card not found cross-kiosk") often
    # ranks the exact buggy function low, but ranks a SIBLING in the same small module high — and the
    # fix is usually revealed by comparing the buggy line against its correct siblings (e.g. the wrong
    # `/api/card/` lookup next to the correct `/api/cards` calls in src/lib/storage.ts). So for each
    # small implicated module we pull ALL of its chunks. Big files (App.tsx) are skipped to stay focused.
    expanded, seen_files, probed = [], set(), set()
    for d in code_docs:
        src = d.metadata.get("source")
        # `probed` guards against re-searching the SAME file: a big file (App.tsx — the login bug lives
        # there) never enters `seen_files` because it exceeds the small-module cap, so without this guard
        # the loop fired a fresh k=60 search (each reloading the model in the old code) for every one of
        # its chunks in the top hits — wasted work that produced no expansion. Probe each source at most once.
        if not src or src in probed:
            continue
        probed.add(src)
        siblings = search(rq, k=60, where={"source": src})
        if 0 < len(siblings) <= _SMALL_FILE_MAX_CHUNKS:
            expanded.extend(siblings)
            seen_files.add(src)
        if len(seen_files) >= _EXPAND_FILES:
            break

    # Order: the offending CODE first, then the DESIGN spec (the intent that reveals the root cause),
    # then expanded neighbours, then general product context. Design goes before the cap so it is never
    # crowded out by code/expansion.
    docs, seen = [], set()
    for d in list(code_docs) + list(design_docs) + expanded + list(general_docs):
        key = (d.metadata.get("source"), d.metadata.get("start_line"), d.page_content[:40])
        if key not in seen:
            seen.add(key)
            docs.append(d)
        if len(docs) >= _MAX_CONTEXT_BLOCKS:
            break

    # Show the WHOLE retrieved chunk to the LLM, up to a generous cap. A tree-sitter code chunk is a
    # single function; the previous 1800-char cut truncated a ~2.8 KB function BEFORE its buggy line
    # (the guard at offset ~1910), so the LLM saw the function's opening + the symptom and guessed. The
    # cap only guards against a pathologically large chunk.
    _SNIPPET_PROMPT = 4000
    _SNIPPET_HIT    = 2500
    hits: list[dict] = []
    blocks: list[str] = []
    for i, doc in enumerate(docs, start=1):
        meta = doc.metadata
        is_design = meta.get("type") == "design_document"
        hits.append({
            "file": meta.get("source", ""),
            "type": meta.get("type", ""),
            "start_line": meta.get("start_line"),
            "end_line": meta.get("end_line"),
            "snippet": doc.page_content[:_SNIPPET_HIT],
        })
        header = ("Context %d — DESIGN SPEC (authoritative: the code MUST conform to this; use it to "
                  "judge the correct behaviour)" % i) if is_design else f"Context {i}"
        blocks.append("\n".join([
            header,
            f"File: {meta.get('source')}",
            f"Type: {meta.get('type')}",
            f"Lines: {meta.get('start_line')} - {meta.get('end_line')}",
            "Snippet:",
            doc.page_content[:_SNIPPET_PROMPT],
        ]))
    return "\n\n".join(blocks), hits


# ── 2. DIAGNOSE (Claude primary, local LLM backup) ──────────────────────────────

def _diagnose_providers() -> list[tuple[str, Callable[[], object]]]:
    """Ordered (label, llm-factory) list for the DIAGNOSE call, driven by `repair_llm_backend`.

    The pipeline makes exactly ONE LLM call, so this is the whole model-selection surface. The UI
    toggle picks which model is TRIED FIRST; the other is the automatic backup so a single provider
    being unreachable (Claude API down, or the local Ollama server not running) doesn't dead-end the
    repair. The deterministic demo rule remains the final last resort below.
      claude → [Claude, local]   (default: Claude primary, local backup — the "backup if Claude can't
                                  be reached" case)
      local  → [local, Claude]   (used to TEST the local path; Claude still backs it up)
    Factories are lazy (called only when that provider's turn comes), so selecting "claude" never
    touches Ollama and vice-versa."""
    claude = ("claude", get_llm)
    local = ("local", get_local_llm)
    return [local, claude] if settings.repair_llm_backend == "local" else [claude, local]


class _DiagnoseTimeout(Exception):
    """A single provider's DIAGNOSE call exceeded its wall-clock budget."""


def _invoke_with_deadline(fn, timeout, cancel_check):
    """Run `fn()` on a daemon thread and wait up to `timeout` seconds, polling `cancel_check`.

    A blocking LLM call (a cold local model, a hung request) can't be interrupted in-thread, so we
    run it on a daemon thread and watch it: on cancel we raise RepairCancelled, on timeout we raise
    _DiagnoseTimeout — either way the caller moves on immediately and the daemon thread is left to
    finish harmlessly (daemon → never blocks shutdown). timeout=None / <=0 means wait indefinitely."""
    box: dict = {}
    done = threading.Event()

    def worker():
        try:
            box["value"] = fn()
        except Exception as exc:          # provider raised — surface it to the caller
            box["error"] = exc
        finally:
            done.set()

    threading.Thread(target=worker, daemon=True).start()
    waited = 0.0
    step = 0.5
    while not done.wait(step):
        waited += step
        if cancel_check and cancel_check():
            raise RepairCancelled()
        if timeout and timeout > 0 and waited >= timeout:
            raise _DiagnoseTimeout()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def propose_patch(failure: str, context: str, *, timeout=None, cancel_check=None) -> RepairPatch:
    """Produce one minimal find/replace patch: try the selected model, then the backup model, then
    the deterministic demo rule. `repair_llm_backend` chooses primary vs backup order. Each provider
    call is bounded by `timeout` (seconds) and interruptible via `cancel_check` so a stuck/slow model
    never freezes the repair."""
    prompt = _DIAGNOSE_PROMPT.format(failure=failure, context=context)

    for label, make_llm in _diagnose_providers():
        if cancel_check and cancel_check():
            raise RepairCancelled()
        try:
            llm = make_llm()   # lazy — a missing/unreachable backup raises here, we move on
        except Exception as exc:
            print(f"  [REPAIR] DIAGNOSE provider '{label}' unavailable ({exc}) — trying next.")
            continue
        try:
            data = _invoke_with_deadline(
                lambda: invoke_json(llm, [HumanMessage(content=prompt)], default=None, label=f"repair/{label}"),
                timeout, cancel_check,
            )
        except _DiagnoseTimeout:
            print(f"  [REPAIR] DIAGNOSE provider '{label}' timed out after {timeout}s — trying next.")
            continue
        if data and data.get("find") and data.get("replace") is not None:
            model_name = (
                settings.repair_local_model if label == "local" else settings.anthropic_model
            )
            print(f"  [REPAIR] DIAGNOSE patch from '{label}' ({model_name}).")
            return RepairPatch(
                file_path=str(data.get("file_path", "")),
                find=data["find"],
                replace=data["replace"],
                explanation=data.get("explanation", f"{label}-proposed repair."),
                source=label,
                model=model_name,
            )
        print(f"  [REPAIR] DIAGNOSE provider '{label}' returned no usable patch — trying next.")

    fallback = _demo_fallback_patch()
    if fallback:
        print("  [REPAIR] No model produced a usable patch — using demo fallback rule.")
        return fallback

    raise RuntimeError("No repair patch could be produced (no model returned a usable patch and no fallback matched).")


def _demo_fallback_patch() -> Optional[RepairPatch]:
    """Deterministic fallbacks for the three intentional demo bugs (used only when Claude returns no
    usable patch, e.g. no API key / rate-limited) so the customer demo never dead-ends.

    Each rule is a small, exact find/replace that can only match its planted bug:
      • login   — `user.password !== `${pw}-bug`` rejects a valid user (pw = password | normalizedPassword).
      • products — Add to Cart passes a hardcoded 0, so the cart never fills.

    NOTE: the cross-kiosk card-sharing design bug (`demo/rps-design-bug`, wrong `/api/card/` endpoint in
    src/lib/storage.ts) has NO fallback ON PURPOSE — it's the showcase for Claude reading the design doc
    ("a card issued at kiosk-1 can be validated and charged at kiosk-2") + the sibling `/api/cards` calls
    and reasoning out the fix itself.
    """
    app = CODEBASE_DIR / "src" / "App.tsx"
    if not app.exists():
        return None
    text = app.read_text(encoding="utf-8")

    rules: list[tuple[str, str, str]] = []
    for pw in ("normalizedPassword", "password"):
        rules.append((
            f"user.password !== `${{{pw}}}-bug`",
            f"user.password !== {pw}",
            f"Login compared the password against {pw} + '-bug', rejecting valid credentials; compare against the real password.",
        ))
    rules.append((
        "onAddToCart(product, 0)",
        "onAddToCart(product, quantity)",
        "Add to Cart passed a hardcoded 0 instead of the selected quantity, so the cart never filled; pass the chosen quantity.",
    ))

    for find, replace, why in rules:
        if text.count(find) == 1:   # exact + unique → safe to apply
            return RepairPatch(file_path=str(app), find=find, replace=replace, explanation=why,
                               source="demo-fallback", model="deterministic")
    return None


# ── 3. APPLY ────────────────────────────────────────────────────────────────────

def _locate_file(patch: RepairPatch) -> Path:
    """Resolve the patch target to a real in-codebase file, tolerating odd paths from the LLM."""
    codebase = CODEBASE_DIR.resolve()

    # Prefer the LLM-provided path when it exists and sits inside the codebase.
    if patch.file_path:
        cand = Path(patch.file_path)
        if not cand.is_absolute():
            cand = (CODEBASE_DIR / patch.file_path).resolve()
        if cand.exists() and codebase in cand.resolve().parents:
            return cand.resolve()

    # Otherwise, find the unique file that actually contains the find-text.
    matches = []
    for dirpath, dirnames, filenames in os.walk(codebase):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if Path(fn).suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".css"}:
                fp = Path(dirpath) / fn
                try:
                    if patch.find in fp.read_text(encoding="utf-8"):
                        matches.append(fp)
                except Exception:
                    pass
    if len(matches) == 1:
        return matches[0].resolve()
    if not matches:
        raise RuntimeError("Patch target not found: the 'find' text is not present in any codebase file.")
    raise RuntimeError(f"Patch 'find' text appears in {len(matches)} files; refusing an ambiguous edit.")


def apply_patch(patch: RepairPatch) -> Path:
    target = _locate_file(patch)
    codebase = CODEBASE_DIR.resolve()
    if codebase not in target.parents and target != codebase:
        raise RuntimeError(f"Refusing to edit a file outside the codebase: {target}")

    text = target.read_text(encoding="utf-8")
    count = text.count(patch.find)
    if count == 0:
        raise RuntimeError("Patch find-text was not found in the target file.")
    if count > 1:
        raise RuntimeError(f"Patch find-text appears {count} times; refusing an ambiguous edit.")

    target.write_text(text.replace(patch.find, patch.replace, 1), encoding="utf-8")
    return target


# ── 6. PR PREP + (gated) OPEN ────────────────────────────────────────────────────

def _git(cmd: list[str]) -> dict:
    return _run(["git", *cmd], CODEBASE_DIR)


def _repo_blocked_reason() -> str:
    """Return a human reason if the codebase repo is in an unsafe state for auto-edits/commits (a
    merge or rebase in progress, or unresolved conflicts), else "". The agent must NOT branch/commit
    on top of such a state — it would sweep the whole conflicted merge into the "fix" branch (observed:
    repair/tc-rps-001-fix picked up a full main↔arm-reachable merge, ~1900 lines, no clean fix commit,
    and the PR failed)."""
    git_dir = CODEBASE_DIR / ".git"
    if (git_dir / "MERGE_HEAD").exists():
        return "a merge is in progress (an unfinished git pull/merge) — resolve or `git merge --abort` first"
    if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
        return "a rebase is in progress — finish or `git rebase --abort` first"
    for line in _git(["status", "--porcelain"]).get("output", "").splitlines():
        code = line[:2]
        if "U" in code or code in ("AA", "DD"):
            return f"there are unresolved merge conflicts (e.g. {line.strip()})"
    return ""


def _branch_name(test_id: str, suffix: str = "") -> str:
    slug = (test_id or "kiosk-test").strip().lower().replace(" ", "-") or "kiosk-test"
    base = f"repair/{slug}-fix"
    # A per-run suffix keeps each run's branch UNIQUE, so a fresh push never collides with (and later
    # gets merged into) a previous run's origin branch on a different lineage.
    return f"{base}-{suffix}" if suffix else base


def _current_branch() -> str:
    """The branch the codebase is on right now (the branch the failing test ran against), or "" if
    detached. This becomes the PR base so the fix branch diffs against the SAME demo bug branch the
    app was on — essential when several demo bug branches exist (login/products/design)."""
    name = _git(["rev-parse", "--abbrev-ref", "HEAD"]).get("output", "").strip()
    return "" if name in ("", "HEAD") else name


def _pr_base(current: str) -> str:
    """PR base = the current demo branch, unless we're already on a repair branch (or detached), in
    which case fall back to the configured base so a fix never bases off another fix branch."""
    if current and not current.startswith("repair/"):
        return current
    return settings.repair_pr_base


def prepare_pr(patch: RepairPatch, target: Path, failure: str, test_id: str, branch_suffix: str = "") -> dict:
    """Create a local branch + commit for the fix and return the diff + prepared PR fields.

    Does NOT push or open a PR — that is the gated open_pull_request() step. Refuses to touch git
    when the repo is mid-merge / has conflicts, so the fix branch always holds ONLY the fix.
    """
    if not (CODEBASE_DIR / ".git").exists():
        return {"prepared": False, "reason": "codebase is not a git repository", "diff": ""}

    rel = os.path.relpath(str(target), str(CODEBASE_DIR)).replace("\\", "/")
    diff = _git(["diff", "--", rel]).get("output", "")

    blocked = _repo_blocked_reason()
    if blocked:
        return {"prepared": False, "reason": f"repo not in a clean state — {blocked}", "diff": diff}

    # Capture the demo branch we're on NOW (before checkout -B switches HEAD) → PR base.
    base_branch = _pr_base(_current_branch())
    branch = _branch_name(test_id, branch_suffix)
    title = f"Auto-repair: fix {test_id or 'kiosk test'}"
    body = (
        f"Automated repair by the Kiosk Test Studio Auto-Repair agent.\n\n"
        f"**Failure**\n{failure}\n\n"
        f"**Fix** ({rel})\n{patch.explanation}\n"
    )

    # Commit ONLY the fixed file (pathspec commit) so nothing else in the tree can leak into the
    # branch, even if other files happen to be modified.
    steps = [
        _git(["checkout", "-B", branch]),
        _git(["commit", "-m", title, "-m", patch.explanation, "--", rel]),
    ]
    committed = all(s["ok"] for s in steps)
    head = _git(["rev-parse", "--short", "HEAD"]).get("output", "").strip()

    return {
        "prepared": committed,
        "branch": branch,
        "base": base_branch,
        "remote": settings.repair_pr_remote,
        "commit": head,
        "title": title,
        "body": body,
        "file": rel,
        "diff": diff,
        "git_log": [s.get("output", "") for s in steps],
    }


def _repo_web_url() -> str:
    """The https://github.com/<owner>/<repo> web URL for the origin remote (strip .git / ssh)."""
    remote = _git(["remote", "get-url", settings.repair_pr_remote]).get("output", "").strip()
    if remote.startswith("git@"):  # git@github.com:owner/repo.git
        remote = "https://" + remote[4:].replace(":", "/", 1)
    if remote.endswith(".git"):
        remote = remote[:-4]
    return remote


def _compare_url(base: str, head: str) -> str:
    repo = _repo_web_url()
    return f"{repo}/compare/{base}...{head}?expand=1" if repo else ""


def _create_pr_via_api(remote_url: str, head: str, base: str, title: str, body: str) -> str:
    """Create a real GitHub PR via the REST API (used when `gh` is absent — the container has no gh).
    Returns the PR html_url, or "" on any failure so the caller falls back to the compare URL. Needs
    settings.github_token (repo scope). If a PR for this head already exists, returns that PR's URL."""
    token = settings.github_token
    if not token or not remote_url:
        return ""
    import re
    import requests
    m = re.search(r"github\.com[:/]+(?:[^/@]+@)?([^/]+)/(.+?)(?:\.git)?/?$", remote_url)
    if not m:
        return ""
    owner, repo = m.group(1), m.group(2)
    hdr = {"Authorization": f"token {token}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.post(
            f"https://api.github.com/repos/{owner}/{repo}/pulls",
            headers=hdr, json={"title": title, "head": head, "base": base, "body": body}, timeout=20,
        )
        if r.status_code == 201:
            return r.json().get("html_url", "")
        if r.status_code == 422:  # PR already exists for this head → return the existing one
            q = requests.get(
                f"https://api.github.com/repos/{owner}/{repo}/pulls",
                headers=hdr, params={"head": f"{owner}:{head}", "base": base, "state": "open"}, timeout=20,
            )
            if q.ok and q.json():
                return q.json()[0].get("html_url", "")
        print(f"  [REPAIR] PR API {r.status_code}: {(r.text or '')[:200]}")
    except Exception as exc:  # never break the repair over PR creation
        print(f"  [REPAIR] PR API error: {exc}")
    return ""


def open_pull_request(branch: str, base: str, title: str, body: str) -> dict:
    """Raise the PR. `gh` isn't required: push the base (so it exists on the remote) and the head,
    then return GitHub's prefilled compare/PR page URL (the gh-less way to open a PR). If `gh` IS
    installed it's used to create the PR directly and its URL is returned instead.

    Auto-invoked by run_repair(auto_pr=True) after a green build, and also reachable via the
    /api/repair/{id}/open-pr endpoint.
    """
    remote = settings.repair_pr_remote
    logs: list[str] = []

    # Push the base branch too so the remote has both sides of the comparison (the demo base carries
    # the intentional bug and lives only locally until now).
    if base and _git(["rev-parse", "--verify", base]).get("ok"):
        pb = _git(["push", remote, f"{base}:{base}"])
        logs.append(pb["output"])
    ph = _git(["push", "-u", remote, branch])
    logs.append(ph["output"])
    pushed = ph["ok"]

    # If gh happens to be available, create the PR outright.
    gh = _run(["gh", "pr", "create", "--base", base, "--head", branch,
               "--title", title, "--body", body], CODEBASE_DIR)
    gh_url = ""
    if gh["ok"]:
        for line in (gh.get("output") or "").splitlines():
            if line.startswith("http"):
                gh_url = line.strip()
                break

    # No gh in the container → create the PR via the GitHub REST API when a token is configured.
    api_url = ""
    if not gh_url and pushed and settings.github_token:
        remote_url = _git(["remote", "get-url", remote]).get("output", "").strip()
        api_url = _create_pr_via_api(remote_url, branch, base, title, body)
        if api_url:
            logs.append(f"created PR via GitHub API: {api_url}")

    url = gh_url or api_url or _compare_url(base, branch)
    return {
        "opened": bool(pushed and url),   # branches pushed + a PR URL is ready
        "pushed": pushed,
        "created": bool(gh_url or api_url),  # a real PR was created (gh or the REST API)
        "url": url,
        "output": "\n".join(x for x in logs if x),
    }


def delete_pull_request(branch: str) -> dict:
    """Tear down a raised PR so repeated demo runs don't pile up branches/PRs on GitHub.

    Deleting the head branch on the remote auto-closes any open PR for it (GitHub behaviour), so the
    gh-less path just does `git push origin --delete <branch>`. If `gh` is present we also `gh pr
    close --delete-branch` for a clean close. The local repair branch is removed too (never delete
    the base demo bug branch — it's reused). Never raises; returns a result dict.
    """
    remote = settings.repair_pr_remote
    if not branch or branch.startswith("demo/") or not branch.startswith("repair/"):
        return {"deleted": False, "output": f"refusing to delete non-repair branch '{branch}'"}

    logs: list[str] = []

    # Best-effort: if gh is available, close the PR and delete its branch in one step.
    gh = _run(["gh", "pr", "close", branch, "--delete-branch"], CODEBASE_DIR)
    if gh.get("output"):
        logs.append(gh["output"])

    # Delete the remote head branch (closes any open PR pointing at it).
    rd = _git(["push", remote, "--delete", branch])
    logs.append(rd.get("output", ""))
    remote_deleted = rd["ok"]

    # Delete the local repair branch too (switch off it first if we're on it).
    if _current_branch() == branch:
        _git(["checkout", settings.repair_pr_base])
    _git(["branch", "-D", branch])

    return {
        "deleted": bool(remote_deleted or gh["ok"]),
        "remote_deleted": remote_deleted,
        "branch": branch,
        "output": "\n".join(x for x in logs if x),
    }


# ── Orchestration ────────────────────────────────────────────────────────────────

def run_repair(failure: str, test_id: str = "", *, apply: bool = True, auto_pr: bool = False,
               branch_suffix: str = "", progress_cb: Optional[ProgressCb] = None,
               cancel_event=None) -> dict:
    """Drive the Auto-Repair LangGraph (retrieve → diagnose → apply → test → build → pr-prep).

    Thin wrapper over the compiled StateGraph in repair_agent/agent.py: it registers the progress
    callback on the broadcaster (callables stay out of graph state), invokes the graph, and reshapes
    the final state into the same result dict the API/UI already consumes. When auto_pr is True and
    the build is green, the prepared PR is pushed + opened inside the pr node. Behaviour is identical
    to the original linear pipeline; only the structure is now agentic.
    """
    # Lazy imports avoid a circular import at module load (nodes import from this module).
    import uuid
    from repair_agent.agent import create_repair_agent
    from repair_agent import broadcaster, canceller

    rid = uuid.uuid4().hex   # per-invocation key for the progress broadcaster + canceller
    broadcaster.register(rid, progress_cb)
    canceller.register(rid, cancel_event)
    try:
        graph = create_repair_agent()
        final = graph.invoke({
            "repair_id": rid, "failure": failure, "test_id": test_id,
            "apply": apply, "auto_pr": auto_pr, "branch_suffix": branch_suffix,
            "stages": {},
        })
    except RepairCancelled:
        # Cooperative cancel — the streamed stages are already in the job dict via progress_cb.
        return {"failure": failure, "test_id": test_id, "success": False,
                "cancelled": True, "stages": {}, "error": "Cancelled by user."}
    finally:
        broadcaster.unregister(rid)
        canceller.unregister(rid)

    result: dict = {
        "failure": failure,
        "test_id": test_id,
        "success": bool(final.get("success")),
        "stages": final.get("stages", {}),
    }
    if not apply:
        result["dry_run"] = True
    if final.get("error"):
        result["error"] = final["error"]
    return result
