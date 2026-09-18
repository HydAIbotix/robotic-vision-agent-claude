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
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from langchain_core.messages import HumanMessage

from vision_agent.config import settings
from vision_agent.llm import get_llm, get_local_llm, invoke_json, effective_local_num_ctx
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
- Make the SMALLEST possible CHANGE — the DIFFERENCE between "find" and "replace" should be one
  sub-expression, ideally a single token. PRESERVE every other condition, guard, and clause (keep
  unrelated `||`/`&&` guards intact); do NOT drop, merge, simplify, or reorder logic that is not the bug.
- BUT "find" ITSELF must be a DISTINCTIVE, VERBATIM snippet that occurs EXACTLY ONCE in the file —
  copy a WHOLE line, or 2–4 full lines including the buggy one. NEVER a bare word or a common token
  like a variable name (e.g. `amount`, `value`, `data`): those match many places and will be REJECTED.
- "replace" is that same snippet with only the buggy fragment changed, and MUST DIFFER from "find".
  Returning "find" and "replace" identical is invalid (it changes nothing).
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
_CHARS_PER_TOKEN       = 3.5  # conservative estimate (code is token-dense) for fitting the local window
_DESIGN_DOCS           = 5    # design-doc chunks to ALWAYS include (the spec that reveals the root cause).
                              # The doc is small (~12 focused section chunks); a symptom-side failure query
                              # can rank the CAUSE-side section (e.g. "Payment and Card Reader Design") ~#4,
                              # so pull a few to reliably include it without bloating context.
_POOL_MULT             = 3    # pull this × top_k CODE candidates so the lexical re-rank has a wider pool to
                              # rescue from; slicing back to top_k reproduces the prior top_k exactly.
_SIGNAL_PROMOTE        = 2    # at most this many lexically-matched code chunks are promoted into context.
_SIGNAL_MIN            = 2    # a promoted chunk must share ≥ this many DISTINCT signal tokens with the failure.

# Words that carry NO discriminating signal between code chunks — test-harness jargon, generic UI verbs,
# and common English. Stripped before scoring so the lexical re-rank keys on the DISTINCTIVE identifiers /
# values / numbers in the failure (e.g. `quantity`, `1`, `topup`) rather than "screen"/"failed"/"the".
_SIGNAL_STOP = {
    "failed", "failing", "failure", "test", "tests", "case", "cases", "step", "steps", "expected",
    "observed", "showed", "actual", "attempted", "order", "assertion", "assertions", "behaviour",
    "behavior", "intent", "only", "adding",
    "design", "spec", "root", "cause", "defect", "result", "results", "wrong", "missing", "reach",
    "reached", "screen", "screens", "button", "buttons", "tap", "tapped", "click", "clicked", "page",
    "pages", "kiosk", "field", "fields", "enter", "entered", "submit", "submitted", "display",
    "displayed", "show", "shown", "verify", "verified", "should", "must", "value", "values", "state",
    "current", "correct", "incorrect", "error", "errors", "the", "and", "for", "that", "with", "this",
    "from", "into", "when", "then", "than", "have", "has", "had", "not", "was", "were", "are", "its",
    "was", "will", "would", "could", "one", "two", "get", "got", "set", "via", "per", "use", "used",
    "using", "does", "did", "done", "which", "what", "where", "after", "before", "because", "your",
    "you", "they", "their", "our", "but",
}


def _dkey(doc) -> tuple:
    """Stable dedupe/identity key for a retrieved Document (source + start line + content prefix)."""
    m = getattr(doc, "metadata", {}) or {}
    return (m.get("source"), m.get("start_line"), (getattr(doc, "page_content", "") or "")[:40])


def _short(path):
    return os.path.basename(path) if path else path


