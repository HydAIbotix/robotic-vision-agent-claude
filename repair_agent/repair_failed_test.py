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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from langchain_core.messages import HumanMessage

from vision_agent.config import settings
from vision_agent.llm import get_llm, invoke_json
from repair_agent.parse_code_and_store import CODEBASE_DIR, PERSIST_DIR, SKIP_DIRS, search


ProgressCb = Callable[[dict], None]


@dataclass
class RepairPatch:
    file_path: str
    find: str
    replace: str
    explanation: str


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


# File-neighborhood expansion tuning: when a SMALL support module is implicated, show Claude the whole
# module. Large files (App.tsx) are skipped so context stays focused.
_SMALL_FILE_MAX_CHUNKS = 25   # a file with ≤ this many indexed chunks is a "small module" → expand fully
_EXPAND_FILES          = 2    # expand at most this many implicated small modules
_MAX_CONTEXT_BLOCKS    = 16   # hard cap on chunks handed to the LLM


def retrieve_context(failure: str, top_k: int = 6) -> tuple[str, list[dict]]:
    """Semantic search over the Chroma RAG index. Returns (prompt_text, structured_hits)."""
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
    code_docs = search(rq, k=top_k, where=code_where)
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

    docs, seen = [], set()
    for d in list(code_docs) + expanded + list(general_docs):
        key = (d.metadata.get("source"), d.metadata.get("start_line"), d.page_content[:40])
        if key not in seen:
            seen.add(key)
            docs.append(d)
        if len(docs) >= _MAX_CONTEXT_BLOCKS:
            break

    hits: list[dict] = []
    blocks: list[str] = []
    for i, doc in enumerate(docs, start=1):
        meta = doc.metadata
        hits.append({
            "file": meta.get("source", ""),
            "type": meta.get("type", ""),
            "start_line": meta.get("start_line"),
            "end_line": meta.get("end_line"),
            "snippet": doc.page_content[:1400],
        })
        blocks.append("\n".join([
            f"Context {i}",
            f"File: {meta.get('source')}",
            f"Type: {meta.get('type')}",
            f"Lines: {meta.get('start_line')} - {meta.get('end_line')}",
            "Snippet:",
            doc.page_content[:1800],
        ]))
    return "\n\n".join(blocks), hits


# ── 2. DIAGNOSE (Claude) ────────────────────────────────────────────────────────

def propose_patch(failure: str, context: str) -> RepairPatch:
    """Ask Claude for one minimal find/replace patch; fall back to the POC demo rule."""
    prompt = _DIAGNOSE_PROMPT.format(failure=failure, context=context)
    data = invoke_json(get_llm(), [HumanMessage(content=prompt)], default=None, label="repair")

    if data and data.get("find") and data.get("replace") is not None:
        return RepairPatch(
            file_path=str(data.get("file_path", "")),
            find=data["find"],
            replace=data["replace"],
            explanation=data.get("explanation", "Claude-proposed repair."),
        )

    fallback = _demo_fallback_patch()
    if fallback:
        print("  [REPAIR] Claude returned no usable patch — using demo fallback rule.")
        return fallback

    raise RuntimeError("No repair patch could be produced (Claude returned nothing and no fallback matched).")


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
            return RepairPatch(file_path=str(app), find=find, replace=replace, explanation=why)
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

    url = gh_url or _compare_url(base, branch)
    return {
        "opened": bool(pushed and url),   # branches pushed + a PR URL is ready
        "pushed": pushed,
        "created": bool(gh_url),          # true only if gh actually created the PR
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
               branch_suffix: str = "", progress_cb: Optional[ProgressCb] = None) -> dict:
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
    from repair_agent import broadcaster

    rid = uuid.uuid4().hex   # per-invocation key for the progress broadcaster
    broadcaster.register(rid, progress_cb)
    try:
        graph = create_repair_agent()
        final = graph.invoke({
            "repair_id": rid, "failure": failure, "test_id": test_id,
            "apply": apply, "auto_pr": auto_pr, "branch_suffix": branch_suffix,
            "stages": {},
        })
    finally:
        broadcaster.unregister(rid)

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
