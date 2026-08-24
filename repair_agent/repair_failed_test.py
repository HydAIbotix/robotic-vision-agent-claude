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
from dataclasses import dataclass, asdict
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


def _emit(cb: Optional[ProgressCb], stage: str, status: str, **extra) -> None:
    """Send one progress update ({stage, status, ...}) to the caller, if listening."""
    if cb:
        try:
            cb({"stage": stage, "status": status, **extra})
        except Exception:
            pass


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
    q = re.sub(r"\bTC-[A-Za-z]+-\d+\b", " ", failure)
    q = re.sub(r"\b(failed|failure|test case|test|step|steps|expected|observed|actual)\b", " ", q, flags=re.I)
    q = re.sub(r"\s+", " ", q).strip(" .:-")
    return q or failure


def retrieve_context(failure: str, top_k: int = 4) -> tuple[str, list[dict]]:
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

    docs, seen = [], set()
    for d in list(code_docs) + list(general_docs):
        key = (d.metadata.get("source"), d.metadata.get("start_line"), d.page_content[:40])
        if key not in seen:
            seen.add(key)
            docs.append(d)
        if len(docs) >= top_k + 2:
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
    """Deterministic fallback for the intentional RPS login bug used in the demo.

    The planted bug compares the password against `<pw>-bug`, so a VALID user is rejected. Removing
    the `-bug` suffix restores login. Handles BOTH codebase variants — arm-reachable-area uses
    `normalizedPassword`, main uses `password` — so the fallback works whichever base the demo runs
    on. Kept small + specific so it can never match unrelated code."""
    app = CODEBASE_DIR / "src" / "App.tsx"
    if not app.exists():
        return None
    text = app.read_text(encoding="utf-8")
    for pw in ("normalizedPassword", "password"):
        bug = f"user.password !== `${{{pw}}}-bug`"
        if bug in text:
            return RepairPatch(
                file_path=str(app),
                find=bug,
                replace=f"user.password !== {pw}",
                explanation=f"Login compared the password against {pw} + '-bug', rejecting valid credentials; compare against the real password.",
            )
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
        "base": settings.repair_pr_base,
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


# ── Orchestration ────────────────────────────────────────────────────────────────

def run_repair(failure: str, test_id: str = "", *, apply: bool = True, auto_pr: bool = False,
               branch_suffix: str = "", progress_cb: Optional[ProgressCb] = None) -> dict:
    """Run the full retrieve → diagnose → apply → test → build → pr-prep pipeline.

    When auto_pr is True and the build is green, the prepared PR is also pushed + opened
    automatically. Returns a structured result dict (also streamed stage-by-stage via progress_cb).
    """
    result: dict = {"failure": failure, "test_id": test_id, "success": False, "stages": {}}

    # 1. RETRIEVE
    _emit(progress_cb, "retrieve", "running")
    context, hits = retrieve_context(failure)
    result["stages"]["retrieve"] = {"status": "done", "hits": hits}
    _emit(progress_cb, "retrieve", "done", hits=hits)

    # 2. DIAGNOSE (Claude)
    _emit(progress_cb, "diagnose", "running")
    patch = propose_patch(failure, context)
    patch_dict = asdict(patch)
    result["stages"]["diagnose"] = {"status": "done", "patch": patch_dict}
    _emit(progress_cb, "diagnose", "done", patch=patch_dict)

    if not apply:
        result["dry_run"] = True
        return result

    # Guard: never mutate a repo that is mid-merge / mid-rebase / has conflicts. Editing App.tsx and
    # committing on top of that state produced a garbage "fix" branch carrying a whole merge. Stop
    # here with a clear message; the operator resolves the repo, then re-runs.
    blocked = _repo_blocked_reason()
    if blocked:
        msg = (f"Repository is not in a clean state: {blocked}. Skipped applying the fix and the PR "
               f"so the branch can't be corrupted. Clean the repo (see the message), then re-run.")
        result["stages"]["apply"] = {"status": "failed", "reason": blocked, "observation": msg}
        result["error"] = msg
        _emit(progress_cb, "apply", "failed", reason=blocked, observation=msg)
        return result

    # 3. APPLY
    _emit(progress_cb, "apply", "running")
    target = apply_patch(patch)
    rel = os.path.relpath(str(target), str(CODEBASE_DIR)).replace("\\", "/")
    result["stages"]["apply"] = {"status": "done", "file": rel}
    _emit(progress_cb, "apply", "done", file=rel)

    # 4. TEST — type-check (the app ships no unit-test runner); build below is the final gate
    _emit(progress_cb, "test", "running")
    test = _unit_test(CODEBASE_DIR)
    result["stages"]["test"] = {"status": "done" if test["ok"] else "warn", **test}
    _emit(progress_cb, "test", "done" if test["ok"] else "warn", **test)

    # 5. BUILD (tsc + vite) — the real validation gate
    _emit(progress_cb, "build", "running")
    build = _run([_npm_cmd(), "run", "build"], CODEBASE_DIR)
    result["stages"]["build"] = {"status": "done" if build["ok"] else "failed", **build}
    _emit(progress_cb, "build", "done" if build["ok"] else "failed", **build)

    # 6. PR PREP (local branch + commit + diff). Auto-open when auto_pr and the build is green.
    _emit(progress_cb, "pr", "running")
    pr = prepare_pr(patch, target, failure, test_id, branch_suffix=branch_suffix)
    if auto_pr and pr.get("prepared") and build["ok"]:
        pr["opened"] = open_pull_request(pr["branch"], pr["base"], pr["title"], pr["body"])
    result["stages"]["pr"] = {"status": "done" if pr.get("prepared") else "warn", **pr}
    _emit(progress_cb, "pr", "done" if pr.get("prepared") else "warn", **pr)

    result["success"] = bool(build["ok"])
    return result