def _signal_tokens(failure: str) -> set:
    """DISTINCTIVE tokens from the failure to lexically match against code: identifiers (camelCase or
    ≥4 chars), small integer literals, and words inside quotes/backticks. Jargon/common words removed."""
    toks: set = set()
    for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", failure):
        wl = w.lower()
        if wl in _SIGNAL_STOP:
            continue
        if len(wl) < 4 and not any(c.isupper() for c in w[1:]):   # keep short camelCase (e.g. isVip), drop 'the'
            continue
        toks.add(wl)
    for n in re.findall(r"(?<![\w.])\d{1,4}(?![\w.])", failure):    # small literals (a guard bound, a count)
        toks.add(n)
    for q in re.findall(r"[\"'`]([^\"'`\n]{2,40})[\"'`]", failure):  # quoted expected/observed values/labels
        for w in re.findall(r"[A-Za-z0-9_]{3,}", q):
            wl = w.lower()
            if wl not in _SIGNAL_STOP:
                toks.add(wl)
    return toks


def _lexical_score(text: str, signals: set) -> int:
    """Count DISTINCT signal tokens present in `text` (word-boundary match for numbers, substring for
    identifiers — code identifiers are compound so a substring hit is meaningful)."""
    tl = (text or "").lower()
    score = 0
    for s in signals:
        if s.isdigit():
            if re.search(r"(?<![\w.])" + re.escape(s) + r"(?![\w.])", tl):
                score += 1
        elif s in tl:
            score += 1
    return score


def _promote_signal_hits(pool, failure, *, already, limit, min_score):
    """From `pool`, return up to `limit` code chunks NOT already selected that share ≥ min_score distinct
    signal tokens with the failure (highest first). This rescues a buggy chunk that pure vector similarity
    ranked below top_k. Returns [] when nothing clears the bar — so it is a strict no-op in that case."""
    signals = _signal_tokens(failure)
    if not signals:
        return []
    akeys = {_dkey(d) for d in already}
    scored = []
    for d in pool:
        if _dkey(d) in akeys:
            continue
        sc = _lexical_score(getattr(d, "page_content", "") or "", signals)
        if sc >= min_score:
            scored.append((sc, d))
    scored.sort(key=lambda x: x[0], reverse=True)
    out, seen = [], set()
    for _sc, d in scored:
        k = _dkey(d)
        if k in seen:
            continue
        seen.add(k)
        out.append(d)
        if len(out) >= limit:
            break
    return out


def retrieve_context(failure: str, top_k: int = 8) -> tuple[str, list[dict]]:
    """Semantic search over the Chroma RAG index. Returns (prompt_text, structured_hits).

    top_k is the number of CODE chunks pulled (was 6). A real buggy chunk can sit at rank ~7–8 when
    the failure query is dominated by symptom/navigation vocabulary (e.g. a login failure reads as
    "wrong screen: products vs login"); a slightly wider code window keeps that chunk in the set
    without crowding out the design-doc / general context (final cap `_MAX_CONTEXT_BLOCKS`)."""
    # Announce the ACTIVE retrieval backend so the console makes it obvious which stack ran.
    print(f"  [REPAIR] RETRIEVE via {retrieval_tool_label()}")
    # The Chroma backend persists to PERSIST_DIR; the graph backends store elsewhere (Neo4j /
    # GraphRAG parquet workspace), so only enforce the on-disk-index precondition for Chroma.
    if settings.repair_retrieval_backend == "chroma" and not PERSIST_DIR.exists():
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
    # Pull a WIDER candidate pool than we finally keep, so the lexical re-rank below can rescue a buggy
    # chunk that vector similarity buried. Slicing the pool to top_k reproduces the previous top_k exactly
    # (vector order is stable), so the base two-lane behaviour is unchanged.
    pool_k = top_k * _POOL_MULT
    intent_code = search(rq, k=pool_k, where=code_where)
    if rq_action == rq:
        interleaved = list(intent_code)
    else:
        action_code = search(rq_action, k=pool_k, where=code_where)
        interleaved, _cseen = [], set()
        for a, b in zip(intent_code, action_code):
            for d in (a, b):
                ck = _dkey(d)
                if ck not in _cseen:
                    _cseen.add(ck)
                    interleaved.append(d)
        # append any tail (unequal lengths) preserving order, still deduped
        for d in list(intent_code) + list(action_code):
            ck = _dkey(d)
            if ck not in _cseen:
                _cseen.add(ck)
                interleaved.append(d)
    # Keep the interleaved top-`top_k` (both lanes' best hits) as the base set.
    code_docs = interleaved[:top_k]

    # LEXICAL RE-RANK (rescue). Pure vector similarity ranks by the failure's DOMINANT vocabulary (design
    # intent + navigation), which can bury the exact buggy function even when it IS indexed — observed on
    # TC-RPS-003, where the `quantity <= 1` guard sat in the index/text-units but never made the retrieved
    # set. So from the WIDER pool we promote up to `_SIGNAL_PROMOTE` code chunks that share the most
    # DISTINCTIVE tokens with the failure (identifiers / numbers / quoted values) yet fell outside top_k,
    # and slot them high. Additive: the vector top hits are kept; when nothing clears `_SIGNAL_MIN` this is
    # a strict no-op (prior behaviour). Backend-agnostic — runs over whatever search() returned (Chroma or
    # msgraphrag whole-function chunks).
    promoted = _promote_signal_hits(interleaved, failure, already=code_docs,
                                    limit=_SIGNAL_PROMOTE, min_score=_SIGNAL_MIN)
    if promoted:
        srcs = ", ".join(f"{_short(d.metadata.get('source'))}:{d.metadata.get('start_line')}" for d in promoted)
        print(f"  [REPAIR] lexical re-rank promoted {len(promoted)} code chunk(s) into context: {srcs}")
        merged, seen = [], set()
        for d in code_docs[:4] + promoted + code_docs[4:]:   # keep the top-4 vector hits ahead of promotions
            k = _dkey(d)
            if k not in seen:
                seen.add(k)
                merged.append(d)
        code_docs = merged[:top_k + _SIGNAL_PROMOTE]
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

    # FIT THE LOCAL WINDOW. A local Ollama model has a fixed context window (repair_local_num_ctx); if the
    # assembled prompt exceeds it, Ollama SILENTLY TRUNCATES (dropping the front — the rules + failure — or
    # the first code blocks), so the model may never see the buggy code or the instructions. Claude's window
    # is huge, so this only applies to the local path. Keep WHOLE blocks in priority order (code first, then
    # design) until the char budget is hit — never truncate a function mid-body; drop the least-important
    # tail blocks instead. Self-adjusts to whatever num_ctx is set (so a smaller, VRAM-safe window is fine).
    if settings.repair_llm_backend == "local" and blocks:
        local_ctx = effective_local_num_ctx()   # the window the model ACTUALLY runs with (large-model clamp applied)
        reserve_tokens = 1024 + 550          # model JSON output + the fixed prompt scaffold (rules)
        fail_tokens = len(failure) / _CHARS_PER_TOKEN
        ctx_char_budget = int(max(2000, local_ctx - reserve_tokens - fail_tokens) * _CHARS_PER_TOKEN)
        kept, total = [], 0
        for b in blocks:
            if kept and total + len(b) + 2 > ctx_char_budget:
                break
            kept.append(b); total += len(b) + 2
        if len(kept) < len(blocks):
            print(f"  [REPAIR] context trimmed to fit local window: {len(kept)}/{len(blocks)} blocks "
                  f"(~{total} chars, budget ~{ctx_char_budget}, num_ctx={local_ctx}).")
        blocks, hits = kept, hits[:len(kept)]
    return "\n\n".join(blocks), hits


# ── 2. DIAGNOSE (Claude primary, local LLM backup) ──────────────────────────────

def retrieval_tool_label() -> str:
    """Human-readable name of the ACTIVE retrieval backend (for logs + the UI stage sub-label)."""
    if settings.repair_retrieval_backend == "graphrag":
        return "GraphRAG + Neo4j (local)"
    if settings.repair_retrieval_backend == "msgraphrag":
        return f"Microsoft GraphRAG · {settings.graphrag_llm_model or settings.repair_local_model} (local)"
    return "Chroma + HuggingFace RAG"


def diagnose_tool_label() -> str:
    """Human-readable name of the ACTIVE diagnose model (for logs + the UI stage sub-label)."""
    if settings.repair_llm_backend == "local":
        suffix = ", air-gapped" if settings.repair_local_only else ""
        return f"Llama · {settings.repair_local_model} (local{suffix})"
    return f"Claude · {settings.anthropic_model}"


def _reject_reason(data) -> str:
    """Why a proposed patch is unusable BEFORE we try to apply it (cheap — no filesystem access; the
    exact-occurrence check happens at apply). A no-op or a bare-token 'find' would otherwise green the
    diagnose stage and then fail at apply ('find-text appears N times'), so we catch it here — which
    also lets a weaker local model be re-prompted. Returns '' when the patch looks applicable."""
    if not data:
        return "no JSON object returned"
    find = str(data.get("find") or "")
    repl = data.get("replace")
    if not find.strip():
        return "empty 'find'"
    if repl is None:
        return "missing 'replace'"
    if find.strip() == str(repl).strip():
        return "'find' equals 'replace' (a no-op — nothing would change)"
    core = find.strip()
    if len(core) < 8 and "\n" not in core and " " not in core:
        return f"'find' is a bare token ({core!r}) — it must be a distinctive multi-line snippet, unique in the file"
    return ""


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
    # AIR-GAP: local-only means the remote model is NEVER in the chain — DIAGNOSE runs the local model
    # then (if it can't produce a patch) the deterministic demo rule, so nothing leaves the box.
    if settings.repair_local_only:
        return [local]
    return [local, claude] if settings.repair_llm_backend == "local" else [claude, local]


class _DiagnoseTimeout(Exception):
    """A single provider's DIAGNOSE call exceeded its wall-clock budget."""


def _invoke_with_deadline(fn, timeout, cancel_check, on_heartbeat=None, heartbeat_every=10.0):
    """Run `fn()` on a daemon thread and wait up to `timeout` seconds, polling `cancel_check`.

    A blocking LLM call (a cold local model, a hung request) can't be interrupted in-thread, so we
    run it on a daemon thread and watch it: on cancel we raise RepairCancelled, on timeout we raise
    _DiagnoseTimeout — either way the caller moves on immediately and the daemon thread is left to
    finish harmlessly (daemon → never blocks shutdown). timeout=None / <=0 means wait indefinitely.
    `on_heartbeat(elapsed_seconds)` is called every `heartbeat_every` seconds while waiting, so a slow
    local model can report progress to the console + UI instead of looking hung."""
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
    next_beat = heartbeat_every
    while not done.wait(step):
        waited += step
        if cancel_check and cancel_check():
            raise RepairCancelled()
        if on_heartbeat and waited >= next_beat:
            try:
                on_heartbeat(waited)
            except Exception:
                pass
            next_beat += heartbeat_every
        if timeout and timeout > 0 and waited >= timeout:
            raise _DiagnoseTimeout()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _ollama_vram_report() -> str:
    """One-line summary of what Ollama has loaded and whether it fits the GPU, from /api/ps.

    Detects the CPU-offload condition that makes a big diagnose model ~10× slower (the timeout cause we
    hit with qwen2.5-coder:32b under a high OLLAMA_NUM_PARALLEL). Best-effort: returns a short note if the
    endpoint can't be reached; never raises. Only meaningful once the model is loaded (after the first
    request), so callers invoke it around the diagnose call."""
    try:
        import urllib.request
        base = settings.repair_local_base_url.rstrip("/")
        with urllib.request.urlopen(f"{base}/api/ps", timeout=5) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        return f"(ollama /api/ps unavailable: {exc})"
    parts = []
    for m in data.get("models", []):
        total = m.get("size", 0) or 0
        vram = m.get("size_vram", 0) or 0
        if total <= 0:
            continue
        gpu_pct = int(round(100 * vram / total))
        tag = "100% GPU" if vram >= total else f"{gpu_pct}% GPU / {100 - gpu_pct}% CPU  ⚠ OFFLOAD"
        parts.append(f"{m.get('name', '?')} {vram / 1e9:.1f}/{total / 1e9:.1f} GB {tag}")
    return "; ".join(parts) if parts else "(no models loaded)"


def _dump_diagnose_debug(failure: str, prompt: str, records: list, vram: str = "") -> None:
    """Write the EXACT prompt + each provider's RAW response to a per-call file (REPAIR_DEBUG_DUMP=true).

    This is the ground truth for "what did we send the model and what did it say" — including the empty/
    timed-out case (raw = None) that the UI shows as "(no output)". Never raises; a dump failure only prints."""
    try:
        d = Path(settings.repair_debug_dir)
        d.mkdir(parents=True, exist_ok=True)
        m = re.search(r"\bTC-[A-Za-z0-9]+-\d+\b", failure or "")
        tid = m.group(0) if m else "repair"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        fp = d / f"diagnose_{ts}_{tid}.txt"
        lines = [
            f"# DIAGNOSE debug dump  {ts}",
            f"# test:              {tid}",
            f"# retrieval backend: {retrieval_tool_label()}",
            f"# diagnose model:    {diagnose_tool_label()}",
            f"# local num_ctx:     {effective_local_num_ctx()} (effective)",
            f"# GPU (ollama ps):   {vram or '(n/a)'}",
            f"# prompt size:       {len(prompt or '')} chars",
            "",
            "=" * 90, "PROMPT SENT TO MODEL", "=" * 90, prompt or "", "",
        ]
        for rec in records:
            lines += [
                "=" * 90,
                f"PROVIDER: {rec.get('label')}  ({rec.get('model', '')})  "
                f"elapsed={rec.get('elapsed', '?')}s  outcome={rec.get('outcome', '')}",
                "=" * 90,
                "--- RAW RESPONSE ---",
                rec.get("raw") if rec.get("raw") is not None else "(no output — timed out or empty)",
                "--- PARSED PATCH ---",
                json.dumps(rec.get("parsed"), indent=2) if rec.get("parsed") else "(none)",
                "--- REJECT REASON ---",
                rec.get("reason") or "(accepted / n.a.)",
                "",
            ]
        fp.write_text("\n".join(lines), encoding="utf-8")
        print(f"  [REPAIR] debug dump → {fp}")
    except Exception as exc:
        print(f"  [REPAIR] debug dump failed: {exc}")


def propose_patch(failure: str, context: str, *, timeout=None, cancel_check=None, on_progress=None) -> RepairPatch:
    """Produce one minimal find/replace patch: try the selected model, then the backup model, then
    the deterministic demo rule. `repair_llm_backend` chooses primary vs backup order. Each provider
    call is bounded by `timeout` (seconds) and interruptible via `cancel_check` so a stuck/slow model
    never freezes the repair. `on_progress(label, elapsed, budget)` is called periodically for a slow
    (local) model so the UI can show live progress instead of a frozen 'working…'."""
    prompt = _DIAGNOSE_PROMPT.format(failure=failure, context=context)

    # Visibility into what the model actually receives — the #1 thing to check when a fix looks wrong.
    approx_tokens = int(len(prompt) / _CHARS_PER_TOKEN)
    print(f"  [REPAIR] DIAGNOSE prompt: {len(prompt)} chars (~{approx_tokens} tokens).")
    if settings.repair_llm_backend == "local" and approx_tokens > effective_local_num_ctx():
        print(f"  [REPAIR] ⚠ prompt (~{approx_tokens} tok) EXCEEDS local num_ctx="
              f"{effective_local_num_ctx()} → Ollama will TRUNCATE it. Raise REPAIR_LOCAL_NUM_CTX "
              f"or reduce retrieved context.")

    chain = " → ".join(lbl for lbl, _ in _diagnose_providers())
    air = "  (AIR-GAPPED: no remote fallback — nothing leaves the environment)" if settings.repair_local_only else ""
    print(f"  [REPAIR] DIAGNOSE via {diagnose_tool_label()}  [providers: {chain}]{air}")

    # DEBUG DUMP bookkeeping (Q2). `records` collects, per provider, the raw response + parsed patch +
    # outcome; `vram_report` snapshots the GPU when a local model runs. Written to a file in a finally so
    # even a full timeout ("no output") is captured. All no-ops unless REPAIR_DEBUG_DUMP is set.
    debug = settings.repair_debug_dump
    records: list = []
    vram_report = ""

    def _record(label, model, raw_holder, parsed, reason, t0, outcome):
        if debug:
            records.append({
                "label": label, "model": model, "raw": raw_holder.get("text"),
                "parsed": parsed, "reason": reason,
                "elapsed": round(time.time() - t0, 1), "outcome": outcome,
            })

    try:
        for label, make_llm in _diagnose_providers():
            if cancel_check and cancel_check():
                raise RepairCancelled()
            try:
                llm = make_llm()   # lazy — a missing/unreachable backup raises here, we move on
            except Exception as exc:
                print(f"  [REPAIR] DIAGNOSE provider '{label}' unavailable ({exc}) — trying next.")
                continue
            model_name = settings.repair_local_model if label == "local" else settings.anthropic_model
            # Per-provider budget. The remote Claude call is fast → the tight `timeout` (repair_diagnose_
            # timeout_s). The LOCAL CPU model needs minutes, and its outer deadline MUST be ≥ its own Ollama
            # client timeout or it gets killed before it can answer — so give it repair_local_timeout_s (+
            # margin), and take just ONE attempt (retries=0): a slow model shouldn't be run 3× on timeout.
            if label == "local":
                provider_timeout = settings.repair_local_timeout_s + 30
                provider_retries = 0
            else:
                provider_timeout = timeout
                provider_retries = 2
            # Heartbeat only for the slow local model, so a multi-minute CPU inference reports progress to
            # the console + UI instead of looking hung. Claude is fast → no heartbeat.
            heartbeat = None
            if label == "local":
                def heartbeat(elapsed, _lbl=label, _budget=provider_timeout):
                    print(f"  [REPAIR] DIAGNOSE {diagnose_tool_label()} still working… {int(elapsed)}s / {int(_budget)}s")
                    if on_progress:
                        on_progress(_lbl, int(elapsed), int(_budget))
            # Capture the model's raw text for the debug dump (before JSON parsing) — the ground truth for
            # "what did QWEN say", including a garbled/partial answer that fails to parse.
            raw_holder = {"text": None}
            on_raw_cb = (lambda t: raw_holder.__setitem__("text", t)) if debug else None
            t0 = time.time()
            try:
                data = _invoke_with_deadline(
                    lambda llm=llm, r=provider_retries: invoke_json(
                        llm, [HumanMessage(content=prompt)], default=None, retries=r,
                        label=f"repair/{label}", on_raw=on_raw_cb),
                    provider_timeout, cancel_check, on_heartbeat=heartbeat,
                )
            except _DiagnoseTimeout:
                print(f"  [REPAIR] DIAGNOSE provider '{label}' timed out after {provider_timeout}s — trying next.")
                # A local timeout is almost always CPU-offload (the model didn't fit the GPU). Snapshot
                # /api/ps so the cause is in the logs, and warn with the exact fix (Q3).
                if label == "local":
                    vram_report = _ollama_vram_report()
                    print(f"  [REPAIR] GPU at timeout (ollama ps): {vram_report}")
                    if "OFFLOAD" in vram_report:
                        print("  [REPAIR] ⚠ diagnose model is CPU-OFFLOADED — the cause of the slow/timed-out "
                              "run. Set OLLAMA_NUM_PARALLEL=0 (auto) and/or lower REPAIR_LOCAL_NUM_CTX so the "
                              "model + KV cache fit the GPU.")
                _record(label, model_name, raw_holder, None, "timed out", t0, "timeout")
                continue
            # Sanity-check the patch before accepting. A weaker (local) model often returns a no-op or a
            # bare-token 'find' (e.g. "amount") that greens diagnose then fails at apply — give it ONE
            # corrective retry with the exact reason, which finished well within budget in practice.
            reason = _reject_reason(data)
            if reason and label == "local":
                print(f"  [REPAIR] DIAGNOSE local patch rejected ({reason}) — one corrective retry.")
                corrective = prompt + (
                    f"\n\nYOUR PREVIOUS ANSWER WAS REJECTED: {reason}. Return corrected JSON where 'find' is a "
                    f"DISTINCTIVE, VERBATIM multi-line snippet copied from the code above (a whole line or 2–4 "
                    f"lines, UNIQUE in the file — never a bare word), and 'replace' is that snippet with only "
                    f"the buggy fragment changed (it MUST differ from 'find'). Fix the ROOT CAUSE, not a label."
                )
                try:
                    data = _invoke_with_deadline(
                        lambda: invoke_json(llm, [HumanMessage(content=corrective)], default=None,
                                            retries=0, label="repair/local-retry", on_raw=on_raw_cb),
                        provider_timeout, cancel_check, on_heartbeat=heartbeat,
                    )
                except _DiagnoseTimeout:
                    print(f"  [REPAIR] DIAGNOSE local corrective retry timed out — trying next.")
                    _record(label, model_name, raw_holder, None, "corrective retry timed out", t0, "timeout")
                    continue
                reason = _reject_reason(data)
            if data and not reason and data.get("find") and data.get("replace") is not None:
                if debug and label == "local":
                    vram_report = vram_report or _ollama_vram_report()
                _record(label, model_name, raw_holder, data, "", t0, "accepted")
                who = {"local": "LOCAL Llama", "claude": "Claude"}.get(label, label)
                print(f"  [REPAIR] DIAGNOSE ✓ patch produced by {who} ({model_name}).")
                return RepairPatch(
                    file_path=str(data.get("file_path", "")),
                    find=data["find"],
                    replace=data["replace"],
                    explanation=data.get("explanation", f"{label}-proposed repair."),
                    source=label,
                    model=model_name,
                )
            _record(label, model_name, raw_holder, data, reason or "incomplete", t0, "no-usable-patch")
            print(f"  [REPAIR] DIAGNOSE provider '{label}' returned no usable patch ({reason or 'incomplete'}) — trying next.")

        fallback = _demo_fallback_patch()
        if fallback:
            print("  [REPAIR] No model produced a usable patch — using demo fallback rule.")
            return fallback

        raise RuntimeError("No repair patch could be produced (no model returned a usable patch and no fallback matched).")
    finally:
        if debug:
            _dump_diagnose_debug(failure, prompt, records, vram_report)


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

def _match_lines(text: str, find: str) -> list[int]:
    """1-based line numbers where `find` begins in `text` (every occurrence)."""
    out, idx = [], text.find(find)
    while idx != -1:
        out.append(text.count("\n", 0, idx) + 1)
        idx = text.find(find, idx + 1)
    return out


def _find_preview(find: str, limit: int = 400) -> str:
    """A readable, bounded echo of the patch find-text for the error message / UI, so the user can see
    exactly what the model tried to match when an edit is refused."""
    f = (find or "").strip("\n")
    return f if len(f) <= limit else f[:limit] + "\n… (truncated)"


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
        raise RuntimeError(
            "Patch target not found: the 'find' text is not present in any codebase file.\n"
            f"--- find-text the model proposed ---\n{_find_preview(patch.find)}\n--- end ---"
        )
    files = ", ".join(str(m.relative_to(codebase)) for m in matches)
    raise RuntimeError(
        f"Patch 'find' text appears in {len(matches)} files ({files}); refusing an ambiguous edit.\n"
        f"--- find-text the model proposed ---\n{_find_preview(patch.find)}\n--- end ---"
    )


def apply_patch(patch: RepairPatch) -> Path:
    target = _locate_file(patch)
    codebase = CODEBASE_DIR.resolve()
    if codebase not in target.parents and target != codebase:
        raise RuntimeError(f"Refusing to edit a file outside the codebase: {target}")

    text = target.read_text(encoding="utf-8")
    count = text.count(patch.find)
    rel = target.relative_to(codebase) if codebase in target.parents else target.name
    if count == 0:
        raise RuntimeError(
            f"Patch find-text was not found in {rel} (it may be stale — the RAG index can lag the on-disk "
            f"code). Rebuild the index against the current branch.\n"
            f"--- find-text the model proposed ---\n{_find_preview(patch.find)}\n--- end ---"
        )
    if count > 1:
        locs = ", ".join(map(str, _match_lines(text, patch.find)))
        raise RuntimeError(
            f"Patch find-text appears {count} times in {rel} (lines {locs}); refusing an ambiguous edit. "
            f"The model must include enough surrounding context that the find-text matches exactly one place.\n"
            f"--- find-text the model proposed ---\n{_find_preview(patch.find)}\n--- end ---"
        )

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
